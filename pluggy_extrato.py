"""Camada de leitura das tabelas pluggy_* para a tela de extrato.

Somente leitura -- quem grava e o importar_pluggy.py.

Convencao de sinal
------------------
O campo "valor" vem da Pluggy com sinais que se invertem conforme o tipo de
conta: na conta corrente uma saida e negativa, no cartao uma compra e positiva.
Somar "valor" direto mistura as duas coisas.

O sinal confiavel e o campo "tipo": DEBIT e sempre saida (compra no cartao ou
debito na conta), CREDIT e sempre entrada (recebimento ou estorno). Por isso
todo agregado aqui usa "valor_normalizado": negativo = saiu, positivo = entrou.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

import extrato_camada as cam

import banco as fin
from importar_pluggy import SCHEMA_PLUGGY

SEM_CATEGORIA = "(sem categoria)"

DESCRICAO_SUBTIPO = {
    "CHECKING_ACCOUNT": "Corrente",
    "SAVINGS_ACCOUNT": "Poupança",
    "CREDIT_CARD": "Cartão",
}

# Negativo = saiu dinheiro, positivo = entrou. Ver docstring do modulo.
VALOR_NORMALIZADO = (
    "CASE WHEN t.tipo = 'DEBIT' THEN -ABS(t.valor) ELSE ABS(t.valor) END"
)

LIMITE_PADRAO = 200
LIMITE_MAXIMO = 2000

# Quantos anos a frente a tira de evolucao pode ir enquanto houver fatura
# projetada. A varredura para sozinha no primeiro ano vazio; o cap so existe
# para o loop nunca depender de o dado ser bem-comportado. 3 anos cobre o
# parcelamento mais longo que os bancos daqui oferecem (24x).
ANOS_PROJECAO_MAX = 3

garantir_extrato_materializado = cam.garantir_extrato_materializado


def _mes_serial(data_iso: str) -> int | None:
    """Converte YYYY-MM... em um indice mensal continuo."""
    encontrado = re.match(r"^(\d{4})-(\d{2})", data_iso or "")
    if not encontrado:
        return None
    return int(encontrado.group(1)) * 12 + int(encontrado.group(2)) - 1


def _ano_mes(serial: int) -> tuple[int, int]:
    ano, indice = divmod(serial, 12)
    return ano, indice + 1


def _nome_banco_cartao(linha: sqlite3.Row) -> str:
    """Nome legivel do banco/produto, sem expor o numero do cartao na grade."""
    nome = str(linha["nome"] or "Cartão").strip()
    normalizado = nome.upper()
    if "ITAU" in normalizado or "ITAÚ" in normalizado:
        return normalizado.replace("ITAU", "ITAÚ")
    if normalizado == "GOLD" or "NUBANK" in normalizado:
        return "NUBANK"
    if "PLATINUM PRIME DUO" in normalizado or "BANCO INTER" in normalizado:
        return "INTER"
    return nome


def _completar_faturas_abertas_e_parcelas(
    conn: sqlite3.Connection,
    ano: int,
    ids_cartoes: list[str],
    valores: dict[str, list[float]],
    quantidades: dict[str, list[int]],
) -> tuple[
    dict[str, list[str]], dict[str, list[int]], set[int],
    dict[str, list[dict[str, Any]]],
]:
    """Inclui a fatura aberta e projeta as parcelas que ainda vao vencer.

    A Pluggy deixa a fatura aberta como PENDING e sem billId. Para os meses
    seguintes ela informa somente parcelaNumero/parcelaTotal; as ocorrencias
    futuras precisam ser derivadas da ultima parcela conhecida.

    O agrupamento por compra usa (conta, data da compra, numero do cartao,
    parcela_total, descricao sem o contador de parcela, valor arredondado).
    Varios lojistas (ex.: Olympikus, Midea) colocam o contador de parcela
    dentro da propria descricao ("1/10", "2/10", ...) -- usar a descricao
    bruta como chave fazia cada parcela real virar uma "compra" diferente,
    multiplicando a mesma compra em varias linhas projetadas erradas. Por
    isso o contador e removido antes de comparar. O valor arredondado entra
    na chave pra nao juntar duas compras distintas feitas no mesmo dia, no
    mesmo cartao, com a mesma descricao generica e o mesmo numero de parcelas
    (ex.: dois produtos diferentes no mesmo pedido do Mercado Livre).
    """
    origens = {conta_id: ["vazio"] * 12 for conta_id in ids_cartoes}
    previstas = {conta_id: [0] * 12 for conta_id in ids_cartoes}
    itens: dict[str, list[dict[str, Any]]] = {conta_id: [] for conta_id in ids_cartoes}
    anos_projetados: set[int] = set()
    for conta_id in ids_cartoes:
        for indice in range(12):
            if quantidades[conta_id][indice] or valores[conta_id][indice]:
                origens[conta_id][indice] = "fechada"

    marcadores = ", ".join("?" for _ in ids_cartoes) or "NULL"
    vencimento_fatura: dict[tuple[str, str], int] = {}
    ultimo_fechamento: dict[str, int] = {}
    for linha in conn.execute(
        f"""
        SELECT
          conta_id,
          fatura_id,
          COALESCE(
            MAX(CASE WHEN NOT {EH_PAGAMENTO_FATURA} THEN t.data END),
            MAX(t.data)
          ) AS ultima
        FROM pluggy_transacoes t
        WHERE conta_id IN ({marcadores}) AND fatura_id <> ''
        GROUP BY conta_id, fatura_id
        """,
        ids_cartoes,
    ):
        mes = _mes_serial(linha["ultima"])
        if mes is None:
            continue
        mes += 1
        chave = (linha["conta_id"], linha["fatura_id"])
        vencimento_fatura[chave] = mes
        ultimo_fechamento[linha["conta_id"]] = max(
            ultimo_fechamento.get(linha["conta_id"], mes), mes
        )

    vencimento_aberta: dict[str, int] = {}
    for linha in conn.execute(
        f"""
        SELECT
          t.conta_id,
          COALESCE(
            MAX(CASE WHEN NOT {EH_PAGAMENTO_FATURA} THEN t.data END),
            MAX(t.data)
          ) AS ultima,
          SUM(
            CASE
              WHEN t.tipo = 'DEBIT' THEN ABS(t.valor)
              WHEN t.tipo = 'CREDIT' AND NOT {EH_PAGAMENTO_FATURA}
                THEN -ABS(t.valor)
              ELSE 0
            END
          ) AS gasto,
          COUNT(*) AS quantidade
        FROM pluggy_transacoes t
        WHERE t.conta_id IN ({marcadores})
          AND t.fatura_id = ''
          AND t.status = 'PENDING'
        GROUP BY t.conta_id
        """,
        ids_cartoes,
    ):
        mes = _mes_serial(linha["ultima"])
        if mes is None:
            continue
        mes += 1
        conta_id = linha["conta_id"]
        vencimento_aberta[conta_id] = mes
        ano_fatura, mes_fatura = _ano_mes(mes)
        anos_projetados.add(ano_fatura)
        if ano_fatura == ano:
            indice = mes_fatura - 1
            valores[conta_id][indice] += float(linha["gasto"] or 0)
            quantidades[conta_id][indice] += int(linha["quantidade"] or 0)
            origens[conta_id][indice] = "aberta"

    series: dict[tuple[Any, ...], sqlite3.Row] = {}
    for linha in conn.execute(
        f"""
        SELECT
          t.transacao_id, t.conta_id, t.data, t.descricao, t.valor, t.status,
          t.fatura_id, t.parcela_numero, t.parcela_total,
          SUBSTR(
            JSON_EXTRACT(t.raw_json, '$.creditCardMetadata.purchaseDate'),
            1, 10
          ) AS compra,
          COALESCE(
            JSON_EXTRACT(t.raw_json, '$.creditCardMetadata.cardNumber'), ''
          ) AS numero_cartao
        FROM pluggy_transacoes t
        WHERE t.conta_id IN ({marcadores})
          AND t.tipo = 'DEBIT'
          AND t.parcela_total > 1
          AND t.parcela_numero IS NOT NULL
          AND JSON_EXTRACT(
            t.raw_json, '$.creditCardMetadata.purchaseDate'
          ) IS NOT NULL
        """,
        ids_cartoes,
    ):
        descricao_sem_contador = re.sub(
            r"\d+\s*/\s*\d+", "", str(linha["descricao"] or "")
        )
        descricao_normalizada = re.sub(
            r"\s+", " ", descricao_sem_contador
        ).strip().casefold()
        chave = (
            linha["conta_id"],
            linha["compra"],
            linha["numero_cartao"],
            linha["parcela_total"],
            descricao_normalizada,
            round(float(linha["valor"] or 0)),
        )
        anterior = series.get(chave)
        ordem = (int(linha["parcela_numero"]), linha["data"])
        if anterior is None or ordem > (
            int(anterior["parcela_numero"]), anterior["data"]
        ):
            series[chave] = linha

    for linha in series.values():
        atual = int(linha["parcela_numero"])
        total = int(linha["parcela_total"])
        if atual >= total:
            continue

        conta_id = linha["conta_id"]
        if linha["fatura_id"]:
            mes_base = vencimento_fatura.get((conta_id, linha["fatura_id"]))
        else:
            mes_base = vencimento_aberta.get(conta_id)
        if mes_base is None:
            continue

        # Quando a fatura aberta já veio como PENDING, suas parcelas atuais já
        # estão incluídas no total acima. Projetá-las novamente nesse mesmo mês
        # duplicava parte da fatura (especialmente no Inter).
        if conta_id in vencimento_aberta:
            primeiro_projetavel = vencimento_aberta[conta_id] + 1
        else:
            primeiro_projetavel = ultimo_fechamento.get(
                conta_id, mes_base - 1
            ) + 1
        for deslocamento in range(1, total - atual + 1):
            mes_previsto = mes_base + deslocamento
            if mes_previsto < primeiro_projetavel:
                continue
            ano_previsto, numero_mes = _ano_mes(mes_previsto)
            anos_projetados.add(ano_previsto)
            if ano_previsto != ano:
                continue

            indice = numero_mes - 1
            valores[conta_id][indice] += abs(float(linha["valor"] or 0))
            previstas[conta_id][indice] += 1
            if origens[conta_id][indice] == "aberta":
                origens[conta_id][indice] = "aberta_projecao"
            elif origens[conta_id][indice] == "vazio":
                origens[conta_id][indice] = "projecao"

            descricao_exibicao = re.sub(
                r"\s+", " ",
                re.sub(r"\d+\s*/\s*\d+", "", str(linha["descricao"] or "")),
            ).strip()
            itens[conta_id].append({
                "mes": numero_mes,
                "transacaoBaseId": linha["transacao_id"],
                "descricao": descricao_exibicao,
                "valor": abs(float(linha["valor"] or 0)),
                "parcelaAtual": atual + deslocamento,
                "parcelaTotal": total,
            })

    return origens, previstas, anos_projetados, itens


_tabelas_prontas = False


def garantir_tabelas(conn: sqlite3.Connection) -> None:
    """Cria as tabelas pluggy_* se ainda nao existirem, para a tela nao quebrar
    quando o import nunca foi rodado.

    Uma vez por processo: o schema nao muda entre requisicoes, e rodar o
    executescript a cada chamada so gastava tempo.
    """
    global _tabelas_prontas
    if _tabelas_prontas:
        return
    conn.executescript(SCHEMA_PLUGGY)
    _tabelas_prontas = True


NOMES_BANCOS = {"itau": "Itaú", "nubank": "Nubank", "inter": "Inter"}


def _grupos_bancarios(conn: sqlite3.Connection,
                      ativas: set[str] | None = None,
                      somente_cartoes: bool = False) -> dict[str, set[str]]:
    """Agrupa conta corrente e cartoes pela instituicao do item Pluggy.

    O agrupamento existe somente para navegacao e filtros. Os ids originais
    continuam nas transacoes e nenhuma soma e alterada por ele.
    """
    linhas = conn.execute(
        "SELECT conta_id, item_id, nome, subtipo, raw_json FROM pluggy_contas"
    ).fetchall()
    textos_item: dict[str, str] = {}
    for linha in linhas:
        textos_item[linha["item_id"]] = (
            textos_item.get(linha["item_id"], "") + " " +
            f"{linha['nome']} {linha['raw_json']}"
        ).lower()

    grupos = {banco: set() for banco in NOMES_BANCOS}
    for linha in linhas:
        conta_id = linha["conta_id"]
        if ativas is not None and conta_id not in ativas:
            continue
        if somente_cartoes and linha["subtipo"] != "CREDIT_CARD":
            continue
        texto = textos_item.get(linha["item_id"], "")
        if "itau" in texto or "itaú" in texto:
            grupos["itau"].add(conta_id)
        elif "nubank" in texto or "nu pagamentos" in texto:
            grupos["nubank"].add(conta_id)
        elif "inter" in texto:
            grupos["inter"].add(conta_id)
    return grupos


def _serie_com_projecao(evolucao: dict[str, dict], projetar) -> list[dict[str, Any]]:
    saida = []
    for mes, item in sorted(evolucao.items()):
        usar, recebidas, estimado, origem = projetar(
            mes, item["entradas"], item["qtdEntradas"],
            bool(item["quantidade"] or item["saidas"]),
        )
        saida.append({
            "mes": mes,
            "entradas": usar,
            "entradasRecebidas": recebidas,
            "entradasEstimativa": estimado,
            "entradasOrigem": origem,
            "saidas": item["saidas"],
            "saidasEstimativa": item.get("saidasEstimativa", False),
            "resultado": usar - float(item["saidas"]),
            "quantidade": item["quantidade"],
        })
    return saida


def _periodo(filtros: dict[str, Any]) -> tuple[str, str]:
    """(mes inicial, mes final) do filtro, em 'YYYY-MM'.

    O periodo e um intervalo de meses, nao de dias: a competencia de um gasto
    de cartao e o mes da fatura, entao "18 a 29 de agosto" nao teria
    significado -- a fatura vence num dia so. Presets como "ultimos 6 meses"
    viram intervalo de mes na tela e chegam aqui ja resolvidos.

    Aceita 'mes' (mes unico) como atalho, que e como os links vindos de outras
    telas ainda mandam.
    """
    mes_unico = str(filtros.get("mes") or "").strip()
    if mes_unico:
        return mes_unico, mes_unico
    de = str(filtros.get("mesDe") or "").strip()
    ate = str(filtros.get("mesAte") or "").strip()
    # Invertido pela tela (Ate anterior ao De): trata como intervalo valido em
    # vez de devolver lista vazia sem explicacao.
    if de and ate and de > ate:
        de, ate = ate, de
    return de, ate


def _onde(filtros: dict[str, Any], ativas: set[str] | None = None,
          grupos: dict[str, set[str]] | None = None,
          grupos_cartoes: dict[str, set[str]] | None = None,
          mes_campo: str = "competencia_fatura") -> tuple[str, list[Any]]:
    clausulas: list[str] = []
    params: list[Any] = []

    if ativas is not None:
        # Esconde as contas substituidas por uma reconexao, senao o mesmo gasto
        # entraria duas vezes nos totais.
        marcadores = ", ".join("?" for _ in ativas) or "NULL"
        clausulas.append(f"t.conta_id IN ({marcadores})")
        params.extend(sorted(ativas))

    mes_de, mes_ate = _periodo(filtros)
    if mes_de or mes_ate:
        # O mes de um gasto de cartao pode ser o mes em que a FATURA vence
        # (modo "fatura", o default) ou o mes da propria data da compra
        # (modo "mes", ver extrato_payload) -- o chamador decide via
        # mes_campo. Conta corrente e poupanca nao tem essa ambiguidade, a
        # coluna cai na propria data nos dois modos.
        campo = mes_campo
        if campo == "competencia_fatura":
            # Pagamento de fatura nao e gasto da fatura: sem isto ele apareceria
            # como lancamento do mes em que a fatura vence.
            clausulas.append(f"NOT {EH_PAGAMENTO_FATURA}")
        # Intervalo aberta de um lado e valido: "de setembro em diante" ou
        # "ate dezembro". Mes unico chega como de == ate.
        if mes_de:
            clausulas.append(f"t.{campo} >= ?")
            params.append(mes_de)
        if mes_ate:
            clausulas.append(f"t.{campo} <= ?")
            params.append(mes_ate)
    if filtros.get("conta"):
        conta = filtros["conta"]
        ids_grupo = grupos.get(conta, set()) if grupos else set()
        if ids_grupo:
            marcadores = ", ".join("?" for _ in ids_grupo)
            clausulas.append(f"t.conta_id IN ({marcadores})")
            params.extend(sorted(ids_grupo))
        else:
            clausulas.append("t.conta_id = ?")
            params.append(conta)
    if filtros.get("cartao"):
        cartao = filtros["cartao"]
        ids_cartao = grupos_cartoes.get(cartao, set()) if grupos_cartoes else set()
        if ids_cartao:
            marcadores = ", ".join("?" for _ in ids_cartao)
            clausulas.append(f"t.conta_id IN ({marcadores})")
            params.extend(sorted(ids_cartao))
        else:
            # Um filtro de cartao desconhecido nunca deve abrir o extrato todo.
            clausulas.append("1 = 0")
    if filtros.get("tipo") in {"DEBIT", "CREDIT"}:
        clausulas.append("t.tipo = ?")
        params.append(filtros["tipo"])
    if filtros.get("categoria"):
        # Filtra pela categoria EFETIVA (a resolvida pela camada local), nao
        # pelo texto cru da Pluggy. Escolher uma categoria-pai traz as
        # subcategorias dela junto -- e o mesmo agrupamento que a lista de
        # "Gastos por categoria" usa (ver por_categoria em extrato_payload),
        # entao clicar num total e ver a lista batendo com ele.
        clausulas.append(
            "t.categoria_id IN (SELECT id FROM extrato_categorias WHERE id = ? OR pai_id = ?)")
        params.extend([filtros["categoria"], filtros["categoria"]])
    if filtros.get("status") in {"POSTED", "PENDING"}:
        clausulas.append("t.status = ?")
        params.append(filtros["status"])
    if filtros.get("busca"):
        clausulas.append("t.descricao LIKE ? COLLATE NOCASE")
        params.append(f"%{filtros['busca']}%")

    return (" WHERE " + " AND ".join(clausulas) if clausulas else ""), params


def filtros_payload(modo: str = "fatura") -> dict[str, Any]:
    """Opcoes disponiveis nos seletores, independentes do filtro atual."""
    modo = modo if modo in ("fatura", "mes") else "fatura"
    campo_meses = "competencia_fatura" if modo == "fatura" else "mes_ref"
    fin.ensure_database()
    with cam.conectar() as conn:
        garantir_tabelas(conn)
        cam.garantir_camada(conn)
        garantir_extrato_materializado(conn)

        ativas = contas_ativas(conn)
        marcadores_meses = ", ".join("?" for _ in ativas) or "NULL"
        # No modo "fatura", competencia_fatura (nao mes_ref): um mes pode so
        # existir por ter fatura vencendo nele, sem nenhuma transacao datada
        # dentro dele. No modo "mes" o oposto vale: so lista mes que tem
        # compra de verdade datada nele.
        meses = [
            linha[0]
            for linha in conn.execute(
                f"SELECT DISTINCT {campo_meses} FROM extrato_efetivo_cache "
                f"WHERE conta_id IN ({marcadores_meses}) ORDER BY {campo_meses} DESC",
                sorted(ativas),
            )
        ]

        grupos = _grupos_bancarios(conn, ativas)
        grupos_cartoes = _grupos_bancarios(conn, ativas, somente_cartoes=True)
        apelidos_edicao = mapa_apelidos(conn, ativas)
        contas = [
            {"id": banco, "nome": nome, "transacoes": len(grupos[banco])}
            for banco, nome in NOMES_BANCOS.items() if grupos[banco]
        ]

        # Ordem de arvore (pai seguido dos filhos): a tela indenta as
        # subcategorias, entao a ordem precisa vir agrupada por pai daqui.
        #
        # Nao ha contagem de uso por categoria aqui de proposito. Ela custava
        # ~535ms dos ~627ms desta chamada: contar por categoria efetiva obriga
        # a varrer a view inteira, e a view avalia as regras (com norm(), uma
        # funcao Python) linha a linha. Era 86% do tempo de troca de tela para
        # exibir um numero ao lado do nome no seletor.
        planas = [
            {
                "id": linha["id"],
                "nome": linha["nome"],
                "cor": linha["cor"],
                "emoji": linha["emoji"] or "",
                "paiId": linha["pai_id"],
                "ordem": linha["ordem"],
            }
            for linha in conn.execute(
                "SELECT id, nome, cor, emoji, pai_id, ordem FROM extrato_categorias "
                "WHERE ativo = 1 ORDER BY ordem"
            )
        ]
        filhos_de: dict[str, list] = {}
        for c in planas:
            if c["paiId"]:
                filhos_de.setdefault(c["paiId"], []).append(c)

        categorias: list[dict[str, Any]] = []
        for c in planas:
            if c["paiId"]:
                continue
            categorias.append(c)
            categorias.extend(filhos_de.get(c["id"], []))
        vistos = {c["id"] for c in categorias}
        categorias.extend(c for c in planas if c["id"] not in vistos)

        # Ignora rodadas que nao gravaram nada: elas nao representam uma
        # importacao de verdade e so confundiriam a data mostrada na tela.
        ultima = conn.execute(
            "SELECT executado_em, data_min, data_max, transacoes_novas "
            "FROM pluggy_sync_log "
            "WHERE (contas_novas + contas_atualizadas + transacoes_novas "
            "       + transacoes_atualizadas) > 0 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return {
        "meses": meses,
        "mesAtual": datetime.now().strftime("%Y-%m"),
        "contas": contas,
        "cartoes": [
            {"id": banco, "nome": nome, "contas": len(grupos_cartoes[banco])}
            for banco, nome in NOMES_BANCOS.items() if grupos_cartoes[banco]
        ],
        "contasEdicao": [
            {"id": conta_id, "nome": apelidos_edicao.get(conta_id, conta_id)}
            for conta_id in sorted(ativas, key=lambda cid: apelidos_edicao.get(cid, cid))
        ],
        "categorias": categorias,
        "ultimaImportacao": (
            {
                "executadoEm": ultima["executado_em"],
                "dataMin": ultima["data_min"],
                "dataMax": ultima["data_max"],
                "transacoesNovas": ultima["transacoes_novas"],
            }
            if ultima
            else None
        ),
    }


# O pagamento da fatura aparece no extrato do proprio cartao como CREDIT. Ele
# quita a fatura ANTERIOR -- nao e estorno de compra. Se entrar na conta, ele
# cancela os gastos do mes e o total fica perto de zero ou negativo, que foi
# exatamente o sintoma: o total anual dava negativo.
#
# A Pluggy nao marca esses lancamentos de forma estruturada (categoriza como
# "Shopping"), entao a deteccao e pela descricao. E heuristica: se o seu banco
# usar outra palavra, acrescente aqui.
# Definido em extrato_camada porque a view tambem precisa dele; reexportado
# aqui para nao mudar quem ja importava deste modulo.
EH_PAGAMENTO_FATURA = cam.EH_PAGAMENTO_FATURA

# Duas fontes de renda (GFS e Jump/NIKY), cada uma paga em duas parcelas por
# mes -- o pagamento do mes e o adiantamento do mes seguinte: 4 creditos e o
# normal. Um mes com menos que isso so ainda nao recebeu tudo que deveria; nao
# significa que a renda caiu. Usado para projetar entradas de mes corrente ou
# futuro que ainda estao incompletas -- ver _media_entradas_completas().
ENTRADAS_ESPERADAS_POR_MES = 4

# Gasto da fatura: compra soma, estorno subtrai, pagamento da fatura e ignorado.
GASTO_LIQUIDO = (
    f"SUM(CASE WHEN {EH_PAGAMENTO_FATURA} THEN 0 "
    "          WHEN t.tipo = 'DEBIT' THEN ABS(t.valor) "
    "          ELSE -ABS(t.valor) END)"
)


def apelido_do_cartao(linha: sqlite3.Row) -> str:
    """Rotulo para a coluna do cartao.

    A Pluggy costuma preencher "name" do cartao com o nome do titular, o que
    nao serve de cabecalho de coluna. Preferimos bandeira + ultimos digitos, que
    e o que identifica o cartao de fato.
    """
    try:
        credito = (json.loads(linha["raw_json"]) or {}).get("creditData") or {}
    except (ValueError, TypeError):
        credito = {}

    bandeira = str(credito.get("brand") or "").strip()
    final = str(linha["numero"] or "").strip()

    if bandeira and final:
        return f"{bandeira} •••• {final}"
    if bandeira:
        return bandeira
    if final:
        return f"Cartão •••• {final}"
    return str(linha["nome"] or "Cartão")


def _chave_conta(linha: sqlite3.Row) -> tuple[str, str]:
    """Identidade da conta no mundo real, para reconhecer a mesma conta vinda
    de conexoes diferentes.

    O numero e o melhor sinal: a mesma conta corrente veio como "07050945-0"
    numa conexao e "70509450" na outra, entao normalizamos para so os digitos,
    sem zeros a esquerda. Sem numero, cai no rotulo.
    """
    numero = re.sub(r"\D", "", str(linha["numero"] or "")).lstrip("0")
    if numero:
        return (str(linha["subtipo"]), f"num:{numero}")
    return (str(linha["subtipo"]), f"rot:{_rotulo_base(linha)}")


def _rotulo_base(linha: sqlite3.Row) -> str:
    return (
        apelido_do_cartao(linha)
        if linha["subtipo"] == "CREDIT_CARD"
        else str(linha["nome"] or "Conta")
    )


def contas_ativas(conn: sqlite3.Connection) -> set[str]:
    """Ids das contas que devem aparecer nas telas.

    Reconectar no Meu Pluggy cria contas novas para o MESMO cartao/conta, com
    ids novos e o historico repetido. As duas versoes convivem no banco, e
    somar as duas conta o mesmo gasto duas vezes. Aqui fica so a mais atual de
    cada grupo -- a que tem a transacao mais recente.

    Nada e apagado: as contas substituidas continuam no banco e voltam a
    aparecer se este filtro for removido.
    """
    ultima_por_conta = {
        linha["conta_id"]: linha["ultima"]
        for linha in conn.execute(
            "SELECT conta_id, MAX(data) AS ultima FROM pluggy_transacoes "
            "GROUP BY conta_id"
        )
    }

    melhor: dict[tuple[str, str], tuple[str, str]] = {}
    for linha in conn.execute("SELECT * FROM pluggy_contas"):
        conta_id = linha["conta_id"]
        chave = _chave_conta(linha)
        candidato = (ultima_por_conta.get(conta_id) or "", conta_id)
        if chave not in melhor or candidato > melhor[chave]:
            melhor[chave] = candidato

    return {conta_id for _, conta_id in melhor.values()}


def mapa_apelidos(conn: sqlite3.Connection,
                  apenas: set[str] | None = None) -> dict[str, str]:
    """conta_id -> rotulo de exibicao, garantidamente unico.

    "apenas" limita o calculo as contas que serao mostradas: nao faz sentido
    desempatar rotulo contra uma conta escondida, senao sobra um sufixo feio
    sem nada com que confundir na tela.

    Cartoes viram bandeira + digitos; contas bancarias mantem o nome que a
    Pluggy manda. Reconectar a mesma conta gera um conta_id novo com os mesmos
    dados de exibicao, entao dois rotulos iguais apareceriam lado a lado sem
    como distinguir. Quando isso acontece, acrescentamos o final do id.
    """
    linhas = [
        linha for linha in conn.execute("SELECT * FROM pluggy_contas")
        if apenas is None or linha["conta_id"] in apenas
    ]
    rotulos = {
        linha["conta_id"]: (
            apelido_do_cartao(linha)
            if linha["subtipo"] == "CREDIT_CARD"
            else str(linha["nome"] or "Conta")
        )
        for linha in linhas
    }
    subtipos = {linha["conta_id"]: linha["subtipo"] for linha in linhas}

    def repetidos(mapa: dict[str, str]) -> set[str]:
        vistos: dict[str, int] = {}
        for rotulo in mapa.values():
            vistos[rotulo] = vistos.get(rotulo, 0) + 1
        return {rotulo for rotulo, n in vistos.items() if n > 1}

    # 1a tentativa: o subtipo separa corrente de poupanca no mesmo banco, que e
    # bem mais util do que um pedaco de id. So vale a pena quando os empatados
    # tem subtipos diferentes -- senao o sufixo so polui (dois cartoes iguais
    # nao ficam mais claros com "(Cartao)" nos dois).
    for rotulo_repetido in repetidos(rotulos):
        empatados = [c for c, r in rotulos.items() if r == rotulo_repetido]
        distintos = {subtipos.get(c) for c in empatados}
        if len(distintos) > 1 and all(s in DESCRICAO_SUBTIPO for s in distintos):
            for conta_id in empatados:
                rotulos[conta_id] = (
                    f"{rotulo_repetido} ({DESCRICAO_SUBTIPO[subtipos[conta_id]]})"
                )

    # 2a tentativa: sobrou empate (o mesmo cartao reconectado, por exemplo).
    colidindo = repetidos(rotulos)
    return {
        conta_id: (f"{rotulo} · {conta_id[-4:]}" if rotulo in colidindo else rotulo)
        for conta_id, rotulo in rotulos.items()
    }


def _ajustar_faturas_itau_por_pagamento(
    conn: sqlite3.Connection,
    ano: int,
    cartoes: list[dict[str, Any]],
    valores: dict[str, list[float]],
    quantidades: dict[str, list[int]],
    origens: dict[str, list[str]],
    previstas: dict[str, list[int]],
) -> None:
    """Usa o pagamento pendente como total da fatura atual do Itaú.

    O proxy Meu Pluggy trouxe os cartões Itaú sem as compras completas das
    faturas históricas, mas trouxe o pagamento final da fatura atual. Somar o
    histórico parcial produz valores negativos ou deslocados. O pagamento
    pendente é, neste caso, o único total conciliável com o app do banco.
    """
    for cartao in cartoes:
        if not str(cartao["nome"]).startswith("ITAÚ"):
            continue

        conta_id = cartao["id"]
        try:
            dia_vencimento = int(str(cartao.get("vencimento") or "")[-2:])
        except ValueError:
            dia_vencimento = 10

        pagamentos: dict[int, float] = {}
        for linha in conn.execute(
            f"""
            SELECT t.data, ABS(t.valor) AS valor
            FROM pluggy_transacoes t
            WHERE t.conta_id = ? AND t.status = 'PENDING'
              AND {EH_PAGAMENTO_FATURA}
            """,
            (conta_id,),
        ):
            serial = _mes_serial(linha["data"])
            if serial is None:
                continue
            try:
                dia_pagamento = int(str(linha["data"])[8:10])
            except ValueError:
                dia_pagamento = 1
            if dia_pagamento > dia_vencimento:
                serial += 1
            pagamentos[serial] = max(
                pagamentos.get(serial, 0.0), float(linha["valor"] or 0)
            )

        if not pagamentos:
            continue

        fatura_atual = max(pagamentos)
        for indice in range(12):
            serial = ano * 12 + indice
            if serial <= fatura_atual:
                valores[conta_id][indice] = 0.0
                quantidades[conta_id][indice] = 0
                previstas[conta_id][indice] = 0
                origens[conta_id][indice] = "vazio"

        ano_fatura, mes_fatura = _ano_mes(fatura_atual)
        if ano_fatura == ano:
            indice = mes_fatura - 1
            valores[conta_id][indice] = pagamentos[fatura_atual]
            quantidades[conta_id][indice] = 1
            previstas[conta_id][indice] = 0
            origens[conta_id][indice] = "pagamento"


def _ajustar_faturas_nubank(
    conn: sqlite3.Connection,
    ano: int,
    cartoes: list[dict[str, Any]],
    valores: dict[str, list[float]],
    quantidades: dict[str, list[int]],
    origens: dict[str, list[str]],
    previstas: dict[str, list[int]],
    itens: dict[str, list[dict[str, Any]]],
) -> None:
    """Reconcilia as faturas Nubank com o saldo e as parcelas da API.

    O saldo da conta de cartão representa todo o crédito utilizado, incluindo
    as parcelas que ainda vencerão. A fatura aberta é, portanto, o saldo menos
    essas parcelas futuras. As próximas faturas recebem somente as parcelas
    que efetivamente vencem em cada mês.

    Os itens projetados aqui vêm direto das transações PENDING reais (cada
    uma já é uma parcela conhecida, sem agrupamento por descrição), por isso
    substituem por completo qualquer item que o motor genérico
    (_completar_faturas_abertas_e_parcelas) tenha gerado para este cartão.
    """
    for cartao in cartoes:
        if cartao["nome"] != "NUBANK":
            continue
        conta_id = cartao["id"]
        itens[conta_id] = []
        linhas = conn.execute(
            f"""
            WITH transacoes_ciclo AS (
              SELECT
                t.*,
                MAX(CASE WHEN NOT {EH_PAGAMENTO_FATURA} THEN t.data END)
                  OVER (PARTITION BY t.conta_id, t.fatura_id) AS ultima_compra
              FROM pluggy_transacoes t
              WHERE t.conta_id = ? AND t.fatura_id <> ''
            ),
            faturas AS (
              SELECT
                t.fatura_id,
                MIN(
                  CASE
                    WHEN NOT {EH_PAGAMENTO_FATURA} AND JSON_EXTRACT(
                      t.raw_json,
                      '$.creditCardMetadata.billForecastDate'
                    ) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
                    THEN JSON_EXTRACT(
                      t.raw_json,
                      '$.creditCardMetadata.billForecastDate'
                    )
                  END
                ) AS mes_ref_fatura,
                SUM(
                  CASE
                    WHEN t.tipo = 'DEBIT' THEN ABS(t.valor)
                    WHEN t.tipo = 'CREDIT' AND NOT {EH_PAGAMENTO_FATURA}
                      THEN -ABS(t.valor)
                    ELSE 0
                  END
                ) - COALESCE((
                  SUM(
                    CASE
                      WHEN {EH_PAGAMENTO_FATURA}
                        AND t.data <= t.ultima_compra
                      THEN ABS(t.valor)
                      ELSE 0
                    END
                  ) - MAX(
                    CASE
                      WHEN {EH_PAGAMENTO_FATURA}
                        AND t.data <= t.ultima_compra
                      THEN ABS(t.valor)
                      ELSE 0
                    END
                  )
                ), 0) AS gasto,
                SUM(CASE WHEN NOT {EH_PAGAMENTO_FATURA} THEN 1 ELSE 0 END)
                  AS quantidade
              FROM transacoes_ciclo t
              GROUP BY t.fatura_id
            )
            SELECT
              CAST(SUBSTR(mes_ref_fatura, 6, 2) AS INTEGER) AS mes,
              SUM(gasto) AS gasto,
              SUM(quantidade) AS quantidade
            FROM faturas
            WHERE CAST(SUBSTR(mes_ref_fatura, 1, 4) AS INTEGER) = ?
            GROUP BY mes_ref_fatura
            ORDER BY mes_ref_fatura
            """,
            (conta_id, ano),
        ).fetchall()
        if linhas:
            corrigidos = {
                int(linha["mes"]) - 1: (
                    float(linha["gasto"] or 0), int(linha["quantidade"] or 0)
                )
                for linha in linhas
            }
            ultimo = max(corrigidos)
            for indice in range(ultimo + 1):
                gasto, quantidade = corrigidos.get(indice, (0.0, 0))
                valores[conta_id][indice] = gasto
                quantidades[conta_id][indice] = quantidade
                previstas[conta_id][indice] = 0
                origens[conta_id][indice] = (
                    "fechada" if quantidade else "vazio"
                )

        pendentes = conn.execute(
            """
            SELECT
              t.transacao_id, t.descricao, t.valor, t.parcela_numero,
              t.parcela_total,
              JSON_EXTRACT(
                t.raw_json, '$.creditCardMetadata.billForecastDate'
              ) AS mes_ref_fatura
            FROM pluggy_transacoes t
            WHERE t.conta_id = ? AND t.status = 'PENDING'
              AND t.tipo = 'DEBIT'
            """,
            (conta_id,),
        ).fetchall()
        meses_abertos = [
            _mes_serial(linha["mes_ref_fatura"])
            for linha in pendentes
            if linha["mes_ref_fatura"]
        ]
        meses_abertos = [mes for mes in meses_abertos if mes is not None]
        if not meses_abertos:
            continue

        mes_aberto = max(meses_abertos)
        pendentes_abertos = [
            linha
            for linha in pendentes
            if _mes_serial(linha["mes_ref_fatura"]) == mes_aberto
        ]
        projecoes: dict[int, float] = {}
        quantidades_projetadas: dict[int, int] = {}
        itens_por_serial: dict[int, list[dict[str, Any]]] = {}
        total_parcelas_futuras = 0.0
        for linha in pendentes_abertos:
            atual = int(linha["parcela_numero"] or 0)
            total = int(linha["parcela_total"] or 0)
            if atual <= 0 or total <= atual:
                continue
            valor_parcela = abs(float(linha["valor"] or 0))
            descricao_exibicao = re.sub(
                r"\s+", " ",
                re.sub(r"\d+\s*/\s*\d+", "", str(linha["descricao"] or "")),
            ).strip()
            restantes = total - atual
            total_parcelas_futuras += valor_parcela * restantes
            for deslocamento in range(1, restantes + 1):
                mes_previsto = mes_aberto + deslocamento
                projecoes[mes_previsto] = (
                    projecoes.get(mes_previsto, 0.0) + valor_parcela
                )
                quantidades_projetadas[mes_previsto] = (
                    quantidades_projetadas.get(mes_previsto, 0) + 1
                )
                itens_por_serial.setdefault(mes_previsto, []).append({
                    "transacaoBaseId": linha["transacao_id"],
                    "descricao": descricao_exibicao,
                    "valor": valor_parcela,
                    "parcelaAtual": atual + deslocamento,
                    "parcelaTotal": total,
                })

        conta = conn.execute(
            "SELECT saldo FROM pluggy_contas WHERE conta_id = ?",
            (conta_id,),
        ).fetchone()
        saldo_utilizado = abs(float(conta["saldo"] or 0)) if conta else 0.0
        valor_fatura_aberta = round(
            max(saldo_utilizado - total_parcelas_futuras, 0.0), 2
        )

        for indice in range(12):
            serial = ano * 12 + indice
            if serial < mes_aberto:
                continue
            if serial == mes_aberto:
                valores[conta_id][indice] = valor_fatura_aberta
                quantidades[conta_id][indice] = len(pendentes_abertos)
                previstas[conta_id][indice] = 0
                origens[conta_id][indice] = "aberta"
                continue

            valor_previsto = round(projecoes.get(serial, 0.0), 2)
            valores[conta_id][indice] = valor_previsto
            quantidades[conta_id][indice] = 0
            previstas[conta_id][indice] = quantidades_projetadas.get(serial, 0)
            origens[conta_id][indice] = (
                "projecao" if valor_previsto else "vazio"
            )
            itens[conta_id].extend(
                {**item, "mes": indice + 1}
                for item in itens_por_serial.get(serial, [])
            )


def _consolidar_cartoes_itau(
    cartoes: list[dict[str, Any]],
    valores: dict[str, list[float]],
    quantidades: dict[str, list[int]],
    origens: dict[str, list[str]],
    previstas: dict[str, list[int]],
    itens: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Transforma os cartões Itaú em uma única coluna mensal consolidada."""
    itaus = [cartao for cartao in cartoes if str(cartao["nome"]).startswith("ITAÚ")]
    if len(itaus) < 2:
        return cartoes

    id_consolidado = "consolidado:itau"
    ids = [cartao["id"] for cartao in itaus]
    valores[id_consolidado] = [
        sum(valores[conta_id][indice] for conta_id in ids)
        for indice in range(12)
    ]
    quantidades[id_consolidado] = [
        sum(quantidades[conta_id][indice] for conta_id in ids)
        for indice in range(12)
    ]
    previstas[id_consolidado] = [
        sum(previstas[conta_id][indice] for conta_id in ids)
        for indice in range(12)
    ]
    itens[id_consolidado] = [
        item for conta_id in ids for item in itens.get(conta_id, [])
    ]

    prioridade = {
        "vazio": 0,
        "movimento": 1,
        "fechada": 2,
        "projecao": 3,
        "aberta": 4,
        "aberta_projecao": 5,
        "pagamento": 6,
    }
    origens[id_consolidado] = [
        max(
            (origens[conta_id][indice] for conta_id in ids),
            key=lambda origem: prioridade.get(origem, 0),
        )
        for indice in range(12)
    ]

    consolidado = {
        "id": id_consolidado,
        "nome": "ITAÚ",
        "numero": "",
        "limiteCredito": sum(float(c.get("limiteCredito") or 0) for c in itaus),
        "limiteDisponivel": sum(
            float(c.get("limiteDisponivel") or 0) for c in itaus
        ),
        "fechamento": "",
        "vencimento": "",
        "cartoesConsolidados": [cartao["nome"] for cartao in itaus],
    }

    resultado: list[dict[str, Any]] = []
    inserido = False
    for cartao in cartoes:
        if cartao["id"] in ids:
            if not inserido:
                resultado.append(consolidado)
                inserido = True
            continue
        resultado.append(cartao)
    return resultado


