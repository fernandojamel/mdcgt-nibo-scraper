#!/usr/bin/env python3
"""
backfill_inss_irrf.py — parseia os Excel "RATEIO DE IMPOSTOS SOBRE FOLHA
PAGTO." (aba COMPOSIÇÃO DA DARF) e popula `impostos` (imposto='INSS_IRRF',
tipo='INSS'/'IRRF'), 1 linha por loja/tipo. Idempotente.

Uso:
  python scripts/backfill_inss_irrf.py            # pasta padrão
  python scripts/backfill_inss_irrf.py "C:/pasta" # outra pasta / arquivos
"""

import os
import sys
import glob
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # rpc
import parse_inss_irrf as P
from feriados_brasil import vencimento_federal

PASTA_PADRAO = r'C:/Users/ferna/Downloads/inss_irrf'
RAZAO = 'RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA'
CNPJ_LOJA = {
    'Tijuca': '41.062.171/0001-22',
    'Metropolitano': '41.062.171/0002-03',
}
# DARF (INSS+IRRF sobre folha) vence sempre dia 20 do mes seguinte a
# competencia, antecipado pro dia util anterior se cair em fim de
# semana/feriado -- confirmado pelo Fernando. O Excel de rateio nao
# imprime essa data -- calculamos.
DIA_VENCIMENTO = 20


def processar(path, competencia=None):
    """Retorna o dict parseado (com 'vencimento' calculado) em caso de
    sucesso, ou None se pulou -- usado pelo sync_inss_irrf.py pra saber se
    disparou o relatorio por e-mail."""
    nome = os.path.basename(path)
    r = P.parse_inss_irrf(path, competencia)
    if not r['competencia']:
        print(f'  [SKIP] {nome}: competência não identificada')
        return None
    if not r['reconcilia']:
        print(f'  [SKIP] {nome}: NÃO reconcilia (INSS+IRRF != TOTAL DARF) — conferir')
        return None

    comp_date = date.fromisoformat(r['competencia'])
    vencimento = vencimento_federal(comp_date, DIA_VENCIMENTO).isoformat()

    for empresa, vals in r['lojas'].items():
        for tipo in ('INSS', 'IRRF'):
            BF.rpc('upsert_imposto', {
                'p_empresa': empresa,
                'p_competencia': r['competencia'],
                'p_imposto': 'INSS_IRRF',
                'p_tipo': tipo,
                'p_valor': vals[tipo],
                'p_cnpj': CNPJ_LOJA.get(empresa),
                'p_razao_social': RAZAO,
                'p_vencimento': vencimento,
                'p_natureza_receita': f'{tipo} (DARF folha)',
            })
    tij = r['lojas']['Tijuca']
    met = r['lojas']['Metropolitano']
    print(f'  [OK] {r["competencia"]} venc {vencimento} | Tij INSS {tij["INSS"]:.2f} IRRF {tij["IRRF"]:.2f} | '
          f'Met INSS {met["INSS"]:.2f} IRRF {met["IRRF"]:.2f} (DARF {r["total_darf"]:.2f})')
    r['vencimento'] = vencimento
    return r


def resolver_arquivos(args):
    pad = ['**/*.xlsx', '**/*.xls']
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
        elif a.lower().endswith(('.xlsx', '.xls')):
            arquivos.append(a)
    return sorted(set(arquivos))


def main():
    arquivos = resolver_arquivos(sys.argv[1:])
    if not arquivos:
        print('Nenhum Excel encontrado.')
        return
    print(f'{len(arquivos)} arquivo(s):')
    for f in arquivos:
        try:
            processar(f)
        except Exception as e:
            print(f'  [ERR] {os.path.basename(f)}: {e}')


if __name__ == '__main__':
    main()
