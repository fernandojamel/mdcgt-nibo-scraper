#!/usr/bin/env python3
"""
backfill_eventos.py — processa os PDFs já no Supabase Storage e popula
folha_funcionario_mes e folha_resumo_mes.

Pra cada documento_id em folha_documentos:
  1. baixa o PDF do Storage
  2. roda parse_folha_pdf.parse_pdf()
  3. chama RPCs upsert_folha_funcionario_mes e upsert_folha_resumo_mes
  4. log de sucesso/falha

Idempotente — pode rodar quantas vezes quiser, vai re-popular as tabelas.

Variáveis de ambiente esperadas (mesmas do backfill.js):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Uso:
  python --env-file=... scripts/backfill_eventos.py            # processa todos
  python scripts/backfill_eventos.py --only-id <doc_uuid>      # 1 documento
  python scripts/backfill_eventos.py --only-pending            # so docs sem
                                                                # entradas em
                                                                # folha_funcionario_mes
                                                                # (modo cron/sync)
"""

import os
import sys
import json
import tempfile
import urllib.request
from urllib.error import HTTPError

# Importa o parser
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_folha_pdf import parse_pdf

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
if not SUPABASE_URL or not SERVICE_KEY or SERVICE_KEY.startswith('cole_'):
    print('Faltam SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY no .env', file=sys.stderr)
    sys.exit(1)


def supabase_request(method, path, body=None, query=None):
    """Chamada genérica à API Supabase (REST ou Storage)."""
    if query:
        from urllib.parse import urlencode
        path = f'{path}?{urlencode(query)}'
    url = f'{SUPABASE_URL}{path}'
    data = None
    headers = {
        'apikey': SERVICE_KEY,
        'Authorization': f'Bearer {SERVICE_KEY}',
    }
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
        headers['Prefer'] = 'return=representation'

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            ct = resp.headers.get('content-type', '')
            raw = resp.read()
            if 'application/json' in ct:
                return json.loads(raw.decode('utf-8'))
            return raw
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'{method} {path} -> {e.code}: {body[:300]}')


def listar_documentos(only_pending=False):
    rows = supabase_request('GET', '/rest/v1/folha_documentos', query={
        'select': 'id,competencia,storage_bucket,storage_path,nibo_document_id,tipo',
        'tipo': 'eq.extrato_mensal',
        'order': 'competencia.desc',
    })
    if not only_pending:
        return rows
    # Filtra docs que NAO tem nenhuma linha em folha_funcionario_mes.
    # Usa documento_id = eq.X via PostgREST: pra cada doc, faz um count.
    # Mais eficiente: traz a lista de documento_ids ja processados em 1 query.
    processados_rows = supabase_request(
        'GET', '/rest/v1/folha_funcionario_mes',
        query={'select': 'documento_id'},
    )
    processados = {r['documento_id'] for r in processados_rows if r.get('documento_id')}
    return [d for d in rows if d['id'] not in processados]


def baixar_pdf(bucket, path):
    raw = supabase_request('GET', f'/storage/v1/object/{bucket}/{path}')
    return raw  # bytes


def upsert_funcionario(documento_id, competencia, f):
    params = {
        'p_documento_id': documento_id,
        'p_competencia': competencia,
        'p_empr_id': f['empr_id'],
        'p_nome': f['nome'],
        'p_cpf': f.get('cpf'),
        'p_situacao': f.get('situacao'),
        'p_vinculo': f.get('vinculo'),
        'p_data_admissao': f.get('data_admissao'),
        'p_cc': f.get('cc'),
        'p_depto': f.get('depto'),
        'p_horas_mes': f.get('horas_mes'),
        'p_cargo_codigo': f.get('cargo_codigo'),
        'p_cargo_nome': f.get('cargo_nome'),
        'p_cbo': f.get('cbo'),
        'p_filial': f.get('filial'),
        'p_salario_base': f.get('salario_base'),
        'p_proventos': f.get('proventos', 0),
        'p_descontos': f.get('descontos', 0),
        'p_liquido': f.get('liquido', 0),
        'p_informativa': f.get('informativa', 0),
        'p_informativa_dedutora': f.get('informativa_dedutora', 0),
        'p_base_inss': f.get('base_inss'),
        'p_excedente_inss': f.get('excedente_inss'),
        'p_base_fgts': f.get('base_fgts'),
        'p_valor_fgts': f.get('valor_fgts'),
        'p_base_irrf': f.get('base_irrf'),
        'p_eventos': f.get('eventos', []),
        'p_demitido_em': f.get('demitido_em'),
        'p_motivo_demissao': f.get('motivo_demissao'),
    }
    supabase_request('POST', '/rest/v1/rpc/upsert_folha_funcionario_mes', body=params)


def upsert_resumo(documento_id, competencia, resumo):
    supabase_request('POST', '/rest/v1/rpc/upsert_folha_resumo_mes', body={
        'p_documento_id': documento_id,
        'p_competencia': competencia,
        'p_resumo': resumo,
    })


def processar_um(doc):
    print(f'\n-> {doc["competencia"]} {doc["storage_path"]}')
    pdf_bytes = baixar_pdf(doc['storage_bucket'], doc['storage_path'])
    print(f'  PDF baixado ({len(pdf_bytes)} bytes)')

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        parsed = parse_pdf(tmp_path)
        funcionarios = parsed['funcionarios']
        resumo = parsed.get('resumo', {}) or {}
        comp = doc['competencia']
        if isinstance(comp, str) and 'T' in comp:
            comp = comp.split('T')[0]

        print(f'  parseados: {len(funcionarios)} funcionarios, '
              f'resumo: total_inss={resumo.get("total_inss")}, valor_fgts={resumo.get("valor_fgts")}')

        ok_func = 0
        falhas = []
        for f in funcionarios:
            try:
                upsert_funcionario(doc['id'], comp, f)
                ok_func += 1
            except Exception as e:
                falhas.append({'nome': f.get('nome'), 'error': str(e)[:200]})

        if resumo:
            try:
                upsert_resumo(doc['id'], comp, resumo)
                print(f'  [OK] resumo gravado')
            except Exception as e:
                print(f'  [ERR] resumo falhou: {e}')

        if falhas:
            print(f'  [WARN] {len(falhas)} funcionarios falharam:')
            for fl in falhas[:5]:
                print(f'    - {fl["nome"]}: {fl["error"]}')
        return ok_func, len(falhas)
    finally:
        os.unlink(tmp_path)


def main():
    only = None
    only_pending = False
    for i, arg in enumerate(sys.argv):
        if arg == '--only-id' and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]
        if arg == '--only-pending':
            only_pending = True

    docs = listar_documentos(only_pending=only_pending)
    if only:
        docs = [d for d in docs if d['id'] == only]
    if not docs:
        msg = 'Nenhum documento pendente.' if only_pending else 'Nenhum documento encontrado.'
        print(msg)
        return

    print(f'{len(docs)} documentos a processar.')
    total_ok = 0
    total_falha = 0
    for d in docs:
        try:
            ok, fail = processar_um(d)
            total_ok += ok
            total_falha += fail
        except Exception as e:
            print(f'  [ERR] FATAL no documento {d["id"]}: {e}')
            total_falha += 1

    print('\n' + '=' * 50)
    print(f'  Total funcionarios upsertados: {total_ok}')
    print(f'  Falhas: {total_falha}')
    print('=' * 50)


if __name__ == '__main__':
    main()
