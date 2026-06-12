#!/usr/bin/env python3
"""
backfill_impostos.py — parseia os DARJ locais (ICMS) e popula a tabela
`impostos` no Supabase (via RPC upsert_imposto). Idempotente.

Reaproveita a infra do backfill do consignado (carregar_env, rpc).

Uso:
  python scripts/backfill_impostos.py                    # pasta padrão
  python scripts/backfill_impostos.py "C:/caminho/pasta" # outra pasta
  python scripts/backfill_impostos.py a.pdf b.pdf        # arquivos
"""

import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # carregar_env, rpc, primeira_linha
import parse_imposto_darj as P

PASTA_PADRAO = r'C:/Users/ferna/Downloads/icms'


def processar(pdf_path):
    nome = os.path.basename(pdf_path)
    r = P.parse_darj(pdf_path)
    if not r['empresa'] or not r['competencia'] or r['valor'] is None:
        print(f'  [SKIP] {nome}: dados incompletos -> {r}')
        return
    BF.rpc('upsert_imposto', {
        'p_empresa': r['empresa'],
        'p_competencia': r['competencia'],
        'p_imposto': r['imposto'],
        'p_tipo': r['tipo'],
        'p_valor': r['valor'],
        'p_cnpj': r['cnpj'],
        'p_razao_social': r['razao_social'],
        'p_vencimento': r['vencimento'],
        'p_numero_documento': r['numero_documento'],
        'p_natureza_receita': r['natureza_receita'],
    })
    print(f'  [OK] {r["empresa"]:13} {r["imposto"]}/{r["tipo"]:8} '
          f'{r["competencia"]} venc {r["vencimento"]} R$ {r["valor"]}')


def resolver_arquivos(args):
    # Busca recursiva: cobre tanto a pasta plana quanto subpastas (Tijuca/, Met/).
    if not args:
        return sorted(set(
            glob.glob(os.path.join(PASTA_PADRAO, '**', '*.pdf'), recursive=True)))
    arquivos = []
    for a in args:
        if os.path.isdir(a):
            arquivos.extend(
                glob.glob(os.path.join(a, '**', '*.pdf'), recursive=True))
        elif a.lower().endswith('.pdf'):
            arquivos.append(a)
    return sorted(set(arquivos))


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
