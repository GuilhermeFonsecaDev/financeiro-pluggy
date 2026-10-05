"""Contas Fixas 2.0 (tela de teste): o mês com o cálculo simplificado.

Regra única -- cada real conta uma vez:

    total = faturas dos cartões no mês
          + contas pagas fora do cartão (PIX, boleto, conta)
          + contas no cartão que ainda não apareceram na fatura (previsto)
          − reembolsos do mês

Conta paga no cartão que já tem a cobrança (transação real ou parcela já
projetada na fatura) só aparece, com ✓: o valor dela já está na fatura, e
somar de novo duplicaria.

Fatura FECHADA não tem mais previsão: a conta no cartão que não apareceu
nela não foi cobrada naquele mês. Ela fica listada como "não cobrada", com
aviso, e não soma -- somar inventaria um gasto que não aconteceu.

Subdescontos (o caso da Mãe): a conta-pai soma só o que falta pagar à pessoa
(valor − subdescontos). Cada subdesconto segue a mesma regra das contas.

Reembolsos têm uma fonte só: a tabela da 2.0. "Como voltou" decide se ele
desconta:
    dinheiro/PIX       -> desconta do mês (o dinheiro voltou fora da fatura)
    estorno no cartão  -> não desconta: a fatura já abate créditos do cartão

Os cadastros são os da 2.0 (tabelas f2_fixas_*, ver fixas.usar_copia_2).
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

import fixas
import pluggy_extrato as px

ESQUEMA = """
CREATE TABLE IF NOT EXISTS fixas2_reembolsos (
  id TEXT PRIMARY KEY,
  mes_ref TEXT NOT NULL,
  descricao TEXT NOT NULL,
  forma TEXT NOT NULL DEFAULT '',
  valor REAL NOT NULL,
  transacao_id TEXT,
  criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fixas2_reembolsos_mes ON fixas2_reembolsos (mes_ref);
"""

COMO_DINHEIRO = "dinheiro"
COMO_ESTORNO = "estorno"
ORIGENS_FECHADAS = {"oficial", "pagamento", "confirmada", "fechada"}


def _abrir() -> sqlite3.Connection:
    conn = fixas._abrir()
    conn.executescript(ESQUEMA)
    colunas = {l[1] for l in conn.execute("PRAGMA table_info(fixas2_reembolsos)")}
    if "como" not in colunas:
        conn.execute(f"ALTER TABLE fixas2_reembolsos ADD COLUMN como TEXT NOT NULL DEFAULT '{COMO_DINHEIRO}'")
    _migrar_reembolsos_das_contas(conn)
    return conn


def _migrar_reembolsos_das_contas(conn) -> None:
    """Uma fonte só de reembolso na 2.0: os subdescontos do tipo reembolso
    viram linhas da tabela, uma vez. Só as tabelas da 2.0 são tocadas."""
    if conn.execute("SELECT 1 FROM app_meta WHERE chave='fixas2_reembolsos_migrados'").fetchone():
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='f2_fixas_descontos'").fetchone():
        return
    linhas = conn.execute(
        "SELECT d.id, d.mes_ref, d.descricao, d.valor, c.nome, c.forma_pagamento "
        "FROM f2_fixas_descontos d JOIN f2_fixas_contas c ON c.id = d.fixa_id "
        "WHERE d.forma_pagamento = ? OR d.reembolso = 1", (fixas.FORMA_REEMBOLSO,)).fetchall()
    agora = datetime.now().isoformat(timespec="seconds")
    for l in linhas:
        conn.execute(
            "INSERT OR IGNORE INTO fixas2_reembolsos (id, mes_ref, descricao, forma, valor, transacao_id, criado_em, como) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            ("rb_" + l[0], l[1], f"{l[4]} › {l[2]}", l[5] or "", float(l[3] or 0), agora, COMO_DINHEIRO))
        conn.execute("DELETE FROM f2_fixas_descontos WHERE id = ?", (l[0],))
    conn.execute("INSERT OR REPLACE INTO app_meta (chave, valor) VALUES ('fixas2_reembolsos_migrados', ?)", (agora,))
    conn.commit()


def _e_cartao(forma: str, formas: dict[str, dict]) -> bool:
    if not forma or forma in (fixas.FORMA_PIX, fixas.FORMA_REEMBOLSO):
        return False
    info = formas.get(forma)
    if info:
        return info.get("tipo") == "cartao"
    return forma.startswith(("tag:", "cartao:"))


def _no_cartao(registro: dict, formas: dict[str, dict]) -> bool:
    """A COBRANÇA decide, não só a forma cadastrada: conta cadastrada como
    PIX pode cair no cartão (transação de cartão, ou projeção numa fatura)."""
    tx = registro.get("transacao") or {}
    return (_e_cartao(registro.get("forma") or "", formas) or bool(tx.get("cartaoId"))
            or (not tx and bool(registro.get("projecao"))))


def _status(no_cartao: bool, registro: dict, fechada: bool, informativa: bool = False) -> str:
    if informativa:
        return "informativa"
    if not no_cartao:
        return "fora_cartao"
    if registro.get("transacao") or registro.get("projecao"):
        return "lancada"
    return "nao_cobrada" if fechada else "prevista"


def _faturas(mes: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fatura de cada cartão (tag) no mês, com a parte cobrada separada das
    estimativas; e o mapa referência (tag, cartão, conta) -> cartão."""
    try:
        dados = px.cartoes_payload(int(mes[:4]), "fatura")
    except Exception:
        return [], {}
    indice = int(mes[5:7]) - 1
    passado = mes < datetime.now().strftime("%Y-%m")
    saida, grupo_de = [], {}
    for cartao in dados["cartoes"]:
        cid = cartao["id"]
        grupo_de[cid] = cid
        for conta in cartao.get("contas") or []:
            grupo_de[conta if isinstance(conta, str) else conta.get("contaId", "")] = cid
        for membro in cartao.get("membros") or []:
            for chave in (membro.get("cartaoId"), membro.get("contaId"), membro.get("id")):
                if chave:
                    grupo_de[chave] = cid
        valor = round(float((dados["valores"].get(cid) or [0] * 12)[indice] or 0), 2)
        origem = ((dados.get("origens") or {}).get(cid) or [""] * 12)[indice]
        comp = ((dados.get("componentes") or {}).get(cid) or [None] * 12)[indice] or {}
        estimado = {
            "parcelas": round(float(comp.get("parcelas") or 0), 2),
            "habitos": round(float(comp.get("recorrencias") or 0) + float(comp.get("reservaRestante") or 0), 2),
        }
        # Fatura fechada ou sem componentes: o valor é todo real.
        cobrado = round(valor - estimado["parcelas"] - estimado["habitos"], 2) if comp else valor
        saida.append({"id": cid, "nome": cartao["nome"], "cor": cartao.get("cor", ""),
                      "valor": valor, "origem": origem, "cobrado": cobrado, **estimado,
                      "fechada": passado or origem in ORIGENS_FECHADAS})
    return [f for f in saida if f["valor"]] , grupo_de


def _reembolsos(conn, mes: str, formas: dict[str, dict]) -> list[dict[str, Any]]:
    return [
        {"id": l["id"], "descricao": l["descricao"], "forma": l["forma"],
         "formaNome": (formas.get(l["forma"]) or {}).get("nome") or l["forma"] or "—",
         "valor": round(float(l["valor"] or 0), 2), "transacaoId": l["transacao_id"] or "",
         "como": l["como"] or COMO_DINHEIRO,
         # Estorno no cartão já está abatido na fatura: listado, não desconta.
         "desconta": (l["como"] or COMO_DINHEIRO) != COMO_ESTORNO}
        for l in conn.execute(
            "SELECT * FROM fixas2_reembolsos WHERE mes_ref = ? ORDER BY criado_em, id", (mes,))
    ]


def _divergencias_de_habitos(mes: str) -> list[str]:
    """Hábito cuja projeção na fatura depende de uma conta fixa "cobrir" a
    cobrança. Essa checagem roda com os cadastros da tela original (é ela que
    alimenta Cartões); aqui ela é refeita com os da 2.0 para avisar quando as
    duas discordam -- aí a 2.0 contaria o hábito duas vezes ou nenhuma."""
    try:
        import recorrentes as rec
        import recorrencias_gestao as g
        avisos = []
        with rec._abrir() as conn:
            copia = fixas._ConexaoCopia2(conn)
            for linha in conn.execute("SELECT chave, dados FROM recorrentes_previsoes WHERE ativo=1"):
                s = g._ler(conn, linha["dados"])
                original, nova = g._fixas_cobrem(conn, s, mes), g._fixas_cobrem(copia, s, mes)
                if original and not nova:
                    avisos.append(f"Hábito {s['descricao']}: coberto por conta fixa só na tela original — na 2.0 ele não entra na fatura prevista.")
                elif nova and not original:
                    avisos.append(f"Hábito {s['descricao']}: coberto por conta fixa só na 2.0 — pode estar contando duas vezes.")
        return avisos
    except Exception:
        return []


def payload(mes: str) -> dict[str, Any]:
    # Sempre os cadastros da 2.0, nunca os da tela original.
    with fixas.usar_copia_2():
        base = fixas.mes_payload(mes)
    formas = {f["id"]: f for f in base.get("formas", [])}
    faturas, grupo_de = _faturas(mes)
    fechadas = {f["id"] for f in faturas if f["fechada"]}
    passado = mes < datetime.now().strftime("%Y-%m")

    def fatura_fechada(registro: dict) -> bool:
        grupo = grupo_de.get(registro.get("formaReferencia") or "") or grupo_de.get(registro.get("forma") or "")
        return passado or (grupo in fechadas if grupo else False)

    avisos: list[str] = []
    itens = []
    for i in base["itens"]:
        no_cartao = _no_cartao(i, formas)
        informativa = i.get("incluidaCalculos") is False
        descontos = []
        total_descontos = 0.0
        for d in i.get("descontos") or []:
            if d.get("forma") == fixas.FORMA_REEMBOLSO or d.get("reembolso"):
                continue   # já migrado para a tabela; nada fica aqui
            valor = round(float(d.get("valor") or 0), 2)
            total_descontos += valor
            d_cartao = _no_cartao(d, formas)
            status = _status(d_cartao, d, fatura_fechada(d))
            descontos.append({
                "id": d["id"], "descricao": d["descricao"], "valor": valor,
                "forma": d.get("forma"), "formaNome": d.get("formaNome") or "",
                "status": status, "noCartao": d_cartao,
                "somado": valor if status in ("prevista", "fora_cartao") and not informativa else 0.0,
                "transacao": d.get("transacao"), "projecao": d.get("projecao"),
            })
            if status == "nao_cobrada":
                avisos.append(f"{i['nome']} › {d['descricao']}: não apareceu na fatura fechada de {mes}.")

        valor = round(float(i.get("valor") or 0), 2)
        # Com subdescontos, o valor é o BRUTO combinado com a pessoa: se ele
        # viesse da própria transferência (já líquida), descontar de novo
        # zeraria a conta.
        if descontos and i.get("origemValor") == "transacao" and i.get("valorPrevisto"):
            valor = round(float(i["valorPrevisto"]), 2)
        if descontos and total_descontos > valor + 0.005:
            avisos.append(f"{i['nome']}: subdescontos ({total_descontos:.2f}) passam do valor da conta ({valor:.2f}).")
        liquido = round(max(0.0, valor - total_descontos), 2) if descontos else valor
        status = _status(no_cartao, i, fatura_fechada(i), informativa)
        if status == "nao_cobrada":
            avisos.append(f"{i['nome']}: não apareceu na fatura fechada de {mes}.")
        itens.append({
            "id": i["id"], "nome": i["nome"], "tag": i.get("tag") or "",
            "forma": i.get("forma"), "formaNome": i.get("formaNome") or "",
            "noCartao": no_cartao, "status": status, "valor": valor,
            "liquido": liquido,
            "somado": liquido if status in ("prevista", "fora_cartao") else 0.0,
            "transacao": i.get("transacao"), "projecao": i.get("projecao"),
            "termo": i.get("termo") or "", "diaVencimento": i.get("diaVencimento"),
            "descontos": descontos,
        })

    with _abrir() as conn:
        reembolsos = _reembolsos(conn, mes, formas)
    avisos.extend(_divergencias_de_habitos(mes))

    def soma(status):
        return sum(it["somado"] for it in itens if it["status"] == status) \
            + sum(d["somado"] for it in itens for d in it["descontos"] if d["status"] == status)

    total_faturas = sum(f["valor"] for f in faturas)
    total_reembolsos = sum(r["valor"] for r in reembolsos if r["desconta"])
    previstos, fora = soma("prevista"), soma("fora_cartao")
    return {
        "mes": mes,
        "itens": itens,
        "faturas": faturas,
        "reembolsos": reembolsos,
        "reembolsosContas": [],
        "avisos": avisos,
        "formas": [f for f in base.get("formas", []) if f.get("tipo") in ("cartao", "pagamento")],
        "resumo": {
            "faturas": round(total_faturas, 2),
            "faturasCobrado": round(sum(f["cobrado"] for f in faturas), 2),
            "faturasParcelas": round(sum(f["parcelas"] for f in faturas), 2),
            "faturasHabitos": round(sum(f["habitos"] for f in faturas), 2),
            "previstos": round(previstos, 2),
            "foraCartao": round(fora, 2),
            "reembolsos": round(total_reembolsos, 2),
            "total": round(total_faturas + previstos + fora - total_reembolsos, 2),
        },
    }


def _validar_reembolso(dados: dict) -> dict:
    descricao = " ".join(str(dados.get("descricao") or "").split())[:160]
    if not descricao:
        raise ValueError("Informe a descrição do reembolso.")
    try:
        valor = round(abs(float(str(dados.get("valor") or "0").replace(",", "."))), 2)
    except ValueError:
        raise ValueError("Valor do reembolso inválido.") from None
    if valor <= 0:
        raise ValueError("Informe um valor maior que zero.")
    mes = str(dados.get("mes") or "")[:7]
    if len(mes) != 7:
        raise ValueError("Mês inválido.")
    como = str(dados.get("como") or COMO_DINHEIRO)
    if como not in (COMO_DINHEIRO, COMO_ESTORNO):
        raise ValueError("Informe como o reembolso voltou.")
    return {"descricao": descricao, "valor": valor, "mes": mes, "como": como,
            "forma": str(dados.get("forma") or "")[:120],
            "transacao_id": str(dados.get("transacaoId") or "") or None}


def criar_reembolso(dados: dict) -> dict:
    r = _validar_reembolso(dados)
    novo = f"rb_{uuid.uuid4().hex[:10]}"
    with _abrir() as conn:
        conn.execute(
            "INSERT INTO fixas2_reembolsos (id, mes_ref, descricao, forma, valor, transacao_id, criado_em, como) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (novo, r["mes"], r["descricao"], r["forma"], r["valor"], r["transacao_id"],
             datetime.now().isoformat(timespec="seconds"), r["como"]))
        conn.commit()
    return {"ok": True, "id": novo}


def editar_reembolso(reembolso_id: str, dados: dict) -> dict:
    r = _validar_reembolso(dados)
    with _abrir() as conn:
        if not conn.execute("SELECT 1 FROM fixas2_reembolsos WHERE id = ?", (reembolso_id,)).fetchone():
            raise ValueError("Reembolso não encontrado.")
        conn.execute(
            "UPDATE fixas2_reembolsos SET mes_ref = ?, descricao = ?, forma = ?, valor = ?, "
            "transacao_id = ?, como = ? WHERE id = ?",
            (r["mes"], r["descricao"], r["forma"], r["valor"], r["transacao_id"], r["como"], reembolso_id))
        conn.commit()
    return {"ok": True, "id": reembolso_id}


def remover_reembolso(reembolso_id: str) -> dict:
    with _abrir() as conn:
        conn.execute("DELETE FROM fixas2_reembolsos WHERE id = ?", (reembolso_id,))
        conn.commit()
    return {"ok": True, "id": reembolso_id, "removido": True}