def _preencher_itau_historico_por_pagamento_principal(
    conn: sqlite3.Connection,
    ano: int,
    cartoes: list[dict[str, Any]],
    valores: dict[str, list[float]],
    quantidades: dict[str, list[int]],
    origens: dict[str, list[str]],
    previstas: dict[str, list[int]],
) -> None:
    """Reconcilia o historico do Itau pelo pagamento do Platinum.

    A conexao do Itau possui dois cartoes. O pequeno ``ITAU MULTIPLO`` gera
    pagamentos recorrentes de R$ 13,90 que nao fazem parte da fatura principal
    exibida pelo usuario. Para meses ja pagos, a saida da conta corrente com a
    descricao ``FATURA PAGA ITAU PLATINU`` e a fonte mais confiavel.

    Pagamentos feitos a partir do dia 25 quitam a fatura do mes seguinte (por
    exemplo, o pagamento de 30/05 corresponde a fatura de junho).
    """
    id_consolidado = "consolidado:itau"
    if not any(cartao["id"] == id_consolidado for cartao in cartoes):
        return

    pagamentos: dict[int, float] = {}
    linhas = conn.execute(
        """
        SELECT t.data, t.descricao, ABS(t.valor) AS valor
        FROM pluggy_transacoes t
        JOIN pluggy_contas c ON c.conta_id = t.conta_id
        WHERE c.subtipo = 'CHECKING_ACCOUNT'
          AND t.tipo = 'DEBIT'
          AND (UPPER(c.nome) LIKE '%ITAU%' OR UPPER(c.nome) LIKE '%ITAÚ%')
        ORDER BY t.data
        """
    )
    for linha in linhas:
        descricao = cam.normalizar(linha["descricao"])
        if "fatura paga itau platinu" not in descricao:
            continue
        serial = _mes_serial(linha["data"])
        if serial is None:
            continue
        try:
            dia_pagamento = int(str(linha["data"])[8:10])
        except ValueError:
            dia_pagamento = 1
        if dia_pagamento >= 25:
            serial += 1
        pagamentos[serial] = max(
            pagamentos.get(serial, 0.0), float(linha["valor"] or 0)
        )

    for indice in range(12):
        serial = ano * 12 + indice
        if serial not in pagamentos:
            continue
        valores[id_consolidado][indice] = round(pagamentos[serial], 2)
        quantidades[id_consolidado][indice] = 1
        previstas[id_consolidado][indice] = 0
        origens[id_consolidado][indice] = "pagamento"


