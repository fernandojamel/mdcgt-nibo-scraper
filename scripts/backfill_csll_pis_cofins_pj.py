#!/usr/bin/env python3
"""
backfill_csll_pis_cofins_pj.py — parseia PDFs da DARF de retencoes PJ a PJ
(IRRF + CSLL/PIS/COFINS unificados) e popula `impostos` (imposto=
'CSLL_PIS_COFINS_PJ', tipo='RETENCAO_PJ', 1 linha por loja).
Idempotente via upsert_imposto.

Uso:
  python scripts/backfill_csll_pis_cofins_pj.py            # pasta padrao
  python scripts/backfill_csll_pis_cofins_pj.py "C:/pasta" # outra pasta/arquivos
"""
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF   # rpc, carregar_env
import parse_csll_pis_cofins_pj as P

IMPOSTO = 'CSLL_PIS_COFINS_PJ'
TIPO = 'RETENCAO_PJ'
RAZAO = 'RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA'
CNPJ_LOJA = {
    'Tijuca': '41.062.171/0001-22',
    'Metropolitano': '41.062.171/0002-03',
}

PASTA_PADRAO = r'C:/Users/ferna/Downloads/csll_pis_cofins_pj'


def processar(path):
    nome = os.path.basename(path)
    r = P.parse_pdf(path)
    if not r['competencia']:
        print(f'  [SKIP] {nome}: competencia nao identificada')
        return
    if not r['lojas']:
        print(f'  [SKIP] {nome}: nenhuma loja com rateio encontrado')
        return
    if not r['reconcilia']:
        print(f'  [SKIP] {nome}: soma das lojas '
              f'({sum(r["lojas"].values()):.2f}) NAO bate com total da guia '
              f'({r["total_guia"]:.2f}) — conferir')
        return

    for empresa, valor in r['lojas'].items():
        BF.rpc('upsert_imposto', {
            'p_empresa': empresa,
            'p_competencia': r['competencia'],
            'p_imposto': IMPOSTO,
            'p_tipo': TIPO,
            'p_valor': valor,
            'p_cnpj': CNPJ_LOJA.get(empresa),
            'p_razao_social': RAZAO,
            'p_natureza_receita': 'Retencao PJ a PJ (IRRF + CSLL/PIS/COFINS)',
        })

    tij = r['lojas'].get('Tijuca', 0)
    met = r['lojas'].get('Metropolitano', 0)
    print(f'  [OK] {r["competencia"]} ({nome}): '
          f'Tij {tij:.2f} | Met {met:.2f} (total {tij + met:.2f})')


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
