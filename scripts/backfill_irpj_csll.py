#!/usr/bin/env python3
"""
backfill_irpj_csll.py — parseia os "Apuração Impostos por loja" (fechamentos de
trimestre) e popula `impostos` (imposto='IRPJ_CSLL', tipo='IRPJ'/'CSLL'),
1 linha por loja/tipo. Idempotente.

Uso:
  python scripts/backfill_irpj_csll.py            # pasta padrão
  python scripts/backfill_irpj_csll.py "C:/pasta" # outra pasta / arquivos
"""

import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # rpc
import parse_irpj_csll as P

PASTA_PADRAO = r'C:/Users/ferna/Downloads/irpj_csll'
RAZAO = 'RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA'
CNPJ_LOJA = {
    'Tijuca': '41.062.171/0001-22',
    'Metropolitano': '41.062.171/0002-03',
}


def processar(path, competencia=None):
    nome = os.path.basename(path)
    r = P.parse_irpj_csll(path, competencia)
    if not r['competencia']:
        print(f'  [SKIP] {nome}: competência não identificada')
        return
    if not r['reconcilia']:
        print(f'  [SKIP] {nome}: NÃO reconcilia (não é o fechamento trimestral?) — conferir')
        return
    for empresa, vals in r['lojas'].items():
        for tipo in ('IRPJ', 'CSLL'):
            BF.rpc('upsert_imposto', {
                'p_empresa': empresa,
                'p_competencia': r['competencia'],
                'p_imposto': 'IRPJ_CSLL',
                'p_tipo': tipo,
                'p_valor': vals[tipo],
                'p_cnpj': CNPJ_LOJA.get(empresa),
                'p_razao_social': RAZAO,
                'p_natureza_receita': f'{tipo} (provisão federal trimestral)',
            })
    tij = r['lojas']['Tijuca']
    met = r['lojas']['Metropolitano']
    print(f'  [OK] {r["competencia"]} | Tij IRPJ {tij["IRPJ"]:.2f} CSLL {tij["CSLL"]:.2f} | '
          f'Met IRPJ {met["IRPJ"]:.2f} CSLL {met["CSLL"]:.2f}')


def resolver_arquivos(args):
    pad = ['**/*.pdf']
    if not args:
        out = []
        for p in pad:
            out += glob.glob(os.path.join(PASTA_PADRAO, p), recursive=True)
        return sorted(set(out))
    arquivos = []
    for a in args:
        if os.path.isdir(a):
            for p in pad:
                arquivos += glob.glob(os.path.join(a, p), recursive=True)
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
