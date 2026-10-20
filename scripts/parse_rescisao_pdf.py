#!/usr/bin/env python3
"""
parse_rescisao_pdf.py — extrai CPF (e nome, best-effort) do PDF "TERMO DE
RESCISÃO" do Nibo (Departamento Pessoal).

Ao contrário dos outros parsers deste repo, NÃO tentamos extrair datas/valor
daqui -- isso já vem confiável nos metadados que a API do Nibo devolve
(accrual, dueDate) junto com o documento, então não vale o risco de um
parser de texto errar uma data. Só o CPF (e o nome, se achar) precisam vir
do PDF, porque é a única identificação do colaborador nesse documento.

Estratégia pro CPF: o TRCT é um formulário padronizado -- sempre tem o
rótulo "CPF" perto do número do empregado. Pegamos o PRIMEIRO CPF que
aparece no texto (11 dígitos, com ou sem pontuação) logo após esse rótulo.
Evita casar com o CNPJ da empresa (14 dígitos, formato diferente).

Se não achar nenhum CPF, retorna cpf=None -- quem chama deve pular o
documento (mesmo padrão de "PDF escaneado, pula com aviso" dos outros
scripts), em vez de gravar uma linha sem forma de cruzar com o colaborador.

Uso:
    python parse_rescisao_pdf.py <pdf_path>
"""

import sys
import json
import re
import pdfplumber

CPF_APOS_ROTULO_RE = re.compile(
    r'CPF\D{0,15}?(\d{3}\.?\d{3}\.?\d{3}-?\d{2})', re.IGNORECASE
)
CPF_QUALQUER_RE = re.compile(r'\b(\d{3}\.\d{3}\.\d{3}-\d{2})\b')

# Rótulos comuns de nome do empregado em TRCT/formulários de DP. Pega o
# texto até o próximo rótulo conhecido ou fim da linha.
NOME_RE = re.compile(
    r'(?:Nome do Empregado|Nome do Trabalhador|Empregado|Funcion[aá]rio|Nome)'
    r'\s*[:.]?\s*([A-ZÀ-Úa-zà-ú][A-ZÀ-Úa-zà-ú\s]{4,60}?)'
    r'(?=\s{2,}|\s*(?:CPF|CTPS|PIS|Admiss[aã]o|Data|Cargo|$))',
    re.IGNORECASE,
)


def extrair_cpf(texto: str) -> str | None:
    m = CPF_APOS_ROTULO_RE.search(texto)
    if m:
        return re.sub(r'\D', '', m.group(1))
    # Fallback: qualquer CPF formatado no documento (menos confiável, mas
    # melhor que nada quando o rótulo não casa com o regex acima).
    m = CPF_QUALQUER_RE.search(texto)
    if m:
        return re.sub(r'\D', '', m.group(1))
    return None


def extrair_nome(texto: str) -> str | None:
    m = NOME_RE.search(texto)
    if not m:
        return None
    nome = ' '.join(m.group(1).split()).strip(' :.-')
    # Nome extraído errado costuma ser curto demais ou só maiúsculas de rótulo
    # (ex.: "DA EMPRESA"). Exige pelo menos duas palavras.
    if len(nome.split()) < 2:
        return None
    return nome


def parse_pdf(pdf_path: str) -> dict:
    texto_completo = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ''
            texto_completo.append(t)
    texto = '\n'.join(texto_completo)

    return {
        'cpf': extrair_cpf(texto),
        'nome': extrair_nome(texto),
    }


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Uso: python parse_rescisao_pdf.py <pdf_path>', file=sys.stderr)
        sys.exit(1)
    resultado = parse_pdf(sys.argv[1])
    print(json.dumps(resultado, ensure_ascii=False, indent=2))
