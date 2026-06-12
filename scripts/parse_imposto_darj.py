#!/usr/bin/env python3
"""
Parser do DARJ (Documento de Arrecadação do Rio de Janeiro) — guia de ICMS
baixada do Nibo. Cada PDF tem 2 vias (páginas) da mesma guia.

Extrai:
  - empresa: derivada do CNPJ (/0001 = Tijuca, /0002 = Metropolitano)
  - tipo:    "Diferencial de alíquota" -> DIFAL ; senão -> A_PAGAR
  - valor:   "TOTAL A PAGAR"
  - vencimento: "DATA VALIDADE" / "NÃO RECEBER APÓS"
  - competência: vencimento − 1 mês (ICMS RJ vence ~dia 10-11 do mês seguinte)
  - numero_documento, razao_social, natureza_receita

Uso:
    python parse_imposto_darj.py <pdf_path> [--pretty]
"""

import sys
import json
import re
import pdfplumber

NORMALIZE = {
    'Superintend�ncia': 'Superintendencia',
    'Arrecada��o': 'Arrecadacao',
    'al�quota': 'aliquota',
    'Opera��es': 'Operacoes',
    'Pr�prias': 'Proprias',
    'Apura��o': 'Apuracao',
    'N�O': 'NAO',
    'AP�S': 'APOS',
    'Número': 'Numero',
    '�': '',
}

CNPJ_RE = re.compile(r'(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})')


def normalize(text):
    if not text:
        return text
    for k, v in NORMALIZE.items():
        text = text.replace(k, v)
    return text


def parse_decimal(s):
    if s is None:
        return None
    s = s.strip().replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def br_date_iso(s):
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    return f'{y}-{mo}-{d}'


def empresa_from_cnpj(cnpj):
    if not cnpj:
        return None
    if '/0001-' in cnpj:
        return 'Tijuca'
    if '/0002-' in cnpj:
        return 'Metropolitano'
    return None


def comp_from_vencimento(venc_iso):
    """'2026-05-11' -> '2026-04-01' (competência = mês do vencimento − 1)."""
    if not venc_iso:
        return None
    y, m, _ = (int(x) for x in venc_iso.split('-'))
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f'{y}-{m:02d}-01'


def parse_darj(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = normalize(pdf.pages[0].extract_text() or '')

    # CNPJ + empresa + razão social
    mcnpj = CNPJ_RE.search(text)
    cnpj = mcnpj.group(1) if mcnpj else None
    empresa = empresa_from_cnpj(cnpj)
    razao = None
    if cnpj:
        for line in text.split('\n'):
            if cnpj in line:
                razao = line.replace(cnpj, '').strip() or None
                break

    # Tipo (DIFAL vs A_PAGAR). A natureza vira um rótulo canônico (o texto
    # original vem com acentos bugados do PDF — não dá pra confiar no display).
    tipo = 'DIFAL' if 'diferencial' in text.lower() else 'A_PAGAR'
    natureza = ('Diferencial de Alíquota' if tipo == 'DIFAL'
                else 'Operações Próprias - Apuração')

    # Vencimento
    mv = re.search(r'N\S*O RECEBER AP\S*S\s+(\d{2}/\d{2}/\d{4})', text)
    if not mv:
        mv = re.search(r'(\d{2}/\d{2}/\d{4})', text)
    vencimento = br_date_iso(mv.group(1)) if mv else None

    # Total a pagar (autoritativo: principal + juros + multa)
    valor = None
    mt = re.search(r'TOTAL A PAGAR\s*\n\s*([\d.]*\d,\d{2})', text)
    if mt:
        valor = parse_decimal(mt.group(1))
    if valor is None:
        # fallback: valor principal na linha da natureza (ICMS/FECP ... <valor>)
        mvp = re.search(r'ICMS/FECP.+?([\d.]*\d,\d{2})', text)
        if mvp:
            valor = parse_decimal(mvp.group(1))

    # Nº documento
    mdoc = re.search(r'(?:N[º�]?\s*DOCUMENTO|N DOCUMENTO)[\s\S]{0,60}?\b(\d{9})\b', text)
    numero_documento = mdoc.group(1) if mdoc else None

    competencia = comp_from_vencimento(vencimento)

    return {
        'empresa': empresa,
        'cnpj': cnpj,
        'razao_social': razao,
        'competencia': competencia,
        'imposto': 'ICMS',
        'tipo': tipo,
        'valor': valor,
        'vencimento': vencimento,
        'numero_documento': numero_documento,
        'natureza_receita': natureza,
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: parse_imposto_darj.py <pdf_path> [--pretty]', file=sys.stderr)
        sys.exit(1)
    pretty = '--pretty' in sys.argv
    result = parse_darj(sys.argv[1])
    print(json.dumps(result, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
