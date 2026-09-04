"""Visão Geral: leituras rápidas de mês, trimestre, semestre ou ano.

Este módulo NÃO consulta o banco direto, de propósito. Ele compõe os payloads
que já passaram por toda a regra sutil do projeto -- competência da fatura,
contas ativas, transações fora dos cálculos, projeção de parcela. Uma consulta
nova aqui seria uma segunda fonte de verdade para o mesmo número, e é assim
que "quanto gastei em setembro" chega a responder R$ 0,00: setembro não tem
transação DATADA em setembro, tem fatura com competência de setembro.

Séries expostas, ambas por competência de fatura (o mês em que a despesa pesa
no bolso, não o dia da compra):

  geral  -> saídas do mês inteiro (cartão + conta), de `extrato_payload`.
  cartao -> total das faturas do mês, de `cartoes_payload`.

O cartão é sempre um subconjunto do geral -- conferido mês a mês em toda a
base antes de a tela existir.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import fixas
import pluggy_extrato as px

# Quantos meses cada período cobre. A conta é de calendário: "trimestre" é o
# trimestre civil que contém o mês escolhido, não os 3 últimos meses -- é o
# que faz "vs período anterior" comparar Q2 com Q3, em vez de janelas tortas.
PERIODOS: dict[str, int] = {"mes": 1, "trimestre": 3, "semestre": 6, "ano": 12}

# O gráfico sempre mostra 12 meses terminando no fim do período, com os meses
# do período destacados. Um período de 1 mês renderia uma barra sozinha; com a
# janela fixa a leitura é a mesma em qualquer período escolhido.
JANELA_GRAFICO = 12

ROTULOS = {"mes": "Mês", "trimestre": "Trimestre",
           "semestre": "Semestre", "ano": "Ano"}
NOMES_MES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
             "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _valido(mes: str) -> bool:
    return bool(mes) and len(mes) == 7 and mes[4] == "-" and mes[:4].isdigit()


def _somar_meses(mes: str, passos: int) -> str:
    ano, numero = int(mes[:4]), int(mes[5:7])
    total = ano * 12 + (numero - 1) + passos
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _meses_entre(de: str, ate: str) -> list[str]:
    meses, atual = [], de
    while atual <= ate and len(meses) < 240:
        meses.append(atual)
        atual = _somar_meses(atual, 1)
    return meses


def _limites(periodo: str, referencia: str) -> tuple[str, str]:
    """Início e fim do período de calendário que contém `referencia`."""
    ano, numero = int(referencia[:4]), int(referencia[5:7])
    passo = PERIODOS[periodo]
    if periodo == "mes":
        primeiro = numero
    else:
        # Ancora no bloco de calendário: trimestre 1..4, semestre 1..2, ano 1.
        primeiro = ((numero - 1) // passo) * passo + 1
    de = f"{ano:04d}-{primeiro:02d}"
    return de, _somar_meses(de, passo - 1)


def _rotulo(periodo: str, de: str, ate: str) -> str:
    ano = de[:4]
    if periodo == "mes":
        return f"{NOMES_MES[int(de[5:7]) - 1].capitalize()} de {ano}"
    if periodo == "ano":
        return ano
    inicio = NOMES_MES[int(de[5:7]) - 1]
    fim = NOMES_MES[int(ate[5:7]) - 1]
    ordem = (int(de[5:7]) - 1) // PERIODOS[periodo] + 1
    nome = "trimestre" if periodo == "trimestre" else "semestre"
    return f"{ordem}º {nome} de {ano} ({inicio}–{fim})"


def _filtros(mes_de: str, mes_ate: str, limite: int = 1) -> dict[str, Any]:
    """Mesmo formato que o backend monta para a tela de Transações."""
    return {
        "mes": "", "mesDe": mes_de, "mesAte": mes_ate,
        "conta": "", "cartao": "", "tipo": "", "categoria": "",
        "status": "", "busca": "", "modo": "fatura",
        "limite": limite, "offset": 0,
    }


def _serie_cartoes(meses: list[str]) -> dict[str, float]:
    """Total de fatura por mês, buscando só os anos que a janela toca."""
    total: dict[str, float] = {}
    for ano in sorted({int(m[:4]) for m in meses}):
        dados = px.cartoes_payload(ano, "fatura")
        for indice, valor in enumerate(dados["totalMes"]):
            total[f"{ano:04d}-{indice + 1:02d}"] = float(valor or 0)
    return total


def _somar(serie: list[dict[str, Any]], chave: str, meses: set[str]) -> float:
    return round(sum(p[chave] for p in serie if p["mes"] in meses), 2)


def _cartoes_do_mes(mes: str) -> list[dict[str, Any]]:
    """Um card por cartão: quanto pesa no mês, limite e vencimento."""
    dados = px.cartoes_payload(int(mes[:4]), "fatura")
    indice = int(mes[5:7]) - 1
    saida = []
    for cartao in dados["cartoes"]:
        valor = float(dados["valores"].get(cartao["id"], [0] * 12)[indice] or 0)
        limite = cartao.get("limiteCredito")
        disponivel = cartao.get("limiteDisponivel")
        saida.append({
            "id": cartao["id"],
            "nome": cartao["nome"],
            "cor": cartao["cor"],
            "grupoId": cartao["grupoId"],
            "tagId": cartao.get("tagId"),
            "tag": cartao.get("tag") or "",
            "membros": cartao["membros"],
            "valor": round(valor, 2),
            "limite": round(limite, 2) if limite is not None else None,
            "disponivel": round(disponivel, 2) if disponivel is not None else None,
            "usoPct": (round((limite - disponivel) / limite * 100, 1)
                       if limite and disponivel is not None else None),
            "vencimento": cartao.get("vencimento") or "",
            "vencimentos": cartao.get("vencimentos") or [],
            "origem": (dados["origens"].get(cartao["id"]) or [""] * 12)[indice],
            "quantidade": (dados["quantidades"].get(cartao["id"]) or [0] * 12)[indice],
        })
    saida.sort(key=lambda c: c["valor"], reverse=True)
    return saida


def _acumulado(mes: str) -> list[dict[str, Any]]:
    """Gasto acumulado dia a dia DENTRO do mês, por data da compra.

    Aqui o modo é "mes", não "fatura": a pergunta é "estou gastando mais rápido
    que no mês passado?", e isso se mede pelo dia em que a compra aconteceu. Em
    competência de fatura as compras de um mês aparecem no mês seguinte, e a
    curva não teria sentido de ritmo.
    """
    filtros = _filtros(mes, mes, limite=3000)
    filtros["modo"] = "mes"
    dados = px.extrato_payload(filtros)
    por_dia: dict[int, float] = {}
    for t in dados["transacoes"]:
        if t["tipo"] != "DEBIT" or not t["incluidaNosCalculos"]:
            continue
        dia = int(t["data"][8:10])
        por_dia[dia] = por_dia.get(dia, 0.0) + abs(t["valorNormalizado"])
    total = 0.0
    curva = []
    for dia in range(1, 32):
        total += por_dia.get(dia, 0.0)
        curva.append({"dia": dia, "total": round(total, 2)})
    return curva


def _detalhe(de: str, ate: str, quantos: int = 15) -> dict[str, Any]:
    """Uma varredura das transações do período serve três leituras de uma vez:
    as maiores saídas, quanto do gasto é parcela e o ticket médio."""
    dados = px.extrato_payload(_filtros(de, ate, limite=4000))
    saidas = [
        {
            "descricao": t["descricao"],
            "valor": round(abs(t["valorNormalizado"]), 2),
            "data": t["data"],
            "categoria": t["categoria"]["nome"],
            "cor": t["categoria"]["cor"],
            "emoji": t["categoria"]["emoji"],
            "conta": t["contaNome"],
            "instrumentoTipo": t.get("instrumentoTipo"),
            "cartaoId": t.get("cartaoId"),
            "grupoId": t.get("grupoId"),
            "cartaoOriginal": t.get("cartaoOriginal"),
            "parcela": (f"{t['parcelaNumero']}/{t['parcelaTotal']}"
                        if t.get("parcelaTotal") else ""),
        }
        for t in dados["transacoes"]
        if t["tipo"] == "DEBIT" and t["incluidaNosCalculos"]
    ]
    # Split cartao x resto pelo EXTRATO. O card "Cartões" mostra a fatura
    # oficial do banco, que nao e a soma das nossas transacoes -- entao
    # "gasto menos fatura oficial" nao corresponde a filtro nenhum e a lista
    # aberta no clique nunca fecharia com o numero do card.
    no_cartao = sum(
        abs(t["valorNormalizado"]) for t in dados["transacoes"]
        if t["tipo"] == "DEBIT" and t["incluidaNosCalculos"]
        and t.get("contaSubtipo") == "CREDIT_CARD"
    )
    fora_cartao = sum(
        abs(t["valorNormalizado"]) for t in dados["transacoes"]
        if t["tipo"] == "DEBIT" and t["incluidaNosCalculos"]
        and t.get("contaSubtipo") != "CREDIT_CARD"
    )
    saidas.sort(key=lambda t: t["valor"], reverse=True)
    parceladas = [t for t in saidas if t["parcela"]]
    total = sum(t["valor"] for t in saidas)
    return {
        "maiores": saidas[:quantos],
        "quantidade": len(saidas),
        "ticket": round(total / len(saidas), 2) if saidas else 0.0,
        "parcelado": round(sum(t["valor"] for t in parceladas), 2),
        "parceladasQtd": len(parceladas),
        "parceladoPct": (round(sum(t["valor"] for t in parceladas) / total * 100, 1)
                         if total else 0.0),
        "noCartao": round(no_cartao, 2),
        "foraCartao": round(fora_cartao, 2),
    }


def payload(periodo: str = "mes", referencia: str = "") -> dict[str, Any]:
    periodo = periodo if periodo in PERIODOS else "mes"
    if not _valido(referencia):
        referencia = datetime.now().strftime("%Y-%m")

    de, ate = _limites(periodo, referencia)
    meses_periodo = _meses_entre(de, ate)
    janela_de = _somar_meses(ate, -(JANELA_GRAFICO - 1))
    meses_janela = _meses_entre(janela_de, ate)

    # Período imediatamente anterior, do mesmo tamanho -- a base do "vs".
    passo = PERIODOS[periodo]
    de_ant, ate_ant = _somar_meses(de, -passo), _somar_meses(de, -1)
    meses_ant = _meses_entre(de_ant, ate_ant)

    # Uma consulta só cobre a janela do gráfico E o período anterior.
    ext = px.extrato_payload(_filtros(min(janela_de, de_ant), ate))
    evolucao = {linha["mes"]: linha for linha in ext["evolucaoMensal"]}
    cartoes = _serie_cartoes(meses_janela + meses_ant)

    def ponto(mes: str) -> dict[str, Any]:
        linha = evolucao.get(mes) or {}
        geral = round(float(linha.get("saidas") or 0), 2)
        cartao = round(cartoes.get(mes, 0.0), 2)
        return {
            "mes": mes,
            "rotulo": NOMES_MES[int(mes[5:7]) - 1][:3],
            "ano": mes[:4],
            "geral": geral,
            # O cartão não pode passar o geral: se passar, aquele mês tem
            # fatura sem as transações correspondentes no extrato, e a barra
            # de dentro estouraria a de fora.
            "cartao": min(cartao, geral) if geral else cartao,
            # `entradas` do extrato ja vem com a projecao de salario do mes
            # corrente (media dos meses anteriores). Somar isso num "resultado"
            # ao lado de gasto REAL infla o saldo: o trimestre fechava com
            # R$ 58,5k de entrada contra os R$ 41,7k que a tela de Transacoes
            # mostra. Aqui vale o recebido; o projetado vai separado.
            "entradas": round(float(linha.get("entradasRecebidas") or 0), 2),
            "entradasProjetadas": round(float(linha.get("entradas") or 0), 2),
            "resultado": round(float(linha.get("resultado") or 0), 2),
            "estimativa": bool(linha.get("saidasEstimativa")),
            "noPeriodo": de <= mes <= ate,
        }

    serie = [ponto(mes) for mes in meses_janela]
    serie_ant = [ponto(mes) for mes in meses_ant]

    alvo = set(meses_periodo)
    gasto = _somar(serie, "geral", alvo)
    cartao = _somar(serie, "cartao", alvo)
    entradas = _somar(serie, "entradas", alvo)
    entradas_proj = _somar(serie, "entradasProjetadas", alvo)
    no_periodo = [p for p in serie if p["mes"] in alvo and p["geral"] > 0]
    # Média, maior e menor mês olham só para mês fechado. Um semestre que
    # ainda tem 3 meses só com parcela projetada renderia uma "média mensal"
    # puxada para baixo por meses que mal comecaram.
    com_dado = [p for p in no_periodo if not p["estimativa"]] or no_periodo
    estimados = [p for p in no_periodo if p["estimativa"]]
    gasto_ant = _somar(serie_ant, "geral", set(meses_ant))
    cartao_ant = _somar(serie_ant, "cartao", set(meses_ant))

    cats = px.categorias_resumo_payload(_filtros(de, ate))
    cats_ant = px.categorias_resumo_payload(_filtros(de_ant, ate_ant))
    ant_por_nome = {c["nome"]: c for c in cats_ant["categorias"]}
    categorias = [
        {
            "nome": c["nome"], "cor": c["cor"], "emoji": c["emoji"],
            "total": round(c["total"], 2),
            "share": round(c["total"] / gasto * 100, 1) if gasto else 0.0,
            "anterior": round(
                float((ant_por_nome.get(c["nome"]) or {}).get("total") or 0), 2),
            "filhos": [
                {"nome": f["nome"], "cor": f["cor"], "emoji": f["emoji"],
                 "total": round(f["total"], 2)}
                for f in c.get("filhos", []) if f["total"] > 0
            ],
        }
        for c in cats["categorias"] if c["total"] > 0
    ]
    # Categoria que existia no período anterior e ZEROU agora precisa aparecer
    # no comparativo -- some da lista de distribuição, mas "Lazer caiu de
    # R$ 800 para nada" é justamente o que se quer enxergar num "vs anterior".
    vistas = {c["nome"] for c in categorias}
    categorias.extend(
        {
            "nome": c["nome"], "cor": c["cor"], "emoji": c["emoji"],
            "total": 0.0, "share": 0.0,
            "anterior": round(c["total"], 2), "filhos": [],
        }
        for c in cats_ant["categorias"]
        if c["total"] > 0 and c["nome"] not in vistas
    )
    categorias.sort(key=lambda c: (c["total"], c["anterior"]), reverse=True)

    # Um mês pode ficar 4x acima dos vizinhos por causa de UM lançamento --
    # uma transferência grande, por exemplo. Em vez de esconder ou aparar a
    # barra, a tela diz de onde veio o pico: a barra alta passa a informar.
    destaque = None
    pico = max((p for p in serie if p["geral"] > 0),
               key=lambda p: p["geral"], default=None)
    if pico and len(serie) > 1:
        media_resto = (sum(p["geral"] for p in serie if p["mes"] != pico["mes"])
                       / max(1, len(serie) - 1))
        if media_resto and pico["geral"] > media_resto * 1.8:
            arvore = px.categorias_resumo_payload(
                _filtros(pico["mes"], pico["mes"]))
            maior = max(arvore["categorias"], key=lambda c: c["total"],
                        default=None)
            if maior and maior["total"] > pico["geral"] * 0.4:
                destaque = {
                    "mes": pico["mes"],
                    "total": pico["geral"],
                    "categoria": maior["nome"],
                    "valor": round(maior["total"], 2),
                    "share": round(maior["total"] / pico["geral"] * 100),
                    "vezesMedia": round(pico["geral"] / media_resto, 1),
                }

    # Blocos que só fazem sentido no último mês do período: fatura aberta,
    # conta fixa a pagar e ritmo do mês são leituras de "onde estou agora",
    # não do acumulado do trimestre.
    mes_foco = ate
    detalhe = _detalhe(de, ate)
    cartoes_mes = _cartoes_do_mes(mes_foco)
    try:
        fx_payload = fixas.mes_payload(mes_foco)
    except Exception:
        fx_payload = {"resumo": {}, "itens": []}
    fx = fx_payload.get("resumo") or {}
    # As que ainda nao foram pagas, do maior valor para o menor. Uma conta de
    # R$ 0,00 em aberto (INSS sem valor no mes) nao e cobranca, e cadastro --
    # entra na contagem mas nao na lista.
    abertas = sorted(
        (
            {
                "nome": item["nome"],
                "valor": round(float(item.get("gastoCalculo") or 0), 2),
                "forma": item.get("formaNome") or item.get("forma") or "",
                "dia": item.get("diaVencimento"),
                "tag": item.get("tag") or "",
                "vinculada": bool(item.get("transacao")),
            }
            for item in fx_payload.get("itens") or []
            if not item.get("pago") and float(item.get("gastoCalculo") or 0) > 0
        ),
        key=lambda i: i["valor"], reverse=True,
    )
    # A curva de ritmo é por DATA DA COMPRA, então o mês de referência dela é o
    # mês corrente do calendário, não a competência da fatura.
    mes_compra = datetime.now().strftime("%Y-%m")
    if mes_foco < mes_compra:
        mes_compra = _somar_meses(mes_foco, -1)
    ritmo = {
        "mes": mes_compra,
        "anterior": _somar_meses(mes_compra, -1),
        "atual": _acumulado(mes_compra),
        "base": _acumulado(_somar_meses(mes_compra, -1)),
        "hoje": int(datetime.now().strftime("%d"))
                if mes_compra == datetime.now().strftime("%Y-%m") else 31,
    }

    return {
        "periodo": {
            "tipo": periodo,
            "rotuloTipo": ROTULOS[periodo],
            "rotulo": _rotulo(periodo, de, ate),
            "mesDe": de, "mesAte": ate,
            "meses": meses_periodo,
            "anteriorRotulo": _rotulo(periodo, de_ant, ate_ant),
            "mesesComDado": len(com_dado),
            "mesesEstimados": len(estimados),
        },
        "serie": serie,
        "resumo": {
            "gasto": gasto,
            "cartao": cartao,
            # Vem do extrato, nao de "gasto - fatura oficial": assim o card
            # bate com a lista que o clique abre (filtro card=nenhum).
            "naoCartao": detalhe["foraCartao"],
            "cartaoExtrato": detalhe["noCartao"],
            "entradas": entradas,
            "resultado": round(entradas - gasto, 2),
            "entradasProjetadas": entradas_proj,
            "gastoEstimado": round(sum(p["geral"] for p in estimados), 2),
            # Média só sobre meses que têm gasto: dividir o trimestre por 3
            # quando dois meses ainda não aconteceram inventa uma média baixa.
            "media": round(gasto / len(com_dado), 2) if com_dado else 0.0,
            "shareCartao": round(cartao / gasto * 100, 1) if gasto else 0.0,
            "gastoAnterior": gasto_ant,
            "cartaoAnterior": cartao_ant,
            "variacao": (round((gasto - gasto_ant) / gasto_ant * 100, 1)
                         if gasto_ant else None),
            "maiorMes": max(com_dado, key=lambda p: p["geral"]) if com_dado else None,
            "menorMes": min(com_dado, key=lambda p: p["geral"]) if com_dado else None,
        },
        "categorias": categorias,
        "destaque": destaque,
        "cartoes": cartoes_mes,
        "mesFoco": mes_foco,
        "fixas": {
            "pago": round(float(fx.get("pago") or 0), 2),
            "pendente": round(float(fx.get("pendente") or 0), 2),
            "quantidade": int(fx.get("quantidade") or 0),
            "conciliadas": int(fx.get("conciliadas") or 0),
            "porTag": fx.get("porTag") or {},
            "abertas": abertas,
        },
        "ritmo": ritmo,
        "detalhe": detalhe,
        "maiores": detalhe["maiores"],
        "anos": sorted({m[:4] for m in evolucao}),
    }
