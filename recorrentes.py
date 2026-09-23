"""Sugestões e previsões mensais independentes de Contas Fixas."""

from __future__ import annotations

import json
import collections
import re
import sqlite3
import statistics
from datetime import datetime
from typing import Any

import extrato_camada as cam
import pluggy_extrato as px

# A evidência é medida por ciclos, não por meses-calendário: 01/07, 31/07
# e 30/08 são três cobranças mensais, apesar de ocuparem apenas dois meses.
MESES_MINIMOS = 3
JANELA_MESES = 12
CV_MAXIMO = 15.0
INTERVALO_MINIMO_DIAS = 24
INTERVALO_MAXIMO_DIAS = 38
RECENCIA_MAXIMA_DIAS = 45
REGULARIDADE_MINIMA = 0.8
# Hábitos são visibilidade de gastos que se repetem, não compromissos que a
# aplicação possa prever com segurança. Ex.: combustível pode ocorrer quatro
# vezes em um mês e nenhuma vez no seguinte; ainda é útil acompanhar.
HABITO_MESES_MINIMOS = 4
HABITO_MESES_RECENTES = 4
HABITO_MESES_RECENTES_MINIMOS = 3
# Reserva de CATEGORIA: vale a pena quando o gasto da categoria se espalha por
# vários estabelecimentos. Um posto só não precisa de categoria -- o cadastro
# do posto resolve; quatro postos diferentes, sim.
CATEGORIA_LOJISTAS_MINIMOS = 3

# Categorias que nunca são "hábito de consumo", por mais regulares que sejam.
# Movimento entre contas, imposto, investimento e encargo não se orça assim.
CATEGORIAS_FORA_DE_HABITO = {
    "transferencia_propria", "transferencias", "fatura", "rendimentos",
    "tarifas", "emprestimos", "investimentos", "impostos",
    # "Outros" é o saco do que não foi classificado: um punhado de gastos sem
    # relação entre si. Reservar a categoria inteira não quer dizer nada, e
    # ela engoliria todo hábito individual cuja categoria ainda é desconhecida.
    "outros", "sem_categoria",
}

REGRAS_SUGESTAO = [
    "Pelo menos 3 cobranças da mesma conta ou cartão.",
    "Intervalos de 24 a 38 dias em pelo menos 80% do histórico e nos 2 últimos ciclos.",
    "Última cobrança nos últimos 45 dias; lançamentos futuros não contam.",
    "Valor estável ou reajuste sequencial; planos de valores diferentes são separados.",
    "Parcelas, duplicidades ambíguas e gastos frequentes não viram previsões; hábitos aparecem em uma aba própria.",
    "Recorrências já cadastradas, recusadas ou cobertas por conta fixa não são sugeridas.",
]

SCHEMA = """
-- Sugestao recusada nao volta a aparecer. Sem isto o painel vira ruido: a
-- mesma coisa que a pessoa ja decidiu que nao e conta fixa reaparece todo
-- mes, e ela para de olhar o painel.
CREATE TABLE IF NOT EXISTS recorrentes_previsoes (
  chave TEXT PRIMARY KEY,
  dados TEXT NOT NULL,
  ativo INTEGER NOT NULL DEFAULT 1,
  atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recorrentes_ignorados (
  chave TEXT PRIMARY KEY,
  descricao TEXT NOT NULL DEFAULT '',
  ignorado_em TEXT NOT NULL
);
"""

