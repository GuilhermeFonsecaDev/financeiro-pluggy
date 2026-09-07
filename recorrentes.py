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

REGRAS_SUGESTAO = [
    "Pelo menos 3 cobranças da mesma conta ou cartão.",
    "Intervalos de 24 a 38 dias em pelo menos 80% do histórico e nos 2 últimos ciclos.",
    "Última cobrança nos últimos 45 dias; lançamentos futuros não contam.",
    "Valor estável ou reajuste sequencial; planos de valores diferentes são separados.",
    "Parcelas, duplicidades ambíguas e gastos frequentes não geram sugestões.",
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
                      "emoji": l["emoji"] or ""}
            for l in conn.execute("SELECT id, nome, cor, emoji FROM extrato_categorias")
        }

    grupos: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
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
    return {
        "janela": {"de": mes_de, "ate": mes_ate, "meses": janela},
        "sugestoes": sugestoes,
        "possiveis": [],
        "regrasSugestao": REGRAS_SUGESTAO,
        "previsoes": _previsoes_payload(previsoes),
        "contasPagamento": _contas_pagamento_payload(),
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


def criar_previsao(dados: dict) -> dict[str, Any]:
    from recorrencias_gestao import criar
    return criar(dados)


def editar_previsao(chave: str, dados: dict) -> dict[str, Any]:
    from recorrencias_gestao import editar
    return editar(chave, dados)


def excluir_previsao(chave: str) -> dict[str, Any]:
    from recorrencias_gestao import excluir
    return excluir(chave)


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
