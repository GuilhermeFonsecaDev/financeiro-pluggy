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
    return 0.0 if item.get("qualificado") == "sim" else float(item["percentual"] or 0)


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
        item["elegivel"] = item.get("qualificado") != "sim"
        # Só é "redistribuído" quem tinha alocação e ficou de fora por ser IQ.
        item["redistribuido"] = not item["elegivel"] and float(item["percentual"] or 0) > 0
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
            })
        for item in itens:
            item["dataResgate"] = _data_resgate(item["diasResgate"])
        abas = [dict(r) for r in conn.execute("SELECT * FROM carteira_abas ORDER BY ordem,id")]
        fatias = [{"percentual": a["percentual"], "ordem": a["ordem"]} for a in abas]
        _distribuir(fatias, aporte)
        grupos = {}
        for aba, fatia in zip(abas, fatias):
            perfil, percentual = aba["id"], aba["percentual"]
            cents = int(round(fatia["aporte"] * 100))
            lista = [i for i in itens if i["perfil"] == perfil]
            _distribuir(lista, cents / 100)
            _marcar_minimos(lista)
            soma_grupo = round(sum(i["percentual"] for i in lista), 4)
            total = round(sum(i["aporte"] for i in lista), 2)
            grupos[perfil] = {"nome": aba["nome"], "percentual": percentual, "aporte": cents / 100,
                "somaPercentual": soma_grupo, "somaFecha": not lista or abs(soma_grupo-100) < .005,
                "totalDistribuido": total, "naoDistribuido": round(cents/100-total, 2),
                "semElegivel": bool(lista) and all(_peso(i) <= 0 for i in lista),
                "abaixoDoMinimo": sum(i["abaixoDoMinimo"] for i in lista)}
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
        ))
    with fin.connect() as conn:
        garantir_tabelas(conn)
        # A tela é a fonte: quem saiu da lista sai da tabela.
        conn.execute("DELETE FROM carteira_alvo")
        conn.executemany(
            "INSERT INTO carteira_alvo (cnpj,ordem,percentual,nome,anbima,aporte_minimo,"
            "dias_resgate,qualificado,atualizado_em,perfil) VALUES (?,?,?,?,?,?,?,?,?,?)", linhas)
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
