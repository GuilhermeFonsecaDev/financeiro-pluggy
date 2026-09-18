"""Previa da fatura: de que o total de cada mes e feito.

Esta tela DECOMPOE, nunca RECALCULA. O total ja existe e e
`pluggy_extrato.cartoes_payload(ano, "fatura")["valores"][cartao][mes-1]`,
depois de passar pela fatura aberta, pelas parcelas projetadas, pelos
recorrentes, pela confirmacao manual, pelo pagamento e pela fatura oficial.

Somar `recorrentes.projetados()` aqui de novo seria o erro obvio e caro: ele
perderia de uma vez as duas protecoes contra dupla contagem que existem hoje
-- `recorrencias_gestao._fixas_cobrem` (conta fixa que ja cobre a cobranca) e
`fixas._casar_projecao` (parcela projetada que a conta fixa ja mostra) -- e
tambem a soberania da fatura fechada. Por isso o unico numero que este modulo
produz e a REPARTICAO de um total que ele nao calculou.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from typing import Any

import banco as fin
import pluggy_extrato as px
import recorrentes as rec

# Meses cujo valor veio de uma fonte completa. Nao se decompoem: o total nao e
# mais a soma dos lancamentos que conhecemos.
ORIGENS_DEFINITIVAS = {"oficial", "pagamento", "confirmada", "fechada"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS previsao_fatura_historico (
  conta_id         TEXT NOT NULL,
  competencia      TEXT NOT NULL,   -- AAAA-MM da fatura prevista
  previsto         REAL NOT NULL,
  aberta           REAL NOT NULL DEFAULT 0,
  parcelas         REAL NOT NULL DEFAULT 0,
  recorrencias     REAL NOT NULL DEFAULT 0,
  reservas         REAL NOT NULL DEFAULT 0,
  dias_para_fechar INTEGER,         -- a quantos dias do fechamento se previu
  registrado_em    TEXT NOT NULL,
  PRIMARY KEY (conta_id, competencia)
);
"""

# Meses de acurácia mostrados de uma vez.
JANELA_ACURACIA = 6

ROTULOS = {
    "aberta": "Já lançado",
    "parcelas": "Parcelas",
    "recorrencias": "Recorrências",
    "reservaRestante": "Reserva não usada",
    "abatimentos": "Crédito anterior",
}

# O que esta tela prevê: gasto que se repete todo mês e NÃO é parcela.
# Parcela e valor já lançado são fato conhecido -- eles compõem o total da
# fatura (e aparecem na tela de Cartões), mas não são o assunto daqui.
PREVISTOS = ("recorrencias", "reservaRestante")
CONHECIDOS = ("aberta", "parcelas", "abatimentos")

# Gasto novo típico: quantos ciclos fechados são necessários, e quantos entram
# na mediana. Abaixo do mínimo a tela não arrisca um número.
CICLOS_MINIMOS_RITMO = 3
CICLOS_RITMO = 6


def _competencias(inicio: date, meses: int) -> list[str]:
    saida = []
    ano, mes = inicio.year, inicio.month
    for _ in range(max(1, meses)):
        saida.append(f"{ano}-{mes:02d}")
        mes += 1
        if mes > 12:
            ano, mes = ano + 1, 1
    return saida


def _fechamentos(conn, contas: set[str], competencia: str) -> str:
    """Data de fechamento do ciclo daquela competencia, a mais proxima.

    Cartao consolidado por tag tem varias contas e podem fechar em dias
    diferentes; a previa mostra o primeiro fechamento, que e o que muda o
    numero primeiro.
    """
    if not contas:
        return ""
    marcadores = ", ".join("?" for _ in contas)
    linha = conn.execute(
        f"SELECT MIN(fim) AS fim FROM pluggy_ciclos "
        f"WHERE conta_id IN ({marcadores}) AND competencia = ?",
        (*contas, competencia),
    ).fetchone()
    return str(linha["fim"] or "")[:10] if linha else ""