def _aplicar_faturas_oficiais(
    conn: sqlite3.Connection,
    ano: int,
    valores: dict[str, list[float]],
    quantidades: dict[str, list[int]],
    origens: dict[str, list[str]],
    previstas: dict[str, list[int]],
    itens: dict[str, list[dict[str, Any]]],
    contas_do_cartao: dict[str, set[str]],
) -> None:
    """Sobrescreve os meses de fatura FECHADA com o total que o banco emitiu.

    pluggy_faturas.valor_total vem de GET /bills -- e o proprio valor da
    fatura, nao uma soma reconstruida das transacoes. Por isso roda por
    ultimo: e a autoridade final para todo mes que tem fatura fechada.

    Somar transacoes acerta quando a base esta completa (conferido: bate ao
    centavo nas 16 faturas de 2026 do Inter e do Nubank), mas erra nas
    bordas -- no primeiro mes importado faltam as compras anteriores ao
    inicio do sync, e nos cartoes Itau o conector nem trouxe o historico de
    compras. Nesses casos a fatura oficial e a unica fonte correta.

    Fica de fora somente quando valor_total = 0: nos cartoes Itau varios
    ciclos vem zerados pela Pluggy mesmo tendo tido gasto, e zero e ambiguo
    ("nao coletei" x "nao gastou nada"), entao ali o calculo por
    transacao/pagamento continua valendo. Valor negativo, ao contrario, e
    dado real (fatura com saldo credor) e entra normalmente.

    A fatura AINDA EM ABERTO nunca chega aqui -- ela nao existe na API
    enquanto o banco nao fecha o ciclo (ver list_bills em pluggy_sync.py),
    e por isso o mes corrente segue estimado pelas outras funcoes.
    """
    por_cartao_mes: dict[tuple[str, int], float] = {}
    for linha in conn.execute(
        """
        SELECT conta_id, competencia, SUM(valor_total) AS total
        FROM pluggy_faturas
        WHERE competencia LIKE ? AND valor_total <> 0
        GROUP BY conta_id, competencia
        """,
        (f"{ano}-%",),
    ):
        try:
            indice = int(str(linha["competencia"])[5:7]) - 1
        except ValueError:
            continue
        if not 0 <= indice < 12:
            continue
        conta_id = linha["conta_id"]
        # Um cartao da grade pode representar varias contas (os Itau
        # consolidados), entao a fatura entra na coluna que contem a conta.
        for cartao_id, contas in contas_do_cartao.items():
            if conta_id in contas:
                chave = (cartao_id, indice)
                por_cartao_mes[chave] = (
                    por_cartao_mes.get(chave, 0.0) + float(linha["total"] or 0)
                )

    for (cartao_id, indice), total in por_cartao_mes.items():
        if cartao_id not in valores:
            continue
        valores[cartao_id][indice] = round(total, 2)
        origens[cartao_id][indice] = "oficial"
        # A fatura fechou: o que havia de previsto para este mes ja esta
        # dentro do total oficial, entao nao pode continuar contando junto.
        previstas[cartao_id][indice] = 0
        if cartao_id in itens:
            itens[cartao_id] = [
                item for item in itens[cartao_id] if item["mes"] != indice + 1
            ]


