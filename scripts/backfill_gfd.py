#!/usr/bin/env python3
"""
backfill_gfd.py — parseia os "GFD - DEMONSTRATIVO - R.E." (FGTS) locais e
popula a tabela `impostos` (imposto='FGTS', tipo='MENSAL'), 1 linha por loja.
Idempotente (upsert_imposto). Reusa a infra do backfill do consignado.

Uso:
  python scripts/backfill_gfd.py                 # pasta padrão
  python scripts/backfill_gfd.py "C:/pasta"      # outra pasta / arquivos
"""

import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # rpc
import parse_gfd_demonstrativo as P

PASTA_PADRAO = r'C:/Users/ferna/Downloads/gfd'
RAZAO = 'RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA'
CNPJ_LOJA = {
    'Tijuca': '41.062.171/0001-22',
    'Metropolitano': '41.062.171/0002-03',
}


def processar(pdf_path):
    nome = os.path.basename(pdf_path)
    r = P.parse_gfd(pdf_path)
    if not r['competencia'] or r['total_guia'] is None:
        print(f'  [SKIP] {nome}: dados incompletos -> {r}')
        return
    if not r['reconcilia']:
        print(f'  [SKIP] {nome}: NÃO reconcilia (soma lojas != total guia) — conferir manualmente')
        return
    for empresa, valor in r['lojas'].items():
        BF.rpc('upsert_imposto', {
            'p_empresa': empresa,
            'p_competencia': r['competencia'],
            'p_imposto': 'FGTS',
            'p_tipo': 'MENSAL',
            'p_valor': valor,
            'p_cnpj': CNPJ_LOJA.get(empresa),
            'p_razao_social': RAZAO,
            'p_vencimento': r['vencimento'],
            'p_numero_documento': r['numero_guia'],
            'p_natureza_receita': 'FGTS Mensal',
        })
    o = f"  (resíduo {r['outros_estabelecimentos']} somado no Tijuca)" if r['outros_estabelecimentos'] else ''
    print(f'  [OK] {r["competencia"]} venc {r["vencimento"]} | '
          f'Tij {r["lojas"]["Tijuca"]:.2f} + Met {r["lojas"]["Metropolitano"]:.2f} '
          f'= {r["total_guia"]:.2f}{o}')


def resolver_arquivos(args):
    if not args:
        return sorted(set(glob.glob(os.path.join(PASTA_PADRAO, '**', '*.pdf'), recursive=True)))
    arquivos = []
    for a in args:
        if os.path.isdir(a):
            arquivos.extend(glob.glob(os.path.join(a, '**', '*.pdf'), recursive=True))
        elif a.lower().endswith('.pdf'):
            arquivos.append(a)
    return sorted(set(arquivos))


def main():
    arquivos = resolver_arquivos(sys.argv[1:])
    if not arquivos:
        print('Nenhum PDF encontrado.')
        return
    print(f'{len(arquivos)} arquivo(s):')
    for f in arquivos:
        try:
            processar(f)
        except Exception as e:
            print(f'  [ERR] {os.path.basename(f)}: {e}')


if __name__ == '__main__':
    main()
