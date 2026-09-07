"""Cadastro e calendário das recorrências aceitas, independente do detector."""
from __future__ import annotations

import json
import math
import re
import uuid
from calendar import monthrange
from datetime import datetime

import ciclos
import extrato_camada as cam
import pluggy_extrato as px
import recorrentes as rec


def contas_pagamento(conn):
    ativas = px.contas_ativas(conn)
    nomes = px.mapa_apelidos(conn, ativas)
    import cartoes
    identidades = cartoes.identidades(conn)
    resultado = []
    for conta in conn.execute("SELECT conta_id,subtipo,numero FROM pluggy_contas"):
        cid = conta["conta_id"]
        if cid not in ativas:
            continue
        cartao = conta["subtipo"] == "CREDIT_CARD"
        nome = nomes.get(cid, "Conta")
        if cartao:
            identidade = identidades.get(cid, {})
            nome = identidade.get("nomeOriginal") or nome
            tag = identidade.get("tag")
            if tag and tag != nome:
                nome = f"{tag} · {nome}"
        resultado.append({"id": cid, "nome": nome, "noCartao": cartao})
    return sorted(resultado, key=lambda c: (not c["noCartao"], c["nome"].casefold(), c["id"]))


def _normalizar(conn, dados, anterior=None):
    anterior = anterior or {}
    s = {**anterior, **{k: dados[k] for k in (
        "descricao", "lojista", "contaId", "valorPrevisto", "modoValor", "diaTipico"
    ) if k in dados}}
    s["descricao"] = str(s.get("descricao") or "").strip()
    s["lojista"] = rec._chave(str(s.get("lojista") or ""))
    if not 2 <= len(s["descricao"]) <= 160:
        raise ValueError("Informe um nome de 2 a 160 caracteres.")
    if not 3 <= len(s["lojista"]) <= 240:
        raise ValueError("Informe a identificação do estabelecimento no extrato (3 a 240 caracteres).")
    cid = str(s.get("contaId") or "")
    conta = conn.execute("SELECT subtipo FROM pluggy_contas WHERE conta_id=?", (cid,)).fetchone()
    if not conta or cid not in px.contas_ativas(conn):
        raise ValueError("Escolha uma conta ou cartão ativo para o pagamento.")
    s["contaId"], s["noCartao"] = cid, conta["subtipo"] == "CREDIT_CARD"
    try:
        valor = float(s.get("valorPrevisto", s.get("valorUltimo", 0)))
    except (ValueError, TypeError):
        raise ValueError("Informe um valor previsto maior que zero.") from None
    if not math.isfinite(valor) or round(valor, 2) <= 0:
        raise ValueError("Informe um valor previsto maior que zero.")
    s["valorPrevisto"] = round(valor, 2)
    modo = s.get("modoValor", "ultimo")
    if modo not in ("ultimo", "fixo"):
        raise ValueError("Escolha último valor cobrado ou valor definido.")
    s["modoValor"] = modo
    try:
        dia = float(s.get("diaTipico") or 0)
        if not math.isfinite(dia) or not dia.is_integer() or not 1 <= dia <= 31:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Informe um dia da cobrança entre 1 e 31.") from None
    s["diaTipico"] = int(dia)
    if anterior and s["lojista"] != anterior.get("lojista"):
        # Faixa inferida pertence ao estabelecimento anterior.
        s["faixaValor"] = None
        s["transacaoBaseId"] = ""
    s.setdefault("valorUltimo", s["valorPrevisto"])
    s.setdefault("transacaoBaseId", "")
    s.setdefault("faixaValor", None)
    return s


def _verificar_duplicata(conn, s):
    for linha in conn.execute("SELECT chave,dados FROM recorrentes_previsoes"):
        outro = json.loads(linha["dados"])
        if linha["chave"] == s.get("chave"):
            continue
        if outro.get("contaId") != s["contaId"] or outro.get("lojista") != s["lojista"]:
            continue
        a, b = s.get("faixaValor"), outro.get("faixaValor")
        if a and b and (a["max"] < b["min"] or b["max"] < a["min"]):
            continue
        raise ValueError("Já existe uma recorrência desse estabelecimento neste pagamento. Edite ou retome o cadastro existente.")


def _gravar(conn, s, ativa=True):
    _verificar_duplicata(conn, s)
    conn.execute(
        "INSERT INTO recorrentes_previsoes (chave,dados,ativo,atualizado_em) VALUES (?,?,?,?) "
        "ON CONFLICT(chave) DO UPDATE SET dados=excluded.dados,ativo=excluded.ativo,atualizado_em=excluded.atualizado_em",
        (s["chave"], json.dumps(s, ensure_ascii=False), int(ativa), datetime.now().isoformat()))


