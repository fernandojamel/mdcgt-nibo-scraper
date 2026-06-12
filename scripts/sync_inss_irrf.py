#!/usr/bin/env python3
"""
sync_inss_irrf.py — recorrência mensal do dashboard de INSS/IRRF (DARF folha).

O documento é um EXCEL ("RATEIO DE IMPOSTOS SOBRE FOLHA PAGTO. - INSS, IRRF e
FGTS"), aba COMPOSIÇÃO DA DARF. Pago pela Matriz, rateado por loja.

Fluxo (idempotente):
  1. Competência-alvo = mês anterior.
  2. Se já há 4 linhas (2 lojas × INSS/IRRF) -> no-op.
  3. Senão: /run na Matriz, filtros amplos ['RATEIO','FOLHA PAGTO','INSS'].
  4. Pra cada arquivo: parseia (parse_inss_irrf) com a competência-alvo e faz
     upsert. Arquivos que não são o rateio (sem "TOTAL DARF") são pulados.

Cron DIÁRIO dias 22-25.

Uso:
  python scripts/sync_inss_irrf.py            # competência automática
  python scripts/sync_inss_irrf.py 2026-04    # força (AAAA-MM)
  python scripts/sync_inss_irrf.py --force
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
import backfill_consignado as BF
import backfill_inss_irrf as BII

IMPOSTO = 'INSS_IRRF'
OBLIGATION_NAMES = ['RATEIO', 'FOLHA PAGTO', 'INSS']
EXPECTED_ROWS = 4  # 2 lojas × (INSS + IRRF)

MATRIZ = {
    'nome': 'Matriz',
    'accountantUuid': '46acdb69-e1e8-4f92-861c-98084e1eb1b5',
    'customerUuid': '276dcb80-6463-4bb7-bab1-c82dd9397b93',
}


def competencia_anterior(hoje):
    y, m = hoje.year, hoje.month
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f'{y}-{m:02d}-01'


def contar(competencia):
    url = (f'{BF.SUPABASE_URL}/rest/v1/impostos'
           f'?select=id&imposto=eq.{IMPOSTO}&competencia=eq.{competencia}')
    req = urllib.request.Request(url, headers={
        'apikey': BF.SERVICE_KEY,
        'Authorization': f'Bearer {BF.SERVICE_KEY}',
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return len(json.loads(resp.read().decode('utf-8')))


def chamar_scraper(due_from, due_to):
    scraper_url = os.environ.get('SCRAPER_URL', 'http://nibo-scraper:3000')
    scraper_token = (os.environ.get('NIBO_SCRAPER_TOKEN')
                     or os.environ.get('SCRAPER_TOKEN'))
    if not scraper_token:
        raise RuntimeError('Token do scraper não definido.')
    payload = {
        'dueDateFrom': due_from,
        'dueDateTo': due_to,
        'obligationName': OBLIGATION_NAMES,
        'skipExisting': False,
        'lojas': [MATRIZ],
    }
    req = urllib.request.Request(
        f'{scraper_url}/run', data=json.dumps(payload).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json',
                 'X-Scraper-Token': scraper_token})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        raise RuntimeError(f'scraper /run -> {e.code}: '
                           f'{e.read().decode("utf-8", "replace")[:300]}')
    if not body.get('ok'):
        raise RuntimeError(f'scraper erro: {body.get("error") or body.get("errors")}')
    return body.get('items', [])


def main():
    BF.carregar_env()
    args = [a for a in sys.argv[1:]]
    forcar = '--force' in args
    args = [a for a in args if a != '--force']

    hoje = date.today()
    if args and len(args[0]) == 7 and '-' in args[0]:
        competencia = f'{args[0]}-01'
    else:
        competencia = competencia_anterior(hoje)

    print(f'[sync_inss_irrf] competência-alvo: {competencia}')

    if not forcar:
        n = contar(competencia)
        if n >= EXPECTED_ROWS:
            print(f'  já completo ({n} linhas) — nada a fazer (use --force).')
            return
        print(f'  {n}/{EXPECTED_ROWS} linhas no banco — buscando.')

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

    print(f'  buscando no Nibo (vencimento {due_from}..{due_to}, filtros {OBLIGATION_NAMES})...')
    try:
        items = chamar_scraper(due_from, due_to)
    except Exception as e:
        print(f'  [ERRO] scraper falhou: {e}')
        return

    if not items:
        print('  scraper não encontrou o documento ainda. Tenta de novo amanhã.')
        return

    print(f'  {len(items)} documento(s) baixado(s). Parseando e ingerindo...')
    # competência (AAAA-MM) pra passar ao parser (arquivo vem com nome temporário).
    comp_mm = competencia[:7]
    for item in items:
        b64 = item.get('pdfBase64')  # campo genérico (qualquer arquivo)
        if not b64:
            continue
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
            tmp.write(base64.b64decode(b64))
            tmp_path = tmp.name
        try:
            BII.processar(tmp_path, competencia=comp_mm)
        except Exception as e:
            print(f'  [SKIP] {item.get("fileOriginalName")}: não é o rateio ({e})')
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    main()
