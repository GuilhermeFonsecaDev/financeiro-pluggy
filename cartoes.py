"""Identidade, tags e catálogo de cartões, independente de bancos.

A Pluggy guarda cartões em accounts. Aqui cada cartão tem identidade própria;
a conexão é apenas origem e a tag é apenas apresentação/agrupamento.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from typing import Any

import banco as fin

PALETA = ["#4a95ea", "#9b74e8", "#f28c35", "#3fbf7f", "#e5533d",
          "#5aa9a3", "#d9a520", "#c96f4f", "#8f7ce0", "#6b7280"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS cartoes_tags (
 tag_id TEXT PRIMARY KEY, nome TEXT NOT NULL, normalizado TEXT NOT NULL UNIQUE,
 cor TEXT NOT NULL DEFAULT '', atualizado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cartoes_catalogo (
 cartao_id TEXT PRIMARY KEY, tag_id TEXT REFERENCES cartoes_tags(tag_id),
 cor TEXT NOT NULL DEFAULT '', ordem INTEGER NOT NULL DEFAULT 0,
 atualizado_em TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cartoes_fontes (
 conta_id TEXT PRIMARY KEY, cartao_id TEXT NOT NULL REFERENCES cartoes_catalogo(cartao_id),
 criado_em TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cartoes_fontes_cartao ON cartoes_fontes(cartao_id);
"""
_trava = threading.RLock()


def _normalizar(texto: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().casefold().split())


def _json(texto):
    try:
        obj = json.loads(texto or "{}")
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def _numero_final(numero) -> str:
    return re.sub(r"\D", "", str(numero or ""))[-4:]


def _nome_original(conta, credito):
    marca = str(credito.get("brand") or "").strip().upper()
    final = _numero_final(conta["numero"])
    if marca and final:
        return f"{marca} {final}"
    nome = str(conta["nome"] or "Cartão").strip()
    if marca:
        return marca
    return f"{nome} {final}" if final and not nome.endswith(final) else nome


def _contas(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM pluggy_contas WHERE subtipo='CREDIT_CARD' ORDER BY conta_id")]


def _cor(chave):
    return PALETA[int(hashlib.sha256(chave.encode()).hexdigest()[:8], 16) % len(PALETA)]


def _agora():
    return datetime.now().isoformat(timespec="microseconds")


def _tabela(conn, nome):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def _obter_tag(conn, nome: str) -> str | None:
    nome = " ".join(str(nome or "").split())
    if not nome:
        return None
    if len(nome) > 60:
        raise ValueError("A tag deve ter no máximo 60 caracteres.")
    normalizado = _normalizar(nome)
    if not normalizado:
        raise ValueError("Informe um nome legível para a tag.")
    existente = conn.execute("SELECT tag_id FROM cartoes_tags WHERE normalizado=?", (normalizado,)).fetchone()
    if existente:
        return existente[0]
    tag = "tag:" + str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cartoes_tags (tag_id,nome,normalizado,cor,atualizado_em) VALUES (?,?,?,?,?)",
        (tag, nome, normalizado, _cor(tag), _agora()),
    )
    return tag


def _assinatura(conta):
    credito = _json(conta["raw_json"]).get("creditData") or {}
    return (_normalizar(conta["nome"]), _numero_final(conta["numero"]),
            _normalizar(credito.get("brand") or ""), _normalizar(conta.get("titular") or ""),
            conta.get("moeda") or "BRL")


def _instituicao(conn, conta):
    instituicao = _json(conta["raw_json"]).get("institution")
    if isinstance(instituicao, dict):
        instituicao = instituicao.get("id") or instituicao.get("name")
    if instituicao:
        return _normalizar(str(instituicao))
    item = conn.execute("SELECT conector FROM pluggy_itens WHERE item_id=?", (conta["item_id"],)).fetchone()
    conector = _normalizar(item[0]) if item else ""
    # Agregador não é instituição: sem contexto não há reconexão automática.
    return conector if conector not in {"", "meupluggy"} else None


