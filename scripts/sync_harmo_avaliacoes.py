#!/usr/bin/env python3
"""
sync_harmo_avaliacoes.py — normaliza as avaliações baixadas da API da Harmo
(JSON já estruturado, ver src/harmo/scraper.js) e importa via RPC
`importar_avaliacoes_automatico` (migration 0057 — mesma regra "substitui o
período" da RPC usada pelo upload manual no app, só que sem exigir admin,
porque quem chama aqui é o scraper com a service_role key).

Fluxo:
  1. Lê o JSON bruto (array de reviews da API) salvo pelo server.js.
  2. Resolve loja_id a partir do prefixo de `establishment_name`
     ("TIJ | ..." -> Tijuca, "MET | ..." -> Metropolitano — mesma regra do
     harmo_excel_parser.dart).
  3. Converte a data (ISO UTC) pro fuso de Brasília (UTC-3, sem horário de
     verão) e extrai só a data (a coluna `data_avaliacao` é DATE).
  4. Chama a RPC. O recompute do Harmo no PEX é AUTOMÁTICO (trigger da
     migration 0039 dispara em cima de qualquer INSERT/DELETE nessa tabela).

Uso:
  python scripts/sync_harmo_avaliacoes.py <reviews.json>
"""

import json
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # carregar_env, SUPABASE_URL, SERVICE_KEY, rpc

PREFIXO_PARA_EMPRESA = {'TIJ': 'Tijuca', 'MET': 'Metropolitano'}
BRT = timezone(timedelta(hours=-3))


def buscar_loja_ids():
    import urllib.request
    url = f'{BF.SUPABASE_URL}/rest/v1/lojas?select=id,empresa'
    req = urllib.request.Request(url, headers={
        'apikey': BF.SERVICE_KEY,
        'Authorization': f'Bearer {BF.SERVICE_KEY}',
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read().decode('utf-8'))
    return {r['empresa']: r['id'] for r in rows}


def empresa_do_local(nome_estabelecimento):
    prefixo = nome_estabelecimento.split('|')[0].strip().upper()
    return PREFIXO_PARA_EMPRESA.get(prefixo)


def data_local(iso_utc):
    if not iso_utc:
        return None
    dt = datetime.fromisoformat(iso_utc.replace('Z', '+00:00'))
    return dt.astimezone(BRT).date().isoformat()


def normalizar(review, loja_ids):
    empresa = empresa_do_local(review.get('establishment_name', ''))
    if empresa is None:
        return None, f"loja não reconhecida em \"{review.get('establishment_name')}\""
    loja_id = loja_ids.get(empresa)
    if not loja_id:
        return None, f"loja {empresa} sem id em `lojas`"

    resp = review.get('response') or {}
    ultimo_status = (resp.get('status') or [{}])[0]

    return {
        'loja_id': loja_id,
        'canal': review.get('source'),
        'data_avaliacao': data_local(review.get('date')),
        'nota': review.get('score'),
        'sentimento': review.get('sentiment'),
        'genero': review.get('gender'),
        'autor_nome': review.get('reviewer'),
        'comentario': review.get('text'),
        'data_resposta': data_local(resp.get('date')) if review.get('responded') else None,
        'resposta': resp.get('response') if review.get('responded') else None,
        'status_resposta': resp.get('lastStatus') if review.get('responded') else None,
        'usuario_respondente': (ultimo_status.get('user') or {}).get('name') if review.get('responded') else None,
        'respondido': bool(review.get('responded')),
        'local_raw': review.get('establishment_name'),
        'source': 'api',
    }, None


def main():
    if len(sys.argv) < 2:
        print('Uso: sync_harmo_avaliacoes.py <reviews.json>', file=sys.stderr)
        sys.exit(1)

    BF.carregar_env()
    with open(sys.argv[1], encoding='utf-8') as f:
        reviews = json.load(f)

    print(f'[sync_harmo_avaliacoes] {len(reviews)} avaliacoes baixadas da API')
    if not reviews:
        print('  nada a importar.')
        return

    loja_ids = buscar_loja_ids()
    linhas = []
    pulados = 0
    for r in reviews:
        linha, erro = normalizar(r, loja_ids)
        if linha is None:
            pulados += 1
            continue
        linhas.append(linha)

    if pulados:
        print(f'  [AVISO] {pulados} avaliacao(oes) pulada(s) (loja nao reconhecida).')
    if not linhas:
        print('  nenhuma linha valida pra importar.')
        return

    por_empresa_canal = {}
    for l in linhas:
        chave = (l['local_raw'], l['canal'])
        por_empresa_canal[chave] = por_empresa_canal.get(chave, 0) + 1
    for (local_raw, canal), qtd in sorted(por_empresa_canal.items()):
        print(f'  {local_raw} | {canal}: {qtd}')

    n = BF.rpc('importar_avaliacoes_automatico', {'p_rows': linhas})
    print(f'[sync_harmo_avaliacoes] {n} linha(s) gravada(s) (substituiu o periodo por loja+canal).')
    print('RESULT_JSON:' + json.dumps({'linhas_importadas': n}))


if __name__ == '__main__':
    main()
