#!/usr/bin/env python3
"""
backfill_pis_cofins.py — parseia os "DEMONSTRATIVO APURAÇÃO PIS E COFINS"
locais e popula `impostos` (imposto='PIS_COFINS', tipo='PIS'/'COFINS'),
1 linha por loja/tipo. Idempotente. Reusa a infra do backfill do consignado.

Uso:
  python scripts/backfill_pis_cofins.py            # pasta padrão
  python scripts/backfill_pis_cofins.py "C:/pasta" # outra pasta / arquivos
"""

import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # rpc
import parse_pis_cofins as P

PASTA_PADRAO = r'C:/Users/ferna/Downloads/pis_cofins'
RAZAO = 'RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA'
CNPJ_LOJA = {
    'Tijuca': '41.062.171/0001-22',
    'Metropolitano': '41.062.171/0002-03',
}


def processar(pdf_path, competencia_hint=None):
    """Se o parser nao conseguir extrair competencia do PDF, usa `competencia_hint`
    (ex: passado pelo sync_pis_cofins com base na competencia-alvo do run)."""
    nome = os.path.basename(pdf_path)
    r = P.parse_pis_cofins(pdf_path, competencia_hint=competencia_hint)
    if not r['competencia'] or not r['lojas']:
        print(f'  [SKIP] {nome}: dados incompletos -> {r}')
        return
    for empresa, vals in r['lojas'].items():
        for tipo in ('PIS', 'COFINS'):
            valor = vals['pis'] if tipo == 'PIS' else vals['cofins']
            BF.rpc('upsert_imposto', {
                'p_empresa': empresa,
                'p_competencia': r['competencia'],
                'p_imposto': 'PIS_COFINS',
                'p_tipo': tipo,
                'p_valor': valor,
                'p_cnpj': CNPJ_LOJA.get(empresa),
                'p_razao_social': RAZAO,
                'p_natureza_receita': f'{tipo} (provisão federal)',
            })
    tij = r['lojas'].get('Tijuca', {})
    met = r['lojas'].get('Metropolitano', {})
    print(f'  [OK] {r["competencia"]} | Tij PIS {tij.get("pis", 0):.2f} COF {tij.get("cofins", 0):.2f} | '
          f'Met PIS {met.get("pis", 0):.2f} COF {met.get("cofins", 0):.2f}')


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