def _mesma_fonte(conn, nova, antiga):
    # Nome/final isolados não identificam reconexão. Exigimos também histórico
    # coincidente em datas distintas, em conexões distintas, sem candidatos ambíguos.
    if nova["item_id"] == antiga["item_id"] or _assinatura(nova) != _assinatura(antiga):
        return False
    if not _numero_final(nova["numero"]):
        return False
    instituicao = _instituicao(conn, nova)
    if not instituicao or instituicao != _instituicao(conn, antiga):
        return False
    dias = conn.execute(
        "SELECT DISTINCT substr(a.data,1,10) dia FROM pluggy_transacoes a "
        "JOIN pluggy_transacoes b ON a.data=b.data AND a.valor=b.valor "
        "AND a.descricao=b.descricao AND a.tipo=b.tipo "
        "WHERE a.conta_id=? AND b.conta_id=? LIMIT 3",
        (nova["conta_id"], antiga["conta_id"])).fetchall()
    return len(dias) >= 3


def _reconciliar_fontes(conn, contas):
    """Reavalia identidades provisórias quando o histórico finalmente chega.

    A descoberta de accounts pode anteceder as transações. Uma fonte já
    cadastrada não pode ficar duplicada para sempre por causa dessa ordem.
    Só unem-se componentes com evidência entre todos os pares, sem cartões
    distintos da mesma conexão nem preferências pessoais conflitantes.
    """
    fontes = {r["conta_id"]: dict(r) for r in conn.execute("SELECT * FROM cartoes_fontes")}
    catalogo = {r["cartao_id"]: dict(r) for r in conn.execute("SELECT * FROM cartoes_catalogo")}
    por_cartao = defaultdict(list)
    for conta in contas:
        fonte = fontes.get(conta["conta_id"])
        if fonte:
            por_cartao[fonte["cartao_id"]].append(conta)
    ids = sorted(por_cartao)
    vizinhos = {cid: set() for cid in ids}
    for indice, primeiro in enumerate(ids):
        for segundo in ids[indice + 1:]:
            if any(_mesma_fonte(conn, a, b)
                   for a in por_cartao[primeiro] for b in por_cartao[segundo]):
                vizinhos[primeiro].add(segundo)
                vizinhos[segundo].add(primeiro)

    def criacao(cid):
        return min(fontes[c["conta_id"]]["criado_em"] for c in por_cartao[cid])

    def preferencia(cid):
        registro = catalogo[cid]
        cor_pessoal = registro["cor"] if registro["cor"] != _cor(cid) else ""
        # Salvar/remover uma tag explicitamente também é preferência, mesmo
        # que o resultado seja vazio. Não deixar outra identidade sobrescrevê-la.
        pessoal = (registro["atualizado_em"] > criacao(cid)
                   or registro["tag_id"] or cor_pessoal or registro["ordem"])
        return (registro["tag_id"], cor_pessoal, registro["ordem"]) if pessoal else None

    vistos = set()
    alterou = False
    for inicio in ids:
        if inicio in vistos:
            continue
        componente, pendentes = set(), [inicio]
        while pendentes:
            cid = pendentes.pop()
            if cid in componente:
                continue
            componente.add(cid)
            pendentes.extend(vizinhos[cid] - componente)
        vistos.update(componente)
        if len(componente) < 2:
            continue
        if any(vizinhos[cid] & componente != componente - {cid} for cid in componente):
            continue  # Candidatos ambíguos: não escolher um por ordem de leitura.
        conexoes = [set(c["item_id"] for c in por_cartao[cid]) for cid in componente]
        if sum(len(c) for c in conexoes) != len(set().union(*conexoes)):
            continue
        preferencias = {p for cid in componente if (p := preferencia(cid)) is not None}
        if len(preferencias) > 1:
            continue
        destino = min(componente, key=lambda cid: (criacao(cid), cid))
        if preferencias:
            tag, cor, ordem = next(iter(preferencias))
            # A identidade estável mais antiga sobrevive; a preferência única
            # acompanha a união mesmo quando foi definida na fonte mais nova.
            conn.execute("UPDATE cartoes_catalogo SET tag_id=?,cor=?,ordem=?,atualizado_em=? WHERE cartao_id=?",
                         (tag, cor or _cor(destino), ordem, _agora(), destino))
        for antiga in sorted(componente - {destino}):
            conn.execute("UPDATE cartoes_fontes SET cartao_id=? WHERE cartao_id=?", (destino, antiga))
            for tabela in ("fixas_contas", "fixas_mes", "fixas_descontos"):
                if not _tabela(conn, tabela):
                    continue
                colunas = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
                for coluna in ("forma_pagamento", "conta_id") if tabela == "fixas_contas" else ("forma_pagamento",):
                    if coluna in colunas:
                        conn.execute(f"UPDATE {tabela} SET {coluna}=? WHERE {coluna}=?", (destino, antiga))
            conn.execute("DELETE FROM cartoes_catalogo WHERE cartao_id=?", (antiga,))
        alterou = True
    if alterou and _tabela(conn, "app_meta"):
        _registrar_revisao(conn)


