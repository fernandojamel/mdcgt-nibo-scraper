#!/usr/bin/env python3
"""
backfill_impostos_scaneados.py — insere as guias de ICMS de 12/2025 e 01/2026
que vieram como PDF ESCANEADO (imagem, sem camada de texto) e por isso NÃO
parseiam pelo parse_imposto_darj. Os valores foram lidos manualmente dos DARJ
(conferidos com o quadro "Valores em Real / Total" da 2ª via — DIP).

Idempotente: upsert_imposto pela chave (empresa, competencia, imposto, tipo).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # carregar_env, rpc

RAZAO = 'RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA'

# (empresa, cnpj, competencia, tipo, valor, vencimento, numero_documento)
GUIAS = [
    ('Metropolitano', '41.062.171/0002-03', '2025-12-01', 'A_PAGAR', 10779.43, '2026-01-12', '171436788'),
    ('Metropolitano', '41.062.171/0002-03', '2025-12-01', 'DIFAL',     223.65, '2026-01-12', '171437492'),
    ('Metropolitano', '41.062.171/0002-03', '2026-01-01', 'A_PAGAR',  9888.95, '2026-02-10', '183155030'),
    ('Metropolitano', '41.062.171/0002-03', '2026-01-01', 'DIFAL',     270.13, '2026-02-10', '183155647'),
    ('Tijuca',        '41.062.171/0001-22', '2025-12-01', 'A_PAGAR', 18632.49, '2026-01-12', '171428625'),
    ('Tijuca',        '41.062.171/0001-22', '2025-12-01', 'DIFAL',     147.37, '2026-01-12', '171429577'),
    ('Tijuca',        '41.062.171/0001-22', '2026-01-01', 'A_PAGAR',  2231.78, '2026-02-10', '183130067'),
    ('Tijuca',        '41.062.171/0001-22', '2026-01-01', 'DIFAL',      92.89, '2026-02-10', '183130455'),
]


def main():
    print(f'{len(GUIAS)} guia(s) escaneada(s):')
    for empresa, cnpj, comp, tipo, valor, venc, doc in GUIAS:
        nat = ('Diferencial de Alíquota' if tipo == 'DIFAL'
               else 'Operações Próprias - Apuração')
        BF.rpc('upsert_imposto', {
            'p_empresa': empresa,
            'p_competencia': comp,
            'p_imposto': 'ICMS',
            'p_tipo': tipo,
            'p_valor': valor,
            'p_cnpj': cnpj,
            'p_razao_social': RAZAO,
            'p_vencimento': venc,
            'p_numero_documento': doc,
            'p_natureza_receita': nat,
        })
        print(f'  [OK] {empresa:13} ICMS/{tipo:8} {comp} venc {venc} R$ {valor}')


if __name__ == '__main__':
    main()