def _gasto_novo_tipico(conn, contas: set[str], dias: int) -> float | None:
    """Quanto costuma entrar de compra NOVA nos últimos `dias` de ciclo.

    É o buraco que a previsão não cobre, e é a razão de a feature existir: a
    fatura fecha acima do previsto porque compras avulsas continuam entrando
    até o fechamento. O número sai só dos ciclos que o banco já fechou --
    nada de ajustar a régua para bater com um total desejado.

    Ficam de fora as parcelas (já estão previstas, uma a uma) e as cobranças
    dos cadastros ativos (já estão previstas como recorrência ou reserva).
    """
    if not contas or dias is None or dias < 0:
        return None
    marcadores = ", ".join("?" for _ in contas)
    lojistas: set[tuple[str, str]] = set()
    if _tem_tabela(conn, "recorrentes_previsoes"):
        for linha in conn.execute(
                "SELECT dados FROM recorrentes_previsoes WHERE ativo=1"):
            cadastro = json.loads(linha["dados"])
            lojistas.add((cadastro.get("contaId"), cadastro.get("lojista")))

    ciclos_fechados = list(conn.execute(
        f"SELECT c.conta_id, c.fatura_id, c.fim FROM pluggy_ciclos c "
        f"JOIN pluggy_faturas f ON f.conta_id = c.conta_id AND f.fatura_id = c.fatura_id "
        f"WHERE c.conta_id IN ({marcadores}) AND c.fatura_id <> '' "
        f"ORDER BY c.fim DESC LIMIT ?",
        (*contas, CICLOS_RITMO * len(contas))))
    if len(ciclos_fechados) < CICLOS_MINIMOS_RITMO:
        return None

    por_ciclo: dict[str, float] = {}
    for ciclo in ciclos_fechados:
        try:
            corte = (date.fromisoformat(str(ciclo["fim"])[:10])
                     - timedelta(days=dias)).isoformat()
        except ValueError:
            continue
        total = 0.0
        for linha in conn.execute(
            "SELECT descricao, valor FROM pluggy_transacoes "
            "WHERE conta_id=? AND fatura_id=? AND tipo='DEBIT' "
            "AND COALESCE(parcela_total,1) <= 1 AND SUBSTR(data,1,10) > ?",
            (ciclo["conta_id"], ciclo["fatura_id"], corte),
        ):
            if (ciclo["conta_id"], rec._chave(linha["descricao"] or "")) in lojistas:
                continue
            total += abs(float(linha["valor"] or 0))
        por_ciclo[str(ciclo["fim"])[:10]] = por_ciclo.get(str(ciclo["fim"])[:10], 0.0) + total

    valores = sorted(por_ciclo.values(), reverse=True)[:CICLOS_RITMO]
    if len(valores) < CICLOS_MINIMOS_RITMO:
        return None
    # Mediana, não média: um mês de viagem não pode definir o mês normal.
    return round(statistics.median(valores), 2)


def _tem_tabela(conn, nome: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (nome,)).fetchone())


def _balde_do_item(item) -> str:
    """Em que camada da previsão este item entra.

    Existe porque a lista de itens e a lista de camadas falam do mesmo
    dinheiro: as parcelas listadas SÃO o total da camada "Parcelas". Mostrar
    as duas coisas lado a lado, no mesmo nível, faz parecer que são valores
    diferentes que se somam.
    """
    if item.get("parcelaTotal"):
        return "parcelas"
    if item.get("tipoPrevisao") == "habito":
        return "reservaRestante"
    return "recorrencias"


def _relevantes(meses_do_cartao, quantos):
    """As próximas faturas que ainda são previsão, e só elas.

    Sai de fora:

    - fatura que o banco já fechou -- ela é fato, e o lugar dela é a tela de
      Cartões. Numa tela de previsão ela só ocupava uma coluna dizendo "fatura
      fechada";
    - ciclo que o banco já deixou para trás. Cartão recém-conectado, sem
      nenhuma fatura fechada, recebia previsão numa competência anterior à
      primeira que tem lançamento real -- um mês que nunca vai ser cobrado.
    """
    abertos = [m for m in meses_do_cartao if not m["definitivo"]]
    if not abertos:
        return []
    # O ciclo corrente é o PRIMEIRO aberto com lançamento -- não o último: uma
    # compra com data futura jogaria o cartão meses para frente.
    com_movimento = [m["competencia"] for m in abertos if m["lancado"]]
    inicio = min(com_movimento) if com_movimento else abertos[0]["competencia"]
    return [m for m in abertos if m["competencia"] >= inicio][:quantos]


