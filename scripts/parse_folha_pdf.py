#!/usr/bin/env python3
"""
Parser do PDF "EXTRATO MENSAL" do Nibo.

Estrutura do PDF:
- Cabeçalho em cada página (Empresa, CNPJ, Cálculo, Competência, etc.)
- Páginas 1 a N-1: blocos por funcionário
  - Linha 1: Empr.: <ID> <NOME>  Situação: <X>  CPF: <X>  Adm: <X>
  - Linha 2: Vínculo: <X>  CC: <X>  Depto: <X>  Horas Mês: <X>
  - Linha 3: Cargo: <COD> <NOME>  C.B.O: <X>  Filial: <X>  Salário: <X>
  - Linhas N: eventos (códigos de provento/desconto)
  - Linha ND: totais (Proventos, Descontos, Líquido)
  - Linha NF: bases (INSS, FGTS, IRRF)
  - Opcional: DEMITIDO EM ... MOTIVO ...
- Última página: Resumo (INSS, FGTS, IRRF, contagem por situação)

Uso:
    python parse_folha_pdf.py <pdf_path>            # imprime JSON parseado
    python parse_folha_pdf.py <pdf_path> --pretty   # JSON formatado

Saída (JSON):
{
  "competencia": "2026-05-01",
  "empresa_codigo": "885",
  "empresa_nome": "RESTAURANTE TIJUCA COMERCIO DE ALIMENTOS",
  "emissao": "27/05/2026 09:33:52",
  "calculo": "Folha Mensal",
  "funcionarios": [{...}, ...],
  "resumo": {...}
}
"""

import sys
import json
import re
from datetime import datetime
import pdfplumber

# Char replacements: PDF cmap está quebrado pra alguns acentos,
# pdfplumber retorna replacement char (�) ou nada. Fazemos best-effort
# de normalização a partir de palavras-chave conhecidas.
NORMALIZE_WORDS = {
    'Situa��o': 'Situacao',
    'V�nculo': 'Vinculo',
    'Sal�rio': 'Salario',
    'Compet�ncia': 'Competencia',
    'C�lculo': 'Calculo',
    'Emiss�o': 'Emissao',
    'P�gina': 'Pagina',
    'Horas M�s': 'Horas Mes',
    'L�quido': 'Liquido',
    'F�rias': 'Ferias',
    'Estagi�rio': 'Estagiario',
    'Estagi�ria': 'Estagiaria',
    'Aprendi�z': 'Aprendiz',
    'Licen�a': 'Licenca',
    'Contribui��o': 'Contribuicao',
    'Demiss�o': 'Demissao',
    'Salar�o': 'Salario',
    'Sal�rio Fam�lia': 'Salario Familia',
    'Trabalh�ando': 'Trabalhando',
    'Aus�ncia': 'Ausencia',
    'Doen�a': 'Doenca',
    'Ger�ncia': 'Gerencia',
    'Manda�to': 'Mandato',
    'Servi�o': 'Servico',
    'F�amilia': 'Familia',
    'Maternidade': 'Maternidade',
    'Mensal': 'Mensal',
    '�': '',  # fallback: remove o caractere restante
}


def normalize(text: str) -> str:
    """Substitui replacement chars por nada (best effort)."""
    if not text:
        return text
    for k, v in NORMALIZE_WORDS.items():
        text = text.replace(k, v)
    return text


def parse_decimal(s):
    """'1.357,58' -> 1357.58 ; '0,00' -> 0.0 ; None ou '' -> None"""
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


