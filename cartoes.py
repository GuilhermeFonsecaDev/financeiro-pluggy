"""Identidade dos cartões: quem é cada cartão e quais dividem uma fatura.

Antes, o nome de cada coluna da tela de Cartões vinha de heurística no código
(`"gold" -> NUBANK`, `"PLATINUM PRIME DUO" -> INTER`) e a consolidação era uma
função dedicada ao Itaú. Isso funcionava para os três cartões que existiam e
falhava calado no quarto: um cartão novo aparecia com o nome cru do banco e
nunca se juntava a ninguém.

Aqui o de-para é dado, não código. Cada conta de cartão tem:

  apelido -- como a pessoa chama aquele cartão.
  grupo   -- vazio: coluna própria. Preenchido: divide a coluna (e a fatura)
             com todo cartão que tenha o mesmo grupo, e o texto do grupo é o
             nome da coluna.
  cor     -- usada nas telas que pintam por cartão.

O PADRÃO reproduz o que a tela já mostrava: `grupo = item_id`, porque cartões
da mesma conexão são justamente os que compartilham fatura (é o caso dos dois
Itaú). Então quem nunca abrir a tela de identificação não vê diferença, e quem
conectar um cartão novo o vê aparecer sozinho na hora.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

import banco as fin
import extrato_camada as cam

SCHEMA = """
CREATE TABLE IF NOT EXISTS cartoes_identidade (
  conta_id TEXT PRIMARY KEY,
  apelido TEXT NOT NULL DEFAULT '',
  grupo TEXT NOT NULL DEFAULT '',
  cor TEXT NOT NULL DEFAULT '',
  ordem INTEGER NOT NULL DEFAULT 0,
  atualizado_em TEXT NOT NULL DEFAULT ''
);
"""

# Paleta usada quando a pessoa não escolheu cor. São as mesmas cores que as
# telas já usavam para Inter, Nubank e Itaú, seguidas de tons distinguíveis
# para os próximos cartões.
PALETA = ["#4a95ea", "#9b74e8", "#f28c35", "#3fbf7f", "#e5533d",
          "#5aa9a3", "#d9a520", "#c96f4f", "#8f7ce0", "#6b7280"]

_pronto = False


def _abrir() -> sqlite3.Connection:
    global _pronto
    fin.ensure_database()
    conn = cam.conectar()
    if not _pronto:
        conn.executescript(SCHEMA)
        conn.commit()
        _pronto = True
    return conn


def _slug(texto: str) -> str:
    limpo = re.sub(r"[^a-z0-9]+", "-", cam.normalizar(texto)).strip("-")
    return limpo or "cartao"


def _apelido_sugerido(nome: str) -> str:
    """Palpite inicial, no lugar da antiga heurística espalhada no código.

    Serve só como sugestão na primeira vez: assim que a pessoa salvar, o valor
    dela manda. Um cartão desconhecido cai no próprio nome do banco em vez de
    virar "Cartão" genérico.
    """
    texto = str(nome or "").strip()
    alto = texto.upper()
    if "ITAU" in alto or "ITAÚ" in alto:
        return alto.replace("ITAU", "ITAÚ")
    if alto == "GOLD" or "NUBANK" in alto:
        return "NUBANK"
    if "PLATINUM PRIME DUO" in alto or "BANCO INTER" in alto:
        return "INTER"
    return texto or "CARTÃO"


def _contas_de_cartao(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Toda conta de cartão conhecida, com o que ajuda a reconhecê-la."""
    saida = []
    for linha in conn.execute(
        "SELECT conta_id, item_id, nome, numero, limite_credito, limite_disponivel, "
        "       raw_json "
        "  FROM pluggy_contas WHERE subtipo = 'CREDIT_CARD' "
        " ORDER BY nome"
    ):
        try:
            credito = (json.loads(linha["raw_json"] or "{}") or {}).get("creditData") or {}
        except (TypeError, ValueError):
            credito = {}
        saida.append({
            "contaId": linha["conta_id"],
            "itemId": linha["item_id"],
            "nomeBanco": str(linha["nome"] or "").strip(),
            "numero": str(linha["numero"] or "").strip(),
            "marca": credito.get("brand") or "",
            "nivel": credito.get("level") or "",
            "limiteCredito": float(linha["limite_credito"] or 0),
            "limiteDisponivel": float(linha["limite_disponivel"] or 0),
        })
    return saida


