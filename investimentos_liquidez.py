"""Quando o dinheiro fica disponível: vencimento, carência e escada de prazos.

A Pluggy já entrega `dueDate`, `gracePeriodDate` e `amountWithdrawal` em cada
papel de renda fixa, e o catálogo local de fundos tem o D+N de resgate. Nada
disso aparecia na tela. Este módulo transforma esses campos nas respostas que
se faz na frente de uma carteira: o que vence quando, o que ainda está preso
em carência e quanto cai na conta se eu pedir resgate hoje.

A regra que atravessa o arquivo é a mesma do resto do projeto: **ausência de
dado não é zero**. Fundo sem D+N conhecido não entra em "resgatável hoje" como
se fosse imediato nem como se fosse bloqueado -- ele é contado à parte, em
`liquidezDesconhecida`, para a soma nunca mentir por omissão.

As funções não tocam no banco: recebem as posições já montadas e, quando
precisam do D+N de um fundo, recebem o mapa CNPJ -> dias pronto. Assim dá para
testar a regra sem SQLite e sem rede.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

# Faixas da escada de liquidez, em dias corridos a partir de hoje.
FAIXAS: list[tuple[str, str, int, int | None]] = [
    ("vencido", "Vencido", -36500, -1),
    ("ate_30", "Até 30 dias", 0, 30),
    ("de_31_90", "31 a 90 dias", 31, 90),
    ("de_91_180", "91 a 180 dias", 91, 180),
    ("de_181_365", "181 dias a 1 ano", 181, 365),
    ("de_1_2_anos", "1 a 2 anos", 366, 730),
    ("acima_2_anos", "Acima de 2 anos", 731, None),
]

MESES_DA_JANELA = 24


def _hoje() -> date:
    return date.today()


def _data(valor: Any) -> date | None:
    """Aceita `2028-09-04` e `2028-09-04T03:00:00.000Z` -- a Pluggy manda os dois."""
    texto = str(valor or "")[:10]
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError:
        return None


def _numero(valor: Any) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def data_resgate(dias: Any, base: date | None = None) -> str:
    """Quando o dinheiro cai, pedindo resgate hoje.

    O D+N é contado em dias corridos, mas liquidação não acontece em fim de
    semana: a data rola para segunda. Feriado não conhecemos, então um feriado
    no caminho atrasa um dia a mais do que aparece aqui.
    """
    try:
        numero = int(dias)
    except (TypeError, ValueError):
        return ""
    if numero < 0:
        return ""
    quando = (base or _hoje()) + timedelta(days=numero)
    while quando.weekday() >= 5:
        quando += timedelta(days=1)
    return quando.isoformat()


def dias_ate(valor: Any, base: date | None = None) -> int | None:
    alvo = _data(valor)
    return None if alvo is None else (alvo - (base or _hoje())).days


def faixa(dias: int | None) -> tuple[str, str]:
    """Em que degrau da escada cai um prazo. Sem prazo, degrau nenhum."""
    if dias is None:
        return ("sem_vencimento", "Sem vencimento")
    for chave, rotulo, minimo, maximo in FAIXAS:
        if dias >= minimo and (maximo is None or dias <= maximo):
            return (chave, rotulo)
    return ("sem_vencimento", "Sem vencimento")


def _rotulo(posicao: dict[str, Any], base: date) -> str:
    """Como a liquidez daquela linha se lê, em uma frase curta."""
    carencia = _data(posicao.get("carencia"))
    vencimento = _data(posicao.get("vencimento"))
    if carencia and carencia > base:
        # Carência que termina no próprio vencimento não é carência: é um papel
        # que só paga no fim. Dizer "carência até" aí sugere uma espera a mais.
        if vencimento and carencia == vencimento:
            return "no vencimento"
        return f"carência até {carencia.strftime('%d/%m/%y')}"
    disponivel = _numero(posicao.get("disponivel"))
    dias = posicao.get("diasResgate")
    if dias is not None:
        return "resgate imediato" if int(dias) == 0 else f"D+{int(dias)}"
    if disponivel is not None and disponivel > 0:
        return "resgatável"
    if vencimento:
        return "no vencimento"
    return ""


def enriquecer(posicoes: list[dict[str, Any]],
               dias_por_codigo: dict[str, int] | None = None,
               base: date | None = None) -> list[dict[str, Any]]:
    """Acrescenta prazo, faixa e rótulo de liquidez a cada posição.

    `dias_por_codigo` é o D+N dos fundos, por CNPJ -- quem resolve isso contra
    o catálogo é quem chama, para este módulo continuar sem banco.
    """
    base = base or _hoje()
    dias_por_codigo = dias_por_codigo or {}
    for posicao in posicoes:
        codigo = "".join(c for c in str(posicao.get("codigo") or "") if c.isdigit())
        dias_resgate = dias_por_codigo.get(codigo)
        if dias_resgate is not None:
            posicao["diasResgate"] = int(dias_resgate)
            posicao["dataResgateEstimada"] = data_resgate(dias_resgate, base)
        dias = dias_ate(posicao.get("vencimento"), base)
        posicao["diasParaVencer"] = dias
        chave, rotulo = faixa(dias)
        posicao["faixaVencimento"] = chave
        posicao["faixaVencimentoRotulo"] = rotulo
        posicao["liquidezRotulo"] = _rotulo(posicao, base)
        posicao["emCarencia"] = bool(
            (_data(posicao.get("carencia")) or base) > base)
    return posicoes


def _resgatavel_hoje(posicao: dict[str, Any], base: date) -> float | None:
    """Quanto desta posição vira dinheiro hoje. `None` quando não dá para saber.

    Distinguir "não dá para resgatar" de "não sei se dá" é o ponto: o primeiro
    é zero, o segundo não pode virar zero na soma.
    """
    if posicao.get("emCarencia"):
        return 0.0
    disponivel = _numero(posicao.get("disponivel"))
    dias = posicao.get("diasResgate")
    if dias is not None:
        return _numero(posicao.get("liquido")) or 0.0 if int(dias) <= 0 else 0.0
    if disponivel is not None:
        return disponivel
    return None


def agregar(posicoes: list[dict[str, Any]], base: date | None = None) -> dict[str, Any]:
    """Os números de liquidez da carteira (ou de uma classe)."""
    base = base or _hoje()
    disponivel_hoje = 0.0
    desconhecida_valor, desconhecida_qtd = 0.0, 0
    carencia_valor = 0.0
    libera_em: dict[str, float] = {}
    escada: dict[str, dict[str, Any]] = {}
    por_mes: dict[str, dict[str, Any]] = {}
    proximos: list[dict[str, Any]] = []
    caixa = {"d0": 0.0, "d1": 0.0, "d5": 0.0, "d30": 0.0, "d90": 0.0}
    vencendo = {"vencendo30": 0.0, "vencendo90": 0.0}

    for posicao in posicoes:
        liquido = _numero(posicao.get("liquido")) or 0.0
        resgatavel = _resgatavel_hoje(posicao, base)
        if resgatavel is None:
            desconhecida_valor += liquido
            desconhecida_qtd += 1
        else:
            disponivel_hoje += resgatavel

        if posicao.get("emCarencia"):
            carencia_valor += liquido
            quando = str(posicao.get("carencia") or "")[:10]
            libera_em[quando] = libera_em.get(quando, 0.0) + liquido

        # Escada do caixa: em quantos dias este dinheiro estaria na conta.
        dias_resgate = posicao.get("diasResgate")
        dias_venc = posicao.get("diasParaVencer")
        if resgatavel is not None:
            prazo = int(dias_resgate) if dias_resgate is not None else (
                0 if resgatavel > 0 else dias_venc)
            for chave, limite in (("d0", 0), ("d1", 1), ("d5", 5), ("d30", 30), ("d90", 90)):
                if prazo is not None and prazo <= limite:
                    caixa[chave] += liquido if prazo > 0 else resgatavel

        chave_faixa, rotulo_faixa = faixa(dias_venc)
        degrau = escada.setdefault(chave_faixa, {
            "faixa": chave_faixa, "rotulo": rotulo_faixa, "valor": 0.0, "quantidade": 0})
        degrau["valor"] += liquido
        degrau["quantidade"] += 1

        if dias_venc is not None:
            if dias_venc <= 30:
                vencendo["vencendo30"] += liquido
            if dias_venc <= 90:
                vencendo["vencendo90"] += liquido
            mes = str(posicao.get("vencimento") or "")[:7]
            if mes:
                alvo = por_mes.setdefault(mes, {"mes": mes, "valor": 0.0, "quantidade": 0})
                alvo["valor"] += liquido
                alvo["quantidade"] += 1
            proximos.append({
                "id": posicao.get("id"), "nome": posicao.get("nome"),
                "instituicao": posicao.get("instituicao"), "classe": posicao.get("classe"),
                "vencimento": str(posicao.get("vencimento") or "")[:10],
                "dias": dias_venc, "valor": round(liquido, 2),
                "indexador": posicao.get("indexador"),
            })

    limite_mes = (base.replace(day=1) + timedelta(days=31 * MESES_DA_JANELA)).strftime("%Y-%m")
    dentro = [m for m in por_mes.values() if m["mes"] <= limite_mes]
    fora = [m for m in por_mes.values() if m["mes"] > limite_mes]

    ordem = {chave: i for i, (chave, *_) in enumerate(FAIXAS)}
    return {
        "disponivelHoje": round(disponivel_hoje, 2),
        "emCarencia": {
            "valor": round(carencia_valor, 2),
            "liberaEm": [{"data": d, "valor": round(v, 2)} for d, v in sorted(libera_em.items())],
        },
        # Ausência de dado fica visível em vez de virar zero na soma.
        "liquidezDesconhecida": {"valor": round(desconhecida_valor, 2),
                                 "posicoes": desconhecida_qtd},
        "caixaEm": {k: round(v, 2) for k, v in caixa.items()},
        "escada": sorted(escada.values(),
                         key=lambda d: ordem.get(d["faixa"], len(FAIXAS))),
        "vencimentosPorMes": sorted(dentro, key=lambda m: m["mes"]),
        "alemDaJanela": {"valor": round(sum(m["valor"] for m in fora), 2),
                         "quantidade": sum(m["quantidade"] for m in fora)},
        "proximosVencimentos": sorted(proximos, key=lambda p: p["dias"]),
        "vencendo30": round(vencendo["vencendo30"], 2),
        "vencendo90": round(vencendo["vencendo90"], 2),
    }


def dias_de_resgate_por_cnpj(conn, codigos: list[str]) -> dict[str, int]:
    """D+N dos fundos, do catálogo local. Sem catálogo, dicionário vazio.

    Uma consulta só para todos os CNPJs: `fundos._resolver_cnpj` varre tabela e
    multiplicado por posição mataria o tempo de resposta.
    """
    limpos = sorted({"".join(c for c in str(c or "") if c.isdigit()) for c in codigos} - {""})
    if not limpos:
        return {}
    marcadores = ",".join("?" * len(limpos))
    try:
        linhas = conn.execute(
            f"SELECT cnpj, dias_resgate FROM fundos_btg WHERE cnpj IN ({marcadores})", limpos)
        return {l[0]: int(l[1]) for l in linhas if l[1] is not None}
    except Exception:      # catálogo ainda não baixado: liquidez fica desconhecida
        return {}
