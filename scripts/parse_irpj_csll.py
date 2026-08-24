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
from datetime import date
import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feriados_brasil import vencimento_trimestral_federal

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
    """Parseia o PDF 'APURAÇÃO IMPOSTOS FEDERAIS MANIA TIJUCA'.

    Layout (jun/2026+): 3 tabelas empilhadas cada uma iniciada por "LOJA
    FATURAMENTO ...":
      1. IRPJ (colunas ... IRPJ A RECOLHER) — trimestral
      2. CSLL (colunas ... CSLL A RECOLHER) — trimestral
      3. PIS/COFINS (mensal, ignorado aqui — cuidado pelo parse_pis_cofins)

    Estrategia: split por "LOJA FATURAMENTO"; pra cada tabela identifica pelo
    header ("IRPJ A RECOLHER" ou "CSLL A RECOLHER") e extrai o ÚLTIMO valor
    R$ de cada linha TIJUCA/METROPOLITANO (que sempre eh o "A RECOLHER").
    """
    full = ''
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            full += (pg.extract_text() or '') + '\n'

    comp = comp_do_texto(full, competencia)

    por_loja = {'Tijuca': {'IRPJ': 0.0, 'CSLL': 0.0},
                'Metropolitano': {'IRPJ': 0.0, 'CSLL': 0.0}}
    total_por_tipo = {'IRPJ': None, 'CSLL': None}

    # Split por "LOJA FATURAMENTO" — cada segmento eh uma tabela.
    partes = re.split(r'LOJA\s+FATURAMENTO', full)
    for parte in partes[1:]:  # partes[0] eh o cabecalho antes da 1a tabela
        linhas = parte.split('\n')
        header = linhas[0].upper() if linhas else ''
        if 'IRPJ A RECOLHER' in header:
            tipo = 'IRPJ'
        elif 'CSLL A RECOLHER' in header:
            tipo = 'CSLL'
        else:
            continue  # tabela de PIS/COFINS ou outra — ignora

        for linha in linhas[1:]:
            mloja = re.match(r'^(TIJUCA|METROPOLITANO|TAQUARA)\s+(.*)', linha)
            mtot = re.match(r'^TOTAL\s+(.*)', linha)
            alvo = mloja or mtot
            if not alvo:
                continue
            resto = mloja.group(2) if mloja else mtot.group(1)
            # Pega TODOS os "R$ N,NN" da linha (com espaco tolerado nos
            # numeros porque pdfplumber as vezes quebra "1 03,28").
            valores = re.findall(r'R\$\s*([\d\s.]+,\d{2})', resto)
            if not valores:
                continue
            recolher = clean_num(valores[-1])
            if mtot:
                total_por_tipo[tipo] = recolher
            else:
                por_loja[LOJA[mloja.group(1)]][tipo] += recolher

    soma_irpj = sum(v['IRPJ'] for v in por_loja.values())
    soma_csll = sum(v['CSLL'] for v in por_loja.values())
    reconcilia = (
        total_por_tipo['IRPJ'] is not None
        and total_por_tipo['CSLL'] is not None
        and abs(soma_irpj - total_por_tipo['IRPJ']) < 0.02
        and abs(soma_csll - total_por_tipo['CSLL']) < 0.02
    )

    vencimento = None
    if comp:
        ano, mes = int(comp[:4]), int(comp[5:7])
        vencimento = vencimento_trimestral_federal(date(ano, mes, 1)).isoformat()

    return {
        'competencia': comp,
        'imposto': 'IRPJ_CSLL',
        'vencimento': vencimento,
        'total': {
            'IRPJ': round(total_por_tipo['IRPJ'], 2) if total_por_tipo['IRPJ'] else None,
            'CSLL': round(total_por_tipo['CSLL'], 2) if total_por_tipo['CSLL'] else None,
        },
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
