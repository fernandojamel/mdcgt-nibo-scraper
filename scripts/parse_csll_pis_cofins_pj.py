#!/usr/bin/env python3
"""
parse_csll_pis_cofins_pj.py — extrai retencoes de PJ a PJ (IRRF + CRF/CSLL+
PIS+COFINS) do PDF unificado "DARF RETENCOES PJ MM.AAAA + RATEIO".

Layout do PDF (2 paginas):
  Pagina 1: DARF da Receita Federal (Matriz 41.062.171/0001-22)
    - Periodo de Apuracao: Maio/2026  ->  competencia
    - Vencimento: 19/06/2026
    - Numero do Documento: 07.16.26156.7126741-5
    - Valor Total: 237,41
  Pagina 2: Relatorio "RETENCOES A RECOLHER" com rateio por estabelecimento:
    - Estabelecimento: 41062171000122 (Tijuca)
       Total Geral do Estabelecimento: 144,24
    - Estabelecimento: 41062171000203 (Metropolitano)
       Total Geral do Estabelecimento: 93,17
    - Total Geral: 237,41  (= soma)

Retorna 1 entrada POR LOJA com o total geral (IRRF + CRF agregados).
Validacao: soma das lojas deve bater com Total Geral do Documento (toleramos
0,02 de arredondamento).
"""
import os
import re
import sys
from datetime import date

import pdfplumber

# CNPJ (no PDF vem sem pontos) -> nome de empresa usado no projeto.
CNPJ_TO_EMPRESA = {
    '41062171000122': 'Tijuca',
    '41062171000203': 'Metropolitano',
}

# Pra mostrar no log: "Maio" -> 5, etc.
_MESES_PT = {
    'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'março': 3, 'abril': 4,
    'maio': 5, 'junho': 6, 'julho': 7, 'agosto': 8, 'setembro': 9,
    'outubro': 10, 'novembro': 11, 'dezembro': 12,
}


def _money(s):
    """'1.234,56' -> 1234.56."""
    return float(s.strip().replace('.', '').replace(',', '.'))


def parse_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = '\n'.join(p.extract_text() or '' for p in pdf.pages)

    # 1) Competencia: "Periodo de Apuracao" tem "Maio/2026" na pagina 1,
    #    OU "Periodo: 01/05/2026 a 31/05/2026" na pagina 2.
    competencia = None
    m = re.search(r'Per[íi]odo:?\s*(\d{2})/(\d{2})/(\d{4})\s*a', text)
    if m:
        competencia = f'{m.group(3)}-{m.group(2)}-01'
    else:
        # Tenta "Janeiro/2026", "Maio/2026", etc.
        m = re.search(r'([A-Z][a-zç]+)/(\d{4})', text)
        if m:
            mes = _MESES_PT.get(m.group(1).lower())
            if mes:
                competencia = f'{m.group(2)}-{mes:02d}-01'

    # 2) Vencimento (DD/MM/AAAA)
    vencimento = None
    m = re.search(r'(?:Vencimento|Pagar at[eé]):?\s*(\d{2})/(\d{2})/(\d{4})',
                  text, re.IGNORECASE)
    if m:
        vencimento = f'{m.group(3)}-{m.group(2)}-{m.group(1)}'

    # 3) Numero do Documento
    numero_doc = None
    m = re.search(r'N[uú]mero do Documento\s*\n?\s*([\d.\-/]+)', text)
    if m:
        numero_doc = m.group(1).strip()

    # 4) Total da guia (pagina 1: "Valor Total do Documento\n237,41" OU
    #    pagina 2: "Total Geral: 237,41").
    total_guia = None
    m = re.search(r'Valor Total do Documento\s*\n?\s*([\d.,]+)', text)
    if m:
        try:
            total_guia = _money(m.group(1))
        except ValueError:
            pass
    if total_guia is None:
        m = re.search(r'Total Geral:\s*([\d.,]+)', text)
        if m:
            try:
                total_guia = _money(m.group(1))
            except ValueError:
                pass

    # 5) Rateio por estabelecimento (pagina 2).
    # Pra cada CNPJ esperado, acha o bloco "Estabelecimento: <cnpj>" e dentro
    # dele o "Total Geral do Estabelecimento: X,XX". DOTALL pra atravessar
    # quebras de linha.
    lojas = {}
    for cnpj, empresa in CNPJ_TO_EMPRESA.items():
        pat = (re.escape(f'Estabelecimento: {cnpj}')
               + r'.*?Total Geral do Estabelecimento:?\s*([\d.,]+)')
        m = re.search(pat, text, re.DOTALL)
        if m:
            try:
                lojas[empresa] = _money(m.group(1))
            except ValueError:
                pass

    # 6) Reconciliacao
    soma_lojas = sum(lojas.values())
    reconcilia = (total_guia is None) or abs(soma_lojas - total_guia) < 0.02

    return {
        'competencia': competencia,
        'vencimento': vencimento,
        'numero_documento': numero_doc,
        'total_guia': total_guia,
        'lojas': lojas,
        'reconcilia': reconcilia,
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Uso: parse_csll_pis_cofins_pj.py <pdf>')
        sys.exit(1)
    r = parse_pdf(sys.argv[1])
    print(r)
