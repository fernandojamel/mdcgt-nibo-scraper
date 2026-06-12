#!/usr/bin/env python3
"""
Parser do "DEMONSTRATIVO APURAÇÃO PIS E COFINS" (PROVISÃO IMPOSTOS FEDERAIS).

Tabela simples por loja:
  LOJA            FATURAMENTO TRIBUTADO   PIS        COFINS
  TIJUCA          462.853,11              3.008,55   13.885,59
  METROPOLITANO   268.267,33              1.743,74   8.048,02

Pago pela Matriz (Tijuca) mas rateado entre as lojas. Competência = "MÊS".
Grava como imposto='PIS_COFINS', tipo='PIS' e 'COFINS' (1 linha por loja/tipo).

Uso:
    python parse_pis_cofins.py <pdf_path> [--pretty]
"""

import sys
import json
import re
import pdfplumber

# Linha de loja: NOME  R$ <faturamento>  R$ <pis>  R$ <cofins>
LINHA_RE = re.compile(
    r'(TIJUCA|METROPOLITANO)\s+'
    r'R\$\s*([\d.]+,\d{2})\s+'
    r'R\$\s*([\d.]+,\d{2})\s+'
    r'R\$\s*([\d.]+,\d{2})')

LOJA = {'TIJUCA': 'Tijuca', 'METROPOLITANO': 'Metropolitano'}


def num(s):
    return float(s.replace('.', '').replace(',', '.'))


def parse_pis_cofins(pdf_path):
    full = ''
    with pdfplumber.open(pdf_path) as pdf:
        for pg in pdf.pages:
            full += (pg.extract_text() or '') + '\n'

    mmes = re.search(r'M[\wÊÉ]S:\s*(\d{2})/(\d{4})', full)
    competencia = f'{mmes.group(2)}-{mmes.group(1)}-01' if mmes else None

    lojas = {}
    for nome, fat, pis, cofins in LINHA_RE.findall(full):
        lojas[LOJA[nome]] = {
            'faturamento': num(fat),
            'pis': num(pis),
            'cofins': num(cofins),
        }

    return {
        'competencia': competencia,
        'imposto': 'PIS_COFINS',
        'lojas': lojas,
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: parse_pis_cofins.py <pdf_path> [--pretty]', file=sys.stderr)
        sys.exit(1)
    pretty = '--pretty' in sys.argv
    r = parse_pis_cofins(sys.argv[1])
    print(json.dumps(r, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
