#!/usr/bin/env python3
"""
sync_pex_folhetim.py — recorrência mensal do Folhetim PEX (Meu Mania).

Recebe um diretório com os folhetins baixados pelo scraper (Folhetim_*.pdf),
parseia TODOS e faz upsert SÓ do mês mais recente — os demais entram apenas pra
calcular a média móvel trimestral do Farol (se gravássemos um mês sem ter os 3
folhetins anteriores no lote, a média móvel dele sairia errada). Idempotente.

A trava manual (migration 0038) protege indicadores editados à mão: o
upsert_pex_folhetim não sobrescreve o que estiver travado.

Uso:
  python scripts/sync_pex_folhetim.py /caminho/do/dir
"""

import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backfill_consignado as BF  # carregar_env, rpc
from parse_pex_folhetim import parse_pdf, pts_farol, farol_media_movel

LOJAS = ('Tijuca', 'Metropolitano')


def main():
    alvo = sys.argv[1] if len(sys.argv) > 1 else '.'
    paths = sorted(glob.glob(os.path.join(alvo, '*.pdf')))
    if not paths:
        print(f'Nenhum PDF em {alvo}')
        return

    parsed = []
    for f in paths:
        try:
            r = parse_pdf(f)
        except Exception as e:
            print(f'  [ERR] {os.path.basename(f)}: {e}')
            continue
        if r['competencia']:
            parsed.append(r)
    parsed.sort(key=lambda r: r['competencia'])
    if not parsed:
        print('Nenhum folhetim com competência válida.')
        return

    # histórico de notas MENSAIS por loja (pra média móvel do Farol)
    hist = {loja: [] for loja in LOJAS}
    for r in parsed:
        for loja in LOJAS:
            hist[loja].append(r['lojas'][loja]['farol_mes'])

    # grava só o mês mais recente
    r = parsed[-1]
    comp = r['competencia']
    print(f'Folhetins lidos: {[p["competencia"] for p in parsed]} — gravando {comp}')
    for loja in LOJAS:
        d = r['lojas'][loja]
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
            fmt = lambda x: '—' if x is None else x
            print(f'  [OK] {comp} {loja}: DISP {fmt(d["dispersao_pts"])} '
                  f'FAROL {fmt(farol_pts)} (média {fmt(media)}) '
                  f'TERM {fmt(d["termometro_pts"])}')
        except Exception as e:
            print(f'  [ERR] {comp} {loja}: {e}')


if __name__ == '__main__':
    main()