def _mes_do_cartao(payload, cartao, indice, contas, conn, competencia):
    origem = payload["origens"].get(cartao["id"], ["vazio"] * 12)[indice]
    total = round(float(payload["valores"].get(cartao["id"], [0] * 12)[indice]), 2)
    definitivo = origem in ORIGENS_DEFINITIVAS
    baldes = payload.get("componentes", {}).get(cartao["id"], [{}] * 12)[indice]
    baldes = {chave: round(float(baldes.get(chave, 0) or 0), 2)
              for chave in px.COMPONENTES_VAZIOS}

    itens = sorted(
        (item for item in payload["itens"].get(cartao["id"], [])
         if item.get("mes") == indice + 1),
        key=lambda item: -abs(float(item.get("valor") or 0)),
    )
    fechamento = _fechamentos(conn, contas, competencia)
    dias = None
    if fechamento:
        try:
            dias = (date.fromisoformat(fechamento) - date.today()).days
        except ValueError:
            dias = None

    previstos = [
        {
            "descricao": item.get("descricao") or "",
            "valor": round(abs(float(item.get("valor") or 0)), 2),
            "tipo": "reserva" if item.get("tipoPrevisao") == "habito" else "recorrencia",
            "valorBase": item.get("valorBase"),
            "valorLancado": item.get("valorLancado"),
            "chave": str(item.get("compraId") or "").removeprefix("recorrente:"),
        }
        # Parcela fica de fora: ela já é certa e já está na tela de Cartões.
        for item in itens if _balde_do_item(item) in PREVISTOS
    ]
    gasto_novo = None if definitivo else _gasto_novo_tipico(conn, contas, dias)
    return {
        "competencia": competencia,
        "total": total,
        "origem": origem,
        "definitivo": definitivo,
        "componentes": baldes,
        # O que ESTA tela prevê, e os itens que formam esse valor.
        "previsto": round(sum(baldes[c] for c in PREVISTOS), 2),
        "previstos": [] if definitivo else previstos,
        # Fato já conhecido, só para a linha de contexto: não é o assunto.
        "conhecido": round(sum(baldes[c] for c in CONHECIDOS), 2),
        "lancado": baldes["aberta"],
        "parcelas": baldes["parcelas"],
        "somaComponentes": round(sum(baldes[c] for c in px.COMPONENTES_SOMADOS), 2),
        "gastoNovo": gasto_novo,
        "fechamento": fechamento,
        "diasParaFechar": dias,
        "quantidadePrevista": payload["quantidadesPrevistas"].get(
            cartao["id"], [0] * 12)[indice],
    }


def _registrar(conn, cartoes) -> None:
    """Congela a previsão de cada ciclo na primeira vez que ela é olhada.

    Sem isto não dá para medir acurácia com honestidade: `projetados` só olha
    do mês corrente para frente, então reconstruir depois "o que teríamos
    previsto" usaria cobranças que só se souberam mais tarde -- o número
    sairia bonito e sem valor.

    Por isso: grava uma vez e nunca sobrescreve (ON CONFLICT DO NOTHING), e
    guarda a quantos dias do fechamento a previsão foi feita, que é o que
    torna o acerto comparável entre um mês e outro.
    """
    conn.executescript(SCHEMA)
    agora = date.today().isoformat()
    for cartao in cartoes:
        for mes in cartao["meses"]:
            if mes["definitivo"] or not mes["total"]:
                continue
            componentes = mes["componentes"]
            conn.execute(
                "INSERT INTO previsao_fatura_historico (conta_id,competencia,previsto,"
                "aberta,parcelas,recorrencias,reservas,dias_para_fechar,registrado_em) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (cartao["id"], mes["competencia"], mes["total"],
                 componentes["aberta"], componentes["parcelas"],
                 componentes["recorrencias"], componentes["reservaRestante"],
                 mes["diasParaFechar"], agora))