def garantir(conn):
    """Migração idempotente e descoberta automática. Não altera os dados de origem."""
    with _trava:
        transacao_anterior = conn.in_transaction
        nova_base = not _tabela(conn, "cartoes_catalogo")
        if nova_base:
            if fin.DATABASE_PATH.resolve() == (fin.ROOT / "pluggy.db").resolve():
                fin.create_database_backup("cartoes_genericos", min_interval_seconds=0)
            for comando in SCHEMA.split(";"):
                if comando.strip():
                    conn.execute(comando)
        colunas_tag = {r[1] for r in conn.execute("PRAGMA table_info(cartoes_tags)")}
        if "cor" not in colunas_tag:
            conn.execute("ALTER TABLE cartoes_tags ADD COLUMN cor TEXT NOT NULL DEFAULT ''")
        for tag_id, cor in conn.execute("SELECT tag_id,cor FROM cartoes_tags").fetchall():
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(cor or "")):
                conn.execute("UPDATE cartoes_tags SET cor=? WHERE tag_id=?", (_cor(tag_id), tag_id))
        contas = _contas(conn)
        fontes = {r["conta_id"]: r["cartao_id"] for r in conn.execute("SELECT * FROM cartoes_fontes")}
        legadas = {r["conta_id"]: dict(r) for r in conn.execute("SELECT * FROM cartoes_identidade")} if _tabela(conn, "cartoes_identidade") else {}
        por_id = {c["conta_id"]: c for c in contas}
        for conta in contas:
            fonte = conta["conta_id"]
            if fonte in fontes:
                continue
            # Uma identidade que já possui outra fonte nesta mesma conexão não
            # pode representar o cartão novo: ambos coexistem no item. Sem
            # este bloqueio, uma fonte antiga parecida poderia unir dois
            # cartões físicos distintos depois de uma reconexão.
            identidades_do_item = {
                cid for origem, cid in fontes.items()
                if origem in por_id and por_id[origem]["item_id"] == conta["item_id"]
            }
            candidatas = {
                cid for origem, cid in fontes.items()
                if cid not in identidades_do_item
                and origem in por_id
                and _mesma_fonte(conn, conta, por_id[origem])
            }
            if len(candidatas) == 1:
                cartao = candidatas.pop()
            else:
                cartao = "cartao:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "pluggy-card:" + fonte))
                antiga = legadas.get(fonte, {})
                # O sistema anterior persistia grupos/apelidos inferidos por
                # banco como se fossem escolhas do usuário. Não há marcador
                # capaz de distinguir esses defaults de uma edição manual;
                # portanto nenhum texto legado vira tag automaticamente.
                tag = None
                cor = antiga.get("cor") or _cor(cartao)
                conn.execute("INSERT OR IGNORE INTO cartoes_catalogo VALUES (?,?,?,?,?)",
                             (cartao, tag, cor, int(antiga.get("ordem") or 0), _agora()))
            conn.execute("INSERT OR IGNORE INTO cartoes_fontes VALUES (?,?,?)", (fonte, cartao, _agora()))
            fontes[fonte] = cartao
        _reconciliar_fontes(conn, contas)
        if _tabela(conn, "app_meta"):
            versao = conn.execute("SELECT valor FROM app_meta WHERE chave='cartoes_schema_versao'").fetchone()
            if not versao or versao[0] != "2":
                conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('cartoes_schema_versao','2')")
        if not transacao_anterior:
            conn.commit()


