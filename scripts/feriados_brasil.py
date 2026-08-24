"""
feriados_brasil.py — calendário de feriados nacionais (+ pontos facultativos
bancários) e cálculo de vencimento de tributo federal com antecipação.

Regra dos tributos federais (ex.: PIS/COFINS, Lei 11.933/2009 art. 1º):
o vencimento é sempre num dia fixo do mês (ex.: dia 25); se cair em fim de
semana ou feriado, ANTECIPA pro último dia útil ANTERIOR (nunca posterga).

Uso:
    from feriados_brasil import vencimento_federal
    from datetime import date
    vencimento_federal(date(2026, 6, 1), dia=25)  # -> vencimento de julho/2026
"""

from datetime import date, timedelta

# Feriados nacionais fixos (mês, dia). Consciência Negra (20/nov) é feriado
# nacional desde a Lei 14.759/2023.
_FERIADOS_FIXOS = {
    (1, 1),    # Confraternização Universal
    (4, 21),   # Tiradentes
    (5, 1),    # Dia do Trabalho
    (9, 7),    # Independência
    (10, 12),  # Nossa Senhora Aparecida
    (11, 2),   # Finados
    (11, 15),  # Proclamação da República
    (11, 20),  # Consciência Negra (a partir de 2023)
    (12, 25),  # Natal
}


def _pascoa(ano: int) -> date:
    """Domingo de Páscoa do ano (algoritmo de Gauss/Meeus)."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


def _feriados_moveis(ano: int) -> set[date]:
    """Carnaval (seg+ter), Sexta-feira Santa e Corpus Christi — não são
    feriados nacionais por lei, mas são ponto facultativo/sem expediente
    bancário em todo o país (calendário do Banco Central), o que já basta
    pra adiantar o vencimento de um tributo federal."""
    pascoa = _pascoa(ano)
    return {
        pascoa - timedelta(days=48),  # Carnaval (segunda)
        pascoa - timedelta(days=47),  # Carnaval (terça)
        pascoa - timedelta(days=2),   # Sexta-feira Santa
        pascoa + timedelta(days=60),  # Corpus Christi
    }


def eh_dia_util(d: date) -> bool:
    """False se for sábado/domingo ou feriado (nacional ou ponto facultativo
    bancário nacional — Carnaval/Sexta Santa/Corpus Christi)."""
    if d.weekday() >= 5:  # 5=sábado, 6=domingo
        return False
    if (d.month, d.day) in _FERIADOS_FIXOS:
        return False
    if d in _feriados_moveis(d.year):
        return False
    return True


def dia_util_anterior_ou_igual(d: date) -> date:
    """Se `d` já é dia útil, devolve `d`; senão, anda pra trás até achar um."""
    while not eh_dia_util(d):
        d -= timedelta(days=1)
    return d


def vencimento_federal(competencia: date, dia: int) -> date:
    """Vencimento de um tributo federal com regra 'dia fixo do mês seguinte
    à competência, antecipado pro dia útil anterior se cair em fim de semana
    ou feriado'. `competencia` é o 1º dia do mês de apuração (ex.: 2026-06-01
    pra apuração de junho/2026 -> vencimento em julho)."""
    ano, mes = competencia.year, competencia.month
    if mes == 12:
        ano, mes = ano + 1, 1
    else:
        mes += 1
    bruto = date(ano, mes, dia)
    return dia_util_anterior_ou_igual(bruto)


def ultimo_dia_util_do_mes(ano: int, mes: int) -> date:
    """Último dia útil de um mês (anda pra trás a partir do último dia
    corrido até achar um dia útil)."""
    if mes == 12:
        prox = date(ano + 1, 1, 1)
    else:
        prox = date(ano, mes + 1, 1)
    ultimo = prox - timedelta(days=1)
    while not eh_dia_util(ultimo):
        ultimo -= timedelta(days=1)
    return ultimo


def vencimento_trimestral_federal(competencia: date) -> date:
    """Vencimento em quota única de um tributo federal apurado por
    trimestre (ex.: IRPJ/CSLL Lucro Presumido): último dia útil do mês
    seguinte ao mês de FECHAMENTO do trimestre. `competencia` é o 1º dia
    do mês de fechamento (ex.: 2026-06-01 pro trimestre abr-jun/2026 ->
    vencimento em julho/2026)."""
    ano, mes = competencia.year, competencia.month
    if mes == 12:
        ano, mes = ano + 1, 1
    else:
        mes += 1
    return ultimo_dia_util_do_mes(ano, mes)