def _aplicar_faturas_confirmadas(
    conn: sqlite3.Connection,
    ano: int,
    valores: dict[str, list[float]],
    origens: dict[str, list[str]],
) -> None:
    """Aplica valores de fatura aberta confirmados no banco.

    A API de Bills só informa faturas fechadas. Para uma fatura ainda aberta,
    o valor confirmado no aplicativo do banco prevalece sobre a estimativa
    formada por transações PENDING até que a fatura seja fechada pela Pluggy.
    """
    prefixo = "fatura_confirmada:"
    for linha in conn.execute(
        "SELECT chave, valor FROM app_meta WHERE chave LIKE ?",
        (f"{prefixo}%",),
    ):
        partes = str(linha["chave"]).split(":")
        if len(partes) != 4:
            continue
        _, conta_id, ano_texto, mes_texto = partes
        try:
            ano_registro = int(ano_texto)
            mes_registro = int(mes_texto)
            registro = json.loads(linha["valor"])
            if isinstance(registro, dict):
                valor_confirmado = float(registro["valor"])
                base_confirmada = float(registro["base"])
            else:
                valor_confirmado = float(registro)
                base_confirmada = valor_confirmado
        except (KeyError, TypeError, ValueError):
            continue
        if ano_registro != ano or conta_id not in valores:
            continue
        indice = mes_registro - 1
        if (
            0 <= indice < 12
            and origens[conta_id][indice] in {"aberta", "aberta_projecao"}
        ):
            variacao = valores[conta_id][indice] - base_confirmada
            valores[conta_id][indice] = round(valor_confirmado + variacao, 2)
            origens[conta_id][indice] = "confirmada"


