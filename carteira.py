"""Carteira-alvo e distribuição de aportes.

A carteira que a assessoria monta vive numa planilha: fundo, CNPJ, percentual
e um punhado de características. Cadastrar isso à mão duas vezes não faz
sentido -- nome, classificação Anbima, aporte mínimo, liquidez e restrição a
investidor qualificado já estão nos catálogos que `fundos.py` mantém aqui.
Então o cadastro pede CNPJ e percentual; o resto vem preenchido e pode ser
corrigido por cima, porque o catálogo às vezes discorda da planilha (aporte
mínimo é o caso mais comum) e porque FIDC e fundo restrito nem aparecem no
catálogo público.

A distribuição segue os percentuais informados. Quando a fatia de um fundo
não alcança o aporte mínimo dele, a linha é marcada com quanto falta em vez
de a conta ser refeita por trás: quem decide juntar com o mês seguinte ou
aportar em menos fundos é quem investe.
"""

from __future__ import annotations

import math
from uuid import uuid4
from datetime import date, datetime, timedelta
from typing import Any

import banco as fin
import fundos

SCHEMA = """
CREATE TABLE IF NOT EXISTS carteira_alvo (
  cnpj TEXT PRIMARY KEY,
  perfil TEXT NOT NULL DEFAULT 'conservador',
  ordem INTEGER NOT NULL DEFAULT 0,
  percentual REAL NOT NULL DEFAULT 0,
  nome TEXT NOT NULL DEFAULT '',
  anbima TEXT NOT NULL DEFAULT '',
  aporte_minimo REAL,
  dias_resgate INTEGER,
  qualificado TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT ''
);
"""

# Colunas de texto livre que a tela já não mostra. Ficam listadas para a
# migração poder apagá-las de bancos criados antes.
REMOVIDAS = ("volatilidade", "taxas", "corretoras", "equivalente_xp")
LIMITE_TEXTO = 120
LIMITE_FUNDOS = 60
def validar_perfil(perfil, permitidos=None):
    if permitidos is None:
        with fin.connect() as conn:
            garantir_tabelas(conn)
            permitidos = {r[0] for r in conn.execute("SELECT id FROM carteira_abas")}
    if perfil not in permitidos:
        raise ValueError("Aba não encontrada.")
    return perfil

def validar_porcentagem(valor):
    try:
        n = float(valor)
    except (TypeError, ValueError):
        raise ValueError("Porcentagem deve ficar entre 0 e 100.")
    if not math.isfinite(n) or not 0 <= n <= 100:
        raise ValueError("Porcentagem deve ficar entre 0 e 100.")
    return round(n, 2)


def garantir_tabelas(conn=None) -> None:
    if conn is not None:
        conn.executescript(SCHEMA)
        _remover_colunas(conn)
        return
    with fin.connect() as conexao:
        conexao.executescript(SCHEMA)
        _remover_colunas(conexao)
        conexao.commit()


def _remover_colunas(conn) -> None:
    """Apaga as colunas de texto livre de um banco criado antes."""
    existentes = {linha[1] for linha in conn.execute("PRAGMA table_info(carteira_alvo)")}
    if "perfil" not in existentes:
        conn.execute("ALTER TABLE carteira_alvo ADD COLUMN perfil TEXT NOT NULL DEFAULT 'conservador'")
    conn.execute("CREATE TABLE IF NOT EXISTS carteira_config (id INTEGER PRIMARY KEY CHECK(id=1), conservador REAL NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO carteira_config VALUES (1,100)")
    conn.execute("CREATE TABLE IF NOT EXISTS carteira_abas (id TEXT PRIMARY KEY, nome TEXT NOT NULL, percentual REAL NOT NULL DEFAULT 0, ordem INTEGER NOT NULL)")
    if not conn.execute("SELECT 1 FROM carteira_abas LIMIT 1").fetchone():
        percentual = conn.execute("SELECT conservador FROM carteira_config WHERE id=1").fetchone()[0]
        conn.executemany("INSERT INTO carteira_abas VALUES (?,?,?,?)", [
            ("conservador", "Conservador", percentual, 0), ("arrojado", "Arrojado", round(100-percentual, 2), 1)])
    for coluna in REMOVIDAS:
        if coluna in existentes:
            conn.execute(f"ALTER TABLE carteira_alvo DROP COLUMN {coluna}")
    # Fundo que fechou para novos aportes: a posição continua aplicada, mas o
    # dinheiro novo da vaga vai para o fundo que o substituiu.
    if "fechado_em" not in existentes:
        conn.execute("ALTER TABLE carteira_alvo ADD COLUMN fechado_em TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS carteira_substituicoes ("
        " origem_cnpj TEXT NOT NULL, destino_cnpj TEXT NOT NULL, perfil TEXT NOT NULL,"
        " data TEXT NOT NULL, motivo TEXT NOT NULL DEFAULT '',"
        " percentual REAL NOT NULL DEFAULT 0, destino_novo INTEGER NOT NULL DEFAULT 0,"
        " criado_em TEXT NOT NULL, PRIMARY KEY (origem_cnpj, destino_cnpj))")
    # Posições que não são fundos (CDB e afins): a aba onde contam, a meta
    # em % da aba e se ficam fora das contas (dado errado vindo do banco).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS carteira_posicoes ("
        " investimento_id TEXT PRIMARY KEY, perfil TEXT NOT NULL DEFAULT '',"
        " percentual REAL NOT NULL DEFAULT 0, ignorado INTEGER NOT NULL DEFAULT 0,"
        " atualizado_em TEXT NOT NULL DEFAULT '')")
    # Meta em reais de cada grupo de CDBs (um grupo por emissor).
    conn.execute("CREATE TABLE IF NOT EXISTS carteira_metas_cdb ("
                 " grupo TEXT PRIMARY KEY, meta REAL NOT NULL, atualizado_em TEXT NOT NULL DEFAULT '')")


