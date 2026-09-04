"""Carteira de investimentos de todas as conexões da Pluggy."""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from collections import defaultdict
from datetime import datetime
from typing import Any

import requests

import banco as fin
from pluggy_sync import API_URL, get_api_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS pluggy_investimentos (
  investimento_chave TEXT PRIMARY KEY,
  investimento_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  nome TEXT NOT NULL DEFAULT '',
  tipo TEXT NOT NULL DEFAULT '',
  subtipo TEXT NOT NULL DEFAULT '',
  moeda TEXT NOT NULL DEFAULT 'BRL',
  saldo_liquido REAL NOT NULL DEFAULT 0,
  valor_bruto REAL NOT NULL DEFAULT 0,
  valor_original REAL,
  lucro_informado REAL,
  disponivel_resgate REAL,
  impostos REAL,
  impostos_2 REAL,
  data_referencia TEXT NOT NULL DEFAULT '',
  quantidade REAL,
  valor_cota REAL,
  taxa REAL,
  tipo_taxa TEXT NOT NULL DEFAULT '',
  taxa_fixa_anual REAL,
  emissor TEXT NOT NULL DEFAULT '',
  data_emissao TEXT NOT NULL DEFAULT '',
  vencimento TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  importado_em TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS pluggy_investimento_movimentos (
  movimento_chave TEXT PRIMARY KEY,
  movimento_id TEXT NOT NULL,
  investimento_chave TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '',
  data_liquidacao TEXT NOT NULL DEFAULT '',
  tipo TEXT NOT NULL DEFAULT '',
  descricao TEXT NOT NULL DEFAULT '',
  valor_bruto REAL NOT NULL DEFAULT 0,
  valor_liquido REAL,
  quantidade REAL,
  valor_cota REAL,
  tipo_movimento TEXT NOT NULL DEFAULT '',
  importado_em TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (investimento_chave) REFERENCES pluggy_investimentos (investimento_chave)
    ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_investimento_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  investimento_chave TEXT NOT NULL,
  coletado_em TEXT NOT NULL,
  data_referencia TEXT NOT NULL DEFAULT '',
  saldo_liquido REAL NOT NULL DEFAULT 0,
  valor_bruto REAL NOT NULL DEFAULT 0,
  valor_original REAL,
  disponivel_resgate REAL,
  status TEXT NOT NULL DEFAULT '',
  UNIQUE (investimento_chave, coletado_em)
);

CREATE INDEX IF NOT EXISTS idx_invest_item ON pluggy_investimentos (item_id);
CREATE INDEX IF NOT EXISTS idx_invest_status ON pluggy_investimentos (status);
CREATE INDEX IF NOT EXISTS idx_invest_mov_data ON pluggy_investimento_movimentos (data);
CREATE INDEX IF NOT EXISTS idx_invest_snap_data ON pluggy_investimento_snapshots (coletado_em);
"""

_trava = threading.Lock()


def _numero(valor: object) -> float | None:
    if valor is None:
        return None
    try:
        return float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _texto(valor: object) -> str:
    return "" if valor is None else str(valor)


def _norm(valor: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", valor).encode("ascii", "ignore").decode()
    return sem_acento.casefold()


def garantir_tabelas(conn=None) -> None:
    if conn is not None:
        conn.executescript(SCHEMA)
        return
    fin.ensure_database()
    with fin.connect() as banco:
        banco.executescript(SCHEMA)
        banco.commit()


def _listar(api_key: str, caminho: str, parametros: dict[str, Any]) -> list[dict[str, Any]]:
    resultados: list[dict[str, Any]] = []
    pagina = 1
    while True:
        resposta = requests.get(
            f"{API_URL}{caminho}",
            headers={"X-API-KEY": api_key},
            params={**parametros, "page": pagina, "pageSize": 500},
            timeout=40,
        )
        resposta.raise_for_status()
        corpo = resposta.json()
        lote = corpo.get("results") or []
        resultados.extend(lote)
        if pagina >= int(corpo.get("totalPages") or 1) or not lote:
            return resultados
        pagina += 1


def _snapshot_mudou(conn, chave: str, inv: dict[str, Any]) -> bool:
    anterior = conn.execute(
        "SELECT data_referencia, saldo_liquido, valor_bruto, valor_original, "
        "disponivel_resgate, status FROM pluggy_investimento_snapshots "
        "WHERE investimento_chave = ? ORDER BY id DESC LIMIT 1",
        (chave,),
    ).fetchone()
    atual = (
        _texto(inv.get("date")), _numero(inv.get("balance")) or 0,
        _numero(inv.get("amount")) or 0, _numero(inv.get("amountOriginal")),
        _numero(inv.get("amountWithdrawal")), _texto(inv.get("status")),
    )
    if not anterior:
        return True
    antigo = tuple(anterior)
    return antigo != atual


def sincronizar(item_ids: list[str] | None = None) -> dict[str, Any]:
    """Coleta posições, movimentos e snapshots de todas as conexões informadas."""
    if not _trava.acquire(blocking=False):
        return {"ok": True, "resultado": "ja_rodando"}
    try:
        garantir_tabelas()
        with fin.connect() as conn:
            if item_ids is None:
                item_ids = [r[0] for r in conn.execute(
                    "SELECT item_id FROM pluggy_itens ORDER BY item_id"
                )]
        api_key = get_api_key()
        agora = datetime.now().isoformat(timespec="seconds")
        total_investimentos = total_movimentos = 0
        falhas: list[str] = []

        for item_id in item_ids:
            try:
                posicoes = _listar(api_key, "/investments", {"itemId": item_id})
            except Exception as exc:  # noqa: BLE001 - um item não bloqueia os demais
                falhas.append(f"{item_id[:8]}: {exc}")
                continue

            with fin.connect() as conn:
                garantir_tabelas(conn)
                for inv in posicoes:
                    inv_id = _texto(inv.get("id"))
                    if not inv_id:
                        continue
                    chave = f"{item_id}:{inv_id}"
                    conn.execute(
                        "INSERT INTO pluggy_investimentos (investimento_chave, investimento_id, item_id, "
                        "nome, tipo, subtipo, moeda, saldo_liquido, valor_bruto, valor_original, "
                        "lucro_informado, disponivel_resgate, impostos, impostos_2, data_referencia, "
                        "quantidade, valor_cota, taxa, tipo_taxa, taxa_fixa_anual, emissor, data_emissao, "
                        "vencimento, status, importado_em, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(investimento_chave) DO UPDATE SET nome=excluded.nome, tipo=excluded.tipo, "
                        "subtipo=excluded.subtipo, moeda=excluded.moeda, saldo_liquido=excluded.saldo_liquido, "
                        "valor_bruto=excluded.valor_bruto, valor_original=excluded.valor_original, "
                        "lucro_informado=excluded.lucro_informado, disponivel_resgate=excluded.disponivel_resgate, "
                        "impostos=excluded.impostos, impostos_2=excluded.impostos_2, "
                        "data_referencia=excluded.data_referencia, quantidade=excluded.quantidade, "
                        "valor_cota=excluded.valor_cota, taxa=excluded.taxa, tipo_taxa=excluded.tipo_taxa, "
                        "taxa_fixa_anual=excluded.taxa_fixa_anual, emissor=excluded.emissor, "
                        "data_emissao=excluded.data_emissao, vencimento=excluded.vencimento, "
                        "status=excluded.status, importado_em=excluded.importado_em, raw_json=excluded.raw_json",
                        (chave, inv_id, item_id, _texto(inv.get("name")), _texto(inv.get("type")),
                         _texto(inv.get("subtype")), _texto(inv.get("currencyCode")) or "BRL",
                         _numero(inv.get("balance")) or 0, _numero(inv.get("amount")) or 0,
                         _numero(inv.get("amountOriginal")), _numero(inv.get("amountProfit")),
                         _numero(inv.get("amountWithdrawal")), _numero(inv.get("taxes")),
                         _numero(inv.get("taxes2")), _texto(inv.get("date")), _numero(inv.get("quantity")),
                         _numero(inv.get("value")), _numero(inv.get("rate")), _texto(inv.get("rateType")),
                         _numero(inv.get("fixedAnnualRate")), _texto(inv.get("issuer")),
                         _texto(inv.get("issueDate")), _texto(inv.get("dueDate")), _texto(inv.get("status")),
                         agora, json.dumps(inv, ensure_ascii=False)))
                    # Cada coleta precisa conter todos os lotes, inclusive os
                    # que não mudaram. Assim a soma do horário representa o
                    # valor total real da carteira, não só o pedaço alterado.
                    conn.execute(
                        "INSERT OR IGNORE INTO pluggy_investimento_snapshots (investimento_chave, coletado_em, "
                        "data_referencia, saldo_liquido, valor_bruto, valor_original, disponivel_resgate, status) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (chave, agora, _texto(inv.get("date")), _numero(inv.get("balance")) or 0,
                         _numero(inv.get("amount")) or 0, _numero(inv.get("amountOriginal")),
                         _numero(inv.get("amountWithdrawal")), _texto(inv.get("status"))))
                    total_investimentos += 1
                conn.commit()

            for inv in posicoes:
                inv_id = _texto(inv.get("id"))
                if not inv_id:
                    continue
                chave = f"{item_id}:{inv_id}"
                try:
                    movimentos = _listar(api_key, f"/investments/{inv_id}/transactions", {})
                except Exception as exc:  # noqa: BLE001
                    falhas.append(f"{inv_id[:8]} movimentos: {exc}")
                    continue
                with fin.connect() as conn:
                    for mov in movimentos:
                        mov_id = _texto(mov.get("id"))
                        if not mov_id:
                            continue
                        mov_chave = f"{chave}:{mov_id}"
                        conn.execute(
                            "INSERT INTO pluggy_investimento_movimentos (movimento_chave, movimento_id, "
                            "investimento_chave, data, data_liquidacao, tipo, descricao, valor_bruto, "
                            "valor_liquido, quantidade, valor_cota, tipo_movimento, importado_em, raw_json) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(movimento_chave) DO UPDATE SET "
                            "data=excluded.data, data_liquidacao=excluded.data_liquidacao, tipo=excluded.tipo, "
                            "descricao=excluded.descricao, valor_bruto=excluded.valor_bruto, "
                            "valor_liquido=excluded.valor_liquido, quantidade=excluded.quantidade, "
                            "valor_cota=excluded.valor_cota, tipo_movimento=excluded.tipo_movimento, "
                            "importado_em=excluded.importado_em, raw_json=excluded.raw_json",
                            (mov_chave, mov_id, chave, _texto(mov.get("date")), _texto(mov.get("tradeDate")),
                             _texto(mov.get("type")), _texto(mov.get("description")),
                             _numero(mov.get("amount")) or 0, _numero(mov.get("netAmount")),
                             _numero(mov.get("quantity")), _numero(mov.get("value")),
                             _texto(mov.get("movementType")), agora, json.dumps(mov, ensure_ascii=False)))
                        total_movimentos += 1
                    conn.commit()

        return {"ok": not falhas, "investimentos": total_investimentos,
                "movimentos": total_movimentos, "falhas": falhas, "coletadoEm": agora}
    finally:
        _trava.release()


def _rotulos_itens(conn) -> dict[str, str]:
    rotulos: dict[str, str] = {}
    for row in conn.execute(
        "SELECT i.item_id, i.conector, GROUP_CONCAT(DISTINCT c.nome) contas "
        "FROM pluggy_itens i LEFT JOIN pluggy_contas c ON c.item_id=i.item_id GROUP BY i.item_id"
    ):
        bruto = " ".join(filter(None, [row["conector"], row["contas"] or ""]))
        n = _norm(bruto)
        if "itau" in n:
            rotulos[row["item_id"]] = "Itaú"
        elif "nubank" in n or "nu pagamentos" in n:
            rotulos[row["item_id"]] = "Nubank"
        elif "inter" in n:
            rotulos[row["item_id"]] = "Inter"
        else:
            rotulos[row["item_id"]] = (
                row["conector"] if row["conector"] and _norm(row["conector"]) != "meupluggy"
                else row["contas"] or "Instituição"
            )
    return rotulos


def _soma(linhas, campo: str) -> float:
    return round(sum(float(l[campo] or 0) for l in linhas), 2)


def _movimentos_sem_posicao(conn, itens_com_posicao: set[str], rotulos: dict[str, str]) -> list[dict[str, Any]]:
    """Identifica operações explícitas de renda fixa sem inventar uma posição.

    Transferências, Pix e estornos não são aplicações. A evidência fica
    separada do saldo/rendimento; quando a conexão passa a entregar posições,
    a API assume a carteira e esse fallback deixa de ser usado.
    """
    movimentos = []
    for m in conn.execute(
        "SELECT t.*, c.item_id, c.nome conta FROM pluggy_transacoes t "
        "JOIN pluggy_contas c ON c.conta_id=t.conta_id "
        "WHERE c.tipo='BANK' AND t.valor<>0 AND t.status NOT IN ('PENDING','CANCELED','CANCELLED') "
        "ORDER BY t.data DESC, t.transacao_id DESC"
    ):
        if m["item_id"] in itens_com_posicao:
            continue
        descricao = _norm(m["descricao"]).strip()
        operacao = re.match(r"^(emissao|aplicacao|aplic|compra|resgate|venda|vencimento)\b", descricao)
        produto = re.search(r"\b(cdb|rdb|lci|lca|lc)\b", descricao)
        if not operacao or not produto or re.search(r"\b(estorno|cancelamento|cancelad[oa])\b", descricao):
            continue
        tipo = "BUY" if operacao[1] in {"emissao", "aplicacao", "aplic", "compra"} else "SELL"
        valor = float(m["valor"])
        if (tipo == "BUY" and valor >= 0) or (tipo == "SELL" and valor <= 0):
            continue
        movimentos.append({
            "id": m["transacao_id"], "data": m["data"], "liquidacao": m["data"],
            "tipo": tipo, "descricao": m["descricao"], "valor": abs(valor),
            "sinal": -valor, "bruto": abs(valor), "quantidade": None, "valorCota": None,
            "conta": m["conta"], "contaId": m["conta_id"], "itemId": m["item_id"],
            "instituicao": rotulos.get(m["item_id"], "Instituição"), "moeda": m["moeda"],
            "origem": "Extrato da conta", "confirmadoExtrato": True, "posicaoPendente": True,
        })
    return movimentos


def _meses_movimentos(movimentos) -> list[dict[str, Any]]:
    mensal = defaultdict(lambda: {"aplicacoes": 0.0, "resgates": 0.0, "quantidade": 0})
    for m in movimentos:
        dados = mensal[m["data"][:7]]
        dados["quantidade"] += 1
        if m["tipo"] == "BUY":
            dados["aplicacoes"] += m["valor"]
        elif m["tipo"] == "SELL":
            dados["resgates"] += m["valor"]
    return [{"mes": mes, "aplicacoes": round(dados["aplicacoes"], 2),
             "resgates": round(dados["resgates"], 2), "quantidade": dados["quantidade"],
             "liquido": round(dados["aplicacoes"] - dados["resgates"], 2)}
            for mes, dados in sorted(mensal.items())]


def payload() -> dict[str, Any]:
    garantir_tabelas()
    with fin.connect() as conn:
        rotulos = _rotulos_itens(conn)
        posicoes = list(conn.execute(
            "SELECT i.*, (SELECT MIN(substr(m.data,1,10)) FROM pluggy_investimento_movimentos m "
            "WHERE m.investimento_chave=i.investimento_chave AND m.tipo='BUY') AS data_aplicacao "
            "FROM pluggy_investimentos i ORDER BY importado_em DESC, data_referencia DESC, investimento_chave"
        ))
        # Usa inclusive as cópias de reconexões para não recriar por extrato
        # uma posição que já foi reconhecida sob outra chave.
        itens_com_posicao = {p["item_id"] for p in posicoes}
        # Reconexões podem repetir o mesmo ID. Todos os componentes da tela
        # usam a mesma posição mais recente, inclusive lotes e movimentos.
        unicas = {}
        for p in posicoes:
            unicas.setdefault(p["investimento_id"], p)
        # Importações antigas podem não ter o nome do conector. Uma cópia do
        # mesmo ID em outra conexão fornece a instituição sem adivinhar pelo
        # emissor do produto (que pode ser diferente da corretora).
        for p in posicoes:
            principal = unicas[p["investimento_id"]]
            nome = rotulos.get(p["item_id"], "Instituição")
            if rotulos.get(principal["item_id"], "Instituição") == "Instituição" and nome != "Instituição":
                rotulos[principal["item_id"]] = nome
        posicoes = list(unicas.values())
        chaves = {p["investimento_chave"] for p in posicoes}
        ativos = [p for p in posicoes if p["status"] == "ACTIVE"]

        movimentos_investimento = list(conn.execute(
            "SELECT m.*, i.nome investimento_nome, i.item_id FROM pluggy_investimento_movimentos m "
            "JOIN pluggy_investimentos i ON i.investimento_chave=m.investimento_chave "
            "ORDER BY m.data DESC, m.movimento_id DESC"
        ))
        movimentos_investimento = [m for m in movimentos_investimento if m["investimento_chave"] in chaves]

        # O endpoint de investimentos pode omitir resgates recentes ou desmembrar um
        # único resgate em vários lotes. O fallback conhecido de COFRINHOS só
        # concilia uma conexão cuja carteira inteira corresponde a esse produto.
        # Outros produtos e instituições preservam os movimentos da API.
        produtos_por_item = defaultdict(set)
        for p in posicoes:
            produtos_por_item[p["item_id"]].add(_norm(p["nome"]))
        itens_cofrinhos = {
            item for item, produtos in produtos_por_item.items()
            if all("cofrinh" in nome for nome in produtos)
            or (rotulos.get(item) == "Itaú" and produtos == {"cdb - itau unibanco s.a."})
        }
        movimentos_extrato = list(conn.execute(
            "SELECT t.transacao_id, t.data, t.descricao, t.valor, t.tipo, "
            "c.nome conta, c.conta_id, c.item_id FROM pluggy_transacoes t "
            "JOIN pluggy_contas c ON c.conta_id=t.conta_id "
            "WHERE upper(t.descricao) LIKE '%COFRINH%' AND t.valor <> 0 "
            "ORDER BY t.data DESC, t.transacao_id DESC"
        ))
        movimentos_extrato = [m for m in movimentos_extrato if m["item_id"] in itens_cofrinhos]

        snaps = list(conn.execute(
            "SELECT s.* FROM pluggy_investimento_snapshots s ORDER BY s.coletado_em, s.id"
        ))
        snaps = [s for s in snaps if s["investimento_chave"] in chaves]
        sem_posicao = _movimentos_sem_posicao(conn, itens_com_posicao, rotulos)

        por_instituicao: dict[str, dict[str, Any]] = {
            nome: {"instituicao": nome, "ativos": 0, "encerrados": 0,
                   "liquido": 0.0, "bruto": 0.0, "original": 0.0}
            for nome in rotulos.values()
        }
        for p in posicoes:
            nome = rotulos.get(p["item_id"], "Instituição")
            grupo = por_instituicao.setdefault(nome, {"instituicao": nome, "ativos": 0,
                "encerrados": 0, "liquido": 0.0, "bruto": 0.0, "original": 0.0})
            if p["status"] == "ACTIVE":
                grupo["ativos"] += 1
                grupo["liquido"] += p["saldo_liquido"] or 0
                grupo["bruto"] += p["valor_bruto"] or 0
                grupo["original"] += p["valor_original"] or 0
            else:
                grupo["encerrados"] += 1
        for m in sem_posicao:
            grupo = por_instituicao[m["instituicao"]]
            campo = "aplicacoesExtrato" if m["tipo"] == "BUY" else "resgatesExtrato"
            grupo[campo] = round(grupo.get(campo, 0) + m["valor"], 2)
            grupo["posicaoPendente"] = True

    bruto = _soma(ativos, "valor_bruto")
    liquido = _soma(ativos, "saldo_liquido")
    original = _soma(ativos, "valor_original")
    disponivel = _soma(ativos, "disponivel_resgate")

    lotes = []
    for p in ativos:
        raw = json.loads(p["raw_json"] or "{}")
        p_bruto = float(p["valor_bruto"] or 0)
        p_liq = float(p["saldo_liquido"] or 0)
        p_orig = float(p["valor_original"] or 0)
        lotes.append({
            "id": p["investimento_id"], "nome": p["nome"], "tipo": p["tipo"],
            "itemId": p["item_id"], "instituicao": rotulos.get(p["item_id"], "Instituição"),
            "moeda": p["moeda"],
            "subtipo": p["subtipo"], "dataReferencia": p["data_referencia"],
            "dataAplicacao": p["data_aplicacao"] or p["data_emissao"],
            "original": p_orig, "bruto": p_bruto, "liquido": p_liq,
            "disponivel": p["disponivel_resgate"], "rendimentoBruto": p_bruto - p_orig,
            "rendimentoLiquido": p_liq - p_orig, "impostosEstimados": p_bruto - p_liq,
            "rentabilidadeBruta": ((p_bruto / p_orig - 1) * 100) if p_orig else 0,
            "taxa": p["taxa"], "tipoTaxa": p["tipo_taxa"],
            "taxaFixaAnual": p["taxa_fixa_anual"], "emissor": p["emissor"],
            "emissao": p["data_emissao"], "vencimento": p["vencimento"], "status": p["status"],
            "codigo": raw.get("code"), "numero": raw.get("number"), "proprietario": raw.get("owner"),
            "carencia": raw.get("gracePeriodDate"), "quantidade": p["quantidade"], "valorCota": p["valor_cota"],
        })

    movs_extrato = []
    for m in movimentos_extrato:
        valor_extrato = float(m["valor"] or 0)
        valor = abs(valor_extrato)
        tipo = "BUY" if valor_extrato < 0 else "SELL"
        sinal = valor if tipo == "BUY" else -valor
        data = m["data"]
        movs_extrato.append({"id": m["transacao_id"], "data": data, "liquidacao": data,
                             "tipo": tipo, "descricao": m["descricao"], "valor": valor,
                             "sinal": sinal, "bruto": valor, "quantidade": None,
                             "valorCota": None, "conta": m["conta"],
                             "itemId": m["item_id"], "contaId": m["conta_id"],
                             "instituicao": rotulos.get(m["item_id"], "Instituição"),
                             "origem": "Extrato da conta", "confirmadoExtrato": True})

    movs_investimento = []
    for m in movimentos_investimento:
        valor = abs(float(m["valor_liquido"] if m["valor_liquido"] is not None else m["valor_bruto"] or 0))
        tipo = m["tipo"]
        movs_investimento.append({"id": m["movimento_id"], "data": m["data"],
                                  "liquidacao": m["data_liquidacao"], "tipo": tipo,
                                  "descricao": m["descricao"] or m["investimento_nome"],
                                  "valor": valor, "sinal": valor if tipo == "BUY" else -valor if tipo == "SELL" else 0,
                                  "bruto": m["valor_bruto"], "quantidade": m["quantidade"],
                                  "valorCota": m["valor_cota"],
                                  "itemId": m["item_id"],
                                  "instituicao": rotulos.get(m["item_id"], "Instituição"),
                                  "conta": rotulos.get(m["item_id"], "Instituição"),
                                  "origem": "API de investimentos", "confirmadoExtrato": False})

    # Nunca cruza conexões, mesmo que sejam do mesmo banco. Sem evidência no
    # extrato, mantém a API: ausência de extrato não significa movimento zero.
    def chave_movimento(m):
        return m["itemId"], m["data"][:7], m["tipo"]

    totais_extrato: dict[tuple[str, str, str], float] = defaultdict(float)
    for m in movs_extrato:
        totais_extrato[chave_movimento(m)] += m["valor"]
    totais_investimento: dict[tuple[str, str, str], float] = defaultdict(float)
    for m in movs_investimento:
        totais_investimento[chave_movimento(m)] += m["valor"]

    chaves_divergentes = {chave for chave in totais_extrato
                          if abs(totais_extrato.get(chave, 0) - totais_investimento.get(chave, 0)) >= .01}
    for m in movs_investimento:
        m["confirmadoExtrato"] = chave_movimento(m) in totais_extrato and chave_movimento(m) not in chaves_divergentes
    movs = ([m for m in movs_investimento if chave_movimento(m) not in chaves_divergentes] +
            [m for m in movs_extrato if chave_movimento(m) in chaves_divergentes])
    # A curva do saldo informado não deve subtrair fluxos de aplicações cuja
    # posição ainda não entrou nesse saldo.
    meses_posicoes = _meses_movimentos(movs)
    movs.extend(sem_posicao)
    movs.sort(key=lambda m: (m["data"], m["id"]), reverse=True)

    meses = _meses_movimentos(movs)

    divergencias = []
    for item_id, mes, tipo in sorted(chaves_divergentes):
        divergencias.append({"itemId": item_id, "instituicao": rotulos.get(item_id, "Instituição"),
                             "mes": mes, "tipo": tipo,
                             "extrato": round(totais_extrato[(item_id, mes, tipo)], 2),
                             "investimentos": round(totais_investimento.get((item_id, mes, tipo), 0), 2)})

    por_coleta: dict[str, dict[str, float]] = defaultdict(lambda: {"liquido": 0, "bruto": 0, "original": 0})
    ultimos = {}
    for s in snaps:
        ultimos[s["investimento_chave"]] = s
        # Uma atualização parcial não zera os investimentos das outras conexões.
        atuais = [p for p in ultimos.values() if p["status"] == "ACTIVE"]
        por_coleta[s["coletado_em"]] = {
            "liquido": _soma(atuais, "saldo_liquido"),
            "bruto": _soma(atuais, "valor_bruto"),
            "original": _soma(atuais, "valor_original"),
        }

    return {
        "resumo": {"liquido": liquido, "bruto": bruto, "original": original,
                   "disponivel": disponivel, "rendimentoBruto": round(bruto-original, 2),
                   "rendimentoLiquido": round(liquido-original, 2),
                   "impostosEstimados": round(bruto-liquido, 2), "lotesAtivos": len(ativos),
                   "lotesEncerrados": len(posicoes)-len(ativos), "movimentos": len(movs),
                   "aplicacoesSemPosicao": _soma([m for m in sem_posicao if m["tipo"] == "BUY"], "valor"),
                   "resgatesSemPosicao": _soma([m for m in sem_posicao if m["tipo"] == "SELL"], "valor"),
                   "dataReferencia": max((p["data_referencia"] for p in ativos), default=""),
                   "coletadoEm": max((p["importado_em"] for p in posicoes), default="")},
        "lotes": lotes, "movimentos": movs, "meses": meses,
        "movimentosSemPosicao": sem_posicao, "mesesPosicoes": meses_posicoes,
        "confirmacaoExtrato": {"fontePrincipal": "API de investimentos Pluggy",
                                "fallback": "Extrato da conta via Pluggy, quando conciliável",
                                "criterio": "por conexão, mês e tipo; fallback COFRINHOS apenas para carteira compatível",
                                "mesesComDiscrepancia": len({d["mes"] for d in divergencias}),
                                "divergenciasEndpointInvestimentos": divergencias},
        "snapshots": [{"coletadoEm": k, **{x: round(v, 2) for x, v in d.items()}}
                      for k, d in sorted(por_coleta.items())],
        "instituicoes": [{**g, "liquido": round(g["liquido"], 2),
                           "bruto": round(g["bruto"], 2), "original": round(g["original"], 2)}
                          for g in sorted(por_instituicao.values(), key=lambda x: -x["liquido"])],
    }