def identidades(conn) -> dict[str, dict[str, Any]]:
    garantir(conn)
    salvas = {r["conta_id"]: dict(r) for r in conn.execute(
        "SELECT f.conta_id, c.*, t.nome tag, t.cor tag_cor FROM cartoes_fontes f "
        "JOIN cartoes_catalogo c USING(cartao_id) LEFT JOIN cartoes_tags t USING(tag_id)")}
    mapa = {}
    for conta in _contas(conn):
        salva = salvas[conta["conta_id"]]
        credito = _json(conta["raw_json"]).get("creditData") or {}
        original = _nome_original(conta, credito)
        tag = salva["tag"] or ""
        grupo = salva["tag_id"] or salva["cartao_id"]
        mapa[conta["conta_id"]] = {
            "contaId": conta["conta_id"], "itemId": conta["item_id"], "cartaoId": salva["cartao_id"],
            "nomeOriginal": original, "nomeExibicao": tag or original, "nomeBanco": conta["nome"],
            "numero": _numero_final(conta["numero"]), "marca": credito.get("brand") or "",
            "nivel": credito.get("level") or "", "tagId": salva["tag_id"], "tag": tag,
            "grupo": grupo, "grupoId": grupo, "apelido": tag or original,
            "cor": (salva["tag_cor"] or _cor(grupo)) if tag else (salva["cor"] or _cor(grupo)),
            "ordem": salva["ordem"],
            "limiteCredito": conta["limite_credito"], "limiteDisponivel": conta["limite_disponivel"],
            "vencimento": conta["vencimento"], "fechamento": conta["fechamento"], "moeda": conta["moeda"],
            "identificado": bool(tag), "tipo": "cartao",
        }
    return mapa


def _ativas(conn, mapa):
    ultimas = {r[0]: r[1] for r in conn.execute("SELECT conta_id, MAX(data) FROM pluggy_transacoes GROUP BY conta_id")}
    importadas = {r[0]: r[1] for r in conn.execute("SELECT conta_id,importado_em FROM pluggy_contas")}
    grupos = defaultdict(list)
    for fonte, ident in mapa.items():
        grupos[ident["cartaoId"]].append(fonte)
    return {max(fontes, key=lambda f: (ultimas.get(f) or "", importadas.get(f) or "", f)) for fontes in grupos.values()}


def fontes_ativas(conn) -> set[str]:
    return _ativas(conn, identidades(conn))


def resolver_contas(conn, referencia: str) -> set[str]:
    mapa = identidades(conn)
    return {fonte for fonte, ident in mapa.items() if referencia in {fonte, ident["cartaoId"], ident["tagId"]}}


def colunas(conn, contas_visiveis: set[str] | None = None) -> list[dict[str, Any]]:
    mapa = identidades(conn)
    visiveis = _ativas(conn, mapa) if contas_visiveis is None else set(contas_visiveis) & _ativas(conn, mapa)
    grupos = defaultdict(list)
    for fonte, ident in mapa.items():
        if fonte in visiveis:
            grupos[ident["grupo"]].append(ident)
    resultado = []
    for grupo, membros in grupos.items():
        membros.sort(key=lambda m: (m["ordem"], m["nomeOriginal"], m["cartaoId"]))
        primeiro = membros[0]
        vencimentos = sorted({m["vencimento"] for m in membros if m["vencimento"]})
        resultado.append({
            "id": grupo, "nome": primeiro["nomeExibicao"], "nomeExibicao": primeiro["nomeExibicao"],
            "grupo": grupo, "grupoId": grupo, "tagId": primeiro["tagId"], "tag": primeiro["tag"],
            "cor": primeiro["cor"], "ordem": primeiro["ordem"],
            "contas": [m["contaId"] for m in membros], "cartoes": [m["cartaoId"] for m in membros],
            "membros": membros, "apelidos": [m["nomeOriginal"] for m in membros],
            "numero": primeiro["numero"] if len(membros) == 1 else "",
            "limiteCredito": primeiro["limiteCredito"] if len(membros) == 1 else None,
            "limiteDisponivel": primeiro["limiteDisponivel"] if len(membros) == 1 else None,
            "vencimento": vencimentos[0] if len(vencimentos) == 1 else "",
            "vencimentos": vencimentos, "tipo": "cartao",
        })
    return sorted(resultado, key=lambda x: (x["ordem"], x["nome"], x["id"]))


def payload():
    fin.ensure_database()
    with closing(fin.connect()) as conn:
        mapa = identidades(conn)
        ativas = _ativas(conn, mapa)
        tags = [{"id": r[0], "nome": r[1], "cor": r[2]} for r in conn.execute(
            "SELECT tag_id,nome,cor FROM cartoes_tags ORDER BY nome")]
        cartoes = [{**i, "ativa": True, "grupoTexto": i["tag"]} for fonte, i in mapa.items() if fonte in ativas]
        for cartao in cartoes:
            cartao["compartilhaColuna"] = sum(i["grupo"] == cartao["grupo"] for i in cartoes) > 1
        revisao = conn.execute("SELECT valor FROM app_meta WHERE chave='cartoes_revisao'").fetchone()
    return {"cartoes": sorted(cartoes, key=lambda c: (c["ordem"], c["nomeOriginal"])), "tags": tags,
            "grupos": [t["nome"] for t in tags], "paleta": PALETA,
            "versao": 2, "revisao": revisao[0] if revisao else ""}


