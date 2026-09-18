"""Carteira de investimentos de todas as conexões da Pluggy."""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import requests

import banco as fin
import indices
import investimentos_liquidez as liquidez
import investimentos_rentabilidade as rentabilidade
import investimentos_taxonomia as taxonomia
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

CREATE TABLE IF NOT EXISTS pluggy_investimento_movimentos_excluidos (
  investimento_id TEXT NOT NULL,
  movimento_id TEXT NOT NULL,
  motivo TEXT NOT NULL,
  excluido_em TEXT NOT NULL,
  registro_json TEXT NOT NULL,
  PRIMARY KEY (investimento_id, movimento_id)
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

# Aplicado derivado que supera o saldo atual nessa proporção, sem nenhum
# resgate registrado, não é prejuízo comprovado: ou entrou aporte repetido no
# histórico, ou saiu dinheiro que a API não contou. Fica marcado, não somado.
DIVERGENCIA_APLICADO = 0.20


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


def excluir_movimento(investimento_chave: str, movimento_id: str, motivo: str) -> None:
    """Exclusão local explícita; preserva auditoria e impede reimportação por ID."""
    if not motivo.strip():
        raise ValueError("Informe o motivo da exclusão.")
    with _trava:
        garantir_tabelas()
        fin.create_database_backup("excluir_movimento_investimento", min_interval_seconds=0)
        with fin.connect() as conn:
            linha = conn.execute(
                "SELECT m.*, i.investimento_id FROM pluggy_investimento_movimentos m "
                "JOIN pluggy_investimentos i ON i.investimento_chave=m.investimento_chave "
                "WHERE m.investimento_chave=? AND m.movimento_id=?",
                (investimento_chave, movimento_id),
            ).fetchone()
            if linha is None:
                raise ValueError("Movimento não encontrado.")
            conn.execute(
                "INSERT INTO pluggy_investimento_movimentos_excluidos "
                "VALUES (?,?,?,?,?) ON CONFLICT(investimento_id,movimento_id) DO NOTHING",
                (linha["investimento_id"], movimento_id, motivo.strip(),
                 datetime.now().isoformat(timespec="seconds"), json.dumps(dict(linha), ensure_ascii=False)),
            )
            # O mesmo investimento pode ter cópias de uma reconexão.
            conn.execute(
                "DELETE FROM pluggy_investimento_movimentos WHERE movimento_id=? AND investimento_chave IN "
                "(SELECT investimento_chave FROM pluggy_investimentos WHERE investimento_id=?)",
                (movimento_id, linha["investimento_id"]),
            )


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


def _itens_arquivados(conn) -> set[str]:
    linha = conn.execute(
        "SELECT valor FROM app_meta WHERE chave='pluggy_conexoes_arquivadas'"
    ).fetchone()
    try:
        ids = json.loads(linha[0]) if linha else []
    except (ValueError, TypeError):
        return set()
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


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
            arquivados = _itens_arquivados(conn)
            item_ids = [item for item in item_ids if item not in arquivados]
        if not item_ids:
            return {"ok": True, "resultado": "sem_itens"}
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
                    excluidos = {r[0] for r in conn.execute(
                        "SELECT movimento_id FROM pluggy_investimento_movimentos_excluidos WHERE investimento_id=?",
                        (inv_id,),
                    )}
                    for mov in movimentos:
                        mov_id = _texto(mov.get("id"))
                        if not mov_id or mov_id in excluidos:
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
        elif re.search(r"\bbtg\b", n):
            rotulos[row["item_id"]] = "BTG"
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
    arquivados = _itens_arquivados(conn)
    for m in conn.execute(
        "SELECT t.*, c.item_id, c.nome conta FROM pluggy_transacoes t "
        "JOIN pluggy_contas c ON c.conta_id=t.conta_id "
        "WHERE c.tipo='BANK' AND t.valor<>0 AND t.status NOT IN ('PENDING','CANCELED','CANCELLED') "
        "ORDER BY t.data DESC, t.transacao_id DESC"
    ):
        if m["item_id"] in itens_com_posicao or m["item_id"] in arquivados:
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


def _indexador(posicao) -> str:
    """"100% do CDI", "12,5% a.a." -- a taxa como se lê, já montada.

    A tela recebe texto pronto em vez de juntar taxa, tipo e periodicidade:
    é a diferença entre uma coluna e três com significado implícito.
    """
    taxa = _numero(posicao["taxa"])
    tipo = _texto(posicao["tipo_taxa"])
    fixa = _numero(posicao["taxa_fixa_anual"])
    if taxa and tipo:
        return f"{_percentual(taxa)} do {tipo}"
    if fixa:
        return f"{_percentual(fixa)} a.a."
    if taxa:
        return _percentual(taxa)
    return tipo


def _percentual(valor: float) -> str:
    texto = f"{valor:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{texto}%"


def _capital_e_rendimento(posicoes) -> dict[str, Any]:
    """Ausência de custo não é custo zero; totais incompletos ficam explícitos.

    Movimentos podem ter histórico parcial, resgates e transferências. Somar
    compras não comprova o custo da posição atual, portanto não o inferimos.
    """
    conhecidas = [p for p in posicoes if p["valor_original"] is not None]
    faltantes = len(posicoes) - len(conhecidas)
    original = _soma(conhecidas, "valor_original")
    bruto = round(_soma(conhecidas, "valor_bruto") - original, 2)
    liquido = round(_soma(conhecidas, "saldo_liquido") - original, 2)
    return {"original": None if faltantes else original,
            "originalConhecido": original,
            "rendimentoBruto": None if faltantes else bruto,
            "rendimentoLiquido": None if faltantes else liquido,
            "rendimentoBrutoConhecido": bruto if conhecidas or not posicoes else None,
            "posicoesSemCapital": faltantes,
            "saldoSemCapital": _soma([p for p in posicoes if p["valor_original"] is None], "saldo_liquido")}


def _series_por_posicao(snapshots) -> dict[str, list[tuple[str, float]]]:
    """Saldo de cada posição ao longo do tempo, uma série por posição.

    Somar todas as posições por dia antes de calcular o retorno lê mudança de
    cobertura como lucro: conexão nova entrando dá "rendimento" que ninguém
    teve. Cada posição rende sozinha; a carteira é a média ponderada.
    """
    series: dict[str, list[tuple[str, float]]] = {}
    for s in snapshots:
        series.setdefault(s["investimento_chave"], []).append(
            (s["coletado_em"], float(s["saldo_liquido"] or 0)))
    # Posição que já estava zerada quando a coleta começou não tem trecho para
    # render: mantê-la só enche o diagnóstico de trechos ignorados.
    return {chave: pontos for chave, pontos in series.items()
            if any(valor > 0 for _, valor in pontos)}


def _movimentos_por_posicao(movimentos, rotulos) -> dict[str, list[dict[str, Any]]]:
    """Aportes e resgates de cada posição, no formato do cálculo de retorno."""
    por_posicao: dict[str, list[dict[str, Any]]] = {}
    for m in movimentos:
        bruto = m["valor_liquido"] if m["valor_liquido"] is not None else m["valor_bruto"]
        por_posicao.setdefault(m["investimento_chave"], []).append({
            "data": m["data"], "tipo": m["tipo"], "valor": abs(float(bruto or 0)),
            "instituicao": rotulos.get(m["item_id"], "Instituição")})
    return por_posicao


def _aplicado_por_movimentos(movimentos: list[dict[str, Any]],
                             saldo_bruto: float | None = None) -> dict[str, Any]:
    """Quanto ainda está aplicado nesta posição, pelos aportes e resgates dela.

    Custo digitado à mão não sobrevive a aporte mensal, e a Pluggy só informa
    `valor_original` em parte das posições. O histórico de movimentos responde
    a mesma pergunta e se atualiza sozinho: aportes menos resgates é o dinheiro
    que continua ali. Rolagem se anula nessa conta -- o SELL e o BUY do mesmo
    valor entram e saem juntos.

    Continua valendo que ausência não é zero: histórico que não cobre a posição
    (nenhum aporte, ou resgates maiores que aportes) devolve `None` em vez de um
    número que pareceria custo.
    """
    aportes = round(sum(m["valor"] for m in movimentos if m["tipo"] == "BUY"), 2)
    resgates = round(sum(m["valor"] for m in movimentos if m["tipo"] == "SELL"), 2)
    saldo = round(aportes - resgates, 2)
    base = {"aportes": aportes, "resgates": resgates, "confiavel": False}
    if not aportes:
        return {**base, "aplicado": None, "motivo": "sem_aporte_no_historico"}
    if saldo <= 0:
        # Saiu mais do que entrou e ainda há posição: falta compra no registro.
        return {**base, "aplicado": None, "motivo": "historico_de_compras_incompleto"}
    if (saldo_bruto and not resgates
            and saldo / float(saldo_bruto) - 1 >= DIVERGENCIA_APLICADO):
        # O valor continua visível na linha, para a conferência ser possível --
        # mas não entra em total nenhum como se fosse custo apurado.
        return {**base, "aplicado": saldo, "motivo": "divergencia_com_saldo"}
    return {**base, "aplicado": saldo, "confiavel": True, "motivo": ""}


def _aplicado_total(lotes: list[dict[str, Any]]) -> dict[str, Any]:
    """Total aplicado somando as duas fontes, sem esconder o que falta."""
    conhecidos = [l for l in lotes if l.get("aplicado") is not None and l.get("aplicadoConfiavel")]
    faltantes = len(lotes) - len(conhecidos)
    aplicado = round(sum(l["aplicado"] for l in conhecidos), 2)
    bruto = round(sum(float(l["bruto"] or 0) for l in conhecidos), 2)
    return {
        "aplicado": None if faltantes else aplicado,
        "aplicadoConhecido": aplicado,
        "posicoesSemAplicado": faltantes,
        "saldoSemAplicado": round(sum(float(l["liquido"] or 0) for l in lotes
                                      if l not in conhecidos), 2),
        "rendimentoBrutoEstimado": None if faltantes else round(bruto - aplicado, 2),
        "porFonte": {"pluggy": sum(1 for l in conhecidos if l.get("origemAplicado") == "pluggy"),
                     "movimentos": sum(1 for l in conhecidos if l.get("origemAplicado") == "movimentos")},
        "posicoesComAplicadoDuvidoso": sum(1 for l in lotes if not l.get("aplicadoConfiavel")
                                           and l.get("aplicado") is not None),
    }


def _benchmark(conn, retorno: float | None, janela: dict[str, str] | None,
               cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """Compara o retorno com o CDI do mesmo intervalo, lendo só a tabela local.

    O primeiro ponto da série é o saldo de partida, não um dia de rendimento:
    o CDI começa a contar no dia seguinte, senão sobra um dia de juros de um
    lado só. Série do índice mais curta que a janela volta como `parcial`, com
    o intervalo realmente usado -- nunca esticada para fechar o período.
    """
    de, ate = (janela or {}).get("de", ""), (janela or {}).get("ate", "")
    inicio = _dia_seguinte(de)
    if not inicio or not ate:
        return indices.comparar(retorno, {"variacao": None, "status": "indisponivel",
                                          "janela": None, "nome": "CDI",
                                          "motivo": "período de comparação indefinido"})
    if (inicio, ate) not in cache:
        fator = indices.fator(conn, indices.PADRAO, inicio, ate)
        fator["nome"] = indices.SERIES[indices.PADRAO]["nome"]
        cache[(inicio, ate)] = fator
    return indices.comparar(retorno, cache[(inicio, ate)])


def _dia_seguinte(iso: str) -> str:
    try:
        return (date.fromisoformat(str(iso)[:10]) + timedelta(days=1)).isoformat()
    except ValueError:
        return ""


def payload() -> dict[str, Any]:
    garantir_tabelas()
    with fin.connect() as conn:
        rotulos = _rotulos_itens(conn)
        rotulos_classes = taxonomia.rotulos_personalizados(conn)
        arquivados = _itens_arquivados(conn)
        posicoes = list(conn.execute(
            "SELECT i.*, (SELECT MIN(substr(m.data,1,10)) FROM pluggy_investimento_movimentos m "
            "WHERE m.investimento_chave=i.investimento_chave AND m.tipo='BUY') AS data_aplicacao "
            "FROM pluggy_investimentos i ORDER BY importado_em DESC, data_referencia DESC, investimento_chave"
        ))
        # Reconectar também pode trocar os IDs dos investimentos. As posições
        # arquivadas permanecem no banco, mas não compõem a carteira atual,
        # seus movimentos ou a curva de saldo.
        posicoes = [p for p in posicoes if p["item_id"] not in arquivados]
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
        series_por_posicao = _series_por_posicao(snaps)
        movs_por_posicao = _movimentos_por_posicao(movimentos_investimento, rotulos)

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

        for nome, grupo in por_instituicao.items():
            grupo.update(_capital_e_rendimento([
                p for p in ativos if rotulos.get(p["item_id"], "Instituição") == nome
            ]))

    bruto = _soma(ativos, "valor_bruto")
    liquido = _soma(ativos, "saldo_liquido")
    disponivel = _soma(ativos, "disponivel_resgate")

    lotes = []
    for p in ativos:
        raw = json.loads(p["raw_json"] or "{}")
        classe = taxonomia.classificar(p["tipo"], p["subtipo"])
        p_bruto = float(p["valor_bruto"] or 0)
        p_liq = float(p["saldo_liquido"] or 0)
        p_orig = _numero(p["valor_original"])
        lotes.append({
            "id": p["investimento_id"], "nome": p["nome"], "tipo": p["tipo"],
            "classe": classe["classe"], "classeRotulo": classe["rotulo"],
            "rotuloSubtipo": classe["rotuloSubtipo"], "indexador": _indexador(p),
            "origem": "pluggy",
            "itemId": p["item_id"], "instituicao": rotulos.get(p["item_id"], "Instituição"),
            "moeda": p["moeda"],
            "subtipo": p["subtipo"], "dataReferencia": p["data_referencia"],
            "dataAplicacao": p["data_aplicacao"] or p["data_emissao"],
            "original": p_orig, "bruto": p_bruto, "liquido": p_liq,
            "disponivel": p["disponivel_resgate"],
            "rendimentoBruto": round(p_bruto - p_orig, 2) if p_orig is not None else None,
            "rendimentoLiquido": round(p_liq - p_orig, 2) if p_orig is not None else None,
            "impostosEstimados": p_bruto - p_liq,
            "rentabilidadeBruta": ((p_bruto / p_orig - 1) * 100) if p_orig else None,
            "lucroInformado": p["lucro_informado"],
            "rentabilidadeFundo12Meses": _numero(raw.get("lastTwelveMonthsRate")),
            "taxa": p["taxa"], "tipoTaxa": p["tipo_taxa"],
            "taxaFixaAnual": p["taxa_fixa_anual"], "emissor": p["emissor"],
            "emissao": p["data_emissao"], "vencimento": p["vencimento"], "status": p["status"],
            "codigo": raw.get("code"), "numero": raw.get("number"), "proprietario": raw.get("owner"),
            "carencia": raw.get("gracePeriodDate"), "quantidade": p["quantidade"], "valorCota": p["valor_cota"],
        })

    # Vencimento, carência e D+N viram prazo, faixa e rótulo antes de qualquer
    # soma. O D+N dos fundos vem do catálogo local, numa consulta só: resolver
    # CNPJ por posição varreria a tabela de fundos uma vez por linha.
    with fin.connect() as conn:
        dias_fundos = liquidez.dias_de_resgate_por_cnpj(conn, [l.get("codigo") for l in lotes])
    liquidez.enriquecer(lotes, dias_fundos)
    liquidez_carteira = liquidez.agregar(lotes)

    # Uma aba por classe que existe na carteira -- e só por classe que existe:
    # a tela mostra o que a pessoa tem, não o catálogo do que poderia ter.
    linhas_por_classe: dict[str, list] = {}
    lotes_por_classe: dict[str, list] = {}
    for linha, lote in zip(ativos, lotes):
        linhas_por_classe.setdefault(lote["classe"], []).append(linha)
        lotes_por_classe.setdefault(lote["classe"], []).append(lote)
        # O mesmo motor da carteira, com uma posição só: o retorno da linha
        # desconta seus próprios aportes e resgates.
        chave = linha["investimento_chave"]
        movs_lote = movs_por_posicao.get(chave, [])
        lote["rentabilidade"] = rentabilidade.rentabilidade_carteira(
            {chave: series_por_posicao.get(chave, [])}, {chave: movs_lote})
        pelos_movimentos = _aplicado_por_movimentos(movs_lote, lote["bruto"])
        informado = lote["original"]
        lote.update({
            "aplicado": informado if informado is not None else pelos_movimentos["aplicado"],
            "origemAplicado": ("pluggy" if informado is not None
                               else "movimentos" if pelos_movimentos["aplicado"] is not None else ""),
            "aportes": pelos_movimentos["aportes"], "resgates": pelos_movimentos["resgates"],
            "aplicadoConfiavel": informado is not None or pelos_movimentos["confiavel"],
            "motivoSemAplicado": "" if informado is not None else pelos_movimentos["motivo"],
        })
        aplicado_lote = lote["aplicado"] if lote["aplicadoConfiavel"] else None
        if informado is None and aplicado_lote:
            # Ganho a partir do aplicado derivado: mesma conta, outra fonte --
            # e por isso marcada, nunca misturada com o custo informado.
            lote["rendimentoBrutoEstimado"] = round(lote["bruto"] - aplicado_lote, 2)
            lote["rentabilidadeBrutaEstimada"] = round((lote["bruto"] / aplicado_lote - 1) * 100, 4)
        else:
            lote["rendimentoBrutoEstimado"] = lote["rendimentoBruto"]
            lote["rentabilidadeBrutaEstimada"] = lote["rentabilidadeBruta"]

    classes = []
    for info in taxonomia.classes_presentes(
            [{"tipo": l["tipo"], "subtipo": l["subtipo"]} for l in ativos], rotulos_classes):
        linhas = linhas_por_classe.get(info["id"], [])
        liquido_classe = _soma(linhas, "saldo_liquido")
        classes.append({
            **info,
            "colunas": taxonomia.colunas(info["perfil"]),
            "destaques": taxonomia.destaques(info["perfil"]),
            "resumo": {"liquido": liquido_classe, "bruto": _soma(linhas, "valor_bruto"),
                       **_capital_e_rendimento(linhas),
                       **_aplicado_total(lotes_por_classe.get(info["id"], [])),
                       # Sem patrimônio não existe fatia: 0/0 não é 0%.
                       "participacao": round(liquido_classe / liquido * 100, 2) if liquido else None,
                       "posicoes": len(linhas)},
            "liquidez": liquidez.agregar(lotes_por_classe.get(info["id"], [])),
            "posicoes": lotes_por_classe.get(info["id"], []),
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
            "original": _capital_e_rendimento(atuais)["original"],
        }

    # ------------------------------------------------- rentabilidade e CDI
    # TWR é a manchete porque não precisa de custo -- serve para os fundos que
    # a Pluggy entrega sem `valor_original` -- e é o único número comparável ao
    # CDI, que não recebe aporte. XIRR entra como medida do dinheiro, sobre o
    # histórico inteiro de movimentos.
    twr_carteira = rentabilidade.rentabilidade_carteira(series_por_posicao, movs_por_posicao)
    xirr_carteira = rentabilidade.rentabilidade_xirr(
        movs, liquido, max((p["data_referencia"] for p in ativos), default=""))

    cache_indice: dict[tuple[str, str], dict[str, Any]] = {}
    with fin.connect() as conn:
        indices.garantir_tabelas(conn)
        benchmark = _benchmark(conn, twr_carteira["valor"], twr_carteira["janela"], cache_indice)
        for classe in classes:
            chaves_classe = [l["investimento_chave"] for l in linhas_por_classe.get(classe["id"], [])]
            twr_classe = rentabilidade.rentabilidade_carteira(
                {c: series_por_posicao[c] for c in chaves_classe if c in series_por_posicao},
                {c: movs_por_posicao.get(c, []) for c in chaves_classe})
            classe["rentabilidade"] = {
                "twr": twr_classe,
                "benchmark": _benchmark(conn, twr_classe["valor"], twr_classe["janela"], cache_indice)}
        estado_indices = indices.estado(conn)

    return {
        "versao": 3,
        # Consolidado e classes são a leitura nova; `resumo`, `lotes` e o resto
        # seguem idênticos para a Visão geral e os testes do v2 não mudarem.
        "consolidado": {
            "liquido": liquido, "bruto": bruto, **_capital_e_rendimento(ativos),
            "disponivel": disponivel, "impostosEstimados": round(bruto - liquido, 2),
            "posicoesAtivas": len(ativos), "posicoesEncerradas": len(posicoes) - len(ativos),
            "classes": len(classes),
            "disponivelHoje": liquidez_carteira["disponivelHoje"],
            "vencendo30": liquidez_carteira["vencendo30"],
            "vencendo90": liquidez_carteira["vencendo90"],
            "rentabilidade": {"twr": twr_carteira, "xirr": xirr_carteira,
                              "benchmark": benchmark},
            **_aplicado_total(lotes),
            "porOrigem": {"pluggy": {"liquido": liquido, "posicoes": len(ativos)}},
            "dataReferencia": max((p["data_referencia"] for p in ativos), default=""),
            "coletadoEm": max((p["importado_em"] for p in posicoes), default=""),
        },
        "classes": classes,
        "liquidez": liquidez_carteira,
        "indices": estado_indices,
        "resumo": {"liquido": liquido, "bruto": bruto, **_capital_e_rendimento(ativos),
                   "disponivel": disponivel,
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
        "snapshots": [{"coletadoEm": k, **{x: round(v, 2) if v is not None else None for x, v in d.items()}}
                      for k, d in sorted(por_coleta.items())],
        "instituicoes": [{**g, "liquido": round(g["liquido"], 2),
                           "bruto": round(g["bruto"], 2)}
                          for g in sorted(por_instituicao.values(), key=lambda x: -x["liquido"])],
    }
