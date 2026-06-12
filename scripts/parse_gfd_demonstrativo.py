#!/usr/bin/env python3
"""
Parser do "GFD - DEMONSTRATIVO - R.E." (guia de FGTS Digital) do Nibo.

A guia é UNIFICADA na Matriz (41.062.171) mas traz o rateio por
estabelecimento (loja). Pro dashboard AGREGADO por loja, pego direto as
linhas "Total do Estabelecimento 41.062.171/XXXX-XX ... <total>":
  - 0001-22 -> Tijuca (Matriz)
  - 0002-03 -> Metropolitano (Filial 01)
  - qualquer outro (ex.: 0003-94) -> somado no Tijuca/Matriz (resíduo)

Confere a reconciliação: soma das lojas == "Total da Guia (FGTS)".

Uso:
    python parse_gfd_demonstrativo.py <pdf_path> [--pretty]
"""

import sys
import json
import re
import pdfplumber

CNPJ_RAIZ = '41.062.171'
ESTAB_RE = re.compile(
    r'Total do Estabelecimento\s*' + re.escape(CNPJ_RAIZ) +
    r'/(\d{4}-\d{2})((?:\s+[\d.]+,\d{2}){6})')


def num(s):
    return float(s.replace('.', '').replace(',', '.'))


def br_date_iso(s):
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s.strip())
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else None


def comp_from_vencimento(venc_iso):
    """FGTS da competência X vence ~dia 20 de X+1. comp = venc − 1 mês."""
    if not venc_iso:
        return None
    y, m, _ = (int(x) for x in venc_iso.split('-'))
    if m == 1:
        y, m = y - 1, 12
    else:
        m -= 1
    return f'{y}-{m:02d}-01'


def loja_from_cnpj(filial):
    if filial == '0002-03':
        return 'Metropolitano'
    return 'Tijuca'  # 0001-22 (Matriz) e quaisquer outros resíduos


def parse_gfd(pdf_path):
    full = ''
    with pdfplumber.open(pdf_path) as pdf:
        for pg in pdf.pages:
            full += (pg.extract_text() or '') + '\n'

    mv = re.search(r'Vencimento da Guia:\s*(\d{2}/\d{2}/\d{4})', full)
    vencimento = br_date_iso(mv.group(1)) if mv else None
    competencia = comp_from_vencimento(vencimento)

    mt = re.search(r'Total da Guia \(FGTS\):\s*([\d.]+,\d{2})', full)
    total_guia = num(mt.group(1)) if mt else None

    mn = re.search(r'mero da Guia:\s*(\S+)', full)  # "Número da Guia: ..."
    numero_guia = mn.group(1) if mn else None

    por_loja = {'Tijuca': 0.0, 'Metropolitano': 0.0}
    outros = {}  # filiais fora de 0001-22/0002-03 (rastreabilidade)
    for filial, nums in ESTAB_RE.findall(full):
        valor = num(re.findall(r'[\d.]+,\d{2}', nums)[-1])  # última col = Total
        por_loja[loja_from_cnpj(filial)] += valor
        if filial not in ('0001-22', '0002-03') and valor:
            outros[filial] = outros.get(filial, 0.0) + valor

    soma = round(por_loja['Tijuca'] + por_loja['Metropolitano'], 2)
    reconcilia = total_guia is not None and abs(soma - total_guia) < 0.01

    return {
        'competencia': competencia,
        'vencimento': vencimento,
        'numero_guia': numero_guia,
        'total_guia': total_guia,
        'lojas': {k: round(v, 2) for k, v in por_loja.items()},
        'outros_estabelecimentos': outros,
        'reconcilia': reconcilia,
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: parse_gfd_demonstrativo.py <pdf_path> [--pretty]', file=sys.stderr)
        sys.exit(1)
    pretty = '--pretty' in sys.argv
    r = parse_gfd(sys.argv[1])
    print(json.dumps(r, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
