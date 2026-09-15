"""Quanto a carteira rendeu, descontando aportes e resgates.

Somar saldo final menos aplicado responde "quanto ganhei", não "quanto rendeu".
Um aporte grande no fim do mês infla o ganho sem que nada tenha rendido, e um
resgate faz o contrário. Para comparar com o CDI é preciso separar o efeito do
dinheiro que entrou do efeito do dinheiro que trabalhou.

Duas medidas, porque respondem perguntas diferentes:

* **TWR** (`twr`) neutraliza aportes e resgates -- é a rentabilidade do
  investimento, e a única comparável ao CDI, que não recebe aporte. Não precisa
  saber quanto custou a posição, o que importa aqui: parte da carteira não tem
  custo informado e mesmo assim tem TWR.
* **XIRR** (`xirr`) responde quanto o *seu dinheiro* rendeu, levando em conta
  quando cada real entrou. Cobre período longo com poucos pontos -- serve para
  posição antiga e para papel já resgatado, onde só há fluxos e valor final.

A armadilha específica desta carteira: a maioria dos SELL é rolagem de CDB no
vencimento, com recompra no mesmo dia ou no dia seguinte. Tratada como resgate,
ela quebra o TWR -- daí `casar_rolagens`.

Nada aqui devolve número inventado: sem série suficiente, a resposta é `None`
com um motivo legível, no mesmo espírito do resto do módulo de investimentos.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

# Abaixo disso, anualizar é fabricar precisão: 20 dias de CDB viram qualquer
# coisa quando elevados a 365/20.
DIAS_PARA_ANUALIZAR = 30
# Buraco na coleta maior que isto, com fluxo dentro, marca o número como
# aproximado -- não sabemos onde o dinheiro entrou dentro da lacuna.
DIAS_DE_BURACO = 5
TOLERANCIA_ROLAGEM = 0.01
# Queda desta ordem num único trecho, sem nenhum movimento que a explique, não
# é rendimento: é resgate que a Pluggy não entregou. O próprio módulo de
# investimentos já documenta que o endpoint "pode omitir resgates recentes ou
# desmembrar um único resgate em vários lotes". Contado como retorno, um CDB a
# 100% do CDI aparecia perdendo 75% num dia.
QUEDA_INEXPLICADA = -0.20


def _iso(valor: Any) -> str:
    return str(valor or "")[:10]


def _dia(valor: Any) -> date | None:
    try:
        return datetime.strptime(_iso(valor), "%Y-%m-%d").date()
    except ValueError:
        return None


def _resposta(valor: float | None, metodo: str, janela: dict[str, str] | None = None,
              **extra: Any) -> dict[str, Any]:
    """Forma única de toda métrica: valor, método, janela e por que confiar."""
    base = {"valor": valor, "metodo": metodo, "janela": janela or {"de": "", "ate": ""},
            "aproximado": False, "confiavel": valor is not None, "motivo": ""}
    base.update(extra)
    return base


# ------------------------------------------------------------------- série

def pontos_diarios(serie: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Um ponto por dia -- o último do dia.

    A coleta roda várias vezes ao dia. Comparar saldo de manhã com saldo de
    tarde não é rentabilidade, é ruído de horário.
    """
    por_dia: dict[str, tuple[str, float]] = {}
    for carimbo, valor in serie:
        dia = _iso(carimbo)
        if not dia:
            continue
        anterior = por_dia.get(dia)
        if anterior is None or str(carimbo) >= anterior[0]:
            por_dia[dia] = (str(carimbo), float(valor or 0))
    return [(dia, valor) for dia, (_, valor) in sorted(por_dia.items())]


# ------------------------------------------------------------------ fluxos

