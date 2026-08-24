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

# Nome de mes por extenso -> numero (fallback pra layouts sem "MÊS: MM/YYYY").
_MES_EXTENSO = {
    'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'março': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8, 'setembro': 9,
    'outubro': 10, 'novembro': 11, 'dezembro': 12,
}


def num(s):
    return float(s.replace('.', '').replace(',', '.'))


def parse_pis_cofins(pdf_path, competencia_hint=None):
    """Parseia o demonstrativo PIS/COFINS.

    Tenta extrair a competencia do PDF em 2 formatos:
      1. "MÊS: 06/2026"     (layout historico)
      2. "Junho/2026"       (nome por extenso — layout novo, jun/2026+)

    Se nenhum bater, usa `competencia_hint` (ex: passado pelo sync com base
    na competencia-alvo do run). Se ainda assim None, retorna None.
    """
    full = ''
    with pdfplumber.open(pdf_path) as pdf:
        for pg in pdf.pages:
            full += (pg.extract_text() or '') + '\n'

    competencia = None

    # Formato 1: "MÊS: MM/YYYY"
    mmes = re.search(r'M[\wÊÉê]S:\s*(\d{2})/(\d{4})', full)
    if mmes:
        competencia = f'{mmes.group(2)}-{mmes.group(1)}-01'

    # Formato 2: "Junho/2026", "JUNHO/2026", etc.
    if not competencia:
        for m in re.finditer(r'([A-Za-zçÇÊêé]+)/(\d{4})', full):
            mes = _MES_EXTENSO.get(m.group(1).lower())
            if mes:
                competencia = f'{m.group(2)}-{mes:02d}-01'
                break

    # Fallback: usa a hint (competencia-alvo do sync).
    if not competencia and competencia_hint:
        competencia = competencia_hint

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
