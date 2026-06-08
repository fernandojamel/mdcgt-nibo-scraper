#!/usr/bin/env python3
"""
backfill_consignado.py — parseia os PDFs locais do "GFD CONSIGNADO
DEMONSTRATIVO" e popula consignado_resumo_mes + consignado_mes no Supabase.

Pra cada PDF:
  1. parse_consignado_pdf.parse_pdf()
  2. RPC upsert_consignado_resumo_mes  -> pega o resumo_id
  3. RPC upsert_consignado_contrato por contrato (resolve a empresa por CPF)
  4. reporta quantos casaram Tijuca / Metropolitano / (nao atribuido)

Idempotente — pode rodar quantas vezes quiser (upsert por chave).

Variáveis de ambiente (lidas de nibo-scraper/.env se existir, ou do ambiente):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Uso:
  python scripts/backfill_consignado.py                       # pasta padrão
  python scripts/backfill_consignado.py "C:/caminho/pasta"    # outra pasta
  python scripts/backfill_consignado.py arquivo1.pdf arquivo2.pdf
"""

import os
import sys
import glob
import json
import urllib.request
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_consignado_pdf import parse_pdf

PASTA_PADRAO = r'C:/Users/ferna/Downloads/consignado'


def carregar_env():
    """Carrega nibo-scraper/.env (best-effort) se as vars não estiverem no ambiente."""
    if os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_SERVICE_ROLE_KEY'):
        return
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


carregar_env()
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
if not SUPABASE_URL or not SERVICE_KEY or SERVICE_KEY.startswith('cole_'):
    print('Faltam SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY (env ou nibo-scraper/.env).',
          file=sys.stderr)
    sys.exit(1)


def rpc(fn, params):
    url = f'{SUPABASE_URL}/rest/v1/rpc/{fn}'
    data = json.dumps(params).encode('utf-8')
    headers = {
        'apikey': SERVICE_KEY,
        'Authorization': f'Bearer {SERVICE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation',
    }
    req = urllib.request.Request(url, data=data, method='POST', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode('utf-8'))
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'rpc {fn} -> {e.code}: {body[:300]}')


def primeira_linha(resp):
    """RPC retorna objeto ou lista de 1 — normaliza pra dict."""
    if isinstance(resp, list):
        return resp[0] if resp else None
    return resp


def processar(pdf_path):
    nome_arq = os.path.basename(pdf_path)
    r = parse_pdf(pdf_path)
    if not r['contratos']:
        print(f'  [SKIP] {nome_arq}: nenhum contrato parseado')
        return
    comp = r['competencia']

    resumo = primeira_linha(rpc('upsert_consignado_resumo_mes', {
        'p_competencia': comp,
        'p_empregador_cnpj': r['empregador_cnpj'],
        'p_empregador_nome': r['empregador_nome'],
        'p_numero_guia': r['numero_guia'],
        'p_vencimento': r['vencimento'],
        'p_data_emissao': r['data_emissao'],
        'p_qtd_trabalhadores': r['qtd_trabalhadores'],
        'p_total_consignado': r['total_consignado'],
        'p_origem': r['origem'],
    }))
    resumo_id = resumo['id'] if resumo else None

    tally = {'Tijuca': 0, 'Metropolitano': 0, 'nao_atribuido': 0}
    falhas = []
    for c in r['contratos']:
        try:
            row = primeira_linha(rpc('upsert_consignado_contrato', {
                'p_resumo_id': resumo_id,
                'p_competencia': comp,
                'p_nome': c['nome'],
                'p_cpf': c['cpf'],
                'p_numero_contrato': c['numero_contrato'],
                'p_matricula': c['matricula'],
                'p_instituicao_codigo': c['instituicao_codigo'],
                'p_valor_consignado': c['valor_consignado'],
            }))
            emp = (row or {}).get('empresa')
            tally[emp if emp in ('Tijuca', 'Metropolitano') else 'nao_atribuido'] += 1
        except Exception as e:
            falhas.append((c['nome'], str(e)[:160]))

    print(f'  [OK] {comp} ({nome_arq}): {len(r["contratos"])} contratos | '
          f'Tijuca={tally["Tijuca"]} Metropolitano={tally["Metropolitano"]} '
          f'nao_atribuido={tally["nao_atribuido"]}')
    if falhas:
        print(f'    [WARN] {len(falhas)} falharam:')
        for nome, err in falhas[:5]:
            print(f'      - {nome}: {err}')


def resolver_arquivos(args):
    if not args:
        return sorted(glob.glob(os.path.join(PASTA_PADRAO, '*.pdf')))
    arquivos = []
    for a in args:
        if os.path.isdir(a):
            arquivos.extend(sorted(glob.glob(os.path.join(a, '*.pdf'))))
        elif a.lower().endswith('.pdf'):
            arquivos.append(a)
    return arquivos


def main():
    arquivos = resolver_arquivos(sys.argv[1:])
    if not arquivos:
        print('Nenhum PDF encontrado.')
        return
    print(f'{len(arquivos)} arquivo(s) a ingerir:')
    for f in arquivos:
        try:
            processar(f)
        except Exception as e:
            print(f'  [ERR] {os.path.basename(f)}: {e}')


if __name__ == '__main__':
    main()