def cartoes_payload(ano: int, agrupamento: str = "fatura") -> dict[str, Any]:
    """Grade 12 meses x cartoes de credito, no mesmo formato da pagina manual
    de faturas.

    agrupamento="mes"    -> soma pelo mes em que a transacao aconteceu.
    agrupamento="fatura" -> soma por fatura (billId), exibindo o ciclo no mes
                            seguinte ao ultimo lancamento. O maior pagamento
                            quita a fatura anterior; pagamentos adicionais
                            abatem o ciclo atual.
    """
    agrupamento = "mes" if agrupamento == "mes" else "fatura"

    fin.ensure_database()
    with fin.connect() as conn:
        garantir_tabelas(conn)

        ativas = contas_ativas(conn)
        cartoes = [
            {
                "id": linha["conta_id"],
                "nome": _nome_banco_cartao(linha),
                "numero": linha["numero"],
                "limiteCredito": linha["limite_credito"],
                "limiteDisponivel": linha["limite_disponivel"],
                "fechamento": linha["fechamento"],
                "vencimento": linha["vencimento"],
            }
            for linha in conn.execute(
                "SELECT * FROM pluggy_contas WHERE subtipo = 'CREDIT_CARD' "
                "ORDER BY nome"
            )
            if linha["conta_id"] in ativas
        ]

        ids_cartoes = sorted(cartao["id"] for cartao in cartoes)
        marcadores = ", ".join("?" for _ in ids_cartoes) or "NULL"

        anos = [
            linha[0]
            for linha in conn.execute(
                "SELECT DISTINCT ano FROM pluggy_transacoes "
                f"WHERE conta_id IN ({marcadores}) ORDER BY ano",
                ids_cartoes,
            )
        ]

        if agrupamento == "fatura":
            # A Pluggy agrupa no mesmo fatura_id os lançamentos do ciclo e os
            # pagamentos. O maior pagamento feito antes do fechamento quita a
            # fatura anterior; os menores são abatimentos do ciclo. Pagamentos
            # importados depois da última compra não podem deslocar nem reduzir
            # uma fatura já fechada.
            consulta = f"""
                WITH transacoes_ciclo AS (
                  SELECT
                    t.*,
                    MAX(CASE WHEN NOT {EH_PAGAMENTO_FATURA} THEN t.data END)
                      OVER (PARTITION BY t.conta_id, t.fatura_id)
                      AS ultima_compra
                  FROM pluggy_transacoes t
                  WHERE t.fatura_id <> ''
                    AND t.conta_id IN ({marcadores})
                ),
                faturas AS (
                  SELECT
                    t.conta_id,
                    t.fatura_id,
                    CAST(STRFTIME(
                      '%Y',
                      DATE(
                        SUBSTR(
                          COALESCE(t.ultima_compra, MAX(t.data)), 1, 7
                        ) || '-01',
                        '+1 month'
                      )
                    ) AS INTEGER) AS ano_fatura,
                    CAST(STRFTIME(
                      '%m',
                      DATE(
                        SUBSTR(
                          COALESCE(t.ultima_compra, MAX(t.data)), 1, 7
                        ) || '-01',
                        '+1 month'
                      )
                    ) AS INTEGER) AS mes_fatura,
                    SUM(
                      CASE
                        WHEN t.tipo = 'DEBIT' THEN ABS(t.valor)
                        WHEN t.tipo = 'CREDIT' AND NOT {EH_PAGAMENTO_FATURA}
                          THEN -ABS(t.valor)
                        ELSE 0
                      END
                    ) - COALESCE((
                      SUM(
                        CASE
                          WHEN {EH_PAGAMENTO_FATURA}
                            AND t.data <= t.ultima_compra
                          THEN ABS(t.valor)
                          ELSE 0
                        END
                      ) - MAX(
                        CASE
                          WHEN {EH_PAGAMENTO_FATURA}
                            AND t.data <= t.ultima_compra
                          THEN ABS(t.valor)
                          ELSE 0
                        END
                      )
                    ), 0) AS gasto,
                    SUM(CASE WHEN NOT {EH_PAGAMENTO_FATURA} THEN 1 ELSE 0 END)
                      AS quantidade
                  FROM transacoes_ciclo t
                  GROUP BY t.conta_id, t.fatura_id
                )
                SELECT
                  conta_id,
                  mes_fatura AS mes,
                  SUM(gasto) AS gasto,
                  SUM(quantidade) AS quantidade
                FROM faturas
                WHERE ano_fatura = ?
                GROUP BY conta_id, mes_fatura
            """
            parametros = [*ids_cartoes, ano]
        else:
            consulta = f"""
                SELECT t.conta_id, t.mes AS mes, {GASTO_LIQUIDO} AS gasto,
                       COUNT(*) AS quantidade
                FROM pluggy_transacoes t
                WHERE t.conta_id IN ({marcadores}) AND t.ano = ?
                GROUP BY t.conta_id, t.mes
            """
            parametros = [*ids_cartoes, ano]

        valores = {cartao["id"]: [0.0] * 12 for cartao in cartoes}
        quantidades = {cartao["id"]: [0] * 12 for cartao in cartoes}
        for linha in conn.execute(consulta, parametros):
            conta_id = linha["conta_id"]
            indice = int(linha["mes"]) - 1
            if conta_id in valores and 0 <= indice < 12:
                valores[conta_id][indice] = float(linha["gasto"] or 0)
                quantidades[conta_id][indice] = int(linha["quantidade"] or 0)

        if agrupamento == "fatura":
            origens, quantidades_previstas, anos_projetados, itens = (
                _completar_faturas_abertas_e_parcelas(
                    conn, ano, ids_cartoes, valores, quantidades
                )
            )
            _ajustar_faturas_nubank(
                conn,
                ano,
                cartoes,
                valores,
                quantidades,
                origens,
                quantidades_previstas,
                itens,
            )
            _ajustar_faturas_itau_por_pagamento(
                conn,
                ano,
                cartoes,
                valores,
                quantidades,
                origens,
                quantidades_previstas,
            )
            _aplicar_faturas_confirmadas(conn, ano, valores, origens)
            anos = sorted(set(anos) | anos_projetados)
        else:
            origens = {
                conta_id: [
                    "movimento" if quantidades[conta_id][indice] else "vazio"
                    for indice in range(12)
                ]
                for conta_id in ids_cartoes
            }
            quantidades_previstas = {
                conta_id: [0] * 12 for conta_id in ids_cartoes
            }
            itens = {conta_id: [] for conta_id in ids_cartoes}

        # Quais contas reais cada coluna da grade representa. Normalmente e
        # 1:1, menos nos Itau, que viram uma coluna consolidada -- e a fatura
        # oficial precisa saber somar as duas contas naquela coluna.
        contas_do_cartao = {cartao["id"]: {cartao["id"]} for cartao in cartoes}
        ids_itau = {
            cartao["id"] for cartao in cartoes
            if str(cartao["nome"]).startswith("ITAÚ")
        }

        cartoes = _consolidar_cartoes_itau(
            cartoes,
            valores,
            quantidades,
            origens,
            quantidades_previstas,
            itens,
        )
        if any(cartao["id"] == "consolidado:itau" for cartao in cartoes):
            contas_do_cartao = {
                cartao["id"]: (
                    ids_itau if cartao["id"] == "consolidado:itau"
                    else {cartao["id"]}
                )
                for cartao in cartoes
            }

        if agrupamento == "fatura":
            _preencher_itau_historico_por_pagamento_principal(
                conn,
                ano,
                cartoes,
                valores,
                quantidades,
                origens,
                quantidades_previstas,
            )
            # Por ultimo: onde existe fatura fechada, o valor emitido pelo
            # banco vale mais que qualquer reconstrucao nossa.
            _aplicar_faturas_oficiais(
                conn,
                ano,
                valores,
                quantidades,
                origens,
                quantidades_previstas,
                itens,
                contas_do_cartao,
            )

    # So um item projetado sobrevive se, depois de todos os ajustes por
    # banco, o mes correspondente ainda estiver marcado como "projecao" --
    # meses virados em lump sum (pagamento confirmado, fatura Itau por
    # pagamento etc.) nao tem decomposicao confiavel e ficam so no total.
    for cartao in cartoes:
        conta_id = cartao["id"]
        lista = itens.get(conta_id, [])
        origens_cartao = origens.get(conta_id, [])
        itens[conta_id] = [
            item for item in lista
            if 1 <= item["mes"] <= 12
            and origens_cartao[item["mes"] - 1] == "projecao"
        ]

    total_mes = [
        sum(valores[cartao["id"]][indice] for cartao in cartoes)
        for indice in range(12)
    ]

    return {
        "ano": ano,
        "agrupamento": agrupamento,
        "anosDisponiveis": anos,
        "cartoes": cartoes,
        "valores": valores,
        "quantidades": quantidades,
        "quantidadesPrevistas": quantidades_previstas,
        "origens": origens,
        "itens": itens,
        "totalMes": total_mes,
        "totalAno": sum(total_mes),
    }