def identidades(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """conta_id -> identidade efetiva (o que foi salvo, ou o padrão).

    É o que `cartoes_payload` consulta para nomear e agrupar as colunas.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cartoes_identidade'"
    ).fetchone():
        conn.executescript(SCHEMA)

    salvas = {
        linha["conta_id"]: linha
        for linha in conn.execute("SELECT * FROM cartoes_identidade")
    }
    saida: dict[str, dict[str, Any]] = {}
    for indice, conta in enumerate(_contas_de_cartao(conn)):
        salva = salvas.get(conta["contaId"])
        # Padrão: agrupa pela conexão. É o que faz os dois Itaú continuarem
        # numa coluna só sem nenhuma regra escrita sobre "Itaú".
        grupo = (salva["grupo"] if salva else "") or conta["itemId"]
        saida[conta["contaId"]] = {
            **conta,
            "apelido": (salva["apelido"] if salva else "") or _apelido_sugerido(conta["nomeBanco"]),
            "grupo": grupo,
            "cor": (salva["cor"] if salva else "") or PALETA[indice % len(PALETA)],
            "ordem": int(salva["ordem"]) if salva else indice,
            "identificado": salva is not None,
        }
    return saida


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def rotulo_de_grupo(grupo: str, apelidos: list[str]) -> str:
    """Nome da coluna de um grupo de cartões.

    Se a pessoa deu um nome ao grupo, é ele. No padrão o grupo é o id da
    conexão (um UUID, que não serve de rótulo), então o nome sai do PREFIXO
    COMUM dos apelidos: "ITAÚ MASTERCARD PLATINUM" + "ITAÚ MULTIPLO VS PLAT"
    dá "ITAÚ", que é exatamente como a coluna já se chamava.
    """
    nomes = [a for a in apelidos if a]
    if grupo and not _UUID.match(grupo):
        return grupo
    if len(nomes) <= 1:
        return nomes[0] if nomes else "CARTÃO"
    palavras = [n.split() for n in nomes]
    comuns: list[str] = []
    for pedaco in zip(*palavras):
        if len(set(p.upper() for p in pedaco)) != 1:
            break
        comuns.append(pedaco[0])
    return " ".join(comuns) if comuns else nomes[0]


def colunas(conn: sqlite3.Connection,
            contas_visiveis: set[str] | None = None) -> list[dict[str, Any]]:
    """Uma entrada por COLUNA da tela de Cartões, já agrupada.

    É daqui que `cartoes_payload` tira quantas colunas existem e como se
    chamam -- por isso um cartão novo aparece sozinho sem nenhuma mudança de
    código, e dois cartões viram uma coluna só se compartilharem o grupo.
    """
    mapa = identidades(conn)
    grupos: dict[str, list[dict[str, Any]]] = {}
    for conta_id, ident in mapa.items():
        if contas_visiveis is not None and conta_id not in contas_visiveis:
            continue
        grupos.setdefault(ident["grupo"], []).append(ident)

    saida = []
    for grupo, membros in grupos.items():
        membros.sort(key=lambda m: (m["ordem"], m["apelido"]))
        saida.append({
            "id": (membros[0]["contaId"] if len(membros) == 1
                   else f"grupo:{_slug(rotulo_de_grupo(grupo, [m['apelido'] for m in membros]))}"),
            "nome": rotulo_de_grupo(grupo, [m["apelido"] for m in membros]),
            "cor": membros[0]["cor"],
            "grupo": grupo,
            "contas": [m["contaId"] for m in membros],
            "apelidos": [m["apelido"] for m in membros],
            "numero": membros[0]["numero"] if len(membros) == 1 else "",
            "limiteCredito": round(sum(m["limiteCredito"] for m in membros), 2),
            "limiteDisponivel": round(sum(m["limiteDisponivel"] for m in membros), 2),
            "ordem": membros[0]["ordem"],
        })
    saida.sort(key=lambda c: (c["ordem"], c["nome"]))
    return saida


def payload() -> dict[str, Any]:
    """Tudo que a tela de identificação precisa mostrar."""
    with _abrir() as conn:
        cam.garantir_camada(conn)
        try:
            import pluggy_extrato as px
            ativas = px.contas_ativas(conn)
        except Exception:
            ativas = None
        mapa = identidades(conn)

    # Quantos cartões caem em cada grupo, e como aquela coluna se chama. O
    # formulário precisa disso para JÁ VIR com o grupo preenchido quando o
    # cartão divide coluna: com o campo vazio, salvar sem mexer em nada
    # separaria a coluna consolidada em duas.
    por_grupo: dict[str, list[dict[str, Any]]] = {}
    for ident in mapa.values():
        por_grupo.setdefault(ident["grupo"], []).append(ident)

    cartoes = []
    for conta_id, ident in sorted(mapa.items(), key=lambda x: (x[1]["ordem"], x[1]["apelido"])):
        irmaos = por_grupo.get(ident["grupo"], [])
        rotulo = rotulo_de_grupo(ident["grupo"], [i["apelido"] for i in irmaos])
        cartoes.append({
            **ident,
            # Conta substituída por reconexão continua no banco, mas não deve
            # virar coluna. A tela mostra e explica, em vez de esconder.
            "ativa": ativas is None or conta_id in ativas,
            "compartilhaColuna": len(irmaos) > 1,
            # Vazio quando o cartão está sozinho (é o que a pessoa deve ver);
            # o rótulo da coluna quando ele divide fatura com outro.
            "grupoTexto": rotulo if len(irmaos) > 1 else "",
        })
    grupos = sorted({
        c["grupoTexto"] for c in cartoes if c["grupoTexto"]
    })
    return {"cartoes": cartoes, "grupos": grupos, "paleta": PALETA}


def salvar(dados: dict) -> dict[str, Any]:
    """Grava a identificação de um ou vários cartões de uma vez."""
    itens = dados.get("cartoes")
    if not isinstance(itens, list) or not itens:
        raise ValueError("Envie a lista de cartões a identificar.")

    agora = datetime.now().isoformat(timespec="seconds")
    with _abrir() as conn:
        validos = {c["contaId"] for c in _contas_de_cartao(conn)}
        for item in itens:
            conta_id = str(item.get("contaId") or "").strip()
            if conta_id not in validos:
                raise ValueError(f"Cartão desconhecido: {conta_id}")
            apelido = str(item.get("apelido") or "").strip()[:60]
            if not apelido:
                raise ValueError("Todo cartão precisa de um apelido.")
            cor = str(item.get("cor") or "").strip()[:9]
            if cor and not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
                raise ValueError(f"Cor inválida: {cor}")
            grupo = str(item.get("grupo") or "").strip()[:60]
            try:
                ordem = int(item.get("ordem", 0))
            except (TypeError, ValueError):
                ordem = 0
            conn.execute(
                "INSERT INTO cartoes_identidade "
                "  (conta_id, apelido, grupo, cor, ordem, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conta_id) DO UPDATE SET "
                "  apelido = excluded.apelido, grupo = excluded.grupo, "
                "  cor = excluded.cor, ordem = excluded.ordem, "
                "  atualizado_em = excluded.atualizado_em",
                (conta_id, apelido, grupo, cor, ordem, agora),
            )
        conn.commit()
    return {"ok": True, **payload()}


def esquecer(conta_id: str) -> dict[str, Any]:
    """Volta um cartão ao padrão (apelido sugerido, grupo pela conexão)."""
    with _abrir() as conn:
        conn.execute("DELETE FROM cartoes_identidade WHERE conta_id = ?", (conta_id,))
        conn.commit()
    return {"ok": True, **payload()}