def _registrar_revisao(conn):
    conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('cartoes_revisao',?)", (_agora(),))


def salvar(dados):
    itens = dados.get("cartoes")
    if not isinstance(itens, list) or not itens:
        raise ValueError("Envie a lista de cartões.")
    fin.ensure_database()
    with _trava, closing(fin.connect()) as conn:
        mapa = identidades(conn)
        validos = {i["cartaoId"] for i in mapa.values()}
        with conn:
            cores_tags: dict[str, str] = {}
            for item in itens:
                cartao = str(item.get("cartaoId") or "")
                if not cartao:
                    cartao = mapa.get(item.get("contaId"), {}).get("cartaoId", "")
                if cartao not in validos:
                    raise ValueError("Cartão desconhecido.")
                if "tag" not in item:
                    raise ValueError("Atualize a página para editar a tag do cartão.")
                tag = _obter_tag(conn, item["tag"])
                atual = conn.execute("SELECT cor,ordem FROM cartoes_catalogo WHERE cartao_id=?", (cartao,)).fetchone()
                cor_informada = item.get("cor") if "cor" in item else None
                cor = str(cor_informada if cor_informada is not None else atual["cor"] or "")
                if cor and not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
                    raise ValueError("Cor inválida.")
                try:
                    ordem = int(item.get("ordem", atual["ordem"]))
                except (TypeError, ValueError):
                    raise ValueError("Ordem inválida.") from None
                conn.execute("UPDATE cartoes_catalogo SET tag_id=?,cor=?,ordem=?,atualizado_em=? WHERE cartao_id=?",
                             (tag, cor, ordem, _agora(), cartao))
                if tag and cor_informada is not None:
                    anterior = cores_tags.setdefault(tag, cor)
                    if anterior.lower() != cor.lower():
                        raise ValueError("Cartões com a mesma tag devem usar a mesma cor.")
            for tag, cor in cores_tags.items():
                conn.execute("UPDATE cartoes_tags SET cor=?,atualizado_em=? WHERE tag_id=?",
                             (cor, _agora(), tag))
            _registrar_revisao(conn)
    return {"ok": True, **payload()}


def renomear_tag(tag_id, dados):
    nome = " ".join(str(dados.get("nome") or "").split())
    if not nome or len(nome) > 60 or not _normalizar(nome):
        raise ValueError("Informe um nome de tag com até 60 caracteres.")
    fin.ensure_database()
    with _trava, closing(fin.connect()) as conn:
        garantir(conn)
        with conn:
            if not conn.execute("SELECT 1 FROM cartoes_tags WHERE tag_id=?", (tag_id,)).fetchone():
                raise ValueError("Tag desconhecida.")
            outra = conn.execute("SELECT tag_id FROM cartoes_tags WHERE normalizado=?", (_normalizar(nome),)).fetchone()
            if outra and outra[0] != tag_id:
                conn.execute("UPDATE cartoes_catalogo SET tag_id=? WHERE tag_id=?", (outra[0], tag_id))
                # Referências já salvas em planejamento acompanham a união.
                for tabela in ("fixas_contas", "fixas_mes", "fixas_descontos"):
                    if _tabela(conn, tabela):
                        conn.execute(f"UPDATE {tabela} SET forma_pagamento=? WHERE forma_pagamento=?", (outra[0], tag_id))
                conn.execute("DELETE FROM cartoes_tags WHERE tag_id=?", (tag_id,))
            else:
                conn.execute("UPDATE cartoes_tags SET nome=?,normalizado=?,atualizado_em=? WHERE tag_id=?",
                             (nome, _normalizar(nome), _agora(), tag_id))
            _registrar_revisao(conn)
    return {"ok": True, **payload()}


def esquecer(referencia):
    fin.ensure_database()
    with _trava, closing(fin.connect()) as conn:
        mapa = identidades(conn)
        cartao = mapa.get(referencia, {}).get("cartaoId", referencia)
        with conn:
            conn.execute("UPDATE cartoes_catalogo SET tag_id=NULL,atualizado_em=? WHERE cartao_id=?", (_agora(), cartao))
            _registrar_revisao(conn)
    return {"ok": True, **payload()}
