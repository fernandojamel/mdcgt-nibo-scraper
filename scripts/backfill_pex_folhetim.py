#!/usr/bin/env python3
"""
backfill_pex_folhetim.py — parseia os "Folhetim de Resultados" (PDFs do Meu
Mania) salvos localmente e popula `pex_resultados` no Supabase, SÓ das nossas
2 lojas (Tijuca e Metropolitano).

Do Folhetim saem 3 dos 7 indicadores PEX (ver [[project_pex_metricas]]):
  Dispersão (DISP25), Farol Delivery (FAROL100), Termômetro (TERM125).
As outras 4 colunas (3 auditorias Gestton + Google/Harmo) vêm por e-mail e
preenchem a MESMA linha depois — a RPC é um upsert incremental.

⚠️ Farol é média móvel trimestral: precisa do histórico das notas MENSAIS dos
últimos 3 folhetins. Por isso processamos TODOS os PDFs juntos, em ordem de
competência, acumulando as notas mensais por loja antes do upsert.

Idempotente — pode rodar quantas vezes quiser (upsert por (empresa, competência)).
Reusa a infra de conexão do backfill do consignado (carregar_env + rpc).

Variáveis de ambiente (lidas de nibo-scraper/.env se existir, ou do ambiente):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Uso:
  python scripts/backfill_pex_folhetim.py                      # pasta padrão (Downloads)
  python scripts/backfill_pex_folhetim.py "C:/caminho/pasta"   # outra pasta
  python scripts/backfill_pex_folhetim.py Folhetim_mai.pdf ... # arquivos avulsos
"""

import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # carregar_env, rpc
from parse_pex_folhetim import parse_pdf, pts_farol, farol_media_movel

PASTA_PADRAO = r'C:/Users/ferna/Downloads'
LOJAS = ('Tijuca', 'Metropolitano')


def resolver_arquivos(args):
    if not args:
        return sorted(glob.glob(os.path.join(PASTA_PADRAO, 'Folhetim*.pdf')))
    arquivos = []
    for a in args:
        if os.path.isdir(a):
            arquivos.extend(glob.glob(os.path.join(a, 'Folhetim*.pdf')))
        elif a.lower().endswith('.pdf'):
            arquivos.append(a)
    return sorted(set(arquivos))


def main():
    arquivos = resolver_arquivos(sys.argv[1:])
    if not arquivos:
        print('Nenhum folhetim encontrado (Folhetim*.pdf).')
        return

    # 1) parse de todos os PDFs, descarta os sem competência, ordena cronologicamente
    parsed = []
    for f in arquivos:
        try:
            r = parse_pdf(f)
        except Exception as e:
            print(f'  [ERR] {os.path.basename(f)}: {e}')
            continue
        if not r['competencia']:
            print(f'  [SKIP] {os.path.basename(f)}: sem competência detectável')
            continue
        parsed.append(r)
    parsed.sort(key=lambda r: r['competencia'])

    if not parsed:
        print('Nenhum folhetim com competência válida.')
        return

    print(f'{len(parsed)} folhetim(ns) a ingerir (ordem cronológica):')

    # 2) acumula notas MENSAIS por loja p/ a média móvel trimestral do Farol
    hist = {loja: [] for loja in LOJAS}
    for r in parsed:
        comp = r['competencia']
        linha = []
        for loja in LOJAS:
            d = r['lojas'][loja]
            hist[loja].append(d['farol_mes'])

            # Farol: usa a média da pág. ACUMULADO se saiu; senão, média móvel
            # das últimas 3 notas mensais (fallback robusto).
            media = d['farol_media_pdf']
            if media is None:
                media = farol_media_movel(hist[loja])
            farol_pts = pts_farol(media)

            try:
                BF.rpc('upsert_pex_folhetim', {
                    'p_empresa': loja,
                    'p_competencia': comp,
                    'p_dispersao_pct': d['dispersao_pct'],
                    'p_dispersao_pts': d['dispersao_pts'],
                    'p_farol_media': media,
                    'p_farol_pts': farol_pts,
                    'p_termometro_indic': d['termometro_indic'],
                    'p_termometro_pts': d['termometro_pts'],
                })
                fmt = lambda x: '—' if x is None else (f'{x:.2f}' if isinstance(x, float) else str(x))
                linha.append(
                    f'{loja[:3]}: DISP {fmt(d["dispersao_pct"])}/{fmt(d["dispersao_pts"])} '
                    f'FAROL {fmt(media)}/{fmt(farol_pts)} '
                    f'TERM {fmt(d["termometro_indic"])}/{fmt(d["termometro_pts"])}'
                )
            except Exception as e:
                linha.append(f'{loja[:3]}: [ERR] {str(e)[:120]}')
        print(f'  [OK] {comp}\n        ' + '\n        '.join(linha))


if __name__ == '__main__':
    main()