def categorias_resumo_payload(filtros: dict[str, Any]) -> dict[str, Any]:
    """Árvore pai → subcategorias com o gasto de cada uma.

    O total do pai soma os filhos MAIS o que caiu direto nele: uma transação
    pode ficar em "Transporte" sem passar por nenhuma subcategoria.
    """
    fin.ensure_database()
    with cam.conectar() as conn:
        garantir_tabelas(conn)
        cam.garantir_camada(conn)
        garantir_extrato_materializado(conn)
        ativas = contas_ativas(conn)
        grupos = _grupos_bancarios(conn, ativas)
        grupos_cartoes = _grupos_bancarios(conn, ativas, somente_cartoes=True)
        onde, params = _onde(filtros, ativas, grupos, grupos_cartoes)

        gasto = {
            linha["categoria_id"]: {
                "total": float(linha["total"] or 0),
                "quantidade": linha["quantidade"],
            }
            for linha in conn.execute(
                f"""
                SELECT t.categoria_id, SUM(ABS(t.valor)) AS total,
                       COUNT(*) AS quantidade
                FROM extrato_efetivo_cache t{onde}
                {'AND' if onde else 'WHERE'} t.tipo = 'DEBIT' AND t.incluida = 1
                GROUP BY t.categoria_id
                """,
                params,
            )
        }

        categorias = [
            {"id": l["id"], "nome": l["nome"], "cor": l["cor"],
             "emoji": l["emoji"] or "", "paiId": l["pai_id"]}
            for l in conn.execute(
                "SELECT id, nome, cor, emoji, pai_id FROM extrato_categorias "
                "WHERE ativo = 1 ORDER BY ordem"
            )
        ]

    def com_gasto(cat: dict) -> dict:
        g = gasto.get(cat["id"], {})
        return {**cat, "total": g.get("total", 0.0), "quantidade": g.get("quantidade", 0)}

    ids = {c["id"] for c in categorias}
    arvore = []
    for cat in categorias:
        # Órfã (pai desativado) entra como raiz para não sumir do relatório.
        if cat["paiId"] and cat["paiId"] in ids:
            continue
        filhos = sorted(
            (com_gasto(f) for f in categorias if f["paiId"] == cat["id"]),
            key=lambda f: -f["total"],
        )
        proprio = com_gasto(cat)
        arvore.append({
            **proprio,
            "totalDireto": proprio["total"],
            "quantidadeDireta": proprio["quantidade"],
            "total": proprio["total"] + sum(f["total"] for f in filhos),
            "quantidade": proprio["quantidade"] + sum(f["quantidade"] for f in filhos),
            "filhos": filhos,
        })

    arvore.sort(key=lambda c: -c["total"])
    return {"categorias": arvore, "total": sum(c["total"] for c in arvore)}


