#!/usr/bin/env python3
"""
Parser do "APURAÇÃO IMPOSTOS FEDERAIS" (PDF "Apuração Impostos por loja"),
tabela trimestral de IRPJ e CSLL. Mesmo relatório do PIS/COFINS, mas a tabela
de IRPJ/CSLL só aparece nos fechamentos de trimestre (mar, jun, set, dez).

Tabela "APURAÇÃO IMPOSTOS FEDERAIS" por loja (10 colunas); as 2 ÚLTIMAS são
IRPJ (total) e CSLL (total):
  TIJUCA          ... R$ 16.369,70 R$ 10.096,75
  METROPOLITANO   ... R$ 18.406,31 R$ 11.155,34
  TAQUARA         ... (zeros -> somado no Tijuca/Matriz)

Pago pela Matriz, rateado por loja. Competência = mês de fechamento do
trimestre (vem do "mar/26"). Grava imposto='IRPJ_CSLL', tipo='IRPJ'/'CSLL'.

Uso:
    python parse_irpj_csll.py <pdf_path> [--pretty]
"""

import sys
import os
import json
import re
import pdfplumber

MES_ABREV = {'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
             'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12}

LOJA = {'TIJUCA': 'Tijuca', 'METROPOLITANO': 'Metropolitano', 'TAQUARA': 'Tijuca'}


def clean_num(s):
    s = re.sub(r'\s+', '', s)  # tira espaços internos ("1 6.369,70" -> "16.369,70")
    if s in ('-', ''):
        return 0.0
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def comp_do_texto(full, override):
    if override:
        return override + '-01' if len(override) == 7 else override
    m = re.search(r'\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)/(\d{2})\b',
                  full, re.IGNORECASE)
    if m:
        return f'20{m.group(2)}-{MES_ABREV[m.group(1).lower()]:02d}-01'
    return None


def parse_irpj_csll(path, competencia=None):
    full = ''
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            full += (pg.extract_text() or '') + '\n'

    comp = comp_do_texto(full, competencia)

    por_loja = {'Tijuca': {'IRPJ': 0.0, 'CSLL': 0.0},
                'Metropolitano': {'IRPJ': 0.0, 'CSLL': 0.0}}
    total_linha = None
    for line in full.split('\n'):
        mloja = re.match(r'^(TIJUCA|METROPOLITANO|TAQUARA)\b(.*)', line)
        mtot = re.match(r'^TOTAL\b(.*)', line)
        alvo = mloja or mtot
        if not alvo:
            continue
        resto = alvo.group(2) if mloja else mtot.group(1)
        valores = [clean_num(v) for v in resto.split('R$') if v.strip()]
        # A tabela de apuração (IRPJ/CSLL) tem muitas colunas; a do PIS/COFINS
        # tem 3. Só nos interessa a de apuração (>= 7 valores).
        if len(valores) < 7:
            continue
        irpj, csll = valores[-2], valores[-1]
        if mtot:
            total_linha = (irpj, csll)
        else:
            por_loja[LOJA[mloja.group(1)]]['IRPJ'] += irpj
            por_loja[LOJA[mloja.group(1)]]['CSLL'] += csll

    soma_irpj = sum(v['IRPJ'] for v in por_loja.values())
    soma_csll = sum(v['CSLL'] for v in por_loja.values())
    reconcilia = (total_linha is not None
                  and abs(soma_irpj - total_linha[0]) < 0.02
                  and abs(soma_csll - total_linha[1]) < 0.02)

    return {
        'competencia': comp,
        'imposto': 'IRPJ_CSLL',
        'total': {'IRPJ': round(total_linha[0], 2) if total_linha else None,
                  'CSLL': round(total_linha[1], 2) if total_linha else None},
        'lojas': {k: {'IRPJ': round(v['IRPJ'], 2), 'CSLL': round(v['CSLL'], 2)}
                  for k, v in por_loja.items()},
        'reconcilia': reconcilia,
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: parse_irpj_csll.py <pdf_path> [--pretty]', file=sys.stderr)
        sys.exit(1)
    pretty = '--pretty' in sys.argv
    r = parse_irpj_csll(sys.argv[1])
    print(json.dumps(r, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