# ------------------------------------------------------------- preenchimento

def _catalogo(conn, cnpj: str) -> dict[str, Any]:
    """O que os catálogos locais sabem deste CNPJ.

    Passa pela mesma ponte fundo/classe da tela de identificação: o CNPJ da
    planilha costuma ser o do fundo, e o catálogo do BTG lista a classe.
    """
    resolvido = fundos._resolver_cnpj(conn, cnpj, fundos.formatar_cnpj(cnpj))
    btg = resolvido.get("btg") or {}
    cvm = resolvido.get("cvm") or {}
    publico = btg.get("publico") or ""
    restrito = ("qualificado" in publico.lower() or "profissional" in publico.lower()) \
        and "não qualificado" not in publico.lower() and "(não" not in publico.lower()
    return {
        "nome": btg.get("nome") or cvm.get("denominacao") or "",
        "anbima": cvm.get("classeAnbima") or cvm.get("classificacao") or "",
        "aporteMinimo": btg.get("aplicacaoMinima"),
        "diasResgate": btg.get("diasResgate"),
        "qualificado": ("sim" if restrito else "nao") if publico else "",
        "url": btg.get("url") or "",
        "urlComo": btg.get("como") or "",
        "situacao": cvm.get("situacao") or "",
        "noCatalogo": bool(btg),
    }


