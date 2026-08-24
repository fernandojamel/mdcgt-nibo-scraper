#!/usr/bin/env python3
"""
parse_pex_folhetim.py — extrai os indicadores PEX que vivem no "Folhetim de
Resultados" (PDF do Meu Mania), SÓ das nossas 2 lojas (Tijuca e Metropolitano).

Contexto (ver memória project_pex_metricas):
  O Folhetim NÃO traz o PEX consolidado — traz 3 dos 7 indicadores, espalhados
  em seções diferentes. Deste PDF saem:
    - Dispersão  (seção "PRIME: DISPERSÃO", coluna Acumulado = média trimestral)
    - Farol      (seção "Farol Delivery": nota MENSAL na tabela com "R$";
                  a página "ACUMULADO/PONTUAÇÃO DELIVERY MANIA" traz a média,
                  mas em algumas edições essa tabela não sai como texto — por
                  isso a média trimestral é calculada fora, no sync, a partir
                  da nota mensal dos últimos 3 folhetins)
    - Termômetro (seção "Termômetro do Cliente": indicador por bloco de TC)
  As outras 4 colunas (3 auditorias Gestton + Google/Harmo) vêm por e-mail.

Identificação das lojas no Folhetim:
  Dispersão/Farol:  "P. TIJUCA" / "P. METROP. RJ"
  Termômetro:       "TIJ P. TIJUCA" / "MET P. METROP. RJ"
  ⚠️ NÃO confundir "P. METROP. RJ" (nossa) com "P. METROPOLE" (outra loja).
     A âncora exige "RJ" depois de METROP, então METROPOLE nunca casa.

Mês de referência: vem do cabeçalho da dispersão ("Restaurantes <m1> <m2> <m3>
  Acumulado") — o 3º mês é o mês do folhetim. NÃO usar a capa (que às vezes traz
  o mês de publicação: o folhetim de maio tem capa "junho").

Conversão p/ pontos (regulamento PEX 2026):
  Dispersão Acum%: 0–0,9→25 · 0,91–1,2→20 · 1,21–1,7→15 · <0 ou >1,7→0
  Farol (média):   9–10→100 · 8–8,99→90 · 7–7,99→80 · 6–6,99→50 · <6→0
  Termômetro ind:  0–0,5→125 · 0,51–1,0→100 · 1,01–2→75 · >2→0
"""
import glob
import os
import re
import sys

import pdfplumber

# Como a loja aparece nas seções dispersão/farol (linha começa com "P. ...").
NOME_RE = {
    'Tijuca':        r'P\.\s*TIJUCA',
    'Metropolitano': r'P\.\s*METROP\.?\s*RJ',   # exige RJ -> não pega METROPOLE
}
MESES_ABBR = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}


def _num(s):
    """'0,50' / '-1,79' -> float. Ignora '%' (vem fora do grupo)."""
    return float(s.replace('.', '').replace(',', '.')) if s else None


def _ultimo_num(linha):
    """Último número PT-BR da linha (ex.: a coluna 'Acumulado' da dispersão)."""
    nums = re.findall(r'-?\d+,\d+', linha)
    return _num(nums[-1]) if nums else None


# ---------------------- conversões p/ pontos ----------------------
def pts_dispersao(v):
    if v is None:
        return None
    if v < 0 or v > 1.7:
        return 0
    if v <= 0.9:
        return 25
    if v <= 1.2:
        return 20
    return 15


def pts_farol(media):
    if media is None:
        return None
    return 100 if media >= 9 else 90 if media >= 8 else 80 if media >= 7 \
        else 50 if media >= 6 else 0


def pts_termometro(ind):
    if ind is None:
        return None
    return 125 if ind <= 0.5 else 100 if ind <= 1.0 else 75 if ind <= 2.0 else 0


