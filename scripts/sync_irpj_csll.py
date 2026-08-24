#!/usr/bin/env python3
"""
sync_irpj_csll.py — recorrência TRIMESTRAL do dashboard de IRPJ/CSLL.

A tabela de IRPJ/CSLL vem no mesmo relatório "Apuração Impostos por loja" do
PIS/COFINS, mas só nos fechamentos de trimestre (mar, jun, set, dez), disponível
no Nibo ~dia 30 de abr/jul/out/jan.

Fluxo (idempotente):
  1. Competência-alvo = mês de fechamento do trimestre conforme o mês de hoje:
     jan→dez(ano-1), abr→mar, jul→jun, out→set (+ buffer no mês seguinte).
  2. Se já há 4 linhas (2 lojas × IRPJ/CSLL) -> no-op.
  3. Senão: /run na Matriz, filtros ['PIS','COFINS','PROVIS'] (mesmo relatório).
  4. Pra cada PDF: parse_irpj_csll com a competência-alvo; só ingere quem tem a
     tabela de apuração (reconcilia). Reports mensais (sem IRPJ/CSLL) são pulados.

Cron nos dias ~30 de jan/abr/jul/out (com 1-2 dias de folga).

Uso:
  python scripts/sync_irpj_csll.py            # competência automática (trimestre)
  python scripts/sync_irpj_csll.py 2026-03    # força (AAAA-MM do fim do trimestre)
  python scripts/sync_irpj_csll.py --force
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
import backfill_irpj_csll as BIC

IMPOSTO = 'IRPJ_CSLL'
# Mesmo relatório do PIS/COFINS, mas o fechamento trimestral pode ter nome um
# pouco diferente — filtros amplos pra garantir que pesca o doc certo.
OBLIGATION_NAMES = ['PIS', 'COFINS', 'PROVIS', 'APURA', 'IMPOSTOS', 'IRPJ']
EXPECTED_ROWS = 4  # 2 lojas × (IRPJ + CSLL)

MATRIZ = {
    'nome': 'Matriz',
    'accountantUuid': '46acdb69-e1e8-4f92-861c-98084e1eb1b5',
    'customerUuid': '276dcb80-6463-4bb7-bab1-c82dd9397b93',
}


def competencia_alvo(hoje):
    """Mês de fechamento do trimestre cuja apuração já está disponível."""
    m, y = hoje.month, hoje.year
    if m in (1, 2):
        return f'{y - 1}-12-01'  # Q4 (dezembro) do ano anterior
    if m in (4, 5):
        return f'{y}-03-01'      # Q1 (março)
    if m in (7, 8):
        return f'{y}-06-01'      # Q2 (junho)
    if m in (10, 11):
        return f'{y}-09-01'      # Q3 (setembro)
    # fora da janela (uso manual sem competência): trimestre fechado mais recente
    qend = ((m - 1) // 3) * 3
    return f'{y - 1}-12-01' if qend == 0 else f'{y}-{qend:02d}-01'


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
        competencia = competencia_alvo(hoje)

    print(f'[sync_irpj_csll] competência-alvo: {competencia}')

    if not forcar:
        n = contar(competencia)
        if n >= EXPECTED_ROWS:
            print(f'  já completo ({n} linhas) — nada a fazer (use --force).')
            return
        print(f'  {n}/{EXPECTED_ROWS} linhas no banco — buscando.')

    # Janela AMPLA: do mês de fechamento do trimestre até ~4 meses depois — o
    # doc trimestral pode cair fora do mês seguinte (cotas, datas de apuração).
    comp_y, comp_m = int(competencia[:4]), int(competencia[5:7])
    due_from = date(comp_y, comp_m, 1).isoformat()
    fim_m, fim_y = comp_m + 4, comp_y
    while fim_m > 12:
        fim_m -= 12
        fim_y += 1
    due_to = date(fim_y, fim_m, 5).isoformat()

    print(f'  buscando no Nibo (vencimento {due_from}..{due_to}, filtros {OBLIGATION_NAMES})...')
    try:
        items = chamar_scraper(due_from, due_to)
    except Exception as e:
        print(f'  [ERRO] scraper falhou: {e}')
        return

    if not items:
        print('  scraper não encontrou o documento ainda. Tenta de novo amanhã.')
        return

    nomes = ', '.join(it.get('fileOriginalName', '?') for it in items)
    print(f'  {len(items)} documento(s) baixado(s): {nomes}')
    print('  Parseando e ingerindo (parser lê a competência do próprio doc)...')
    novos = []  # resultados com sucesso nesta rodada (pra decidir o e-mail)
    for item in items:
        b64 = item.get('pdfBase64')
        if not b64:
            continue
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(base64.b64decode(b64))
            tmp_path = tmp.name
        try:
            r = BIC.processar(tmp_path)  # competência vem do doc ("mar/26")
            if r:
                novos.append(r)
        except Exception as e:
            print(f'  [SKIP] {item.get("fileOriginalName")}: {e}')
        finally:
            os.unlink(tmp_path)

    # O fechamento trimestral também traz as DUAS lojas juntas (mesmo
    # relatório rateado do PIS/COFINS) — se deu certo, as duas atualizaram.
    if novos:
        print('RESULT_JSON:' + json.dumps({
            'lojas_atualizadas': ['Tijuca', 'Metropolitano'],
            'competencia': competencia,
        }))


if __name__ == '__main__':
    main()