def _numero(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return round(float(valor), 2)
    except (TypeError, ValueError):
        return None


def _texto(valor: Any) -> str:
    return str(valor or "").strip()[:LIMITE_TEXTO]


# -------------------------------------------------------------- distribuição

def _peso(item: dict[str, Any]) -> float:
    """Quanto este fundo pesa na divisão do aporte.

    Fundo marcado como IQ (restrito a investidor qualificado) pesa zero: não
    dá para aportar nele, então a fatia dele é diluída entre os outros na
    proporção que eles já tinham. O percentual cadastrado não muda -- o que
    muda é a base do cálculo.
    """
    if item.get("qualificado") == "sim" or item.get("fechadoEm"):
        return 0.0
    return float(item["percentual"] or 0)


def _distribuir(itens: list[dict[str, Any]], aporte: float) -> None:
    """Reparte o aporte entre os fundos, em centavos, sem sobra nem estouro.

    Os pesos são normalizados pela própria soma: se a planilha somar 90% ou
    110%, ou se um fundo IQ sair da conta, o aporte informado continua sendo
    distribuído inteiro e a tela avisa o que aconteceu. Arredondar cada fatia
    para baixo deixa centavos de resto, que vão para as maiores fatias -- um
    por fundo, até acabar, para o total bater exatamente com o aporte.
    """
    pesos = [_peso(item) for item in itens]
    soma = sum(pesos)
    centavos_totais = int(round(max(aporte, 0) * 100))
    for item in itens:
        item["aporte"] = 0.0
        item["elegivel"] = item.get("qualificado") != "sim" and not item.get("fechadoEm")
        # Só é "redistribuído" quem tinha alocação e ficou de fora por ser IQ.
        item["redistribuido"] = (item.get("qualificado") == "sim" and not item.get("fechadoEm")
                                 and float(item["percentual"] or 0) > 0)
    if not itens or soma <= 0 or centavos_totais <= 0:
        return
    centavos = [int(centavos_totais * peso / soma) for peso in pesos]
    resto = centavos_totais - sum(centavos)
    maiores = sorted(range(len(itens)), key=lambda i: (-pesos[i], itens[i]["ordem"]))
    # Centavo de resto só cai em quem participa da divisão.
    elegiveis = [i for i in maiores if pesos[i] > 0]
    for posicao in range(resto):
        centavos[elegiveis[posicao % len(elegiveis)]] += 1
    for item, valor in zip(itens, centavos):
        item["aporte"] = round(valor / 100, 2)


# ------------------------------------------------------------- linhagem

def _posicoes_por_cnpj(conn) -> dict[str, dict[str, Any]]:
    """Posição atual e primeiro aporte de cada fundo, pelo CNPJ.

    O CNPJ de uma posição Pluggy é o `code` do raw_json. Mesmas regras da
    tela de Investimentos: sem conexões arquivadas, uma cópia por
    investimento (a importação mais recente) e só posições ativas no saldo.
    """
    import json
    import investimentos
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pluggy_investimentos'").fetchone():
        return {}
    def cnpj_de(linha) -> str:
        try:
            codigo = fundos.digitos((json.loads(linha["raw_json"] or "{}") or {}).get("code") or "")
        except (ValueError, TypeError):
            codigo = ""
        return codigo if len(codigo) == 14 else ""

    arquivados = investimentos._itens_arquivados(conn)
    linhas = list(conn.execute(
        "SELECT i.*, (SELECT MIN(substr(m.data,1,10)) FROM pluggy_investimento_movimentos m "
        "WHERE m.investimento_chave=i.investimento_chave AND m.tipo='BUY') AS primeiro_aporte "
        "FROM pluggy_investimentos i ORDER BY importado_em DESC, investimento_chave"))
    saida: dict[str, dict[str, Any]] = {}
    # Primeiro aporte: TODAS as conexões, inclusive as arquivadas. Reconectar
    # o banco cria posições novas cujos movimentos começam na reconexão; o
    # histórico do mesmo fundo (mesmo CNPJ) está na conexão antiga.
    for p in linhas:
        codigo = cnpj_de(p)
        if not codigo or not p["primeiro_aporte"]:
            continue
        s = saida.setdefault(codigo, {"posicao": 0.0, "primeiroAporte": ""})
        if not s["primeiroAporte"] or p["primeiro_aporte"] < s["primeiroAporte"]:
            s["primeiroAporte"] = p["primeiro_aporte"]
    # Posição: só conexões ativas, uma cópia por investimento.
    unicas: dict[str, Any] = {}
    for p in linhas:
        if p["item_id"] not in arquivados:
            unicas.setdefault(p["investimento_id"], p)
    for p in unicas.values():
        codigo = cnpj_de(p)
        if not codigo:
            continue
        s = saida.setdefault(codigo, {"posicao": 0.0, "primeiroAporte": ""})
        if p["status"] == "ACTIVE":
            s["posicao"] = round(s["posicao"] + float(p["saldo_liquido"] or 0), 2)
    return saida


def _vagas(itens: list[dict[str, Any]], arestas: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Vaga = cadeia de substituições, do primeiro fundo até o aberto.

    Devolve {cnpj do fundo da ponta: [cnpjs da cadeia, do mais antigo ao
    atual]}. Fundo sem substituição nenhuma é uma vaga de um fundo só.
    """
    proximo = {a["origem"]: a["destino"] for a in arestas}
    tem_origem = {a["destino"] for a in arestas}
    existentes = {i["cnpj"] for i in itens}
    vagas: dict[str, list[str]] = {}
    for cnpj in existentes:
        if cnpj in tem_origem:
            continue
        cadeia, atual, vistos = [cnpj], cnpj, {cnpj}
        while atual in proximo and proximo[atual] not in vistos:
            atual = proximo[atual]
            vistos.add(atual)
            cadeia.append(atual)
        vagas[cadeia[-1]] = cadeia
    return vagas


def _distribuir_rebalanceando(lista: list[dict[str, Any]], aporte: float,
                              fixo_atual: float = 0.0, fixo_pct: float = 0.0) -> dict[str, float]:
    """Reparte o aporte da aba aproximando cada vaga do seu % alvo, só aportando.

    Alvo de uma vaga = % dela × (tudo o que a aba já tem + o aporte). O
    dinheiro vai primeiro para as vagas abaixo do alvo, proporcional ao que
    falta a cada uma; se sobrar depois de zerar os déficits, a sobra segue o
    % alvo (a `_distribuir` de sempre). Nunca há valor negativo: rebalancear
    aqui é só decidir onde pôr o dinheiro novo, sem resgate. Quem não recebe
    (fechado, IQ) fica fora da conta do alvo.

    `fixo_atual` e `fixo_pct` são as posições da aba que não recebem aporte
    (CDBs): o valor delas entra no total e a meta delas ocupa parte do alvo.
    """
    elegiveis = [i for i in lista if _peso(i) > 0]
    for item in lista:
        item["aporte"] = 0.0
        item["elegivel"] = item.get("qualificado") != "sim" and not item.get("fechadoEm")
        item["redistribuido"] = (item.get("qualificado") == "sim" and not item.get("fechadoEm")
                                 and float(item["percentual"] or 0) > 0)
    total_atual = round(sum(i.get("atualVaga", 0.0) for i in lista if not i.get("fechadoEm")) + fixo_atual, 2)
    soma_pesos = sum(_peso(i) for i in elegiveis)
    denominador = soma_pesos + max(0.0, fixo_pct)
    resumo = {"totalAtual": total_atual, "deficit": 0.0, "faltaParaAlvo": 0.0}
    centavos_totais = int(round(max(aporte, 0) * 100))
    if not elegiveis or soma_pesos <= 0:
        return resumo
    base = total_atual + centavos_totais / 100
    deficits = []
    for i in elegiveis:
        i["alvoVaga"] = round(base * _peso(i) / denominador, 2)
        deficits.append(max(0.0, i["alvoVaga"] - i.get("atualVaga", 0.0)))
    soma_def = sum(deficits)
    resumo["deficit"] = round(soma_def, 2)
    if centavos_totais <= 0:
        resumo["faltaParaAlvo"] = round(soma_def, 2)
        return resumo
    if soma_def > 0:
        parte = min(centavos_totais, int(round(soma_def * 100)))
        cent = [int(parte * d / soma_def) for d in deficits]
        ordem = sorted(range(len(elegiveis)), key=lambda k: -deficits[k])
        for k in range(parte - sum(cent)):
            cent[ordem[k % len(ordem)]] += 1
    else:
        cent = [0] * len(elegiveis)
    sobra = centavos_totais - sum(cent)
    if sobra > 0:
        copia = [dict(i) for i in elegiveis]
        _distribuir(copia, sobra / 100)
        cent = [c + int(round(x["aporte"] * 100)) for c, x in zip(cent, copia)]
    for i, c in zip(elegiveis, cent):
        i["aporte"] = round(c / 100, 2)
    depois = [max(0.0, i["alvoVaga"] - i.get("atualVaga", 0.0) - i["aporte"]) for i in elegiveis]
    resumo["faltaParaAlvo"] = round(sum(depois), 2)
    return resumo


def _hoje() -> date:
    return date.today()


def _data_resgate(dias: Any) -> str:
    """Quando o dinheiro cai, resgatando hoje.

    O D+N dos fundos é contado em dias corridos, mas liquidação financeira não
    acontece em fim de semana: a data rola para a segunda-feira seguinte. Não
    conhecemos feriados, então um feriado no caminho atrasa um dia a mais do
    que aparece aqui.
    """
    try:
        numero = int(dias)
    except (TypeError, ValueError):
        return ""
    if numero < 0:
        return ""
    data = _hoje() + timedelta(days=numero)
    while data.weekday() >= 5:
        data += timedelta(days=1)
    return data.isoformat()


def _marcar_minimos(itens: list[dict[str, Any]]) -> None:
    for item in itens:
        minimo = item.get("aporteMinimo")
        falta = None
        if minimo and item["aporte"] > 0 and item["aporte"] < minimo:
            falta = round(minimo - item["aporte"], 2)
        item["abaixoDoMinimo"] = falta is not None
        item["falta"] = falta


# ----------------------------------------------------------------- consulta

@fin.escopo_leitura
def _outras_posicoes(conn) -> list[dict[str, Any]]:
    """Posições ativas sem CNPJ de fundo (CDB, LCI, tesouro...), uma por investimento."""
    import json
    import investimentos
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='pluggy_investimentos'").fetchone():
        return []
    arquivados = investimentos._itens_arquivados(conn)
    config = {r["investimento_id"]: r for r in conn.execute("SELECT * FROM carteira_posicoes")}
    vistos, saida = set(), []
    for p in conn.execute("SELECT * FROM pluggy_investimentos ORDER BY importado_em DESC, investimento_chave"):
        if p["item_id"] in arquivados or p["investimento_id"] in vistos:
            continue
        vistos.add(p["investimento_id"])
        if p["status"] != "ACTIVE":
            continue
        try:
            codigo = fundos.digitos((json.loads(p["raw_json"] or "{}") or {}).get("code") or "")
        except (ValueError, TypeError):
            codigo = ""
        if len(codigo) == 14:
            continue                      # fundo: já entra pela carteira_alvo
        c = config.get(p["investimento_id"])
        saida.append({
            "id": p["investimento_id"],
            "nome": p["nome"] or p["subtipo"] or "Investimento",
            "tipo": p["subtipo"] or p["tipo"],
            "emissor": p["emissor"],
            "vencimento": (p["vencimento"] or "")[:10],
            "taxa": p["taxa"], "tipoTaxa": p["tipo_taxa"],
            "posicao": round(float(p["saldo_liquido"] or 0), 2),
            "perfil": c["perfil"] if c else "",
            "percentual": round(float(c["percentual"]), 4) if c else 0.0,
            "ignorado": bool(c["ignorado"]) if c else False,
        })
    saida.sort(key=lambda o: (o["vencimento"] or "9999", o["nome"]))
    return saida


def payload(aporte: float = 0) -> dict[str, Any]:
    aporte = _numero(aporte) or 0.0
    with fin.connect() as conn:
        garantir_tabelas(conn)
        fundos.garantir_tabelas(conn)
        itens = []
        for linha in conn.execute("SELECT * FROM carteira_alvo ORDER BY ordem, cnpj"):
            catalogo = _catalogo(conn, linha["cnpj"])
            itens.append({
                "cnpj": linha["cnpj"],
                "perfil": linha["perfil"],
                "cnpjFormatado": fundos.formatar_cnpj(linha["cnpj"]),
                "ordem": linha["ordem"],
                "percentual": round(float(linha["percentual"] or 0), 4),
                # O que a pessoa digitou manda; vazio significa "use o catálogo".
                "nome": linha["nome"] or catalogo["nome"] or fundos.formatar_cnpj(linha["cnpj"]),
                "nomeProprio": bool(linha["nome"]),
                "anbima": linha["anbima"] or catalogo["anbima"],
                "aporteMinimo": linha["aporte_minimo"] if linha["aporte_minimo"] is not None
                                else catalogo["aporteMinimo"],
                "aporteMinimoProprio": linha["aporte_minimo"] is not None,
                "diasResgate": linha["dias_resgate"] if linha["dias_resgate"] is not None
                               else catalogo["diasResgate"],
                "qualificado": linha["qualificado"] or catalogo["qualificado"],
                "url": catalogo["url"],
                "urlComo": catalogo["urlComo"],
                "noCatalogo": catalogo["noCatalogo"],
                "situacao": catalogo["situacao"],
                "fechadoEm": linha["fechado_em"] or "",
            })
        for item in itens:
            item["dataResgate"] = _data_resgate(item["diasResgate"])

        # Linhagem: posição de cada fundo, as substituições e as vagas. A vaga
        # soma a posição de todos os fundos da cadeia (o fechado continua
        # aplicado) e é o fundo aberto da ponta que recebe o dinheiro novo.
        posicoes = _posicoes_por_cnpj(conn)
        arestas = [{"origem": r["origem_cnpj"], "destino": r["destino_cnpj"], "perfil": r["perfil"],
                    "data": r["data"], "motivo": r["motivo"], "percentual": r["percentual"]}
                   for r in conn.execute("SELECT * FROM carteira_substituicoes ORDER BY data, origem_cnpj")]
        por_cnpj = {i["cnpj"]: i for i in itens}
        for item in itens:
            pos = posicoes.get(item["cnpj"], {})
            item["posicao"] = pos.get("posicao", 0.0)
            item["primeiroAporte"] = pos.get("primeiroAporte", "")
        vagas = _vagas(itens, arestas)
        for ponta, cadeia in vagas.items():
            atual = round(sum(por_cnpj[c]["posicao"] for c in cadeia if c in por_cnpj), 2)
            for c in cadeia:
                if c in por_cnpj:
                    por_cnpj[c]["vaga"] = ponta
            if ponta in por_cnpj:
                por_cnpj[ponta]["atualVaga"] = atual
                por_cnpj[ponta]["cadeia"] = cadeia
        abas = [dict(r) for r in conn.execute("SELECT * FROM carteira_abas ORDER BY ordem,id")]
        outros = _outras_posicoes(conn)
        fatias = [{"percentual": a["percentual"], "ordem": a["ordem"]} for a in abas]
        _distribuir(fatias, aporte)
        grupos = {}
        for aba, fatia in zip(abas, fatias):
            perfil, percentual = aba["id"], aba["percentual"]
            cents = int(round(fatia["aporte"] * 100))
            lista = [i for i in itens if i["perfil"] == perfil]
            fixos = [o for o in outros if o["perfil"] == perfil and not o["ignorado"]]
            rebalance = _distribuir_rebalanceando(
                lista, cents / 100,
                fixo_atual=sum(o["posicao"] for o in fixos),
                fixo_pct=sum(o["percentual"] for o in fixos))
            _marcar_minimos(lista)
            base_aba = rebalance["totalAtual"] + cents / 100
            for i in lista:
                if i.get("fechadoEm"):
                    continue
                atual_vaga = i.get("atualVaga", 0.0)
                i["pctAtual"] = (round(100 * atual_vaga / rebalance["totalAtual"], 2)
                                 if rebalance["totalAtual"] else 0.0)
                i["pctDepois"] = round(100 * (atual_vaga + i["aporte"]) / base_aba, 2) if base_aba else 0.0
            # CDBs: meta em reais sobre o total da aba depois do aporte e a
            # diferença (positivo = acima da meta, negativo = abaixo).
            soma_pct = sum(_peso(i) for i in lista) + sum(o["percentual"] for o in fixos)
            for o in fixos:
                o["pctAtual"] = (round(100 * o["posicao"] / rebalance["totalAtual"], 2)
                                 if rebalance["totalAtual"] else 0.0)
                o["meta"] = round(base_aba * o["percentual"] / soma_pct, 2) if soma_pct else 0.0
                o["diferenca"] = round(o["posicao"] - o["meta"], 2) if o["percentual"] else None
            soma_grupo = round(sum(i["percentual"] for i in lista) + sum(o["percentual"] for o in fixos), 4)
            total = round(sum(i["aporte"] for i in lista), 2)
            grupos[perfil] = {"nome": aba["nome"], "percentual": percentual, "aporte": cents / 100,
                "somaPercentual": soma_grupo, "somaFecha": not lista or abs(soma_grupo-100) < .005,
                "totalDistribuido": total, "naoDistribuido": round(cents/100-total, 2),
                "semElegivel": bool(lista) and all(_peso(i) <= 0 for i in lista),
                "abaixoDoMinimo": sum(i["abaixoDoMinimo"] for i in lista),
                "totalAtual": rebalance["totalAtual"],
                "faltaParaAlvo": rebalance["faltaParaAlvo"]}
        soma = round(sum(item["percentual"] for item in itens), 4)
        redistribuidos = [item for item in itens if item["redistribuido"]]
        return {
            "aporte": aporte,
            "perfis": grupos,
            "abas": abas,
            "itens": itens,
            "somaPercentual": soma,
            "somaFecha": not itens or abs(soma - 100) < 0.005,
            "totalDistribuido": round(sum(item["aporte"] for item in itens), 2),
            "abaixoDoMinimo": sum(1 for item in itens if item["abaixoDoMinimo"]),
            "foraDoCatalogo": sum(1 for item in itens if not item["noCatalogo"]),
            "redistribuidos": len(redistribuidos),
            "percentualRedistribuido": round(sum(i["percentual"] for i in redistribuidos), 4),
            # Carteira inteira marcada como IQ: não há para onde mandar o aporte.
            "semElegivel": bool(itens) and all(_peso(item) <= 0 for item in itens),
            "catalogoBtg": fundos.CATALOGO_BTG_TELA,
            # Para o grafo de linhagem: cada vaga com a cadeia de fundos.
            "vagas": [
                {"ponta": ponta, "perfil": por_cnpj[ponta]["perfil"] if ponta in por_cnpj else "",
                 "cadeia": cadeia, "atual": por_cnpj.get(ponta, {}).get("atualVaga", 0.0),
                 "percentual": por_cnpj.get(ponta, {}).get("percentual", 0.0),
                 "pctAtual": por_cnpj.get(ponta, {}).get("pctAtual"),
                 "aporte": por_cnpj.get(ponta, {}).get("aporte", 0.0)}
                for ponta, cadeia in vagas.items()
            ],
            "substituicoes": arestas,
            "outros": outros,
            "metasCdb": {r["grupo"]: r["meta"] for r in conn.execute("SELECT grupo, meta FROM carteira_metas_cdb")},
        }


# ------------------------------------------------------------------ escrita

def _conhecido(conn, cnpj: str) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM fundos_btg WHERE cnpj=?", (cnpj,)).fetchone()
        or conn.execute("SELECT 1 FROM fundos_cvm WHERE cnpj=?", (cnpj,)).fetchone())


def adicionar(cnpj: str, percentual: Any = 0, perfil: str = "conservador") -> dict[str, Any]:
    validar_perfil(perfil)
    digitos = fundos.digitos(cnpj)
    if len(digitos) != 14:
        raise ValueError("Informe um CNPJ completo, com 14 dígitos.")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        fundos.garantir_tabelas(conn)
        # Dígito verificador é indício de erro de digitação, não veredito: se o
        # CNPJ está em algum catálogo, o fundo existe e entra. Só recusamos o
        # que falha na conta e ninguém conhece.
        if not fundos.cnpj_valido(digitos) and not _conhecido(conn, digitos):
            raise ValueError("Os dígitos verificadores deste CNPJ não fecham e ele não "
                             "aparece nos catálogos. Confira o número.")
        if conn.execute("SELECT 1 FROM carteira_alvo WHERE cnpj=?", (digitos,)).fetchone():
            raise ValueError("Este fundo já está na carteira.")
        if conn.execute("SELECT COUNT(*) n FROM carteira_alvo").fetchone()["n"] >= LIMITE_FUNDOS:
            raise ValueError(f"A carteira já tem {LIMITE_FUNDOS} fundos.")
        proxima = conn.execute("SELECT COALESCE(MAX(ordem),0)+1 AS n FROM carteira_alvo").fetchone()["n"]
        conn.execute(
            "INSERT INTO carteira_alvo (cnpj,ordem,percentual,atualizado_em,perfil) VALUES (?,?,?,?,?)",
            (digitos, proxima, _numero(percentual) or 0.0,
             datetime.now().isoformat(timespec="seconds"), perfil))
        conn.commit()
    return payload()


def excluir(cnpj: str) -> dict[str, Any]:
    digitos = fundos.digitos(cnpj)
    with fin.connect() as conn:
        garantir_tabelas(conn)
        # Remover um fundo da linhagem apagaria a história da vaga (e o %
        # que voltaria num desfazer). Primeiro desfaz, depois remove.
        if conn.execute("SELECT 1 FROM carteira_substituicoes WHERE origem_cnpj=? OR destino_cnpj=?",
                        (digitos, digitos)).fetchone():
            raise ValueError("Este fundo faz parte de uma linhagem. Desfaça a substituição antes de removê-lo.")
        conn.execute("DELETE FROM carteira_alvo WHERE cnpj=?", (digitos,))
        conn.commit()
    return payload()


def salvar(itens: list[dict[str, Any]], aporte: float = 0, conservador=None, percentuais=None) -> dict[str, Any]:
    """Grava a carteira inteira: a tela manda a tabela como ela está.

    Campo que a tela não mostra também não é enviado, e o que não vem fica
    como está no banco. Antes a tela precisava devolver de volta tudo o que
    não exibia, e esquecer um campo o apagava sem aviso.
    """
    if not isinstance(itens, list):
        raise ValueError("Carteira inválida.")
    if len(itens) > LIMITE_FUNDOS:
        raise ValueError(f"A carteira aceita até {LIMITE_FUNDOS} fundos.")
    if conservador is not None:
        conservador = validar_porcentagem(conservador)
    agora = datetime.now().isoformat(timespec="seconds")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        abas_atuais = {r["id"]: r["percentual"] for r in conn.execute("SELECT id,percentual FROM carteira_abas")}
        if percentuais is not None:
            if not isinstance(percentuais, dict) or set(percentuais) != set(abas_atuais):
                raise ValueError("As abas mudaram. Recarregue a calculadora.")
            percentuais = {k: validar_porcentagem(v) for k, v in percentuais.items()}
            if abs(sum(percentuais.values()) - 100) > .005:
                raise ValueError("As porcentagens das abas devem somar 100%.")
        elif conservador is not None:
            if set(abas_atuais) != {"conservador", "arrojado"}:
                raise ValueError("Recarregue a calculadora para editar todas as abas.")
            percentuais = {"conservador": conservador, "arrojado": round(100-conservador, 2)}
        atuais = {linha["cnpj"]: dict(linha)
                  for linha in conn.execute("SELECT * FROM carteira_alvo")}
    linhas = []
    vistos = set()
    for ordem, item in enumerate(itens, start=1):
        digitos = fundos.digitos(item.get("cnpj"))
        if len(digitos) != 14:
            raise ValueError("Todo fundo da carteira precisa de um CNPJ completo.")
        if digitos in vistos:
            raise ValueError(f"CNPJ repetido na carteira: {fundos.formatar_cnpj(digitos)}")
        vistos.add(digitos)
        percentual = _numero(item.get("percentual")) or 0.0
        if percentual < 0 or percentual > 100:
            raise ValueError("Percentual de alocação deve ficar entre 0 e 100.")
        qualificado = _texto(item.get("qualificado")).lower()
        if qualificado not in ("", "sim", "nao"):
            qualificado = ""
        atual = atuais.get(digitos, {})
        if "nome" in item:
            nome = _texto(item.get("nomeProprio") and item.get("nome"))
        else:
            nome = atual.get("nome", "")
        anbima = _texto(item["anbima"]) if "anbima" in item else atual.get("anbima", "")
        if "aporteMinimo" in item:
            minimo = _numero(item["aporteMinimo"]) if item.get("aporteMinimoProprio") else None
        else:
            minimo = atual.get("aporte_minimo")
        linhas.append((
            digitos, ordem, percentual, nome, anbima, minimo,
            int(item["diasResgate"]) if str(item.get("diasResgate") or "").strip().isdigit() else None,
            qualificado, agora, validar_perfil(item.get("perfil", atual.get("perfil", "conservador")), abas_atuais),
            # A tela não edita o fechamento; ele só muda por substituir/desfazer.
            atual.get("fechado_em", "") or "",
        ))
    with fin.connect() as conn:
        garantir_tabelas(conn)
        # A tela é a fonte: quem saiu da lista sai da tabela.
        conn.execute("DELETE FROM carteira_alvo")
        conn.executemany(
            "INSERT INTO carteira_alvo (cnpj,ordem,percentual,nome,anbima,aporte_minimo,"
            "dias_resgate,qualificado,atualizado_em,perfil,fechado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?)", linhas)
        # Fundo que saiu da lista leva junto as substituições que o citam.
        conn.execute("DELETE FROM carteira_substituicoes WHERE origem_cnpj NOT IN (SELECT cnpj FROM carteira_alvo) "
                     "OR destino_cnpj NOT IN (SELECT cnpj FROM carteira_alvo)")
        if percentuais is not None:
            conn.executemany("UPDATE carteira_abas SET percentual=? WHERE id=?", [(v,k) for k,v in percentuais.items()])
        conn.commit()
    return payload(aporte)


def salvar_aba(nome, aba_id=None):
    nome = str(nome or "").strip()
    if not nome or len(nome) > 50:
        raise ValueError("Informe um nome de até 50 caracteres.")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        abas = list(conn.execute("SELECT id,nome FROM carteira_abas"))
        if aba_id is not None and aba_id not in {a["id"] for a in abas}:
            raise ValueError("Aba não encontrada.")
        if any(a["nome"].casefold() == nome.casefold() and a["id"] != aba_id for a in abas):
            raise ValueError("Já existe uma aba com esse nome.")
        if aba_id is None:
            aba_id = uuid4().hex
            ordem = conn.execute("SELECT COALESCE(MAX(ordem),0)+1 FROM carteira_abas").fetchone()[0]
            conn.execute("INSERT INTO carteira_abas VALUES (?,?,0,?)", (aba_id,nome,ordem))
        else:
            conn.execute("UPDATE carteira_abas SET nome=? WHERE id=?", (nome,aba_id))
        conn.commit()
    return {"ok": True, "id": aba_id}


def excluir_aba(aba_id: str):
    with fin.connect() as conn:
        garantir_tabelas(conn)
        abas = list(conn.execute("SELECT id FROM carteira_abas ORDER BY ordem"))
        if aba_id not in {a["id"] for a in abas}:
            raise ValueError("Aba não encontrada.")
        if len(abas) <= 1:
            raise ValueError("Mantenha pelo menos uma aba.")
        if conn.execute("SELECT 1 FROM carteira_alvo WHERE perfil=? LIMIT 1", (aba_id,)).fetchone():
            raise ValueError("Mova os fundos desta aba antes de excluí-la.")
        conn.execute("DELETE FROM carteira_abas WHERE id=?", (aba_id,))
        conn.commit()
    return {"ok": True}


# ------------------------------------------------------- fechar e substituir

def substituir(origem: str, destino: str, data: str = "", motivo: str = "") -> dict[str, Any]:
    """O fundo `origem` fechou para aportes; a vaga dele segue em `destino`.

    Uma ação só: marca o fechamento, cadastra o destino na mesma aba se ele
    ainda não estiver na carteira, passa o % alvo para ele e grava a ligação.
    Nada é resgatado -- a posição do fundo fechado continua somando na vaga.
    """
    origem, destino = fundos.digitos(origem), fundos.digitos(destino)
    if origem == destino:
        raise ValueError("Escolha um fundo diferente para substituir.")
    data = (str(data or "")[:10]) or date.today().isoformat()
    try:
        date.fromisoformat(data)
    except ValueError:
        raise ValueError("Data inválida.") from None
    motivo = _texto(motivo)
    with fin.connect() as conn:
        garantir_tabelas(conn)
        linha = conn.execute("SELECT * FROM carteira_alvo WHERE cnpj=?", (origem,)).fetchone()
        if not linha:
            raise ValueError("Fundo de origem não está na carteira.")
        if linha["fechado_em"]:
            raise ValueError("Este fundo já foi substituído.")
        perfil = linha["perfil"]
        existente = conn.execute("SELECT perfil, fechado_em FROM carteira_alvo WHERE cnpj=?", (destino,)).fetchone()
        if existente and existente["fechado_em"]:
            raise ValueError("O fundo novo está fechado para aportes.")
        if existente and existente["perfil"] != perfil:
            raise ValueError("O fundo novo já está em outra aba da carteira.")
    destino_novo = not existente
    if destino_novo:
        adicionar(destino, 0, perfil)
    with fin.connect() as conn:
        garantir_tabelas(conn)
        percentual = float(linha["percentual"] or 0)
        agora = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE carteira_alvo SET fechado_em=?, percentual=0, atualizado_em=? WHERE cnpj=?",
                     (data, agora, origem))
        conn.execute("UPDATE carteira_alvo SET percentual=percentual+?, atualizado_em=? WHERE cnpj=?",
                     (percentual, agora, destino))
        conn.execute(
            "INSERT INTO carteira_substituicoes (origem_cnpj, destino_cnpj, perfil, data, motivo, percentual,"
            " destino_novo, criado_em) VALUES (?,?,?,?,?,?,?,?)",
            (origem, destino, perfil, data, motivo, percentual, 1 if destino_novo else 0, agora))
        conn.commit()
    return payload()


def configurar_posicao(investimento_id: str, perfil: Any = None, percentual: Any = None,
                       ignorado: Any = None) -> dict[str, Any]:
    """Aba, meta e "ignorar" de uma posição que não é fundo (CDB e afins)."""
    investimento_id = str(investimento_id or "").strip()
    if not investimento_id:
        raise ValueError("Posição não informada.")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        atual = conn.execute("SELECT * FROM carteira_posicoes WHERE investimento_id=?",
                             (investimento_id,)).fetchone()
        novo = {"perfil": atual["perfil"] if atual else "",
                "percentual": float(atual["percentual"]) if atual else 0.0,
                "ignorado": int(atual["ignorado"]) if atual else 0}
        if perfil is not None:
            abas = {r[0] for r in conn.execute("SELECT id FROM carteira_abas")}
            if perfil and perfil not in abas:
                raise ValueError("Aba desconhecida.")
            novo["perfil"] = perfil or ""
        if percentual is not None:
            novo["percentual"] = validar_porcentagem(percentual)
        if ignorado is not None:
            novo["ignorado"] = 1 if ignorado else 0
        conn.execute(
            "INSERT INTO carteira_posicoes VALUES (?,?,?,?,?) ON CONFLICT(investimento_id) DO UPDATE SET "
            "perfil=excluded.perfil, percentual=excluded.percentual, ignorado=excluded.ignorado, "
            "atualizado_em=excluded.atualizado_em",
            (investimento_id, novo["perfil"], novo["percentual"], novo["ignorado"],
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    return payload()


def salvar_meta_cdb(grupo: str, meta: Any) -> dict[str, Any]:
    """Meta em reais de um grupo de CDBs; vazio ou zero apaga a meta."""
    grupo = str(grupo or "").strip()[:LIMITE_TEXTO]
    if not grupo:
        raise ValueError("Grupo não informado.")
    valor = _numero(meta)
    if valor is not None and (not math.isfinite(valor) or valor < 0):
        raise ValueError("A meta precisa ser um valor positivo.")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        if not valor:
            conn.execute("DELETE FROM carteira_metas_cdb WHERE grupo=?", (grupo,))
        else:
            conn.execute("INSERT INTO carteira_metas_cdb VALUES (?,?,?) ON CONFLICT(grupo) DO UPDATE SET "
                         "meta=excluded.meta, atualizado_em=excluded.atualizado_em",
                         (grupo, round(valor, 2), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    return payload()


def desfazer_substituicao(origem: str) -> dict[str, Any]:
    """Volta ao estado de antes: reabre a origem e devolve o % a ela.

    Só desfaz a ponta da cadeia (o destino não pode ter sido substituído
    depois). Se o destino entrou na carteira por causa da substituição e
    ficou sem % próprio, ele sai junto.
    """
    origem = fundos.digitos(origem)
    with fin.connect() as conn:
        garantir_tabelas(conn)
        aresta = conn.execute("SELECT * FROM carteira_substituicoes WHERE origem_cnpj=?", (origem,)).fetchone()
        if not aresta:
            raise ValueError("Este fundo não foi substituído.")
        destino = aresta["destino_cnpj"]
        if conn.execute("SELECT 1 FROM carteira_substituicoes WHERE origem_cnpj=?", (destino,)).fetchone():
            raise ValueError("Desfaça primeiro a substituição mais recente desta vaga.")
        percentual = float(aresta["percentual"] or 0)
        agora = datetime.now().isoformat(timespec="seconds")
        conn.execute("DELETE FROM carteira_substituicoes WHERE origem_cnpj=?", (origem,))
        conn.execute("UPDATE carteira_alvo SET fechado_em='', percentual=?, atualizado_em=? WHERE cnpj=?",
                     (percentual, agora, origem))
        resto = conn.execute("SELECT percentual FROM carteira_alvo WHERE cnpj=?", (destino,)).fetchone()
        sobra = round(float(resto["percentual"] or 0) - percentual, 4) if resto else 0
        if aresta["destino_novo"] and abs(sobra) < 0.005:
            conn.execute("DELETE FROM carteira_alvo WHERE cnpj=?", (destino,))
        elif resto:
            conn.execute("UPDATE carteira_alvo SET percentual=?, atualizado_em=? WHERE cnpj=?",
                         (max(0.0, sobra), agora, destino))
        conn.commit()
    return payload()
