#!/usr/bin/env python3
"""
Parser do PDF "GFD CONSIGNADO DEMONSTRATIVO" (FGTS Digital — Detalhe da Guia
Emitida) do Nibo.

Estrutura:
- Cabeçalho (repetido por página):
    Empregador: <cnpj_raiz> Nome Empregador: <NOME> Qtd. Trabalhadores Consignado: <N> Origem: <X>
    Vencimento da Guia: <dd/mm/aaaa> Total da Guia (Consignado): <valor>
    Número da Guia: <num> Data Emissão: <dd/mm/aaaa hh:mm:ss> (Brasília) Emitida por: <X>
- Tabela "Relação de Trabalhadores", 1 linha por CONTRATO:
    <Comp. Apuração> <Vencimento> <Nome...> <Matrícula> <CPF> <Nº Contrato> <Inst. Financeira> <Valor>
  (um trabalhador pode ter várias linhas — vários contratos)
- "Total Consignado" no rodapé.

A linha é ancorada no CPF (ddd.ddd.ddd-dd), que tem formato fixo:
  - antes do CPF: <comp> <venc> <nome...> <matricula>  → matrícula = último token
  - depois do CPF: <contrato> <instituicao> <valor>     → exatamente 3 tokens

Uso:
    python parse_consignado_pdf.py <pdf_path> [--pretty]

Saída (JSON):
{
  "competencia": "2026-01-01",
  "empregador_cnpj": "41.062.171",
  "empregador_nome": "RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS LTDA",
  "numero_guia": "0126021120278451-2",
  "vencimento": "2026-02-20",
  "data_emissao": "2026-02-11T10:02:43-03:00",
  "qtd_trabalhadores": 15,
  "total_consignado": 8326.44,
  "origem": "Gestao de Guias",
  "contratos": [
    {"comp": "01/2026", "vencimento": "2026-02-20", "nome": "...",
     "matricula": "92", "cpf": "147.460.817-51",
     "numero_contrato": "000584694138", "instituicao_codigo": "389",
     "valor_consignado": 403.23},
    ...
  ]
}
"""

import sys
import json
import re
import pdfplumber

# Encoding do PDF está quebrado pra acentos — pdfplumber retorna �.
# Como só precisamos de valores numéricos + nomes (sem acento já serve),
# normalizamos as palavras-chave conhecidas dos rótulos.
NORMALIZE = {
    'Apura��o': 'Apuracao',
    'Matr�cula': 'Matricula',
    'N�mero': 'Numero',
    'Institui��o': 'Instituicao',
    'Rela��o': 'Relacao',
    'Emiss�o': 'Emissao',
    'Gest�o': 'Gestao',
    'Bras�lia': 'Brasilia',
    '�': '',
}

CPF_RE = re.compile(r'\d{3}\.\d{3}\.\d{3}-\d{2}')
# Linha de dados começa com "MM/AAAA  dd/mm/aaaa "
DATA_LINE_RE = re.compile(r'^\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}\s')
VALOR_RE = re.compile(r'^[\d.]*\d,\d{2}$')


def normalize(text):
    if not text:
        return text
    for k, v in NORMALIZE.items():
        text = text.replace(k, v)
    return text


def parse_decimal(s):
    """'8.326,44' -> 8326.44 ; '403,23' -> 403.23"""
    if s is None:
        return None
    s = s.strip()
    if not s or s == '-':
        return None
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def comp_to_date(s):
    """'01/2026' -> '2026-01-01'"""
    m = re.match(r'(\d{2})/(\d{4})', s.strip())
    if not m:
        return None
    mo, y = m.groups()
    return f'{y}-{mo}-01'


def br_date_to_iso(s):
    """'20/02/2026' -> '2026-02-20'"""
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s.strip())
    if not m:
        return None
    d, mo, y = m.groups()
    return f'{y}-{mo}-{d}'


def br_datetime_to_iso(s):
    """'11/02/2026 10:02:43' -> '2026-02-11T10:02:43-03:00' (horário de Brasília)"""
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})', s.strip())
    if not m:
        return None
    d, mo, y, hh, mm, ss = m.groups()
    return f'{y}-{mo}-{d}T{hh}:{mm}:{ss}-03:00'


