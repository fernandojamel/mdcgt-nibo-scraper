#!/usr/bin/env python3
"""
sync_rescisoes.py — recorrência dos "TERMO DE RESCISÃO" lançados no Nibo
(Departamento Pessoal). 4º sinal de "colaborador inativo" no Quadro
Colaboradores -- o mais forte dos quatro, porque é o documento legal da
rescisão em si (os outros 3 são inferência indireta: Folha "Demitido",
Convenia data_demissao, sumiço do ponto).

Diferente dos sync_* de impostos (mensais, competência fechada), rescisão
é um evento ESPARSO -- não tem "competência completa" pra checar. Em vez
disso: janela ROLANTE (default 75 dias pra trás) + dedup por nibo_file_id
(idempotente via upsert, UNIQUE em rescisoes_nibo.nibo_file_id). Reroda
sem medo -- documento já gravado não duplica nem reprocessa.

Pra cada PDF: baixa (via /run do scraper), parseia só o CPF (e o nome,
best-effort) -- datas e valor vêm dos METADADOS do Nibo (accrual, dueDate),
não do texto do PDF (mais confiável). Documento sem CPF extraível é pulado
com aviso (não dá pra cruzar com o colaborador sem CPF).

Variáveis de ambiente (lidas de nibo-scraper/.env se existir):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  NIBO_SCRAPER_TOKEN  (ou SCRAPER_TOKEN)
  SCRAPER_URL         (default http://nibo-scraper:3000)

Uso:
  python scripts/sync_rescisoes.py                 # janela padrão (75 dias)
  python scripts/sync_rescisoes.py --dias 30        # janela custom
"""

import os
import sys
import json
import base64
import tempfile
import urllib.request
from datetime import date, timedelta, datetime
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF   # carregar_env, SUPABASE_URL, SERVICE_KEY
from parse_rescisao_pdf import parse_pdf

OBLIGATION_NAME = 'TERMO DE RESCISÃO'
JANELA_PADRAO_DIAS = 75

ACCOUNTANT = '46acdb69-e1e8-4f92-861c-98084e1eb1b5'
LOJAS = [
    {'nome': 'Tijuca', 'accountantUuid': ACCOUNTANT,
     'customerUuid': '276dcb80-6463-4bb7-bab1-c82dd9397b93'},
    {'nome': 'Metropolitano', 'accountantUuid': ACCOUNTANT,
     'customerUuid': '133a28e0-bfc5-4cd2-aa89-da5b30659ae2'},
]


def chamar_scraper(due_from, due_to, lojas):
    """POST /run no scraper pra TODAS as lojas de uma vez (1 login)."""
    scraper_url = os.environ.get('SCRAPER_URL', 'http://nibo-scraper:3000')
    scraper_token = (os.environ.get('NIBO_SCRAPER_TOKEN')
                     or os.environ.get('SCRAPER_TOKEN'))
    if not scraper_token:
        raise RuntimeError(
            'Token do scraper não definido (NIBO_SCRAPER_TOKEN ou SCRAPER_TOKEN).')
    payload = {
        'dueDateFrom': due_from,
        'dueDateTo': due_to,
        'obligationName': OBLIGATION_NAME,
        'skipExisting': False,
        'lojas': lojas,
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'{scraper_url}/run', data=data, method='POST',
        headers={
            'Content-Type': 'application/json',
            'X-Scraper-Token': scraper_token,
        })
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        raise RuntimeError(f'scraper /run -> {e.code}: '
                           f'{e.read().decode("utf-8", "replace")[:300]}')
    if not body.get('ok'):
        raise RuntimeError(
            f'scraper retornou erro: {body.get("error") or body.get("errors")}')
    return body.get('items', [])


def parse_accrual(accrual):
    """'10/2026' -> '2026-10-01'. None se não bater o formato."""
    if not accrual or '/' not in accrual:
        return None
    try:
        mes, ano = accrual.split('/')
        return date(int(ano), int(mes), 1).isoformat()
    except (ValueError, TypeError):
        return None


def parse_due_date(due_date_iso):
    """'2026-10-15T00:00:00Z' -> '2026-10-15'. None se vazio."""
    if not due_date_iso:
        return None
    try:
        return datetime.fromisoformat(due_date_iso.replace('Z', '+00:00')).date().isoformat()
    except (ValueError, TypeError):
        return None


def upsert_rescisao(row):
    url = f'{BF.SUPABASE_URL}/rest/v1/rescisoes_nibo?on_conflict=nibo_file_id'
    data = json.dumps(row).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST', headers={
        'apikey': BF.SERVICE_KEY,
        'Authorization': f'Bearer {BF.SERVICE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates,return=minimal',
    })
    try:
        with urllib.request.urlopen(req, timeout=60):
            pass
    except HTTPError as e:
        raise RuntimeError(f'upsert rescisoes_nibo -> {e.code}: '
                           f'{e.read().decode("utf-8", "replace")[:300]}')


def main():
    BF.carregar_env()
    args = sys.argv[1:]
    dias = JANELA_PADRAO_DIAS
    if '--dias' in args:
        i = args.index('--dias')
        dias = int(args[i + 1])

    hoje = date.today()
    due_from = (hoje - timedelta(days=dias)).isoformat()
    due_to = hoje.isoformat()

    print(f'[sync_rescisoes] janela {due_from}..{due_to} (obligationName={OBLIGATION_NAME!r})')

    try:
        items = chamar_scraper(due_from, due_to, LOJAS)
    except Exception as e:
        print(f'  [ERRO] scraper falhou: {e}')
        sys.exit(1)

    if not items:
        print('  nenhum termo de rescisão nessa janela.')
        return

    print(f'  {len(items)} documento(s) encontrado(s). Parseando e ingerindo...')
    gravados = 0
    pulados_sem_cpf = 0
    novos_nomes = []  # pra decidir se dispara alerta/relatório
    for item in items:
        pdf_b64 = item.get('pdfBase64')
        if not pdf_b64:
            continue
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(base64.b64decode(pdf_b64))
            tmp_path = tmp.name
        try:
            parsed = parse_pdf(tmp_path)
        finally:
            os.unlink(tmp_path)

        cpf = parsed.get('cpf')
        if not cpf:
            pulados_sem_cpf += 1
            print(f'  [AVISO] não achei CPF no PDF (fileId={item.get("fileId")}, '
                  f'loja={item.get("lojaNome")}) -- pulado, precisa conferir manual.')
            continue

        row = {
            'loja_apelido': item.get('lojaNome'),
            'cpf': cpf,
            'nome': parsed.get('nome'),
            'competencia': parse_accrual(item.get('accrual')),
            'data_pagamento': parse_due_date(item.get('dueDate')),
            'nibo_file_id': item.get('fileId'),
            'nibo_document_id': str(item.get('documentId')) if item.get('documentId') else None,
        }
        try:
            upsert_rescisao(row)
            gravados += 1
            novos_nomes.append(parsed.get('nome') or cpf)
            print(f'  [OK] {row["loja_apelido"]:13} CPF {cpf} '
                  f'({parsed.get("nome") or "nome não extraído"})')
        except Exception as e:
            print(f'  [ERRO] upsert falhou pra CPF {cpf}: {e}')

    print(f'  resumo: {gravados} gravado(s), {pulados_sem_cpf} pulado(s) sem CPF.')
    if novos_nomes:
        print('RESULT_JSON:' + json.dumps({'rescisoes': novos_nomes}, ensure_ascii=False))


if __name__ == '__main__':
    main()
