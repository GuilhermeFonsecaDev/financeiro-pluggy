"""Investimentos coletados pela Pluggy e visão detalhada da Caixinha Itaú."""

from __future__ import annotations

import json
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
    """Coleta posições e movimentos. Snapshots só são gravados quando algo muda."""
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
                    # valor total real da Caixinha, não só o pedaço alterado.
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
            rotulos[row["item_id"]] = row["conector"] or (row["contas"] or "Instituição")
    return rotulos


def _soma(linhas, campo: str) -> float:
    return round(sum(float(l[campo] or 0) for l in linhas), 2)


def payload() -> dict[str, Any]:
    garantir_tabelas()
    with fin.connect() as conn:
        rotulos = _rotulos_itens(conn)
        posicoes = list(conn.execute(
            "SELECT i.*, (SELECT MIN(substr(m.data,1,10)) FROM pluggy_investimento_movimentos m "
            "WHERE m.investimento_chave=i.investimento_chave AND m.tipo='BUY') AS data_aplicacao "
            "FROM pluggy_investimentos i ORDER BY status='ACTIVE' DESC, data_referencia DESC"
        ))
        itau_chaves = {p["investimento_chave"] for p in posicoes
                       if rotulos.get(p["item_id"]) == "Itaú"}
        itau_item_ids = {p["item_id"] for p in posicoes
                         if rotulos.get(p["item_id"]) == "Itaú"}
        itau = [p for p in posicoes if p["investimento_chave"] in itau_chaves]
        ativos = [p for p in itau if p["status"] == "ACTIVE"]

        movimentos_investimento = list(conn.execute(
            "SELECT m.*, i.nome investimento_nome FROM pluggy_investimento_movimentos m "
            "JOIN pluggy_investimentos i ON i.investimento_chave=m.investimento_chave "
            f"WHERE m.investimento_chave IN ({','.join('?' for _ in itau_chaves) or "''"}) "
            "ORDER BY m.data DESC, m.movimento_id DESC", tuple(itau_chaves)
        )) if itau_chaves else []

        # O endpoint de investimentos pode omitir resgates recentes ou desmembrar um
        # único resgate em vários lotes. Para o fluxo mensal, o extrato da conta é a
        # fonte de verdade: cada aplicação/resgate COFRINHOS aparece com o valor que
        # efetivamente entrou ou saiu da conta corrente.
        movimentos_extrato = list(conn.execute(
            "SELECT t.transacao_id, t.data, t.descricao, t.valor, t.tipo, "
            "c.nome conta, c.item_id FROM pluggy_transacoes t "
            "JOIN pluggy_contas c ON c.conta_id=t.conta_id "
            f"WHERE c.item_id IN ({','.join('?' for _ in itau_item_ids) or "''"}) "
            "AND upper(t.descricao) LIKE '%COFRINH%' AND t.valor <> 0 "
            "ORDER BY t.data DESC, t.transacao_id DESC", tuple(itau_item_ids)
        )) if itau_item_ids else []

        snaps = list(conn.execute(
            "SELECT s.* FROM pluggy_investimento_snapshots s "
            f"WHERE s.investimento_chave IN ({','.join('?' for _ in itau_chaves) or "''"}) "
            "ORDER BY s.coletado_em", tuple(itau_chaves)
        )) if itau_chaves else []

        por_instituicao: dict[str, dict[str, Any]] = {}
        ids_contados: set[str] = set()
        for p in posicoes:
            # Uma conexão antiga do mesmo banco pode devolver os mesmos IDs.
            # Consolida pelo identificador da Pluggy para não contar em dobro.
            if p["investimento_id"] in ids_contados:
                continue
            ids_contados.add(p["investimento_id"])
            nome = rotulos.get(p["item_id"], "Instituição")
            if nome == "Instituição":
                produto = _norm(p["nome"] or "")
                if "inter" in produto:
                    nome = "Inter"
                elif "nu financeira" in produto or "nubank" in produto:
                    nome = "Nubank"
                elif "itau" in produto:
                    nome = "Itaú"
            grupo = por_instituicao.setdefault(nome, {"instituicao": nome, "ativos": 0,
                "encerrados": 0, "liquido": 0.0, "bruto": 0.0, "original": 0.0})
            if p["status"] == "ACTIVE":
                grupo["ativos"] += 1
                grupo["liquido"] += p["saldo_liquido"] or 0
                grupo["bruto"] += p["valor_bruto"] or 0
                grupo["original"] += p["valor_original"] or 0
            else:
                grupo["encerrados"] += 1

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
                             "origem": "Extrato Itaú", "confirmadoExtrato": True})

    movs_investimento = []
    for m in movimentos_investimento:
        valor = abs(float(m["valor_liquido"] if m["valor_liquido"] is not None else m["valor_bruto"] or 0))
        tipo = m["tipo"]
        movs_investimento.append({"id": m["movimento_id"], "data": m["data"],
                                  "liquidacao": m["data_liquidacao"], "tipo": tipo,
                                  "descricao": m["descricao"] or m["investimento_nome"],
                                  "valor": valor, "sinal": valor if tipo == "BUY" else -valor,
                                  "bruto": m["valor_bruto"], "quantidade": m["quantidade"],
                                  "valorCota": m["valor_cota"], "conta": "Itaú",
                                  "origem": "API de investimentos", "confirmadoExtrato": True})

    # API é a fonte principal. O extrato substitui somente o mês/tipo cujo total
    # esteja ausente ou diferente, evitando tanto omissões quanto dupla contagem.
    totais_extrato: dict[tuple[str, str], float] = defaultdict(float)
    for m in movs_extrato:
        totais_extrato[(m["data"][:7], m["tipo"])] += m["valor"]
    totais_investimento: dict[tuple[str, str], float] = defaultdict(float)
    for m in movs_investimento:
        totais_investimento[(m["data"][:7], m["tipo"])] += m["valor"]

    chaves_divergentes = {chave for chave in set(totais_extrato) | set(totais_investimento)
                          if abs(totais_extrato.get(chave, 0) - totais_investimento.get(chave, 0)) >= .01}
    movs = ([m for m in movs_investimento if (m["data"][:7], m["tipo"]) not in chaves_divergentes] +
            [m for m in movs_extrato if (m["data"][:7], m["tipo"]) in chaves_divergentes])
    movs.sort(key=lambda m: (m["data"], m["id"]), reverse=True)

    mensal: dict[str, dict[str, Any]] = defaultdict(lambda: {"aplicacoes": 0.0, "resgates": 0.0, "quantidade": 0})
    for m in movs:
        dados = mensal[m["data"][:7]]
        dados["quantidade"] += 1
        if m["tipo"] == "BUY": dados["aplicacoes"] += m["valor"]
        elif m["tipo"] == "SELL": dados["resgates"] += m["valor"]
    meses = [{"mes": mes, "aplicacoes": round(dados["aplicacoes"], 2),
              "resgates": round(dados["resgates"], 2), "quantidade": dados["quantidade"],
              "liquido": round(dados["aplicacoes"] - dados["resgates"], 2)}
             for mes, dados in sorted(mensal.items())]

    divergencias = []
    for mes, tipo in sorted(chaves_divergentes):
        divergencias.append({"mes": mes, "tipo": tipo,
                             "extrato": round(totais_extrato.get((mes, tipo), 0), 2),
                             "investimentos": round(totais_investimento.get((mes, tipo), 0), 2)})

    por_coleta: dict[str, dict[str, float]] = defaultdict(lambda: {"liquido": 0, "bruto": 0, "original": 0})
    for s in snaps:
        if s["status"] == "ACTIVE":
            por_coleta[s["coletado_em"]]["liquido"] += s["saldo_liquido"] or 0
            por_coleta[s["coletado_em"]]["bruto"] += s["valor_bruto"] or 0
            por_coleta[s["coletado_em"]]["original"] += s["valor_original"] or 0

    return {
        "resumo": {"liquido": liquido, "bruto": bruto, "original": original,
                   "disponivel": disponivel, "rendimentoBruto": round(bruto-original, 2),
                   "rendimentoLiquido": round(liquido-original, 2),
                   "impostosEstimados": round(bruto-liquido, 2), "lotesAtivos": len(ativos),
                   "lotesEncerrados": len(itau)-len(ativos), "movimentos": len(movs),
                   "dataReferencia": max((p["data_referencia"] for p in ativos), default=""),
                   "coletadoEm": max((p["importado_em"] for p in itau), default="")},
        "lotes": lotes, "movimentos": movs, "meses": meses,
        "confirmacaoExtrato": {"fontePrincipal": "API de investimentos Pluggy",
                                "fallback": "Extrato Itaú via Pluggy",
                                "criterio": "total mensal por tipo; descrição contém COFRINHOS",
                                "mesesComDiscrepancia": len({d["mes"] for d in divergencias}),
                                "divergenciasEndpointInvestimentos": divergencias},
        "snapshots": [{"coletadoEm": k, **{x: round(v, 2) for x, v in d.items()}}
                      for k, d in sorted(por_coleta.items())],
        "instituicoes": [{**g, "liquido": round(g["liquido"], 2),
                           "bruto": round(g["bruto"], 2), "original": round(g["original"], 2)}
                          for g in sorted(por_instituicao.values(), key=lambda x: -x["liquido"])],
    }