def _acuracia(conn, contas_do_cartao) -> dict[str, Any]:
    """Quanto a previsão congelada errou, nas faturas que já fecharam.

    A referência é `pluggy_faturas.valor_total`, a mesma autoridade final que
    `_aplicar_faturas_oficiais` usa. Competência com valor_total = 0 fica de
    fora: zero é ambíguo entre "não coletei" e "não gastou nada".
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='previsao_fatura_historico'"
    ).fetchone():
        return {"meses": [], "erroMedio": None, "viesMedio": None}

    oficiais: dict[tuple[str, str], float] = {}
    for linha in conn.execute(
        "SELECT conta_id, competencia, SUM(valor_total) AS total FROM pluggy_faturas "
        "WHERE valor_total <> 0 GROUP BY conta_id, competencia"
    ):
        for cartao_id, contas in contas_do_cartao.items():
            if linha["conta_id"] in contas:
                chave = (cartao_id, linha["competencia"])
                oficiais[chave] = oficiais.get(chave, 0.0) + float(linha["total"] or 0)

    meses = []
    for linha in conn.execute(
        "SELECT * FROM previsao_fatura_historico ORDER BY competencia DESC"
    ):
        real = oficiais.get((linha["conta_id"], linha["competencia"]))
        if real is None or not real:
            continue
        previsto = float(linha["previsto"])
        meses.append({
            "contaId": linha["conta_id"], "competencia": linha["competencia"],
            "previsto": round(previsto, 2), "real": round(real, 2),
            "diferenca": round(previsto - real, 2),
            "erroPercentual": round((previsto - real) / real * 100, 1),
            "diasParaFechar": linha["dias_para_fechar"],
        })
        if len(meses) >= JANELA_ACURACIA:
            break

    if not meses:
        return {"meses": [], "erroMedio": None, "viesMedio": None}
    return {
        "meses": meses,
        # Erro médio: o tamanho do engano, sem sinal.
        "erroMedio": round(sum(abs(m["erroPercentual"]) for m in meses) / len(meses), 1),
        # Viés: para que lado a previsão erra. Negativo = prevê menos do que vem.
        "viesMedio": round(sum(m["erroPercentual"] for m in meses) / len(meses), 1),
    }


@fin.escopo_leitura
def previsao_payload(meses: int = 2) -> dict[str, Any]:
    """Previa da fatura dos proximos `meses`, cartao a cartao."""
    fin.ensure_database()
    meses = max(1, min(12, int(meses or 2)))
    # A janela é maior do que o pedido porque cada cartão descarta as
    # competências que já não são previsão (ver `_relevantes`) e ainda precisa
    # sobrar `meses` para mostrar.
    competencias = _competencias(date.today().replace(day=1), meses + 3)

    payloads: dict[int, dict[str, Any]] = {}
    for competencia in competencias:
        ano = int(competencia[:4])
        if ano not in payloads:
            payloads[ano] = px.cartoes_payload(ano, "fatura")

    primeiro = payloads[int(competencias[0][:4])]
    with fin.connect() as conn:
        contas_do_cartao = {
            cartao["id"]: set(cartao.get("contas") or [cartao["id"]])
            for cartao in primeiro["cartoes"]
        }
        cartoes = []
        for cartao in primeiro["cartoes"]:
            contas = contas_do_cartao[cartao["id"]]
            meses_do_cartao = []
            for competencia in competencias:
                payload = payloads[int(competencia[:4])]
                indice = int(competencia[5:7]) - 1
                meses_do_cartao.append(_mes_do_cartao(
                    payload, cartao, indice, contas, conn, competencia))
            meses_do_cartao = _relevantes(meses_do_cartao, meses)
            if not meses_do_cartao:
                continue
            cartoes.append({
                "id": cartao["id"],
                "nome": cartao["nome"],
                "cor": cartao["cor"],
                "numero": cartao.get("numero") or "",
                "tag": cartao.get("tag") or "",
                "contas": sorted(contas),
                "meses": meses_do_cartao,
            })

    # Cada cartão pode estar num ponto diferente do próprio ciclo, então os
    # totais são por competência presente, não por posição na lista.
    por_competencia: dict[str, list[dict[str, Any]]] = {}
    for cartao in cartoes:
        for mes in cartao["meses"]:
            por_competencia.setdefault(mes["competencia"], []).append(mes)
    totais = [
        {
            "competencia": competencia,
            "total": round(sum(m["total"] for m in lista), 2),
            "previsto": round(sum(m["previsto"] for m in lista), 2),
            "cartoes": len(lista),
        }
        for competencia, lista in sorted(por_competencia.items())
    ]

    visiveis = [c for c in cartoes if any(
        m["total"] or m["previsto"] or m["gastoNovo"] for m in c["meses"])]
    with fin.connect() as conn:
        _registrar(conn, visiveis)
        acuracia = _acuracia(conn, contas_do_cartao)

    return {
        "competencias": competencias,
        "cartoes": visiveis,
        "totais": totais,
        "acuracia": acuracia,
        "rotulos": ROTULOS,
    }
