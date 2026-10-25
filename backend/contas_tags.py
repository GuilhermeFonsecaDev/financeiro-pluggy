"""Tags das contas bancárias: o mesmo agrupamento que os cartões já têm.

Duas contas com a mesma tag aparecem como um lugar só nos seletores do app
(Inter Corrente + Inter Poupança = "INTER"). Sem tag, a conta fica sozinha.
Nada é deduzido do banco: quem decide o agrupamento é a tag que o usuário
escolhe, na tela Contas.

As tags são as mesmas de `cartoes_tags`: "BTG" nomeia o cartão e a conta do
BTG, com a mesma cor.

A tag é guardada pela identidade estável da conta (o número de
transferência do banco), não pelo conta_id da Pluggy, que muda a cada
reconexão -- do mesmo jeito que o cartão é guardado pelo cartaoId.
"""
from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime
from typing import Any

import cartoes
import banco as fin
import pluggy_extrato as px

ESQUEMA = """
CREATE TABLE IF NOT EXISTS contas_tags (
  chave TEXT PRIMARY KEY,
  tag_id TEXT REFERENCES cartoes_tags(tag_id),
  atualizado_em TEXT NOT NULL
);
"""


def garantir(conn) -> None:
    cartoes.garantir(conn)
    conn.executescript(ESQUEMA)


def chave_da_conta(conta_id: str, raw_json) -> str:
    """Identidade da conta que sobrevive a reconectar o banco.

    O número de transferência (banco/agência/conta) é o mesmo antes e depois;
    sem ele, o próprio conta_id.
    """
    try:
        numero = str((json.loads(raw_json or "{}").get("bankData") or {})
                     .get("transferNumber") or "")
    except (ValueError, TypeError, AttributeError):
        numero = ""
    digitos = re.sub(r"\D", "", numero)
    return f"tn:{digitos}" if digitos else f"id:{conta_id}"


def tags_das_contas(conn) -> dict[str, dict[str, str]]:
    """conta_id -> {"tagId", "tag", "cor"} para as contas que têm tag."""
    garantir(conn)
    por_chave = {
        l[0]: {"tagId": l[1], "tag": l[2], "cor": l[3] or ""}
        for l in conn.execute(
            "SELECT c.chave, c.tag_id, t.nome, t.cor FROM contas_tags c "
            "JOIN cartoes_tags t USING(tag_id)")
    }
    resultado = {}
    for linha in conn.execute(
            "SELECT conta_id, raw_json FROM pluggy_contas WHERE subtipo <> 'CREDIT_CARD'"):
        achado = por_chave.get(chave_da_conta(linha[0], linha[1]))
        if achado:
            resultado[linha[0]] = achado
    return resultado


def payload() -> dict[str, Any]:
    fin.ensure_database()
    with closing(fin.connect()) as conn:
        garantir(conn)
        ativas = px.contas_ativas(conn)
        apelidos = px.mapa_apelidos(conn, ativas)
        tags = tags_das_contas(conn)
        contas = []
        for linha in conn.execute(
                "SELECT conta_id, subtipo, raw_json, nome FROM pluggy_contas "
                "WHERE subtipo <> 'CREDIT_CARD'"):
            cid = linha["conta_id"]
            if cid not in ativas:
                continue
            tag = tags.get(cid, {})
            contas.append({
                "contaId": cid,
                "nome": apelidos.get(cid) or px.nome_da_conta(linha),
                "tipo": "Poupança" if linha["subtipo"] == "SAVINGS_ACCOUNT" else "Conta",
                "tag": tag.get("tag", ""), "tagId": tag.get("tagId", ""), "cor": tag.get("cor", ""),
            })
        lista_tags = [{"id": l[0], "nome": l[1], "cor": l[2]} for l in conn.execute(
            "SELECT tag_id, nome, cor FROM cartoes_tags ORDER BY nome")]
    contas.sort(key=lambda c: (c["tag"].casefold() or "~", c["nome"].casefold()))
    return {"contas": contas, "tags": lista_tags}


def salvar(dados: dict) -> dict[str, Any]:
    itens = dados.get("contas")
    if not isinstance(itens, list):
        raise ValueError("Envie a lista de contas.")
    fin.ensure_database()
    with closing(fin.connect()) as conn, conn:
        garantir(conn)
        raws = {l[0]: l[1] for l in conn.execute(
            "SELECT conta_id, raw_json FROM pluggy_contas WHERE subtipo <> 'CREDIT_CARD'")}
        agora = datetime.now().isoformat(timespec="seconds")
        for item in itens:
            cid = str(item.get("contaId") or "")
            if cid not in raws:
                raise ValueError("Conta desconhecida.")
            chave = chave_da_conta(cid, raws[cid])
            tag = cartoes._obter_tag(conn, item.get("tag") or "")
            if tag:
                conn.execute(
                    "INSERT INTO contas_tags (chave, tag_id, atualizado_em) VALUES (?, ?, ?) "
                    "ON CONFLICT(chave) DO UPDATE SET tag_id = excluded.tag_id, "
                    "atualizado_em = excluded.atualizado_em", (chave, tag, agora))
            else:
                conn.execute("DELETE FROM contas_tags WHERE chave = ?", (chave,))
    return {"ok": True, **payload()}