def parse_pdf(pdf_path):
    """Lê UM folhetim e devolve os indicadores das 2 lojas.

    Retorna:
      {
        'competencia': '2026-05-01',          # 1º dia do mês de referência
        'lojas': {
          'Tijuca': {
            'dispersao_pct': 0.50, 'dispersao_pts': 25,
            'termometro_indic': 0.65, 'termometro_pts': 100,
            'farol_mes': 10.0,            # nota mensal (sempre que houver)
            'farol_media_pdf': 9.0,       # média da pág. ACUMULADO (ou None)
          },
          'Metropolitano': {...},
        }
      }
    farol_pts NÃO entra aqui: depende da média trimestral, calculada no sync
    com os últimos 3 meses (ver farol_media_movel / __main__).
    """
    with pdfplumber.open(pdf_path) as pdf:
        pages = [p.extract_text() or '' for p in pdf.pages]
    full = '\n'.join(pages)
    disp_txt = '\n'.join(p for p in pages if 'DISPERS' in p.upper())
    farol_acum_txt = '\n'.join(p for p in pages if 'DELIVERY MANIA' in p.upper())
    term_txt = '\n'.join(
        p for p in pages if 'BLOCO' in p.upper() and 'ACUMULADO' not in p.upper()
    )

    # competência: cabeçalho da dispersão "Restaurantes m1 m2 m3 Acumulado"
    competencia = None
    mh = re.search(r'Restaurantes\s+(\w+)\s+(\w+)\s+(\w+)\s+Acumulado',
                   disp_txt, re.IGNORECASE)
    my = re.search(r'_?(20\d\d)', full)
    if mh and mh.group(3).lower()[:3] in MESES_ABBR:
        mes = MESES_ABBR[mh.group(3).lower()[:3]]
        ano = my.group(1) if my else '2026'
        competencia = f'{ano}-{mes:02d}-01'

    lojas = {}
    for loja, nome_re in NOME_RE.items():
        # Dispersão: linha "P. <loja> ... <Acumulado>" -> último número (%)
        dline = next((l for l in disp_txt.split('\n')
                      if re.search(r'^\s*' + nome_re + r'\b', l, re.IGNORECASE)), '')
        disp = _ultimo_num(dline)

        # Farol nota MENSAL: "P. <loja> <nota> R$" (a tabela mensal tem Ticket R$)
        fm = re.search(nome_re + r'\s+(\d+,\d{2})\s+R\$', full, re.IGNORECASE)
        farol_mes = _num(fm.group(1)) if fm else None

        # Farol média (pág. ACUMULADO): "P. <loja> m1 m2 m3 <média>" -> último.
        # Pode faltar (em algumas edições a tabela não sai como texto).
        fa = next((l for l in farol_acum_txt.split('\n')
                   if re.search(r'^\s*' + nome_re + r'\b', l, re.IGNORECASE)), '')
        farol_media_pdf = _ultimo_num(fa)

        # Termômetro: "<COD> P. <loja> ... <TC> <indicador>" -> 1º par
        # (TC, indicador) depois do nome. A linha pode ter 2 lojas grudadas;
        # o match preguiçoso para no 1º número-com-vírgula = indicador da loja.
        mt = re.search(nome_re + r'\b[\s\S]*?([\d.]+)\s+(\d+,\d{2})',
                       term_txt, re.IGNORECASE)
        term = _num(mt.group(2)) if mt else None

        lojas[loja] = {
            'dispersao_pct': disp,
            'dispersao_pts': pts_dispersao(disp),
            'termometro_indic': term,
            'termometro_pts': pts_termometro(term),
            'farol_mes': farol_mes,
            'farol_media_pdf': farol_media_pdf,
        }

    return {'competencia': competencia, 'lojas': lojas}


def farol_media_movel(notas_mensais):
    """Média móvel trimestral = média das últimas até-3 notas mensais (em ordem
    cronológica). É como o regulamento calcula o Farol. `notas_mensais` é a
    lista das notas mensais já ordenada por competência (None ignorado)."""
    vals = [v for v in notas_mensais[-3:] if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


# ============================================================
# Teste local: roda nos folhetins do Downloads e imprime a tabela das 2 lojas
# (reproduz a validação manual). Uso: parse_pex_folhetim.py [pasta_ou_glob]
# ============================================================
if __name__ == '__main__':
    alvo = sys.argv[1] if len(sys.argv) > 1 else 'C:/Users/ferna/Downloads'
    if os.path.isdir(alvo):
        alvo = os.path.join(alvo, 'Folhetim*.pdf')
    paths = sorted(glob.glob(alvo))
    if not paths:
        print(f'Nenhum folhetim encontrado em {alvo}')
        sys.exit(1)

    # 1) parse de cada PDF
    parsed = [parse_pdf(p) for p in paths]
    parsed = [r for r in parsed if r['competencia']]
    parsed.sort(key=lambda r: r['competencia'])

    # 2) Farol: média móvel das últimas 3 notas mensais por loja
    hist = {'Tijuca': [], 'Metropolitano': []}
    print(f"{'Compet.':10}{'Loja':14}|{'Disp%':>7}{'DISP25':>7} |"
          f"{'FarolMéd':>9}{'FAROL100':>9} |{'TermInd':>8}{'TERM125':>8}")
    print('-' * 82)
    for r in parsed:
        for loja in ('Tijuca', 'Metropolitano'):
            d = r['lojas'][loja]
            hist[loja].append(d['farol_mes'])
            media = d['farol_media_pdf']
            if media is None:                      # fallback robusto
                media = farol_media_movel(hist[loja])
            fp = pts_farol(media)
            f = lambda x: '—' if x is None else f'{x:.2f}'
            g = lambda x: '—' if x is None else str(x)
            print(f"{r['competencia']:10}{loja:14}|"
                  f"{f(d['dispersao_pct']):>7}{g(d['dispersao_pts']):>7} |"
                  f"{f(media):>9}{g(fp):>9} |"
                  f"{f(d['termometro_indic']):>8}{g(d['termometro_pts']):>8}")