def extrato_payload(filtros: dict[str, Any]) -> dict[str, Any]:
    limite = min(int(filtros.get("limite") or LIMITE_PADRAO), LIMITE_MAXIMO)
    offset = max(int(filtros.get("offset") or 0), 0)
    # Modo "fatura" (default): gasto de cartao conta no mes em que a fatura
    # vence. Modo "mes": conta no mes da propria data da compra -- sem essa
    # ambiguidade, conta corrente/poupanca sao iguais nos dois modos (ver
    # _COMPETENCIA_FATURA em extrato_camada.py).
    modo = filtros.get("modo") if filtros.get("modo") in ("fatura", "mes") else "fatura"
    mes_campo_gasto = "competencia_fatura" if modo == "fatura" else "mes_ref"
    mes_de, mes_ate = _periodo(filtros)
    # As projecoes de fatura e de entrada raciocinam sobre UM mes ("este mes
    # ainda nao recebeu tudo", "esta fatura ainda esta aberta"). Num intervalo
    # de varios meses isso nao se traduz, entao ali elas ficam desligadas --
    # a tira de evolucao continua mostrando cada mes projetado.
    mes_filtro = mes_de if mes_de and mes_de == mes_ate else ""

    fin.ensure_database()
    with cam.conectar() as conn:
        garantir_tabelas(conn)
        cam.garantir_camada(conn)
        ativas = contas_ativas(conn)
        apelidos = mapa_apelidos(conn, ativas)
        grupos = _grupos_bancarios(conn, ativas)
        grupos_cartoes = _grupos_bancarios(conn, ativas, somente_cartoes=True)
        banco_por_conta = {
            conta_id: banco for banco, ids in grupos.items() for conta_id in ids
        }

        # A view aplica todas as regras e é cara. A cópia materializada é
        # compartilhada entre aberturas e invalidada pela assinatura das
        # transações, regras, mapeamentos e edições que a alimentam.
        garantir_extrato_materializado(conn)
        onde, params = _onde(
            filtros, ativas, grupos, grupos_cartoes, mes_campo=mes_campo_gasto
        )
        onde_entradas, params_entradas = _onde(
            filtros, ativas, grupos, grupos_cartoes,
            mes_campo="competencia_entrada"
        )

        marcadores = ", ".join("?" for _ in ativas) or "NULL"
        total_geral = conn.execute(
            f"SELECT COUNT(*) FROM pluggy_transacoes WHERE conta_id IN ({marcadores})",
            sorted(ativas),
        ).fetchone()[0]

        # Duas contagens diferentes de proposito: a tabela lista tudo que casa
        # com o filtro (inclusive o que esta fora dos calculos), mas entradas,
        # saidas e resultado so olham incluida = 1.
        resumo = conn.execute(
            f"""
            SELECT COUNT(*) AS no_filtro,
                   COALESCE(SUM(t.incluida), 0) AS quantidade,
                   COALESCE(SUM(CASE WHEN t.incluida = 1 AND t.tipo = 'DEBIT'
                                     THEN ABS(t.valor) END), 0) AS saidas,
                   COALESCE(SUM(CASE WHEN t.incluida = 0 THEN 1 END), 0) AS ignoradas
            FROM extrato_efetivo_cache t{onde}
            """,
            params,
        ).fetchone()

        entrada_linha = conn.execute(
            f"""
            SELECT COALESCE(SUM(ABS(t.valor)), 0) AS total, COUNT(*) AS qtd
            FROM extrato_efetivo_cache t{onde_entradas}
            {'AND' if onde_entradas else 'WHERE'}
              t.incluida = 1 AND t.tipo = 'CREDIT' AND t.entrada_considerada = 1
              AND NOT EXISTS (SELECT 1 FROM extrato_entradas_exclusoes x
                              WHERE x.transacao_id = t.transacao_id)
            """,
            params_entradas,
        ).fetchone()
        entrada_total = entrada_linha["total"]
        entrada_qtd = int(entrada_linha["qtd"] or 0)

        # Série mensal para o cabeçalho da tela. O período selecionado não
        # limita a série; os demais filtros (conta, cartão, categoria etc.)
        # continuam valendo, para a evolução refletir o contexto atual.
        filtros_evolucao = {**filtros, "mes": "", "mesDe": "", "mesAte": ""}
        onde_evolucao, params_evolucao = _onde(
            filtros_evolucao, ativas, grupos, grupos_cartoes, mes_campo=mes_campo_gasto
        )
        onde_evolucao_entradas, params_evolucao_entradas = _onde(
            filtros_evolucao, ativas, grupos, grupos_cartoes,
            mes_campo="competencia_entrada",
        )
        # A serie usa o mesmo campo de mes que o filtro (vencimento de fatura
        # no modo "fatura", data da compra no modo "mes"): agrupar por um e
        # filtrar por outro deixaria a barra de um mes vazia enquanto a lista
        # embaixo mostra a fatura/compra inteira daquele mes.
        evolucao: dict[str, dict[str, float | int]] = {}
        for linha in conn.execute(
            f"""
            SELECT t.{mes_campo_gasto} AS mes_ref,
                   COALESCE(SUM(CASE WHEN t.incluida = 1 AND t.tipo = 'DEBIT'
                                     THEN ABS(t.valor) END), 0) AS saidas,
                   COUNT(*) AS quantidade
            FROM extrato_efetivo_cache t{onde_evolucao}
            GROUP BY t.{mes_campo_gasto}
            ORDER BY t.{mes_campo_gasto}
            """,
            params_evolucao,
        ):
            evolucao[linha["mes_ref"]] = {
                "entradas": 0.0, "qtdEntradas": 0,
                "saidas": float(linha["saidas"] or 0),
                "quantidade": int(linha["quantidade"] or 0),
            }
        for linha in conn.execute(
            f"""
            SELECT t.competencia_entrada AS mes_ref,
                   COALESCE(SUM(ABS(t.valor)), 0) AS entradas,
                   COUNT(*) AS qtd
            FROM extrato_efetivo_cache t{onde_evolucao_entradas}
            {'AND' if onde_evolucao_entradas else 'WHERE'}
              t.incluida = 1 AND t.tipo = 'CREDIT'
              AND t.entrada_considerada = 1
              AND t.competencia_entrada IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM extrato_entradas_exclusoes x
                              WHERE x.transacao_id = t.transacao_id)
            GROUP BY t.competencia_entrada
            ORDER BY t.competencia_entrada
            """,
            params_evolucao_entradas,
        ):
            item = evolucao.setdefault(
                linha["mes_ref"], {"entradas": 0.0, "qtdEntradas": 0, "saidas": 0.0, "quantidade": 0}
            )
            item["entradas"] = float(linha["entradas"] or 0)
            item["qtdEntradas"] = int(linha["qtd"] or 0)

        # Projecao de entradas: mes corrente ou futuro cujas 4 entradas de
        # salario esperadas ainda nao aconteceram todas usa a media do ano em
        # vez do valor parcial recebido ate agora -- ver ENTRADAS_ESPERADAS_POR_MES.
        # So faz sentido na visao sem filtro estreito (conta/cartao/categoria
        # etc.): filtrado, "entradas" pode nem existir na fatia escolhida, e
        # tirar media dali so inventaria numero.
        mes_atual = datetime.now().strftime("%Y-%m")
        sem_filtro_estreito = not any(
            filtros.get(k) for k in ("conta", "cartao", "categoria", "status", "tipo", "busca")
        )
        # Filtrar por UM cartao (sem mais nenhum filtro estreito) ainda deixa
        # a projecao de fatura fazer sentido -- so precisa ser a fatia daquele
        # cartao, nao o total de todos. Categoria/status/tipo/busca nao tem
        # equivalente projetavel, entao esses continuam bloqueando.
        apenas_cartao_filtrado = bool(filtros.get("cartao")) and not any(
            filtros.get(k) for k in ("conta", "categoria", "status", "tipo", "busca")
        )
        # A projecao (fatura aberta + parcelas que ainda vao vencer) so existe
        # no modo "fatura" -- no modo "mes" a tela mostra a data real da
        # compra, e uma parcela que ainda nao foi cobrada nao tem "data de
        # compra futura" nenhuma pra mostrar.
        permite_projecao_cartoes = (
            (sem_filtro_estreito or apenas_cartao_filtrado) and modo == "fatura"
        )
        # Buscar por texto TEM equivalente projetavel: os itens projetados
        # (parcelas futuras) tem descricao real, dai da pra casar contra o
        # termo buscado mesmo sem mes selecionado -- so bloqueia se algum
        # filtro sem equivalente (conta/categoria/status/tipo) tambem estiver
        # ativo.
        busca_termo = (filtros.get("busca") or "").strip().casefold()
        permite_projecao_busca = (
            bool(busca_termo) and modo == "fatura"
            and not any(filtros.get(k) for k in ("conta", "categoria", "status", "tipo"))
        )
        # Cadastrado manualmente na tela de Entradas ("valor esperado"), tem
        # prioridade sobre a media: quem cadastrou sabe quanto espera receber
        # melhor do que uma media de meses passados, principalmente logo apos
        # um reajuste de salario, quando a media ainda carrega o valor antigo.
        valor_esperado_manual = cam.valor_esperado_entradas(conn)

        def media_entradas_completas(ano: str, excluir: str | None = None) -> float | None:
            valores = [
                it["entradas"] for m, it in evolucao.items()
                if m[:4] == ano and m != excluir
                   and it["qtdEntradas"] >= ENTRADAS_ESPERADAS_POR_MES
            ]
            return sum(valores) / len(valores) if valores else None

        def projetar(mes_ref: str, entradas_reais: float, qtd: int,
                     tem_atividade: bool) -> tuple[float, float, bool, str]:
            """(entradas a usar, entradas realmente recebidas, projetou?, origem)."""
            if (sem_filtro_estreito and mes_ref >= mes_atual
                    and qtd < ENTRADAS_ESPERADAS_POR_MES and tem_atividade):
                if valor_esperado_manual is not None:
                    return valor_esperado_manual, entradas_reais, True, "manual"
                media = media_entradas_completas(mes_ref[:4], excluir=mes_ref)
                if media is not None:
                    return media, entradas_reais, True, "media"
            return entradas_reais, entradas_reais, False, ""

        # Mes sem nenhuma transacao ainda pode ter fatura de cartao projetada:
        # uma compra parcelada em 5/12 so vira linha no banco quando a Pluggy
        # realmente cobra aquela parcela -- as parcelas 6..12 nao existem como
        # transacao enquanto o mes delas nao chega. cartoes_payload ja sabe
        # projetar isso (mesma conta de Cartoes Pluggy), entao reaproveita em
        # vez de duplicar a logica de fatura aberta + parcelamento aqui. So
        # PREENCHE um mes que a view não achou nada; nunca sobrescreve um mes
        # com dado real, mesmo que os dois numeros um dia divirjam.
        # Vai ano a ano enquanto houver fatura projetada: um parcelamento em
        # 12x contratado em dezembro estoura o ano corrente, e a tira precisa
        # continuar mostrando enquanto existir fatura para mostrar. Para no
        # primeiro ano sem nada -- e o cap existe para o loop nunca depender
        # so de dado bem-comportado.
        projecoes_cartoes: list[dict[str, Any]] = []
        if permite_projecao_cartoes or permite_projecao_busca:
            ano_base = int(mes_atual[:4])
            for ano_projecao in range(ano_base, ano_base + ANOS_PROJECAO_MAX):
                try:
                    payload_ano = cartoes_payload(ano_projecao, "fatura")
                except Exception:
                    break
                # O ano corrente entra sempre (tem o historico real); os
                # seguintes so se de fato sobrou parcela caindo neles.
                if ano_projecao > ano_base and not any(
                    total > 0 for total in payload_ano["totalMes"]
                ):
                    break
                projecoes_cartoes.append(payload_ano)

        def _valores_do_ano(payload: dict[str, Any]) -> list[float]:
            """Fatia da projecao que corresponde ao filtro de cartao atual.

            Sem filtro: soma todos os cartoes (totalMes ja vem pronto). Com
            filtro de UM cartao, so a fatia dos cartoes daquele grupo -- mesma
            resolucao banco->contas que o _onde usa, pra bater com o que a
            tela mostra quando filtra por esse cartao.
            """
            if sem_filtro_estreito:
                return payload["totalMes"]
            ids_alvo = grupos_cartoes.get(filtros["cartao"]) or {filtros["cartao"]}
            return [
                sum(
                    payload["valores"].get(cartao["id"], [0.0] * 12)[indice]
                    for cartao in payload["cartoes"]
                    if cartao["id"] in ids_alvo
                )
                for indice in range(12)
            ]

        if permite_projecao_cartoes:
            for payload_ano in projecoes_cartoes:
                ano_projecao = payload_ano["ano"]
                for indice, total in enumerate(_valores_do_ano(payload_ano)):
                    if total <= 0:
                        continue
                    chave = f"{ano_projecao}-{indice + 1:02d}"
                    item = evolucao.get(chave)
                    if item and (item["quantidade"] or item["saidas"]):
                        continue
                    evolucao[chave] = {
                        "entradas": 0.0, "qtdEntradas": 0,
                        "saidas": float(total), "quantidade": 0,
                        "saidasEstimativa": True,
                    }

        # So a categoria-pai aparece aqui -- uma transacao na subcategoria
        # "Livraria e educacao" soma dentro de "Compras". A lista de
        # Transacoes e para varrer de relance; navegar a arvore com pai e
        # filho e o trabalho da tela de Categorias.
        por_categoria = [
            {
                "categoriaId": linha["categoria_id"],
                "categoria": linha["nome"],
                "cor": linha["cor"],
                "emoji": linha["emoji"] or "",
                "total": linha["total"],
                "quantidade": linha["quantidade"],
            }
            for linha in conn.execute(
                f"""
                SELECT raiz.id AS categoria_id, raiz.nome, raiz.cor, raiz.emoji,
                       SUM(ABS(t.valor)) AS total, COUNT(*) AS quantidade
                FROM extrato_efetivo_cache t
                LEFT JOIN extrato_categorias filho ON filho.id = t.categoria_id
                LEFT JOIN extrato_categorias raiz
                       ON raiz.id = COALESCE(filho.pai_id, t.categoria_id){onde}
                {'AND' if onde else 'WHERE'} t.tipo = 'DEBIT' AND t.incluida = 1
                GROUP BY raiz.id
                ORDER BY total DESC
                """,
                params,
            )
        ]

        linhas = conn.execute(
            f"""
            SELECT t.transacao_id, t.data, t.mes_ref, t.descricao, t.valor,
                   CASE WHEN t.tipo = 'DEBIT' THEN -ABS(t.valor)
                        ELSE ABS(t.valor) END AS valor_normalizado,
                   t.moeda, t.tipo, t.status,
                   t.categoria_original, t.categoria_id,
                   t.origem_categorizacao, t.incluida, t.motivo_exclusao,
                   t.entrada_considerada, t.editada_manualmente,
                   cat.nome AS categoria_nome, cat.cor AS categoria_cor,
                   cat.emoji AS categoria_emoji,
                   t.parcela_numero, t.parcela_total, t.fatura_id,
                   c.nome AS conta_nome, c.subtipo AS conta_subtipo,
                   t.conta_id
            FROM extrato_efetivo_cache t
            JOIN pluggy_contas c ON c.conta_id = t.conta_id
            LEFT JOIN extrato_categorias cat ON cat.id = t.categoria_id{onde}
            ORDER BY t.data DESC, t.ordem DESC, t.transacao_id
            LIMIT ? OFFSET ?
            """,
            [*params, limite, offset],
        ).fetchall()

        # Itens projetados (parcelas futuras de cartao) que caem no filtro
        # atual, so nos meses que nao tem nenhuma transacao real -- nunca
        # some ou substitui dado real, so preenche o vazio, igual a projecao
        # acima. Sem periodo selecionado (ex.: busca por texto "Olympikus" em
        # Todo periodo) varre os 12 meses; com periodo, so o que cai dentro.
        transacoes_extra: list[dict[str, Any]] = []
        por_categoria_extra: list[dict[str, Any]] = []
        pares = []
        if (
            (permite_projecao_cartoes or permite_projecao_busca)
            and projecoes_cartoes and offset == 0
        ):
            for payload_ano in projecoes_cartoes:
                if filtros.get("cartao"):
                    ids_alvo_itens = (
                        grupos_cartoes.get(filtros["cartao"]) or {filtros["cartao"]}
                    )
                else:
                    ids_alvo_itens = {
                        cartao["id"] for cartao in payload_ano["cartoes"]
                    }
                ano_projecao_itens = payload_ano["ano"]
                for cartao in payload_ano["cartoes"]:
                    if cartao["id"] not in ids_alvo_itens:
                        continue
                    for item in payload_ano["itens"].get(cartao["id"], []):
                        mes_ref_item = f"{ano_projecao_itens}-{item['mes']:02d}"
                        if mes_de and mes_ref_item < mes_de:
                            continue
                        if mes_ate and mes_ref_item > mes_ate:
                            continue
                        if busca_termo and busca_termo not in item["descricao"].casefold():
                            continue
                        # Mes com transacao real (mesmo filtro de busca/cartao
                        # aplicado na tira de evolucao) ja mostra os itens de
                        # verdade -- nao injeta projecao por cima.
                        existente = evolucao.get(mes_ref_item)
                        if existente and existente.get("quantidade"):
                            continue
                        pares.append((cartao, item, mes_ref_item))

        if pares:
            base_ids = sorted({item["transacaoBaseId"] for _, item, _ in pares})
            categorias_base: dict[str, dict[str, Any]] = {}
            if base_ids:
                marcadores_base = ", ".join("?" for _ in base_ids)
                for linha_cat in conn.execute(
                    f"""
                    SELECT t.transacao_id, t.categoria_id,
                           cat.nome AS categoria_nome,
                           cat.cor AS categoria_cor,
                           cat.emoji AS categoria_emoji,
                           raiz.id AS raiz_id, raiz.nome AS raiz_nome,
                           raiz.cor AS raiz_cor, raiz.emoji AS raiz_emoji
                    FROM extrato_efetivo_cache t
                    LEFT JOIN extrato_categorias cat ON cat.id = t.categoria_id
                    LEFT JOIN extrato_categorias raiz
                           ON raiz.id = COALESCE(cat.pai_id, t.categoria_id)
                    WHERE t.transacao_id IN ({marcadores_base})
                    """,
                    base_ids,
                ):
                    categorias_base[linha_cat["transacao_id"]] = {
                        "id": linha_cat["categoria_id"],
                        "nome": linha_cat["categoria_nome"] or "Outros",
                        "cor": linha_cat["categoria_cor"] or "#9ba1ab",
                        "emoji": linha_cat["categoria_emoji"] or "",
                        "raizId": linha_cat["raiz_id"],
                        "raizNome": linha_cat["raiz_nome"] or "Outros",
                        "raizCor": linha_cat["raiz_cor"] or "#9ba1ab",
                        "raizEmoji": linha_cat["raiz_emoji"] or "",
                    }
            agregados_categoria: dict[str, dict[str, Any]] = {}
            for cartao, item, mes_ref_item in pares:
                categoria = categorias_base.get(item["transacaoBaseId"]) or {
                    "id": None, "nome": "Outros", "cor": "#9ba1ab", "emoji": "",
                    "raizId": None, "raizNome": "Outros", "raizCor": "#9ba1ab",
                    "raizEmoji": "",
                }
                valor = round(float(item["valor"]), 2)
                transacoes_extra.append({
                    "id": (
                        f"projetada:{item['transacaoBaseId']}:"
                        f"{item['parcelaAtual']}"
                    ),
                    "data": f"{mes_ref_item}-01",
                    "mesRef": mes_ref_item,
                    "descricao": item["descricao"],
                    "valor": valor,
                    "valorNormalizado": -valor,
                    "moeda": "BRL",
                    "tipo": "DEBIT",
                    "status": "PROJECTED",
                    "categoriaOriginal": "",
                    "categoria": categoria,
                    "origemCategorizacao": "projetada",
                    "incluidaNosCalculos": True,
                    "motivoExclusao": None,
                    "entradaConsiderada": False,
                    "editadaManualmente": False,
                    "parcelaNumero": item["parcelaAtual"],
                    "parcelaTotal": item["parcelaTotal"],
                    "faturaId": "",
                    "contaId": cartao["id"],
                    "contaNome": NOMES_BANCOS.get(
                        banco_por_conta.get(cartao["id"]), cartao["nome"]
                    ),
                    "contaSubtipo": "CREDIT_CARD",
                    "projetada": True,
                })
                chave_categoria = categoria["raizId"] or "outros"
                agregado = agregados_categoria.setdefault(chave_categoria, {
                    "categoriaId": categoria["raizId"],
                    "categoria": categoria["raizNome"],
                    "cor": categoria["raizCor"],
                    "emoji": categoria["raizEmoji"],
                    "total": 0.0,
                    "quantidade": 0,
                })
                agregado["total"] += valor
                agregado["quantidade"] += 1
            transacoes_extra.sort(key=lambda t: t["data"], reverse=True)
            por_categoria_extra = sorted(
                agregados_categoria.values(),
                key=lambda item: item["total"],
                reverse=True,
            )

    transacoes = [
        {
            "id": linha["transacao_id"],
            "data": linha["data"][:10],
            "mesRef": linha["mes_ref"],
            "descricao": linha["descricao"],
            "valor": linha["valor"],
            "valorNormalizado": linha["valor_normalizado"],
            "moeda": linha["moeda"],
            "tipo": linha["tipo"],
            "status": linha["status"],
            "categoriaOriginal": linha["categoria_original"] or "",
            "categoria": {
                "id": linha["categoria_id"],
                "nome": linha["categoria_nome"] or "Outros",
                "cor": linha["categoria_cor"] or "#9ba1ab",
                "emoji": linha["categoria_emoji"] or "",
            },
            "origemCategorizacao": linha["origem_categorizacao"],
            "incluidaNosCalculos": bool(linha["incluida"]),
            "motivoExclusao": linha["motivo_exclusao"],
            "entradaConsiderada": bool(linha["entrada_considerada"]),
            "editadaManualmente": bool(linha["editada_manualmente"]),
            "parcelaNumero": linha["parcela_numero"],
            "parcelaTotal": linha["parcela_total"],
            "faturaId": linha["fatura_id"],
            "contaId": linha["conta_id"],
            "contaNome": NOMES_BANCOS.get(
                banco_por_conta.get(linha["conta_id"]),
                apelidos.get(linha["conta_id"], linha["conta_nome"]),
            ),
            "contaSubtipo": linha["conta_subtipo"],
        }
        for linha in linhas
    ]

    if transacoes_extra:
        transacoes = transacoes_extra + transacoes
    if por_categoria_extra:
        # Mescla por categoria: com mes unico selecionado o real ja vem
        # vazio (mes sem transacao), mas numa busca sem mes o real pode ter
        # entradas de outros meses -- soma em vez de substituir.
        por_categoria_mapa = {item["categoriaId"]: dict(item) for item in por_categoria}
        for extra in por_categoria_extra:
            chave = extra["categoriaId"]
            if chave in por_categoria_mapa:
                por_categoria_mapa[chave]["total"] += extra["total"]
                por_categoria_mapa[chave]["quantidade"] += extra["quantidade"]
            else:
                por_categoria_mapa[chave] = dict(extra)
        por_categoria = sorted(
            por_categoria_mapa.values(), key=lambda item: item["total"], reverse=True
        )

    saidas = float(resumo["saidas"])
    saidas_estimativa = False
    if transacoes_extra and not mes_filtro:
        # Periodo com mais de um mes (ou nenhum): os itens projetados que caem
        # em meses futuros (sem transacao real ainda) somam por cima do que a
        # consulta real ja contou -- cada um so entrou porque aquele mes
        # especifico estava vazio, ver o filtro de "evolucao" acima.
        saidas += sum(item["valor"] for item in transacoes_extra)
        saidas_estimativa = True
    # Mesma projecao de fatura de cartao da serie, para o mes unico
    # selecionado no filtro -- sem isto, escolher outubro no seletor mostraria
    # vazio mesmo com a tira de evolucao ja mostrando a fatura projetada.
    if permite_projecao_cartoes and mes_filtro and not (resumo["quantidade"] or saidas):
        projetado = evolucao.get(mes_filtro)
        if projetado and projetado.get("saidasEstimativa"):
            saidas = float(projetado["saidas"])
            saidas_estimativa = True

    # A mesma projecao da serie, para o mes unico selecionado no filtro: sem
    # isto, escolher setembro no seletor mostraria a mesma queda que a tira de
    # evolucao ja nao mostra mais.
    entradas_usar, entradas_recebidas, entradas_estimativa, entradas_origem = projetar(
        mes_filtro, float(entrada_total), entrada_qtd,
        bool(resumo["quantidade"] or saidas),
    )

    total_filtrado = resumo["no_filtro"] + len(transacoes_extra)
    return {
        "total": total_geral,
        "totalFiltrado": total_filtrado,
        "resumo": {
            "entradas": entradas_usar,
            "entradasRecebidas": entradas_recebidas,
            "entradasEstimativa": entradas_estimativa,
            "entradasOrigem": entradas_origem,
            "saidas": saidas,
            "saidasEstimativa": saidas_estimativa,
            "resultado": entradas_usar - saidas,
            "quantidade": resumo["quantidade"],
            "ignoradas": resumo["ignoradas"],
        },
        "porCategoria": por_categoria,
        "evolucaoMensal": _serie_com_projecao(evolucao, projetar),
        "transacoes": transacoes,
        "limite": limite,
        "offset": offset,
        "temMais": offset + len(transacoes) < total_filtrado,
    }