def salvar(chave, ativa=True):
    with rec._abrir() as conn:
        linha = conn.execute("SELECT * FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if linha:
            if ativa:
                s = json.loads(linha["dados"])
                _verificar_duplicata(conn, s)
            conn.execute("UPDATE recorrentes_previsoes SET ativo=?,atualizado_em=? WHERE chave=?",
                         (int(ativa), datetime.now().isoformat(), chave))
    if not linha:
        sugestao = next((s for s in rec.sugestoes_payload()["sugestoes"] if s["chave"] == chave), None)
        if not sugestao or not ativa:
            raise ValueError("Sugestão não encontrada. Atualize a lista.")
        with rec._abrir() as conn:
            _gravar(conn, _normalizar(conn, {}, sugestao))
    return {"ok": True, **rec.sugestoes_payload()}


def criar(dados):
    with rec._abrir() as conn:
        s = _normalizar(conn, dados)
        s["chave"] = "manual:" + uuid.uuid4().hex
        _gravar(conn, s)
    return {"ok": True, "chave": s["chave"], **rec.sugestoes_payload()}


def editar(chave, dados):
    with rec._abrir() as conn:
        linha = conn.execute("SELECT * FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if not linha:
            raise ValueError("Recorrência não encontrada. Atualize a lista.")
        anterior = json.loads(linha["dados"])
        s = _normalizar(conn, dados, anterior)
        if (s["contaId"] != anterior.get("contaId") and s["lojista"] == anterior.get("lojista")
                and s["modoValor"] == "ultimo"
                and s["valorPrevisto"] == anterior.get("valorPrevisto", anterior.get("valorUltimo"))):
            # O pagamento novo pode ainda não ter histórico. Levar o preço
            # atual evita voltar ao preço antigo no primeiro mês após a troca.
            limite = rec._mes_atual() + datetime.now().strftime("-%d")
            s["valorPrevisto"], _ = _valor(anterior, _reais(conn, anterior), limite)
            s["valorUltimo"] = s["valorPrevisto"]
        s["chave"] = chave
        _gravar(conn, s, bool(linha["ativo"]))
    return {"ok": True, **rec.sugestoes_payload()}


def excluir(chave):
    with rec._abrir() as conn:
        linha = conn.execute("SELECT dados FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if not linha:
            raise ValueError("Recorrência não encontrada. Atualize a lista.")
        s = json.loads(linha["dados"])
        chaves = {chave}
        if not s.get("faixaValor"):
            chaves.add(s["contaId"] + "|" + s["lojista"])
        else:
            faixa = s["faixaValor"]
            chaves.add(f"{s['contaId']}|{s['lojista']}|faixa:{faixa['min']:.2f}:{faixa['max']:.2f}")
        conn.executemany(
            "INSERT OR REPLACE INTO recorrentes_ignorados (chave,descricao,ignorado_em) VALUES (?,?,?)",
            [(k, s["descricao"], datetime.now().isoformat()) for k in chaves])
        conn.execute("DELETE FROM recorrentes_previsoes WHERE chave=?", (chave,))
    return {"ok": True, **rec.sugestoes_payload()}


def _reais(conn, s):
    faixa = s.get("faixaValor")
    return [dict(r) for r in conn.execute(
        "SELECT transacao_id,descricao,valor,data,competencia_fatura FROM extrato_efetivo_cache "
        "WHERE conta_id=? AND tipo='DEBIT' AND incluida=1 AND COALESCE(parcela_total,1)<=1 "
        "ORDER BY data DESC, transacao_id", (s["contaId"],))
        if rec._chave(r["descricao"]) == s["lojista"]
        and not re.search(r"\bparcela(?:s|mento)?\b|\b\d+\s*/\s*(?:[2-9]|[1-9]\d+)\b", cam.normalizar(r["descricao"]))
        and (not faixa or faixa["min"] <= abs(float(r["valor"])) <= faixa["max"])]


def _fixas_cobrem(conn, s, mes):
    import cartoes
    for f in conn.execute("SELECT * FROM fixas_contas WHERE ativo=1 AND incluir_calculos=1"):
        if (f["desde"] and mes < f["desde"]) or (f["ate"] and mes > f["ate"]):
            continue
        if f["conta_id"]:
            fontes = cartoes.resolver_contas(conn, f["conta_id"]) or {f["conta_id"]}
            if s["contaId"] not in fontes:
                continue
        termos = [r[0] for r in conn.execute("SELECT termo FROM fixas_termos WHERE fixa_id=?", (f["id"],))] or [f["termo"]]
        descricoes = [s["lojista"], cam.normalizar(s["descricao"])]
        if any(cam.normalizar(t) and any(cam.normalizar(t) in d for d in descricoes) for t in termos):
            return True
    return False


def _calendario(conn, s):
    atual = rec._mes_atual()
    fim = rec._recuar(atual, -11)
    dia = min(31, max(1, int(s.get("diaTipico") or 1)))
    datas = {}
    # Começar antes do horizonte cobre a fatura atual, cuja cobrança pode
    # ter ocorrido no mês anterior. O ciclo decide o destino, nunca o nome do banco.
    for deslocamento in range(-2, 12):
        mes = rec._recuar(atual, -deslocamento)
        ano, numero = map(int, mes.split("-"))
        data = f"{mes}-{min(dia, monthrange(ano, numero)[1]):02d}"
        competencia = mes
        if s["noCartao"]:
            competencia = ciclos.competencia_de(conn, s["contaId"], data) or rec._recuar(mes, -1)
        if atual <= competencia <= fim:
            datas[competencia] = data
    return datas


def _valor(s, reais, limite=None):
    base = next((r for r in reais if limite is None or r["data"][:10] <= limite), None)
    valor = s.get("valorPrevisto", s.get("valorUltimo", 0))
    if s.get("modoValor", "ultimo") == "ultimo" and base:
        valor = abs(float(base["valor"]))
    return round(float(valor), 2), base


def projetados(conn, ano):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='recorrentes_previsoes'").fetchone():
        return []
    linhas = conn.execute("SELECT chave,dados FROM recorrentes_previsoes WHERE ativo=1 ORDER BY chave").fetchall()
    if not linhas:
        return []
    conn.create_function("norm", 1, cam.normalizar, deterministic=True)
    cam.garantir_camada(conn)
    px.garantir_extrato_materializado(conn)
    ativas = px.contas_ativas(conn)
    subtipos = {r[0]: r[1] for r in conn.execute("SELECT conta_id,subtipo FROM pluggy_contas")}
    fechadas = {(r[0], r[1]) for r in conn.execute("SELECT conta_id,competencia FROM pluggy_faturas")}
    resultado = []
    for linha in linhas:
        s = json.loads(linha["dados"])
        s["chave"] = linha["chave"]
        if s["contaId"] not in ativas:
            continue
        s["noCartao"] = subtipos.get(s["contaId"]) == "CREDIT_CARD"
        reais = _reais(conn, s)
        meses_reais = {r["competencia_fatura"] if s["noCartao"] else r["data"][:7] for r in reais}
        for competencia, data in _calendario(conn, s).items():
            if int(competencia[:4]) != ano or competencia in meses_reais:
                continue
            if s["noCartao"] and (s["contaId"], competencia) in fechadas:
                continue
            if _fixas_cobrem(conn, s, competencia):
                continue
            valor, base = _valor(s, reais, data)
            resultado.append({
                "mes": int(competencia[5:]), "data": data, "contaId": s["contaId"],
                "transacaoBaseId": base["transacao_id"] if base else s.get("transacaoBaseId", ""),
                "compraId": "recorrente:" + s["chave"], "descricao": s["descricao"],
                "valor": valor, "parcelaAtual": None, "parcelaTotal": None,
                "recorrente": True, "noCartao": s["noCartao"],
            })
    return resultado


def previsoes_payload(linhas):
    if not linhas:
        return []
    with rec._abrir() as conn:
        contas = {c["id"]: c for c in contas_pagamento(conn)}
        atual = rec._mes_atual()
        ano = int(atual[:4])
        futuras = {}
        for a in (ano, ano + 1):
            for item in projetados(conn, a):
                chave = item["compraId"].removeprefix("recorrente:")
                futuras.setdefault(chave, {"mes": f"{a}-{item['mes']:02d}", "valor": item["valor"]})
        resultado = []
        for linha in linhas:
            s = json.loads(linha["dados"])
            s["chave"] = linha["chave"]
            s["ativa"] = bool(linha["ativo"])
            conta = contas.get(s["contaId"])
            s["contaNome"] = conta["nome"] if conta else "Pagamento indisponível · escolha outra conta"
            if conta:
                s["noCartao"] = conta["noCartao"]
            reais = _reais(conn, s)
            # Cobrança já informada para uma data futura não altera o valor
            # exibido hoje, nem o valor das competências anteriores.
            hoje = atual + datetime.now().strftime("-%d")
            valor, base = _valor(s, reais, hoje)
            s.setdefault("modoValor", "ultimo")
            s.setdefault("valorPrevisto", s.get("valorUltimo", valor))
            s["valorUltimo"] = abs(float(base["valor"])) if base else s.get("valorUltimo", valor)
            s["valorAtual"] = valor
            s["proximaPrevisao"] = futuras.get(s["chave"])
            s["statusPrevisao"] = ("Pausada" if not s["ativa"] else "Escolha um pagamento ativo" if not conta
                else "Coberta por conta fixa" if _fixas_cobrem(conn, s, atual)
                else "Previsão ativa" if s["proximaPrevisao"] else "Cobranças já identificadas nas faturas")
            resultado.append(s)
        return sorted(resultado, key=lambda s: (not s["ativa"], s["descricao"].casefold()))