def parse_data_line(line):
    """Parseia 1 linha de contrato. Retorna dict ou None."""
    cpf_match = CPF_RE.search(line)
    if not cpf_match:
        return None
    cpf = cpf_match.group(0)
    prefixo = line[:cpf_match.start()].strip()
    sufixo = line[cpf_match.end():].strip()

    pre_tokens = prefixo.split()
    suf_tokens = sufixo.split()
    # Prefixo: comp venc nome... matricula  (mínimo 4 tokens)
    # Sufixo:  contrato instituicao valor    (exatamente 3 tokens)
    if len(pre_tokens) < 4 or len(suf_tokens) < 3:
        return None

    comp = pre_tokens[0]
    venc = pre_tokens[1]
    matricula = pre_tokens[-1]
    nome = ' '.join(pre_tokens[2:-1]).strip()

    valor = suf_tokens[-1]
    instituicao = suf_tokens[-2]
    numero_contrato = ' '.join(suf_tokens[:-2])

    if not VALOR_RE.match(valor):
        return None

    return {
        'comp': comp,
        'vencimento': br_date_to_iso(venc),
        'nome': nome,
        'matricula': matricula,
        'cpf': cpf,
        'numero_contrato': numero_contrato,
        'instituicao_codigo': instituicao,
        'valor_consignado': parse_decimal(valor),
    }


def parse_header(lines, header):
    """Extrai campos do cabeçalho da guia. Atualiza `header` in-place."""
    for line in lines:
        if 'empregador_cnpj' not in header:
            m = re.search(
                r'Empregador:\s*([\d\.]+)\s+Nome Empregador:\s*(.+?)\s+Qtd\.?\s*Trabalhadores',
                line)
            if m:
                header['empregador_cnpj'] = m.group(1).strip()
                header['empregador_nome'] = m.group(2).strip()
            m = re.search(r'Trabalhadores\s+Consignado:\s*(\d+)', line)
            if m:
                header['qtd_trabalhadores'] = int(m.group(1))
            m = re.search(r'Origem:\s*(.+?)\s*$', line)
            if m:
                header['origem'] = normalize(m.group(1)).strip()
        if 'vencimento' not in header:
            m = re.search(r'Vencimento da Guia:\s*(\d{2}/\d{2}/\d{4})', line)
            if m:
                header['vencimento'] = br_date_to_iso(m.group(1))
        if 'total_consignado' not in header:
            m = re.search(r'Total da Guia\s*\(Consignado\):\s*([\d.,]+)', line)
            if m:
                header['total_consignado'] = parse_decimal(m.group(1))
        if 'numero_guia' not in header:
            m = re.search(r'N\S*mero da Guia:\s*(\S+)', line)
            if m:
                header['numero_guia'] = m.group(1).strip()
        if 'data_emissao' not in header:
            m = re.search(r'Data Emiss\S*o:\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})', line)
            if m:
                header['data_emissao'] = br_datetime_to_iso(m.group(1))


def parse_pdf(pdf_path):
    header = {}
    contratos = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = normalize(page.extract_text() or '')
            lines = [l for l in text.split('\n') if l.strip()]
            parse_header(lines, header)
            for line in lines:
                if not DATA_LINE_RE.match(line):
                    continue
                c = parse_data_line(line)
                if c:
                    contratos.append(c)

    # Competência = do primeiro contrato (Comp. Apuração)
    competencia = comp_to_date(contratos[0]['comp']) if contratos else None

    return {
        'competencia': competencia,
        'empregador_cnpj': header.get('empregador_cnpj'),
        'empregador_nome': header.get('empregador_nome'),
        'numero_guia': header.get('numero_guia'),
        'vencimento': header.get('vencimento'),
        'data_emissao': header.get('data_emissao'),
        'qtd_trabalhadores': header.get('qtd_trabalhadores'),
        'total_consignado': header.get('total_consignado'),
        'origem': header.get('origem'),
        'contratos': contratos,
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: parse_consignado_pdf.py <pdf_path> [--pretty]', file=sys.stderr)
        sys.exit(1)
    pdf_path = sys.argv[1]
    pretty = '--pretty' in sys.argv
    result = parse_pdf(pdf_path)
    print(json.dumps(result, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
