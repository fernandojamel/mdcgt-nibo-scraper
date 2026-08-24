#!/usr/bin/env python3
"""
sync_consignado.py — recorrência mensal do dashboard de Consignado.

Fluxo (1 script só, idempotente):
  1. Calcula a competência-alvo (mês anterior ao de hoje — o doc do mês X
     chega no Nibo entre os dias 10-17 do mês X+1).
  2. Se a competência-alvo JÁ está em consignado_resumo_mes -> nada a fazer
     (evita re-baixar do Nibo todo dia da janela).
  3. Senão: chama o nibo-scraper /run com o filtro do consignado (só a Matriz,
     guia unificada), recebe o(s) PDF(s) em base64.
  4. Pra cada PDF: parseia (parse_consignado_pdf) e faz upsert via as RPCs
     (reusa backfill_consignado.processar).

Pensado pra rodar via cron DIÁRIO nos dias 10-17 (ver README/crontab). Nos dias
em que o doc ainda não apareceu, o scraper volta vazio = no-op. No dia em que
aparecer, ingere. Nos dias seguintes, o passo 2 pula sem nem chamar o scraper.

Variáveis de ambiente (lidas de nibo-scraper/.env se existir):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (Supabase)
  NIBO_SCRAPER_TOKEN                          (token do scraper)
  SCRAPER_URL                                 (default http://nibo-scraper:3000)

Uso:
  python scripts/sync_consignado.py                 # competência automática (mês anterior)
  python scripts/sync_consignado.py 2026-04         # força uma competência (AAAA-MM)
  python scripts/sync_consignado.py --force         # ignora o "já existe" e re-baixa
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
import backfill_consignado as BF  # reusa carregar_env, rpc, processar

# Filtro do documento no Nibo. Fica em CONTADOR -> "Documentos recebidos"
# (mesma lista/endpoint `documents/toreceive` que a folha já usa).
OBLIGATION_NAME = 'GFD E-CONSIGNADO'

# Guia é UNIFICADA na Matriz (raiz 41.062.171). Só precisa buscar dela.
MATRIZ = {
    'nome': 'Matriz',
    'accountantUuid': '46acdb69-e1e8-4f92-861c-98084e1eb1b5',
    'customerUuid': '276dcb80-6463-4bb7-bab1-c82dd9397b93',
}


def competencia_anterior(hoje):
    """Mês anterior ao de `hoje`, como '2026-04-01'."""
    y, m = hoje.year, hoje.month
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f'{y}-{m:02d}-01'


def resumo_existe(competencia):
    """True se já há resumo dessa competência (evita re-baixar)."""
    url = (f'{BF.SUPABASE_URL}/rest/v1/consignado_resumo_mes'
           f'?select=id&competencia=eq.{competencia}&limit=1')
    req = urllib.request.Request(url, headers={
        'apikey': BF.SERVICE_KEY,
        'Authorization': f'Bearer {BF.SERVICE_KEY}',
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        rows = json.loads(resp.read().decode('utf-8'))
    return bool(rows)


def chamar_scraper(due_from, due_to):
    """POST /run no scraper. Retorna a lista de items (PDFs em base64)."""
    scraper_url = os.environ.get('SCRAPER_URL', 'http://nibo-scraper:3000')
    # n8n usa NIBO_SCRAPER_TOKEN; o .env do scraper usa SCRAPER_TOKEN. Aceita os dois.
    scraper_token = (os.environ.get('NIBO_SCRAPER_TOKEN')
                     or os.environ.get('SCRAPER_TOKEN'))
    if not scraper_token:
        raise RuntimeError(
            'Token do scraper não definido (NIBO_SCRAPER_TOKEN ou SCRAPER_TOKEN).')
    payload = {
        'dueDateFrom': due_from,
        'dueDateTo': due_to,
        'obligationName': OBLIGATION_NAME,
        'skipExisting': False,  # idempotência é por upsert; scraper não conhece consignado
        'lojas': [MATRIZ],
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
        raise RuntimeError(f'scraper retornou erro: {body.get("error") or body.get("errors")}')
    return body.get('items', [])


def main():
    BF.carregar_env()  # garante SUPABASE_URL / SERVICE_KEY no ambiente do BF
    args = [a for a in sys.argv[1:]]
    forcar = '--force' in args
    args = [a for a in args if a != '--force']

    hoje = date.today()
    if args and len(args[0]) == 7 and '-' in args[0]:  # 'AAAA-MM'
        competencia = f'{args[0]}-01'
    else:
        competencia = competencia_anterior(hoje)

    print(f'[sync_consignado] competência-alvo: {competencia}')

    if not forcar and resumo_existe(competencia):
        print('  já ingerido — nada a fazer (use --force pra re-baixar).')
        return

    # Janela de vencimento derivada da COMPETÊNCIA-alvo: o doc da competência X
    # vence ~dia 18-20 de X+1. Buscamos o mês de vencimento inteiro (com folga),
    # o que vale tanto na recorrência quanto ao forçar uma competência passada.
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
          f'filtro "{OBLIGATION_NAME}")...')
    items = chamar_scraper(due_from, due_to)
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
            BF.processar(tmp_path)  # parseia + upsert (resolve empresa por CPF)
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    main()