def casar_rolagens(movimentos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Marca resgate seguido de reaplicação como rolagem, não como saída.

    CDB que vence e é recomprado aparece como SELL + BUY de mesmo valor, na
    mesma instituição, no mesmo dia ou no dia seguinte. É o mesmo dinheiro
    continuando aplicado: contado como resgate e novo aporte, ele distorce a
    rentabilidade sem que nada tenha acontecido.
    """
    compras = [m for m in movimentos if m.get("tipo") == "BUY"]
    usados: set[int] = set()
    for venda in movimentos:
        if venda.get("tipo") != "SELL":
            continue
        dia_venda = _dia(venda.get("data"))
        if dia_venda is None:
            continue
        for indice, compra in enumerate(compras):
            if indice in usados:
                continue
            dia_compra = _dia(compra.get("data"))
            if dia_compra is None or not 0 <= (dia_compra - dia_venda).days <= 1:
                continue
            if compra.get("instituicao") != venda.get("instituicao"):
                continue
            if abs(float(compra.get("valor") or 0) - float(venda.get("valor") or 0)) > TOLERANCIA_ROLAGEM:
                continue
            usados.add(indice)
            venda["rolagem"] = True
            compra["rolagem"] = True
            break
    return movimentos


def fluxos_por_dia(movimentos: list[dict[str, Any]], ignorar_rolagem: bool = True) -> dict[str, float]:
    """Entrada líquida de dinheiro em cada dia (aporte positivo, resgate negativo)."""
    fluxos: dict[str, float] = {}
    for mov in movimentos:
        if ignorar_rolagem and mov.get("rolagem"):
            continue
        dia = _iso(mov.get("data"))
        if not dia:
            continue
        valor = abs(float(mov.get("valor") or 0))
        fluxos[dia] = fluxos.get(dia, 0.0) + (valor if mov.get("tipo") == "BUY" else -valor)
    return fluxos


# --------------------------------------------------------------------- TWR

def twr(pontos: list[tuple[str, float]], fluxos: dict[str, float] | None = None) -> dict[str, Any]:
    """Rentabilidade que desconta aportes e resgates, em cadeia de subperíodos.

    Convenção: o fluxo do dia entra no começo do dia, antes de render. É a
    escolha conservadora -- assume que o dinheiro trabalhou o dia inteiro, o
    que reduz a rentabilidade aparente em vez de inflá-la.
    """
    fluxos = fluxos or {}
    pontos = [(d, v) for d, v in pontos if d]
    if len(pontos) < 2:
        return _resposta(None, "twr", confiavel=False, motivo="serie_curta",
                         janela={"de": pontos[0][0] if pontos else "", "ate": pontos[-1][0] if pontos else ""})

    acumulado = 1.0
    subperiodos = pulados = 0
    aproximado = False
    for (dia_anterior, valor_anterior), (dia, valor) in zip(pontos, pontos[1:]):
        fluxo = sum(v for d, v in fluxos.items() if dia_anterior < d <= dia)
        base = valor_anterior + fluxo
        if base <= 0:
            # Carteira zerada no meio do caminho: o subperíodo não tem retorno
            # definido. Pular é melhor que dividir por quase zero e explodir.
            pulados += 1
            aproximado = True
            continue
        d1, d2 = _dia(dia_anterior), _dia(dia)
        if d1 and d2 and (d2 - d1).days > DIAS_DE_BURACO and fluxo:
            # Sem coleta no intervalo, não sabemos em que dia o dinheiro entrou.
            aproximado = True
        acumulado *= valor / base
        subperiodos += 1

    if not subperiodos:
        return _resposta(None, "twr", {"de": pontos[0][0], "ate": pontos[-1][0]},
                         aproximado=True, subperiodos=0, subperiodosPulados=pulados,
                         confiavel=False, motivo="sem_subperiodo_valido")

    dias = ((_dia(pontos[-1][0]) or date.today()) - (_dia(pontos[0][0]) or date.today())).days
    return _resposta(
        round((acumulado - 1) * 100, 4), "twr",
        {"de": pontos[0][0], "ate": pontos[-1][0]},
        aproximado=aproximado, subperiodos=subperiodos, subperiodosPulados=pulados,
        dias=dias, anualizado=anualizar((acumulado - 1) * 100, dias),
        confiavel=not aproximado,
        motivo="periodo_com_lacuna" if aproximado else "")


def anualizar(retorno: float | None, dias: int) -> float | None:
    """Retorno do período em taxa ao ano. Período curto demais não anualiza."""
    if retorno is None or dias < DIAS_PARA_ANUALIZAR or dias <= 0:
        return None
    return round(((1 + retorno / 100) ** (365 / dias) - 1) * 100, 4)


# -------------------------------------------------------------------- XIRR

def _valor_presente(fluxos: list[tuple[date, float]], taxa: float) -> float:
    base = fluxos[0][0]
    return sum(v / (1 + taxa) ** ((d - base).days / 365) for d, v in fluxos)


def xirr(fluxos: list[tuple[Any, float]], palpite: float = 0.1) -> float | None:
    """Taxa anual que zera o valor presente dos fluxos.

    Aporte é negativo (saiu do bolso) e resgate/valor atual é positivo. Sem os
    dois sinais não existe taxa: devolve `None` em vez de um número qualquer.
    Newton primeiro; se escapar, bisseção -- que converge devagar mas sempre.
    """
    limpos = [(d, float(v)) for d, v in ((_dia(data), valor) for data, valor in fluxos)
              if d is not None and v]
    if len(limpos) < 2:
        return None
    limpos.sort(key=lambda f: f[0])
    if not (any(v < 0 for _, v in limpos) and any(v > 0 for _, v in limpos)):
        return None

    taxa = palpite
    for _ in range(80):
        try:
            valor = _valor_presente(limpos, taxa)
            derivada = (_valor_presente(limpos, taxa + 1e-6) - valor) / 1e-6
            if not derivada:
                break
            proxima = taxa - valor / derivada
        except (OverflowError, ZeroDivisionError, ValueError):
            break
        if proxima <= -0.9999:
            break
        if abs(proxima - taxa) < 1e-9:
            return round(proxima * 100, 4)
        taxa = proxima

    baixo, alto = -0.9999, 10.0
    try:
        if _valor_presente(limpos, baixo) * _valor_presente(limpos, alto) > 0:
            return None
        for _ in range(200):
            meio = (baixo + alto) / 2
            if _valor_presente(limpos, baixo) * _valor_presente(limpos, meio) <= 0:
                alto = meio
            else:
                baixo = meio
        return round((baixo + alto) / 2 * 100, 4)
    except (OverflowError, ZeroDivisionError, ValueError):
        return None


def rentabilidade_xirr(movimentos: list[dict[str, Any]], saldo_atual: float,
                       data_saldo: Any = None) -> dict[str, Any]:
    """XIRR de uma posição ou carteira: fluxos do histórico mais o saldo de hoje."""
    fluxos: list[tuple[Any, float]] = []
    for mov in movimentos:
        valor = abs(float(mov.get("valor") or 0))
        if not valor:
            continue
        fluxos.append((mov.get("data"), -valor if mov.get("tipo") == "BUY" else valor))
    if not fluxos:
        return _resposta(None, "xirr", confiavel=False, motivo="sem_movimentos")
    if saldo_atual:
        fluxos.append((data_saldo or date.today().isoformat(), float(saldo_atual)))

    taxa = xirr(fluxos)
    datas = sorted(_iso(d) for d, _ in fluxos)
    if taxa is None:
        motivo = ("historico_de_compras_incompleto"
                  if not any(v < 0 for _, v in fluxos) else "nao_convergiu")
        return _resposta(None, "xirr", {"de": datas[0], "ate": datas[-1]},
                         confiavel=False, motivo=motivo)
    return _resposta(taxa, "xirr", {"de": datas[0], "ate": datas[-1]}, anualizado=taxa)


# ---------------------------------------------------------------- carteira

def twr_encadeado(series: dict[str, list[tuple[str, float]]],
                  fluxos: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """TWR da carteira: retorno diário ponderado, posição por posição.

    Somar o saldo de todas as posições a cada dia parece o caminho óbvio e é
    uma armadilha. O total medido muda por três motivos, e só um é rendimento:

    * rendimento de verdade;
    * aporte e resgate -- descontados pelos fluxos;
    * **cobertura** -- conexão nova entrando, conexão arquivada saindo, ou uma
      rodada que só atualizou uma instituição. Nada disso é rendimento de
      ninguém, mas mexe no total e o TWR ingênuo lê como lucro. Na carteira
      real isso produziu 38% em 29 dias, sendo que um único trecho após uma
      coleta parcial valia +45%.

    Por isso cada posição rende sozinha: o retorno do dia é a média dos
    retornos individuais, ponderada pelo saldo que cada uma tinha no início.
    Posição não observada naquele dia simplesmente não vota -- não vira zero
    nem vira salto.
    """
    fluxos = fluxos or {}
    normalizadas = {chave: pontos_diarios(serie) for chave, serie in series.items()}
    dias_todos = sorted({dia for pontos in normalizadas.values() for dia, _ in pontos})
    if len(dias_todos) < 2:
        return _resposta(None, "twr",
                         {"de": dias_todos[0] if dias_todos else "",
                          "ate": dias_todos[-1] if dias_todos else ""},
                         confiavel=False, motivo="serie_curta", pontos=len(dias_todos))

    # Retorno de cada posição em cada dia observado, com o peso do saldo inicial.
    votos: dict[str, list[tuple[float, float]]] = {}
    ignorados = inexplicados = 0
    for chave, pontos in normalizadas.items():
        for (dia_anterior, valor_anterior), (dia, valor) in zip(pontos, pontos[1:]):
            fluxo = sum(v for d, v in fluxos.get(chave, {}).items() if dia_anterior < d <= dia)
            base = valor_anterior + fluxo
            if base <= 0:
                # Posição zerada ou resgatada por inteiro: sem base, não há
                # retorno definido para o trecho.
                ignorados += 1
                continue
            retorno = valor / base - 1
            if retorno <= QUEDA_INEXPLICADA and not fluxo:
                # Saída que não veio nos movimentos. Não inventamos o valor do
                # resgate: apenas não deixamos a queda passar por prejuízo.
                inexplicados += 1
                continue
            votos.setdefault(dia, []).append((base, retorno))

    acumulado = 1.0
    subperiodos = 0
    for dia in dias_todos[1:]:
        do_dia = votos.get(dia)
        if not do_dia:
            continue
        peso = sum(p for p, _ in do_dia)
        if peso <= 0:
            continue
        acumulado *= 1 + sum(p * r for p, r in do_dia) / peso
        subperiodos += 1

    if not subperiodos:
        return _resposta(None, "twr", {"de": dias_todos[0], "ate": dias_todos[-1]},
                         aproximado=True, subperiodos=0, subperiodosPulados=ignorados,
                         confiavel=False, motivo="sem_subperiodo_valido")

    total = (acumulado - 1) * 100
    corridos = ((_dia(dias_todos[-1]) or date.today())
                - (_dia(dias_todos[0]) or date.today())).days
    return _resposta(
        round(total, 4), "twr", {"de": dias_todos[0], "ate": dias_todos[-1]},
        aproximado=bool(ignorados or inexplicados), subperiodos=subperiodos,
        subperiodosPulados=ignorados, trechosSemMovimento=inexplicados,
        posicoes=len(normalizadas), dias=corridos, pontos=len(dias_todos),
        anualizado=anualizar(total, corridos), confiavel=not inexplicados,
        motivo=("queda_sem_movimento" if inexplicados else
                "trechos_sem_base" if ignorados else ""))


def rentabilidade_carteira(series: dict[str, list[tuple[str, float]]],
                           movimentos_por_posicao: dict[str, list[dict[str, Any]]]
                           ) -> dict[str, Any]:
    """TWR da carteira a partir das séries por posição e dos movimentos."""
    # Rolagem é marcada para a tela poder dizer "isto não foi resgate", mas o
    # fluxo de cada posição conta inteiro: no vencimento o dinheiro sai mesmo
    # de um papel e entra em outro. Ignorar o par aqui faria a posição que
    # venceu cair para zero sem saída registrada -- e virar -100%.
    todos = [m for movs in movimentos_por_posicao.values() for m in movs]
    casar_rolagens(todos)
    fluxos = {chave: fluxos_por_dia(movs, ignorar_rolagem=False)
              for chave, movs in movimentos_por_posicao.items()}
    resultado = twr_encadeado(series, fluxos)
    # A série começa quando a coleta começou, não quando a carteira começou.
    resultado["serieDesdeAplicacao"] = False
    return resultado
