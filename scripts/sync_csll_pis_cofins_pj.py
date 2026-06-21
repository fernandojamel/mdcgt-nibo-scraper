#!/usr/bin/env python3
"""
sync_csll_pis_cofins_pj.py — recorrencia do dashboard CSLL/PIS/COFINS PJ.

Fluxo (idempotente):
  1. Competencia-alvo = mes anterior (DARF de Retencao PJ da competencia X
     e postado no Nibo entre os dias 15-20 do mes X+1).
  2. Se a competencia ja esta em `impostos` (imposto='CSLL_PIS_COFINS_PJ',
     >=2 linhas: Tijuca + Met) -> no-op.
  3. Senao: chama /run na MATRIZ, filtros amplos pra cobrir variacoes de
     nomenclatura do Nibo.
  4. Pra cada PDF: parseia + upsert por loja.

Cron DIARIO dias 15-20.

Uso:
  python scripts/sync_csll_pis_cofins_pj.py            # competencia automatica
  python scripts/sync_csll_pis_cofins_pj.py 2026-05    # forca competencia
  python scripts/sync_csll_pis_cofins_pj.py --force    # ignora "ja completo"
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
import backfill_csll_pis_cofins_pj as BCPC

IMPOSTO = 'CSLL_PIS_COFINS_PJ'
# Nome no Nibo: "CSLL/PIS/COFINS - SERVIÇOS PRESTADOS POR PJ" — Departamento
# Fiscal. Filtros amplos pra cobrir variacoes que o Nibo possa usar.
OBLIGATION_NAMES = ['CSLL', 'COFINS', 'PRESTADOS POR PJ', 'RETENC']
EXPECTED_ROWS = 2  # 1 por loja (Tij + Met)

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
        raise RuntimeError('Token do scraper nao definido.')
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
    args = list(sys.argv[1:])
    forcar = '--force' in args
    args = [a for a in args if a != '--force']

    hoje = date.today()
    if args and len(args[0]) == 7 and '-' in args[0]:
        competencia = f'{args[0]}-01'
    else:
        competencia = competencia_anterior(hoje)

    print(f'[sync_csll_pis_cofins_pj] competencia-alvo: {competencia}')

    if not forcar:
        n = contar(competencia)
        if n >= EXPECTED_ROWS:
            print(f'  ja completo ({n} linhas) — nada a fazer (use --force).')
            return
        print(f'  {n}/{EXPECTED_ROWS} linhas no banco — buscando.')

    # Janela de vencimento: mes seguinte a competencia (dia 1 ao 25)
    comp_y, comp_m = int(competencia[:4]), int(competencia[5:7])
    if comp_m == 12:
        venc_y, venc_m = comp_y + 1, 1
    else:
        venc_y, venc_m = comp_y, comp_m + 1
    due_from = date(venc_y, venc_m, 1).isoformat()
    due_to = date(venc_y, venc_m, 25).isoformat()

    print(f'  buscando no Nibo (vencimento {due_from}..{due_to}, '
          f'filtros {OBLIGATION_NAMES})...')
    try:
        items = chamar_scraper(due_from, due_to)
    except Exception as e:
        print(f'  [ERRO] scraper falhou: {e}')
        return

    if not items:
        print('  scraper nao encontrou o documento ainda. Tenta de novo amanha.')
        return

    print(f'  {len(items)} documento(s) baixado(s). Parseando e ingerindo...')
    for item in items:
        pdf_b64 = item.get('pdfBase64')
        if not pdf_b64:
            continue
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(base64.b64decode(pdf_b64))
            tmp_path = tmp.name
        try:
            BCPC.processar(tmp_path)
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    main()