def parse_date_br(s):
    """'14/03/2026' -> '2026-03-14' """
    if not s:
        return None
    s = s.strip()
    m = re.match(r'(\d{2})/(\d{2})/(\d{4})', s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f'{y}-{mo}-{d}'


def parse_competencia(s):
    """'05/2026' -> '2026-05-01' """
    if not s:
        return None
    m = re.match(r'(\d{2})/(\d{4})', s.strip())
    if not m:
        return None
    mo, y = m.groups()
    return f'{y}-{mo}-01'


# ============================================================
# Header / footer parsing
# ============================================================

HEADER_LINE1 = re.compile(
    # Tolerante a "NOMESitua??o:" (sem espaço) que acontece com nomes longos
    r'^Empr\.:\s*(?P<empr_id>\d+)\s*(?P<nome>.+?)\s*Situa\S+:\s*(?P<situacao>\S+(?:\s+\S+)*?)\s+CPF:\s*(?P<cpf>[\d\.\-]+)\s+Adm:\s*(?P<adm>[\d/]+)\s*$'
)
HEADER_LINE2 = re.compile(
    r'^V\S+culo:\s*(?P<vinculo>\S+(?:\s+\S+)*?)\s+CC:\s*(?P<cc>\d+)\s+Depto:\s*(?P<depto>\d+)\s+Horas\s+M\S+s:\s*(?P<horas>[\d,\.]+)\s*$'
)
HEADER_LINE3 = re.compile(
    r'^Cargo:\s*(?P<cod>\d+)\s*(?P<cargo>.+?)\s+C\.B\.O\.?:\s*(?P<cbo>\d+)\s+Filial:\s*(?P<filial>\S+)\s+Sal\S+rio:\s*(?P<salario>[\d\.,]+)\s*$'
)

# Footer line ND: ... line
FOOTER_ND = re.compile(
    r'^ND:\s*(?P<nd>\d+)\s+Proventos:\s*(?P<proventos>[\d\.,]+)\s+Descontos:\s*(?P<descontos>[\d\.,]+)\s+Informativa:\s*(?P<inf>[\d\.,]+)\s+Informativa\s+Dedutora:\s*(?P<inf_ded>[\d\.,]+)\s+L\S+quido:\s*(?P<liquido>[\d\.,\-]+)'
)
FOOTER_NF = re.compile(
    r'^NF:\s*(?P<nf>\d+)\s+Base\s+INSS:\s*(?P<base_inss>[\d\.,\-]+)\s*Excedente\s+INSS:\s*(?P<exc_inss>[\d\.,\-]+)\s+Base\s+FGTS:\s*(?P<base_fgts>[\d\.,\-]+)\s+Valor\s+FGTS:\s*(?P<valor_fgts>[\d\.,\-]+)\s+Base\s+IRRF:\s*(?P<base_irrf>[\d\.,\-]+)'
)
DEMITIDO_RE = re.compile(
    r'^DEMITIDO\s+EM\s+(?P<data>\d{2}/\d{2}/\d{4})\s*-\s*MOTIVO\s+(?P<motivo>.+)$'
)

# Event line: pode ter 1 ou 2 eventos por linha
# Padrão: <CODIGO><DESCRIÇÃO> <REF> <VALOR>P  [<CODIGO> <DESCRIÇÃO> <REF> <VALOR>D]
# REF pode ser: "183:20", "1,00", "26,00", "0,00", "150:00", etc.
EVENT_TOKEN = re.compile(
    r'(?P<codigo>\d+)\s*(?P<descricao>[A-ZÀ-Úa-zà-ú\.\(\) ]+?)\s+(?P<ref>[\d:,\.]+)\s+(?P<valor>[\d\.,]+)(?P<tipo>[PD])'
)


def parse_funcionario_block(lines):
    """Recebe as linhas de um bloco de funcionário (entre dois 'Empr.:').
    Retorna dict com os dados do funcionário, ou None se não conseguiu parsear o header.
    """
    if not lines:
        return None

    # Header lines (linha 1 = identidade, linha 2 = vínculo, linha 3 = cargo)
    h1 = HEADER_LINE1.match(lines[0])
    if not h1:
        return None
    h2 = HEADER_LINE2.match(lines[1]) if len(lines) > 1 else None
    h3 = HEADER_LINE3.match(lines[2]) if len(lines) > 2 else None

    funcionario = {
        'empr_id': int(h1.group('empr_id')),
        'nome': h1.group('nome').strip(),
        'situacao': normalize(h1.group('situacao')).strip(),
        'cpf': h1.group('cpf'),
        'data_admissao': parse_date_br(h1.group('adm')),
    }
    if h2:
        funcionario['vinculo'] = normalize(h2.group('vinculo')).strip()
        funcionario['cc'] = int(h2.group('cc'))
        funcionario['depto'] = int(h2.group('depto'))
        funcionario['horas_mes'] = parse_decimal(h2.group('horas'))
    if h3:
        funcionario['cargo_codigo'] = int(h3.group('cod'))
        funcionario['cargo_nome'] = h3.group('cargo').strip()
        funcionario['cbo'] = h3.group('cbo')
        funcionario['filial'] = h3.group('filial')
        funcionario['salario_base'] = parse_decimal(h3.group('salario'))

    # Eventos e footer
    eventos = []
    for line in lines[3:]:
        # Tenta footer ND
        m_nd = FOOTER_ND.match(line)
        if m_nd:
            funcionario['proventos'] = parse_decimal(m_nd.group('proventos')) or 0
            funcionario['descontos'] = parse_decimal(m_nd.group('descontos')) or 0
            funcionario['informativa'] = parse_decimal(m_nd.group('inf')) or 0
            funcionario['informativa_dedutora'] = parse_decimal(m_nd.group('inf_ded')) or 0
            funcionario['liquido'] = parse_decimal(m_nd.group('liquido')) or 0
            continue
        m_nf = FOOTER_NF.match(line)
        if m_nf:
            funcionario['base_inss'] = parse_decimal(m_nf.group('base_inss'))
            funcionario['excedente_inss'] = parse_decimal(m_nf.group('exc_inss'))
            funcionario['base_fgts'] = parse_decimal(m_nf.group('base_fgts'))
            funcionario['valor_fgts'] = parse_decimal(m_nf.group('valor_fgts'))
            funcionario['base_irrf'] = parse_decimal(m_nf.group('base_irrf'))
            continue
        m_dem = DEMITIDO_RE.match(line)
        if m_dem:
            funcionario['demitido_em'] = parse_date_br(m_dem.group('data'))
            funcionario['motivo_demissao'] = normalize(m_dem.group('motivo')).strip()
            continue
        # Caso contrário, é uma linha de evento — pode ter 1 ou 2 eventos
        for m in EVENT_TOKEN.finditer(line):
            eventos.append({
                'codigo': m.group('codigo'),
                'descricao': normalize(m.group('descricao')).strip(),
                'ref': m.group('ref'),
                'valor': parse_decimal(m.group('valor')),
                'tipo': m.group('tipo'),
            })

    funcionario['eventos'] = eventos
    return funcionario


# ============================================================
# Parser principal
# ============================================================

def parse_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        # Junta texto de TODAS as páginas (exceto resumo) em uma lista de linhas
        all_lines = []
        resumo_text = ''
        empresa_header = {}

        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ''
            text = normalize(text)
            lines = [l for l in text.split('\n') if l.strip()]

            # Detecta empresa e competência da primeira página.
            # Tolerante a encoding: usa \S* pros caracteres acentuados.
            if i == 0:
                for line in lines:
                    m = re.match(r'^Empresa:\s*(\d+)\s*-\s*(.+?)\s+P\S+gina:', line)
                    if m:
                        empresa_header['empresa_codigo'] = m.group(1)
                        empresa_header['empresa_nome'] = m.group(2).strip()
                    m = re.match(r'^CNPJ:\s*([\d\.\-/]+)\s+Emiss\S+:\s*([\d/]+)', line)
                    if m:
                        empresa_header['cnpj'] = m.group(1)
                        empresa_header['emissao_data'] = parse_date_br(m.group(2))
                    m = re.match(r'^C\S+lculo:\s*(.+?)\s+Horas:\s*([\d:]+)', line)
                    if m:
                        empresa_header['calculo'] = m.group(1).strip()
                        empresa_header['emissao_hora'] = m.group(2)
                    m = re.match(r'^Compet\S+ncia:\s*([\d/]+)', line)
                    if m:
                        empresa_header['competencia'] = parse_competencia(m.group(1))

            # A última página é o Resumo
            if i == n_pages - 1:
                resumo_text = text
                continue

            # Pula linhas de cabeçalho repetidas e a linha "EXTRATO MENSAL"
            for line in lines:
                if (line.startswith('Empresa:') or line.startswith('CNPJ:') or
                    line.startswith('Calculo:') or line.startswith('Competencia:') or
                    line == 'EXTRATO MENSAL' or
                    line.startswith('Sistema licenciado')):
                    continue
                all_lines.append(line)

        # Divide as linhas em blocos por funcionário (começam com 'Empr.:')
        # Linhas antes do primeiro 'Empr.:' são descartadas (sobras de cabeçalho).
        funcionarios = []
        current_block = None  # None = ainda não vimos o primeiro Empr.
        for line in all_lines:
            if line.startswith('Empr.:'):
                if current_block is not None:
                    f = parse_funcionario_block(current_block)
                    if f:
                        funcionarios.append(f)
                current_block = [line]
            elif current_block is not None:
                current_block.append(line)
        if current_block is not None:
            f = parse_funcionario_block(current_block)
            if f:
                funcionarios.append(f)

        resumo = parse_resumo(resumo_text)

        return {
            **empresa_header,
            'funcionarios': funcionarios,
            'resumo': resumo,
        }


def parse_resumo(text):
    """Parseia a última página (Resumo) — extrai os totais consolidados."""
    if not text:
        return {}

    # IMPORTANTE: a página tem 2 colunas. A primeira tem "Empresa:" no header
    # E "Empresa:" na seção INSS (= INSS patronal). Pra desambiguar, recortamos
    # o trecho que começa em "INSS" (cabeçalho da seção INSS) até o fim.
    inss_section_start = text.find('Salário contribui')
    if inss_section_start < 0:
        # fallback se encoding diferente
        m = re.search(r'Salario contribui', text)
        if m:
            inss_section_start = m.start()
    inss_section = text[inss_section_start:] if inss_section_start >= 0 else text

    def grab(pattern, group=1, source=inss_section):
        m = re.search(pattern, source)
        return parse_decimal(m.group(group)) if m else None

    def grab_int(pattern, group=1, source=text):
        m = re.search(pattern, source)
        return int(m.group(group)) if m else None

    return {
        # INSS (todos buscados na seção INSS — evita falso match com "Empresa:" do header)
        'salario_contribuicao_empregados': grab(r'Sal\S+rio contribui\S+ empregados:\s*([\d\.,]+)'),
        'salario_contribuicao_contribuintes': grab(r'Sal\S+rio contribui\S+ contribuintes:\s*([\d\.,]+)'),
        'excedente': grab(r'Excedente:\s*([\d\.,]+)'),
        'base_total_inss': grab(r'Base total:\s*([\d\.,]+)'),
        'inss_segurados': grab(r'Segurados:\s*([\d\.,]+)'),
        'inss_empresa': grab(r'Empresa:\s*([\d\.,]+)'),
        'rat': grab(r'RAT:\s*([\d\.,]+)'),
        'inss_contribuintes': grab(r'Contribuintes:\s*([\d\.,]+)'),
        'inss_terceiros': grab(r'Terceiros:\s*([\d\.,]+)'),
        'total_inss': grab(r'Total INSS:\s*([\d\.,]+)'),
        'base_inss_receita_bruta': grab(r'Base INSS Receita Bruta:\s*([\d\.,]+)'),
        'deducao_salario_familia': grab(r'\(\s*-\s*\)\s*Sal\S+rio Fam\S+lia:\s*([\d\.,]+)'),
        'deducao_salario_maternidade': grab(r'\(\s*-\s*\)\s*Sal\S+rio Maternidade:\s*([\d\.,]+)'),

        # FGTS
        'base_fgts': grab(r'Base do FGTS:\s*([\d\.,]+)'),
        'valor_fgts': grab(r'Valor do FGTS:\s*([\d\.,]+)'),
        'base_fgts_aprendiz': grab(r'Base do FGTS Aprendiz:\s*([\d\.,]+)'),
        'valor_fgts_aprendiz': grab(r'Valor do FGTS Aprendiz:\s*([\d\.,]+)'),
        'base_fgts_rescisorio': grab(r'Base FGTS Rescis\S+rio:\s*([\d\.,]+)'),
        'valor_fgts_rescisorio': grab(r'Valor FGTS Rescis\S+rio:\s*([\d\.,]+)'),

        # IRRF (pega a primeira coluna — "conforme competência do cálculo")
        'base_irrf_mensal': grab(r'Base IRRF Mensal:\s*([\d\.,]+)'),
        'valor_irrf_mensal': grab(r'Valor IRRF Mensal:\s*([\d\.,]+)'),
        'base_irrf_ferias': grab(r'Base IRRF F\S+rias:\s*([\d\.,]+)'),
        'valor_irrf_ferias': grab(r'Valor IRRF F\S+rias:\s*([\d\.,]+)'),
        'valor_total_irrf': grab(r'Valor Total do IRRF:\s*([\d\.,]+)'),

        # Situações (contagem)
        'num_empregados': grab_int(r'No\.\s*Empregados:\s*(\d+)'),
        'num_estagiarios': grab_int(r'No\.\s*Estagi\S+rios:\s*(\d+)'),
        'num_aprendizes': grab_int(r'Aprendiz(?:es)?:\s*(\d+)'),
        'num_contribuintes': grab_int(r'No\.\s*Contribuintes:\s*(\d+)'),
        'num_trabalhando': grab_int(r'Trabalhando:\s*(\d+)'),
        'num_demitidos': grab_int(r'Demitido:\s*(\d+)'),
        'num_ferias': grab_int(r'F\S+rias:\s*(\d+)'),
        'num_salario_maternidade': grab_int(r'Salario maternidade:\s*(\d+)'),
        'num_admissoes': grab_int(r'Admiss\S+es:\s*(\d+)'),
    }


def main():
    if len(sys.argv) < 2:
        print('Uso: parse_folha_pdf.py <pdf_path> [--pretty]', file=sys.stderr)
        sys.exit(1)
    pdf_path = sys.argv[1]
    pretty = '--pretty' in sys.argv

    result = parse_pdf(pdf_path)
    print(json.dumps(result, indent=2 if pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()
