#!/usr/bin/env python3
"""
sync_impostos.py — recorrência mensal do dashboard de ICMS (impostos).

Fluxo (idempotente):
  1. Calcula a competência-alvo (mês anterior — o DARJ da competência X é
     postado no Nibo entre os dias 08-11 do mês X+1 e vence ~dia 10-12).
  2. Se a competência-alvo já está COMPLETA em `impostos` (>= 4 guias: 2 lojas
     × 2 tipos) -> nada a fazer (não re-baixa todo dia da janela).
  3. Senão: chama o nibo-scraper /run nas DUAS lojas, com DOIS filtros
     ('ICMS' e 'DIFAL') — cobre "ICMS A PAGAR", "DIFAL" e "ICMS DIFAL". Junta
     os PDFs (dedup por fileId).
  4. Pra cada PDF: parseia (parse_imposto_darj) e faz upsert (backfill_impostos
     .processar — resolve loja pelo CNPJ e tipo pela natureza). PDFs escaneados
     (imagem) são pulados com aviso (precisam de entrada manual).

Pensado pra cron DIÁRIO nos dias 05-11 (folga antes do 08 de costume, caso
o Nibo poste mais cedo). Nos dias sem o doc, o scraper volta
vazio = no-op. Quando aparece, ingere. Depois de completo, o passo 2 pula.

Variáveis de ambiente (lidas de nibo-scraper/.env se existir):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  NIBO_SCRAPER_TOKEN  (ou SCRAPER_TOKEN)
  SCRAPER_URL         (default http://nibo-scraper:3000)

Uso:
  python scripts/sync_impostos.py            # competência automática (mês anterior)
  python scripts/sync_impostos.py 2026-05    # força uma competência (AAAA-MM)
  python scripts/sync_impostos.py --force    # ignora o "já completo" e re-baixa
"""

import os
import sys
import json
import base64
import tempfile
import urllib.request
from datetime import date
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF   # carregar_env, SUPABASE_URL, SERVICE_KEY
import backfill_impostos as BI      # processar (parse_imposto_darj + upsert)

IMPOSTO = 'ICMS'
# Dois filtros cobrem as variantes de nome no Nibo (não dependemos do exato).
OBLIGATION_NAMES = ['ICMS', 'DIFAL']
EXPECTED_DOCS = 4  # 2 lojas × 2 tipos (A_PAGAR + DIFAL)

ACCOUNTANT = '46acdb69-e1e8-4f92-861c-98084e1eb1b5'
LOJAS = [
    {'nome': 'Tijuca', 'accountantUuid': ACCOUNTANT,
     'customerUuid': '276dcb80-6463-4bb7-bab1-c82dd9397b93'},
    {'nome': 'Metropolitano', 'accountantUuid': ACCOUNTANT,
     'customerUuid': '133a28e0-bfc5-4cd2-aa89-da5b30659ae2'},
]


def competencia_anterior(hoje):
    """Mês anterior ao de `hoje`, como '2026-05-01'."""
    y, m = hoje.year, hoje.month
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f'{y}-{m:02d}-01'


def contar_impostos(competencia):
    """Quantas guias de ICMS já há nessa competência (pra saber se completou)."""
    url = (f'{BF.SUPABASE_URL}/rest/v1/impostos'
           f'?select=id&imposto=eq.{IMPOSTO}&competencia=eq.{competencia}')
    req = urllib.request.Request(url, headers={
        'apikey': BF.SERVICE_KEY,
        'Authorization': f'Bearer {BF.SERVICE_KEY}',
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        rows = json.loads(resp.read().decode('utf-8'))
    return len(rows)


def chamar_scraper(due_from, due_to, obligations):
    """POST /run no scraper com a LISTA de filtros (1 login, vários filtros).
    Retorna items (PDFs base64), já deduplicados por fileId pelo scraper."""
    scraper_url = os.environ.get('SCRAPER_URL', 'http://nibo-scraper:3000')
    scraper_token = (os.environ.get('NIBO_SCRAPER_TOKEN')
                     or os.environ.get('SCRAPER_TOKEN'))
    if not scraper_token:
        raise RuntimeError(
            'Token do scraper não definido (NIBO_SCRAPER_TOKEN ou SCRAPER_TOKEN).')
    payload = {
        'dueDateFrom': due_from,
        'dueDateTo': due_to,
        'obligationName': obligations,  # lista -> scraper consulta todos numa sessão
        'skipExisting': False,  # idempotência é por upsert
        'lojas': LOJAS,
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


def main():
    BF.carregar_env()
    args = [a for a in sys.argv[1:]]
    forcar = '--force' in args
    args = [a for a in args if a != '--force']

    hoje = date.today()
    if args and len(args[0]) == 7 and '-' in args[0]:  # 'AAAA-MM'
        competencia = f'{args[0]}-01'
    else:
        competencia = competencia_anterior(hoje)

    print(f'[sync_impostos] competência-alvo: {competencia}')

    if not forcar:
        n = contar_impostos(competencia)
        if n >= EXPECTED_DOCS:
            print(f'  já completo ({n} guias) — nada a fazer (use --force).')
            return
        print(f'  {n}/{EXPECTED_DOCS} guias no banco — buscando o que falta.')

    # Janela de vencimento derivada da competência: comp X vence no mês X+1.
    comp_y, comp_m = int(competencia[:4]), int(competencia[5:7])
    if comp_m == 12:
        venc_y, venc_m = comp_y + 1, 1
    else:
        venc_y, venc_m = comp_y, comp_m + 1
    due_from = date(venc_y, venc_m, 1).isoformat()
    if venc_m == 12:
        due_to = date(venc_y + 1, 1, 5).isoformat()
    else:
        due_to = date(venc_y, venc_m + 1, 5).isoformat()

    print(f'  buscando no Nibo (vencimento {due_from}..{due_to}, '
          f'filtros {OBLIGATION_NAMES})...')

    # Uma chamada só, com os dois filtros (1 login). Dedup é feito no scraper.
    try:
        items = chamar_scraper(due_from, due_to, OBLIGATION_NAMES)
    except Exception as e:
        print(f'  [ERRO] scraper falhou: {e}')
        return

    if not items:
        print('  scraper não encontrou os documentos ainda. Tenta de novo amanhã.')
        return

    print(f'  {len(items)} documento(s) baixado(s). Parseando e ingerindo...')
    novos = []  # guias efetivamente upsertadas nesta rodada (pra decidir o e-mail)
    for item in items:
        pdf_b64 = item.get('pdfBase64')
        if not pdf_b64:
            continue
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(base64.b64decode(pdf_b64))
            tmp_path = tmp.name
        try:
            r = BI.processar(tmp_path)  # parseia + upsert (loja por CNPJ, tipo por natureza)
            if r:
                novos.append(r)
        finally:
            os.unlink(tmp_path)

    # Linha marcada (RESULT_JSON:) pro n8n saber quais lojas ganharam guia
    # nova nesta rodada e disparar o relatorio por e-mail so pra elas.
    if novos:
        lojas_atualizadas = sorted(set(n['empresa'] for n in novos))
        print('RESULT_JSON:' + json.dumps({
            'lojas_atualizadas': lojas_atualizadas,
            'competencia': competencia,
        }))


if __name__ == '__main__':
    main()