def _abrir() -> sqlite3.Connection:
    conn = cam.conectar()
    conn.row_factory = sqlite3.Row
    px.garantir_tabelas(conn)
    conn.create_function("norm", 1, cam.normalizar, deterministic=True)
    cam.garantir_camada(conn)
    px.garantir_extrato_materializado(conn)
    import fixas
    conn.executescript(fixas.SCHEMA)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _chave(descricao: str) -> str:
    """Normalização conservadora; aliases conhecidos não fundem outros lojistas."""
    texto = cam.normalizar(descricao)
    aliases = {
        "netflix": r"netflix(?:\.com)?(?: entretenimento)?(?: (?:sao paulo|barueri)(?: bra)?)?",
        "livelo": r"(?:clube )?livelo(?: clube liv)?(?: (?:santana de pa|sao paulo)(?: bra)?)?",
    }
    for marca, padrao in aliases.items():
        if re.fullmatch(padrao, texto):
            return marca
    texto = re.sub(r"\d+\s*/\s*\d+", " ", texto)
    # Números curtos podem identificar o estabelecimento ou plano (Loja 373,
    # Microsoft 365); somente identificadores longos são removidos.
    texto = re.sub(r"\b\d{6,}\b", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _termo_sugerido(chave: str) -> str:
    """Trecho para o vinculo CONTEM da conta fixa.

    Usa as primeiras palavras com 3+ letras: o comeco da descricao e a parte
    estavel (nome do lojista), o fim carrega cidade/pais que variam. Termo
    curto demais casaria com transacao alheia, longo demais para de casar na
    primeira variacao do lojista.
    """
    palavras = [p for p in chave.split() if len(p) >= 3][:3]
    return " ".join(palavras).upper()


def _mes_atual() -> str:
    return datetime.now().strftime("%Y-%m")


def _janela(meses: int) -> tuple[str, str]:
    """(primeiro, ultimo) mes da janela, terminando no mes corrente."""
    atual = _mes_atual()
    serial = int(atual[:4]) * 12 + int(atual[5:7]) - 1
    inicio = serial - (meses - 1)
    return f"{inicio // 12:04d}-{inicio % 12 + 1:02d}", atual


def _analisar_series(itens: list) -> tuple[list[dict], str]:
    """Retorna somente sequências com evidência mensal suficiente.

    Primeiro avalia a sequência inteira, preservando reajustes. Só separa
    preços quando cobranças simultâneas impedem uma sequência única. Nenhum
    lançamento é removido do extrato nem aceito automaticamente.
    """
    linhas = sorted(itens, key=lambda i: (i["data"], i["transacao_id"]))
    if len(linhas) < MESES_MINIMOS:
        return [], "historicoCurto"
    meses = collections.Counter(str(i["data"])[:7] for i in linhas)
    if len(linhas) / len(meses) > 3:
        return [], "frequente"

    def avaliar(banda: list) -> dict | None:
        banda = sorted(banda, key=lambda i: (i["data"], i["transacao_id"]))
        if len(banda) < MESES_MINIMOS:
            return None
        datas = [datetime.fromisoformat(str(i["data"])[:10]) for i in banda]
        intervalos = [(b - a).days for a, b in zip(datas, datas[1:])]
        mensais = [INTERVALO_MINIMO_DIAS <= d <= INTERVALO_MAXIMO_DIAS for d in intervalos]
        regularidade = sum(mensais) / len(mensais)
        # Uma falha antiga de sincronização é tolerada em histórico extenso.
        # Cobranças próximas, bimestrais, semanais ou lacunas recentes não são.
        if (regularidade < REGULARIDADE_MINIMA or not all(mensais[-2:])
                or any(d < INTERVALO_MINIMO_DIAS or d > 68 for d in intervalos)
                or not 27 <= statistics.median(intervalos) <= 34):
            return None
        valores = [abs(float(i["valor"])) for i in banda]
        if min(valores) <= 0:
            return None
        media = statistics.mean(valores)
        cv = statistics.pstdev(valores) / media * 100
        # Um reajuste é uma mudança de patamar depois de cobranças estáveis,
        # sem alternar entre preços de dois planos ou flutuar indefinidamente.
        patamares: list[list[float]] = []
        for valor in valores:
            if (not patamares or abs(valor - statistics.median(patamares[-1]))
                    > max(.5, statistics.median(patamares[-1]) * .12)):
                patamares.append([valor])
            else:
                patamares[-1].append(valor)
        reajuste = (len(patamares) == 2 and len(patamares[0]) >= 2
                    and max(valores) / min(valores) <= 1.6)
        if cv > CV_MAXIMO and not reajuste:
            return None
        if len(patamares) > 2:
            return None
        return {
            "itens": banda, "avisos": [], "faixaValor": None, "incerta": False,
            "evidencia": {
                "cobrancas": len(banda),
                "ciclosMensais": 1 + sum(mensais),
                "intervaloMinDias": min(intervalos),
                "intervaloMaxDias": max(intervalos),
                "intervaloMedianoDias": round(statistics.median(intervalos), 1),
                "regularidade": round(regularidade, 2),
                "valorReajustado": bool(reajuste),
            },
        }

    completa = avaliar(linhas)
    if completa:
        return [completa], ""
    # Sem sobreposição temporal não há motivo para transformar valores
    # instáveis de compras avulsas em várias supostas assinaturas.
    datas = [datetime.fromisoformat(str(i["data"])[:10]) for i in linhas]
    if not any((b - a).days < INTERVALO_MINIMO_DIAS for a, b in zip(datas, datas[1:])):
        return [], "irregular"
    bandas = []
    for item in sorted(linhas, key=lambda i: abs(float(i["valor"]))):
        valor = abs(float(item["valor"]))
        banda = next((b for b in bandas if abs(valor - statistics.median(
            abs(float(x["valor"])) for x in b)) <= max(0.5, valor * 0.08)), None)
        if banda is None:
            bandas.append([item])
        else:
            banda.append(item)
    resultado = []
    for indice, banda in enumerate(bandas):
        serie = avaliar(banda)
        if not serie:
            continue
        vs = [abs(float(i["valor"])) for i in banda]
        minimo = min(vs) - max(.5, min(vs) * .12)
        maximo = max(vs) + max(.5, max(vs) * .12)
        if indice > 0:
            minimo = max(minimo, (max(abs(float(i["valor"])) for i in bandas[indice - 1]) + min(vs)) / 2 + .005)
        if indice + 1 < len(bandas):
            maximo = min(maximo, (min(abs(float(i["valor"])) for i in bandas[indice + 1]) + max(vs)) / 2 - .005)
        serie["faixaValor"] = {"min": round(max(0, minimo), 2), "max": round(maximo, 2)}
        serie["evidencia"]["planoSeparado"] = True
        resultado.append(serie)
    return resultado, "frequente" if any((b - a).days < 24 for a, b in zip(datas, datas[1:])) else "irregular"


def _habitos_por_categoria(por_categoria, mes_atual, hoje, nomes):
    """Reserva da categoria inteira, quando ela se espalha entre lojistas.

    Mercado e restaurante têm dezenas de estabelecimentos diferentes: uma
    descoberta por lojista produz uma lista inútil e nenhuma previsão que
    preste. A categoria é a unidade certa nesses casos -- é o que permite
    "qualquer posto que eu abastecer conta".

    As regras de presença e recência são as mesmas do hábito por lojista; o
    que se acrescenta é a exigência de vários lojistas, porque com um só o
    cadastro do próprio lojista já resolve, e é mais preciso.
    """
    meses_recentes = {_recuar(mes_atual, passo) for passo in range(1, HABITO_MESES_RECENTES + 1)}
    achados = []
    for (conta_id, categoria_id), itens_brutos in por_categoria.items():
        if categoria_id in CATEGORIAS_FORA_DE_HABITO:
            continue
        itens = sorted(itens_brutos, key=lambda i: (i["data"], i["transacao_id"]))
        lojistas = {_chave(i["descricao"]) for i in itens}
        if len(lojistas) < CATEGORIA_LOJISTAS_MINIMOS:
            continue
        por_mes = collections.defaultdict(list)
        for item in itens:
            por_mes[str(item["data"])[:7]].append(item)
        if (len(por_mes) < HABITO_MESES_MINIMOS
                or len(set(por_mes) & meses_recentes) < HABITO_MESES_RECENTES_MINIMOS):
            continue
        recente = itens[-1]
        dias = (hoje - datetime.fromisoformat(str(recente["data"])[:10]).date()).days
        if dias > RECENCIA_MAXIMA_DIAS:
            continue
        totais = [sum(abs(float(i["valor"] or 0)) for i in grupo)
                  for _, grupo in sorted(por_mes.items())]
        achados.append({
            "chave": f"categoria:{conta_id}|{categoria_id}",
            "descricao": nomes.get(categoria_id, {}).get("nome") or categoria_id,
            "lojista": "",
            "categoriaId": categoria_id,
            "contaId": conta_id,
            "noCartao": recente["conta_subtipo"] == "CREDIT_CARD",
            "mesesAtivos": len(por_mes),
            "quantidadeCompras": len(itens),
            "lojistasDistintos": len(lojistas),
            "mediaMensal": round(statistics.mean(totais), 2),
            "ultimaCobranca": {"data": str(recente["data"])[:10],
                               "valor": abs(float(recente["valor"] or 0))},
            "diasDesdeUltima": dias,
            "historicoMensal": [
                {"mes": mes, "valor": round(sum(abs(float(i["valor"] or 0)) for i in grupo), 2),
                 "compras": len(grupo)}
                for mes, grupo in sorted(por_mes.items(), reverse=True)
            ][:12],
        })
    # Como cada lançamento conta para a categoria e para as mães dela, a mãe
    # CONTÉM a filha -- sugerir as duas somaria o mesmo gasto duas vezes.
    # Fica a MAIS ESPECÍFICA: uma reserva de gasolina é útil, uma reserva de
    # "Automotivo" com estacionamento e manutenção dentro já não é. A mãe só
    # aparece quando nenhuma filha dela se qualificou sozinha.
    presentes = {(a["contaId"], a["categoriaId"]) for a in achados}

    def tem_filha_sugerida(achado):
        familia = {achado["categoriaId"]}
        while True:
            filhas = {i for i, dados in nomes.items()
                      if (dados or {}).get("paiId") in familia} - familia
            if not filhas:
                return False
            if any((achado["contaId"], filha) in presentes for filha in filhas):
                return True
            familia |= filhas

    achados = [a for a in achados if not tem_filha_sugerida(a)]
    return sorted(achados, key=lambda a: (-a["mediaMensal"], a["descricao"].casefold()))


def _habitos_mensais(grupos: dict[str, list], mes_atual: str, hoje) -> list[dict[str, Any]]:
    """Consolida gastos repetidos que merecem acompanhamento, sem previsão.

    A regra é deliberadamente diferente de recorrência: em vez de exigir uma
    cobrança mensal única e estável, pede presença consistente em meses
    recentes. Isto captura abastecimentos e outras despesas de rotina, sem
    somá-las antecipadamente à fatura.
    """
    meses_recentes = {_recuar(mes_atual, passo) for passo in range(1, HABITO_MESES_RECENTES + 1)}
    habitos: list[dict[str, Any]] = []
    for chave_grupo, itens_brutos in grupos.items():
        itens = sorted(itens_brutos, key=lambda i: (i["data"], i["transacao_id"]))
        por_mes: dict[str, list] = collections.defaultdict(list)
        for item in itens:
            por_mes[str(item["data"])[:7]].append(item)
        if len(por_mes) < HABITO_MESES_MINIMOS or len(set(por_mes) & meses_recentes) < HABITO_MESES_RECENTES_MINIMOS:
            continue
        recente = itens[-1]
        dias_desde_ultima = (hoje - datetime.fromisoformat(str(recente["data"])[:10]).date()).days
        if dias_desde_ultima > RECENCIA_MAXIMA_DIAS:
            continue
        # Uma sequência mensal com valor regular pertence às sugestões de
        # previsão. Aqui entram os gastos que a regra conservadora recusou.
        if _analisar_series(itens)[0]:
            continue
        totais = [sum(abs(float(i["valor"] or 0)) for i in grupo)
                  for _, grupo in sorted(por_mes.items())]
        if not any(len(grupo) > 1 for grupo in por_mes.values()) and len(totais) < 6:
            continue
        categoria = collections.Counter(
            i["categoria_id"] for i in itens if i["categoria_id"]
        ).most_common(1)
        historico = [
            {"mes": mes, "valor": round(sum(abs(float(i["valor"] or 0)) for i in grupo), 2),
             "compras": len(grupo)}
            for mes, grupo in sorted(por_mes.items(), reverse=True)
        ]
        habitos.append({
            "chave": chave_grupo,
            "descricao": recente["descricao"],
            "lojista": chave_grupo.split("|", 1)[1],
            "contaId": recente["conta_id"],
            "noCartao": recente["conta_subtipo"] == "CREDIT_CARD",
            "mesesAtivos": len(por_mes),
            "quantidadeCompras": len(itens),
            "mediaMensal": round(statistics.mean(totais), 2),
            "ultimaCobranca": {"data": str(recente["data"])[:10], "valor": abs(float(recente["valor"] or 0))},
            "diasDesdeUltima": dias_desde_ultima,
            "historicoMensal": historico[:12],
            "categoriaId": categoria[0][0] if categoria else None,
        })
    return sorted(habitos, key=lambda h: (-h["mediaMensal"], h["descricao"].casefold()))


def _categorias_disponiveis(categorias: dict) -> list[dict[str, Any]]:
    """Categorias para o seletor, com o caminho "Mãe › Filha".

    Sem o caminho a lista fica cheia de nomes ambíguos ("Serviços" existe solto
    e dentro de outra), e escolher a categoria errada muda silenciosamente o
    que a reserva vai casar.
    """
    def caminho(chave):
        nomes, visto = [], set()
        while chave and chave not in visto:
            visto.add(chave)
            dados = categorias.get(chave) or {}
            nomes.append(dados.get("nome") or chave)
            chave = dados.get("paiId")
        return " › ".join(reversed(nomes))

    return sorted(
        ({"id": chave, "nome": dados.get("nome") or chave, "caminho": caminho(chave),
          "emoji": dados.get("emoji") or ""}
         for chave, dados in categorias.items()
         if chave not in CATEGORIAS_FORA_DE_HABITO),
        key=lambda c: c["caminho"].casefold())


def _descobertas(sugestoes: list[dict], habitos: list[dict],
                 categorias_habito: list[dict] | None = None) -> list[dict]:
    """Uma lista só, com o comportamento que cada achado pede.

    A classificação continua sendo a heurística: o que tem cadência mensal e
    valor estável entra como cobrança única; o que se repete sem cadência
    entra como reserva do mês. A lista única existe porque, para quem olha,
    as duas respondem a mesma pergunta -- "isto vai cair na minha fatura?" --
    e a pessoa pode discordar da classificação na hora de ativar.
    """
    saida = []
    for s in sugestoes:
        saida.append({
            "chave": s["chave"], "descricao": s["descricao"], "lojista": s["lojista"],
            "contaId": s["contaId"], "noCartao": s.get("noCartao"),
            "categoria": s.get("categoria"),
            "comportamentoSugerido": "cobranca",
            "valorSugerido": s["valorUltimo"],
            "unidadeValor": "por mês",
            "confianca": s.get("confianca") or "media",
            "resumoEvidencia": s.get("resumoEvidencia") or "",
            "avisos": s.get("avisos") or [],
            "diaTipico": s.get("diaTipico"),
            "historico": [
                {"rotulo": str(h["data"])[8:10] + "/" + str(h["data"])[5:7],
                 "detalhe": h.get("descricao") or "", "valor": h["valor"]}
                for h in (s.get("historico") or [])
            ],
            "origem": "sugestao",
        })
    for h in habitos:
        meses, compras = h.get("mesesAtivos") or 0, h.get("quantidadeCompras") or 0
        saida.append({
            "chave": h["chave"], "descricao": h["descricao"], "lojista": h["lojista"],
            "contaId": h["contaId"], "noCartao": h.get("noCartao"),
            "categoria": h.get("categoria"),
            "comportamentoSugerido": "reserva",
            "valorSugerido": h["mediaMensal"],
            "unidadeValor": "média mensal",
            # Sem cadência para medir: a confiança vem da presença nos meses.
            "confianca": "alta" if meses >= 6 else "media",
            "resumoEvidencia": f"{meses} meses · {compras} compras",
            "avisos": [],
            "diaTipico": 1,
            "historico": [
                {"rotulo": m["mes"], "detalhe": f"{m['compras']} compras", "valor": m["valor"]}
                for m in (h.get("historicoMensal") or [])
            ],
            "origem": "habito",
        })
    for c in (categorias_habito or []):
        saida.append({
            "chave": c["chave"], "descricao": c["descricao"], "lojista": "",
            "categoriaId": c["categoriaId"],
            "contaId": c["contaId"], "noCartao": c.get("noCartao"),
            "categoria": c.get("categoria"),
            "comportamentoSugerido": "reserva",
            "valorSugerido": c["mediaMensal"],
            "unidadeValor": "média mensal",
            "confianca": "alta" if c["mesesAtivos"] >= 6 else "media",
            "resumoEvidencia": (f"{c['lojistasDistintos']} estabelecimentos · "
                                f"{c['mesesAtivos']} meses · {c['quantidadeCompras']} compras"),
            "avisos": [],
            "diaTipico": 1,
            "historico": [
                {"rotulo": m["mes"], "detalhe": f"{m['compras']} compras", "valor": m["valor"]}
                for m in (c.get("historicoMensal") or [])
            ],
            "origem": "categoria",
        })
    ordem = {"alta": 0, "media": 1, "baixa": 2}
    return sorted(saida, key=lambda d: (ordem.get(d["confianca"], 9), -d["valorSugerido"]))


def sugestoes_payload(janela: int = JANELA_MESES) -> dict[str, Any]:
    """Sugestões elegíveis com evidência resumida, além das recorrências salvas."""
    mes_de, mes_ate = _janela(janela)
    hoje = datetime.now().date()
    with _abrir() as conn:
        ativas = px.contas_ativas(conn)
        marcadores = ", ".join("?" for _ in ativas) or "NULL"
        linhas = conn.execute(
            f"""
            SELECT e.transacao_id, e.descricao, e.valor, e.data, SUBSTR(e.data, 1, 7) AS mes,
                   e.competencia_fatura,
                   e.parcela_total, e.categoria_id, e.conta_id,
                   c.subtipo AS conta_subtipo
            FROM extrato_efetivo_cache e
            JOIN pluggy_contas c ON c.conta_id = e.conta_id
            WHERE e.conta_id IN ({marcadores})
              AND e.tipo = 'DEBIT' AND e.incluida = 1
              AND SUBSTR(e.data, 1, 7) BETWEEN ? AND ?
              AND SUBSTR(e.data, 1, 10) <= ? AND ABS(e.valor) > 0
            """,
            [*sorted(ativas), mes_de, mes_ate, hoje.isoformat()],
        ).fetchall()

        # Contas fixas ativas, para saber o que já está coberto. Precisa do
        # nome E do termo: uma conta pode estar cadastrada (nome igual ao do
        # gasto) com um termo que não pega a transação -- foi o que aconteceu
        # com duas contas cujo termo aponta para o nome do recebedor no
        # extrato, não para o nome da conta. Olhar só o termo fazia elas
        # voltarem como "nova", e
        # aceitar a sugestão criaria conta fixa duplicada.
        import cartoes
        termos_por_fixa: dict[str, list[str]] = collections.defaultdict(list)
        for linha in conn.execute("SELECT fixa_id, termo FROM fixas_termos ORDER BY ordem"):
            termos_por_fixa[linha["fixa_id"]].append(cam.normalizar(linha["termo"]))
        fixas_ativas = [
            {"id": l["id"], "nome": l["nome"],
             "nome_norm": cam.normalizar(l["nome"]),
             "termo": l["termo"], "termo_norm": cam.normalizar(l["termo"]),
             "termos": termos_por_fixa.get(l["id"]) or [cam.normalizar(l["termo"])],
             "contas": (cartoes.resolver_contas(conn, l["conta_id"]) or {l["conta_id"]}) if l["conta_id"] else set(),
             "valor": float(l["valor_previsto"] or 0)}
            for l in conn.execute(
                "SELECT id, nome, termo, valor_previsto, conta_id FROM fixas_contas WHERE ativo = 1 "
                "AND (desde = '' OR desde <= ?) AND (ate = '' OR ate >= ?)",
                (mes_ate, mes_ate),
            )
        ]
        ignorados = {
            l["chave"] if "|" in l["chave"] else _chave(l["chave"])
            for l in conn.execute("SELECT chave FROM recorrentes_ignorados")
        }
        previsoes = [dict(l) for l in conn.execute("SELECT * FROM recorrentes_previsoes")]
        cadastradas = {l["chave"] for l in previsoes}
        categorias = {
            l["id"]: {"id": l["id"], "nome": l["nome"], "cor": l["cor"],
                      "emoji": l["emoji"] or "", "paiId": l["pai_id"]}
            for l in conn.execute("SELECT id, nome, cor, emoji, pai_id FROM extrato_categorias")
        }

    grupos: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    por_categoria: dict[tuple[str, str], list[sqlite3.Row]] = collections.defaultdict(list)
    for linha in linhas:
        descricao = cam.normalizar(linha["descricao"])
        if ((linha["parcela_total"] or 0) > 1 or re.search(
                r"\bparcela(?:s|mento)?\b|\b\d+\s*/\s*(?:[2-9]|[1-9]\d+)\b", descricao)):
            continue
        if linha["categoria_id"] in {"transferencia_propria", "fatura", "rendimentos"}:
            continue
        movimento_financeiro = (linha["categoria_id"] in {"transferencias", "tarifas", "emprestimos"}
                                or re.search(r"\b(?:pix|ted|doc|transferencia|iof|juros|encargos|tarifa|taxa)\b|pagamento de fatura", descricao))
        mensalidade_explicita = re.search(
            r"\b(?:mensalidade|assinatura|clube|aluguel|condominio)\b|(?:pacote|cesta) de servicos", descricao)
        if movimento_financeiro and not mensalidade_explicita:
            continue
        lojista = _chave(linha["descricao"])
        if lojista:
            grupos[linha["conta_id"] + "|" + lojista].append(linha)
        # O lançamento conta para a própria categoria e para todas as mães: um
        # cadastro de "Automotivo" casa também com "Postos de combustível", e o
        # valor sugerido tem de refletir exatamente o que ele vai casar.
        categoria = linha["categoria_id"]
        while categoria:
            por_categoria[(linha["conta_id"], categoria)].append(linha)
            categoria = (categorias.get(categoria) or {}).get("paiId")

    sugestoes: list[dict[str, Any]] = []
    vinculos_quebrados: list[dict[str, Any]] = []
    descartados = collections.Counter()

    def _cobertura(chave: str, itens: list) -> tuple[dict | None, dict | None]:
        """(fixa cujo termo pega, fixa cujo nome bate mas o termo nao pega).

        Nome curto casaria com qualquer coisa ("Mae" dentro de "maetra..."),
        entao substring so vale de 6 letras pra cima; abaixo disso exige
        igualdade.
        """
        elegiveis = [f for f in fixas_ativas if not f["contas"] or itens[-1]["conta_id"] in f["contas"]]
        descricoes = [cam.normalizar(i["descricao"]) for i in itens[-3:]]
        pelo_termo = next((f for f in elegiveis if any(
            termo and termo in descricao for termo in f["termos"] for descricao in descricoes)), None)
        if pelo_termo:
            return pelo_termo, None
        pelo_nome = next(
            (f for f in elegiveis
             if f["nome_norm"] and (
                 f["nome_norm"] == chave
                 or (len(f["nome_norm"]) >= 6 and f["nome_norm"] in chave))),
            None,
        )
        return None, pelo_nome

    candidatos = []
    for chave_grupo, linhas_grupo in grupos.items():
        series, motivo = _analisar_series(linhas_grupo)
        if not series:
            if len({i["mes"] for i in linhas_grupo}) >= 2:
                descartados[motivo] += 1
            continue
        for serie in series:
            chave = chave_grupo
            if serie["faixaValor"]:
                centro = statistics.median(abs(float(i["valor"])) for i in serie["itens"])
                chave += f"|valor:{centro:.2f}"
            candidatos.append((chave, chave_grupo, serie))

    for chave, chave_grupo, serie in candidatos:
        itens = serie["itens"]
        meses = sorted({i["mes"] for i in itens})
        valores = [abs(float(i["valor"] or 0)) for i in itens]
        media = statistics.mean(valores)
        cv = statistics.pstdev(valores) / media * 100 if media else 0
        avisos = list(serie["avisos"])
        idade_dias = (hoje - datetime.fromisoformat(str(itens[-1]["data"])[:10]).date()).days
        if idade_dias > RECENCIA_MAXIMA_DIAS:
            descartados["encerrado"] += 1
            continue
        lojista = chave_grupo.split("|", 1)[1]
        ja_prevista = any(
            salva.get("contaId") == itens[-1]["conta_id"] and _chave(salva.get("lojista") or salva.get("descricao", "")) == lojista
            and (not salva.get("faixaValor") or salva["faixaValor"]["min"] <= statistics.median(valores) <= salva["faixaValor"]["max"])
            for salva in (json.loads(p["dados"]) for p in previsoes)
        )
        if chave in cadastradas or chave_grupo in cadastradas or ja_prevista:
            descartados["jaPrevisto"] += 1
            continue
        faixa_ignorada = False
        prefixo_faixa = chave_grupo + "|faixa:"
        for ignorado in ignorados:
            if not ignorado.startswith(prefixo_faixa):
                continue
            try:
                minimo, maximo = map(float, ignorado[len(prefixo_faixa):].split(":"))
            except (ValueError, TypeError):
                continue
            if minimo <= statistics.median(valores) <= maximo:
                faixa_ignorada = True
                break
        if chave in ignorados or chave_grupo in ignorados or lojista in ignorados or faixa_ignorada:
            descartados["ignorado"] += 1
            continue

        pelo_termo, pelo_nome = _cobertura(lojista, itens)
        if pelo_termo:
            descartados["jaCadastrado"] += 1
            continue

        # Categoria e dia mais frequentes viram o pré-preenchimento; a
        # descrição mais recente é a que o banco está mandando hoje.
        recente = max(itens, key=lambda i: i["data"])
        categoria_id = collections.Counter(
            i["categoria_id"] for i in itens if i["categoria_id"]
        ).most_common(1)
        dias = [int(str(i["data"])[8:10]) for i in itens if len(str(i["data"])) >= 10]
        evidencia = {**serie["evidencia"], "diasDesdeUltima": idade_dias}
        regularidade = evidencia["regularidade"]
        confianca = "alta" if regularidade == 1 else "media"
        sugestao = {
            "chave": chave,
            "descricao": recente["descricao"],
            "termoSugerido": _termo_sugerido(lojista),
            "lojista": lojista,
            "contaId": recente["conta_id"],
            "transacaoBaseId": recente["transacao_id"],
            "valorMedio": round(media, 2),
            "valorUltimo": round(abs(float(recente["valor"] or 0)), 2),
            "variacao": round(cv, 1),
            "meses": len(meses),
            "mesesJanela": janela,
            "primeiroMes": meses[0],
            "ultimoMes": meses[-1],
            "regularidade": round(regularidade, 2),
            "confianca": confianca,
            "evidencia": evidencia,
            "resumoEvidencia": f"{len(itens)} cobranças · a cada {evidencia['intervaloMedianoDias']:g} dias · última em {str(recente['data'])[8:10]}/{str(recente['data'])[5:7]}",
            "avisos": avisos,
            "faixaValor": serie["faixaValor"],
            "historico": [{"data": str(i["data"])[:10], "valor": abs(float(i["valor"])),
                            "descricao": i["descricao"]} for i in reversed(itens[-12:])],
            "diaTipico": round(statistics.median(dias[-6:])) if dias else None,
            "categoria": categorias.get(
                categoria_id[0][0] if categoria_id else None
            ),
            "noCartao": recente["conta_subtipo"] == "CREDIT_CARD",
        }
        # Já cadastrada pelo nome, mas o termo de vínculo não pega o gasto:
        # a conta existe e fica Pendente todo mês, porque nada casa com ela.
        # Aqui a ação certa é corrigir o termo, não cadastrar de novo.
        if pelo_nome:
            vinculos_quebrados.append({
                **sugestao,
                "fixaId": pelo_nome["id"],
                "fixaNome": pelo_nome["nome"],
                "termoAtual": pelo_nome["termo"],
                "valorCadastrado": pelo_nome["valor"],
            })
        else:
            sugestoes.append(sugestao)

    ordem_confianca = {"alta": 0, "media": 1}
    chave_ordem = lambda s: (ordem_confianca[s["confianca"]], -s["valorMedio"])
    sugestoes.sort(key=chave_ordem)
    vinculos_quebrados.sort(key=chave_ordem)
    cadastros_por_pagamento = {
        (str(salva.get("contaId") or ""), _chave(salva.get("lojista") or salva.get("descricao", "")))
        for salva in (json.loads(p["dados"]) for p in previsoes)
    }
    categorias_habito = []
    for achado in _habitos_por_categoria(por_categoria, mes_ate, hoje, categorias):
        if achado["chave"] in ignorados:
            descartados["ignorado"] += 1
            continue
        if any((p.get("contaId") == achado["contaId"]
                and p.get("categoriaId") == achado["categoriaId"])
               for p in (json.loads(l["dados"]) for l in previsoes)):
            descartados["jaPrevisto"] += 1
            continue
        achado["categoria"] = categorias.get(achado["categoriaId"])
        categorias_habito.append(achado)

    # O lojista individual não aparece duas vezes: se a categoria dele já foi
    # sugerida como reserva, ele está coberto por ela.
    cobertos_por_categoria = {(a["contaId"], a["categoriaId"]) for a in categorias_habito}

    def categoria_cobre(conta_id, categoria_id):
        """A categoria do lojista, ou qualquer mãe dela, já virou reserva?"""
        while categoria_id:
            if (conta_id, categoria_id) in cobertos_por_categoria:
                return True
            categoria_id = (categorias.get(categoria_id) or {}).get("paiId")
        return False

    habitos = []
    for habito in _habitos_mensais(grupos, mes_ate, hoje):
        if categoria_cobre(habito["contaId"], habito.get("categoriaId")):
            descartados["cobertoPelaCategoria"] += 1
            continue
        if (habito["contaId"], habito["lojista"]) in cadastros_por_pagamento:
            continue
        # Dispensar tem de valer aqui também. Faltava, e o hábito reaparecia
        # na lista logo depois de ser dispensado.
        if habito["chave"] in ignorados or habito["lojista"] in ignorados:
            descartados["ignorado"] += 1
            continue
        habito["categoria"] = categorias.get(habito.pop("categoriaId"))
        habitos.append(habito)
    return {
        "janela": {"de": mes_de, "ate": mes_ate, "meses": janela},
        "sugestoes": sugestoes,
        "habitos": habitos,
        "categoriasHabito": categorias_habito,
        "descobertas": _descobertas(sugestoes, habitos, categorias_habito),
        "possiveis": [],
        "regrasSugestao": REGRAS_SUGESTAO,
        "regrasHabitos": [
            "Apareceu em pelo menos 4 meses e em 3 dos últimos 4 meses completos.",
            "A última cobrança foi há no máximo 45 dias.",
            "O valor sugerido é a média dos meses com gasto.",
        ],
        "previsoes": _previsoes_payload(previsoes),
        "contasPagamento": _contas_pagamento_payload(),
        "categoriasDisponiveis": _categorias_disponiveis(categorias),
        "vinculosQuebrados": vinculos_quebrados,
        "totalMensal": round(sum(s["valorUltimo"] for s in sugestoes), 2),
        "descartados": dict(descartados),
    }


def corrigir_termo(fixa_id: str, termo: str) -> dict[str, Any]:
    """Troca só o termo de vínculo de uma conta fixa já cadastrada.

    Existe separado de fixas.atualizar porque aquele valida o cadastro
    inteiro -- para mudar um campo o cliente teria que reenviar todos, e um
    campo esquecido apagaria configuração.
    """
    termo = str(termo or "").strip()
    if not termo:
        raise ValueError("Informe o termo de vínculo.")
    with _abrir() as conn:
        cursor = conn.execute(
            "UPDATE fixas_contas SET termo = ? WHERE id = ?", (termo, fixa_id))
        if not cursor.rowcount:
            raise ValueError(f"Conta fixa {fixa_id} não encontrada.")
        # A lista de termos é o que o casamento lê hoje: gravar só a coluna
        # antiga deixaria a correção sem efeito nenhum. "Corrigir" substitui a
        # lista -- o termo estava errado, não faltando.
        conn.execute("DELETE FROM fixas_termos WHERE fixa_id = ?", (fixa_id,))
        conn.execute(
            "INSERT INTO fixas_termos (fixa_id, ordem, termo) VALUES (?, 0, ?)",
            (fixa_id, termo))
        conn.commit()
    return {"ok": True, "fixaId": fixa_id, "termo": termo, **sugestoes_payload()}


def _serial(mes_ref: str) -> int:
    return int(mes_ref[:4]) * 12 + int(mes_ref[5:7])


def _recuar(mes_ref: str, meses: int) -> str:
    serial = _serial(mes_ref) - 1 - meses
    return f"{serial // 12:04d}-{serial % 12 + 1:02d}"


def ignorar(chave: str, descricao: str = "") -> dict[str, Any]:
    """Marca uma sugestão como recusada para ela não voltar."""
    chave = str(chave or "").strip()
    if not chave:
        raise ValueError("Informe a chave da sugestão.")
    with _abrir() as conn:
        conn.execute(
            "INSERT INTO recorrentes_ignorados (chave, descricao, ignorado_em) "
            "VALUES (?, ?, ?) ON CONFLICT(chave) DO UPDATE SET "
            "ignorado_em = excluded.ignorado_em",
            (chave, str(descricao or ""), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    return {"ok": True, "chave": chave, **sugestoes_payload()}


def reconsiderar(chave: str) -> dict[str, Any]:
    """Desfaz um "ignorar" -- a sugestão volta a aparecer."""
    with _abrir() as conn:
        conn.execute("DELETE FROM recorrentes_ignorados WHERE chave = ?", (chave,))
        conn.commit()
    return {"ok": True, "chave": chave, **sugestoes_payload()}


def ignorados_payload() -> dict[str, Any]:
    with _abrir() as conn:
        return {
            "ignorados": [
                {"chave": l["chave"], "descricao": l["descricao"],
                 "ignoradoEm": l["ignorado_em"]}
                for l in conn.execute(
                    "SELECT * FROM recorrentes_ignorados ORDER BY ignorado_em DESC"
                )
            ]
        }


def salvar_previsao(chave: str, ativa: bool = True) -> dict[str, Any]:
    from recorrencias_gestao import salvar
    return salvar(chave, ativa)


def projetar_habito(chave: str) -> dict[str, Any]:
    from recorrencias_gestao import projetar_habito as projetar
    return projetar(chave)


def ativar_previsao(chave: str, comportamento: str = "") -> dict[str, Any]:
    from recorrencias_gestao import ativar
    return ativar(chave, comportamento)


def testar_previsao(dados: dict) -> dict[str, Any]:
    from recorrencias_gestao import testar
    return testar(dados)


def converter_previsao(chave: str, comportamento: str) -> dict[str, Any]:
    from recorrencias_gestao import converter
    return converter(chave, comportamento)


def previa_conversao(chave: str, comportamento: str) -> dict[str, Any]:
    from recorrencias_gestao import previa_conversao as previa
    return previa(chave, comportamento)


def criar_previsao(dados: dict) -> dict[str, Any]:
    from recorrencias_gestao import criar
    return criar(dados)


def editar_previsao(chave: str, dados: dict) -> dict[str, Any]:
    from recorrencias_gestao import editar
    return editar(chave, dados)


def excluir_previsao(chave: str) -> dict[str, Any]:
    from recorrencias_gestao import excluir
    return excluir(chave)


def cobradas(conn: sqlite3.Connection, competencia: str, contas) -> list[dict]:
    from recorrencias_gestao import cobradas as calcular
    return calcular(conn, competencia, contas)


def consumidas(conn: sqlite3.Connection, competencia: str, contas) -> list[dict]:
    from recorrencias_gestao import consumidas as calcular
    return calcular(conn, competencia, contas)


def projetados(conn: sqlite3.Connection, ano: int) -> list[dict]:
    from recorrencias_gestao import projetados as calcular
    return calcular(conn, ano)


def _previsoes_payload(linhas: list) -> list[dict]:
    from recorrencias_gestao import previsoes_payload
    return previsoes_payload(linhas)


def _contas_pagamento_payload() -> list[dict]:
    from recorrencias_gestao import contas_pagamento
    with _abrir() as conn:
        return contas_pagamento(conn)
