#!/usr/bin/env python3
"""
sync_gfd.py — recorrência mensal do dashboard de FGTS (GFD).

Fluxo (idempotente):
  1. Competência-alvo = mês anterior (o demonstrativo da competência X é
     postado no Nibo ~dia 17 e a guia vence ~dia 20 de X+1).
  2. Se a competência já está em `impostos` (imposto='FGTS', >=2 linhas:
     Tijuca + Metro) -> no-op.
  3. Senão: chama /run só na MATRIZ (guia unificada), filtro 'GFD'. Pode vir
     também o demonstrativo do consignado ("GFD E-CONSIGNADO") — o parser de
     FGTS ignora (não tem "Total da Guia (FGTS)"), então é inofensivo.
  4. Pra cada PDF: parseia (parse_gfd_demonstrativo) e faz upsert por loja
     (backfill_gfd.processar).

Cron DIÁRIO dias 17-20. Variáveis: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
NIBO_SCRAPER_TOKEN (ou SCRAPER_TOKEN), SCRAPER_URL (default nibo-scraper:3000).

Uso:
  python scripts/sync_gfd.py            # competência automática (mês anterior)
  python scripts/sync_gfd.py 2026-05    # força uma competência (AAAA-MM)
  python scripts/sync_gfd.py --force    # ignora o "já completo" e re-baixa
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
import backfill_gfd as BG           # processar (parse_gfd + upsert por loja)

IMPOSTO = 'FGTS'
OBLIGATION_NAME = 'GFD'  # amplo; o parser separa FGTS do consignado
EXPECTED_ROWS = 2        # Tijuca + Metropolitano

# Guia UNIFICADA na Matriz (raiz 41.062.171). Só busca dela.
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


def contar_fgts(competencia):
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
        'obligationName': OBLIGATION_NAME,
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

    print(f'[sync_gfd] competência-alvo: {competencia}')

    if not forcar:
        n = contar_fgts(competencia)
        if n >= EXPECTED_ROWS:
            print(f'  já completo ({n} linhas) — nada a fazer (use --force).')
            return
        print(f'  {n}/{EXPECTED_ROWS} linhas no banco — buscando.')

    # Janela de vencimento: comp X vence ~dia 20 do mês X+1.
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

    print(f'  buscando no Nibo (vencimento {due_from}..{due_to}, filtro "{OBLIGATION_NAME}")...')
    try:
        items = chamar_scraper(due_from, due_to)
    except Exception as e:
        print(f'  [ERRO] scraper falhou: {e}')
        return

    if not items:
        print('  scraper não encontrou o documento ainda. Tenta de novo amanhã.')
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
            BG.processar(tmp_path)  # FGTS -> upsert por loja; não-FGTS -> [SKIP]
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    main()
