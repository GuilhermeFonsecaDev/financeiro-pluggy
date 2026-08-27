"""Importa para o pluggy.db os extratos baixados pelo pluggy_sync.py.

O pluggy_sync grava JSON/CSV em ./data; este modulo le esses arquivos e os
grava nas tabelas "pluggy_".

A importacao e idempotente: a chave primaria e o id que a propria Pluggy gera,
entao rodar duas vezes atualiza as linhas em vez de duplicar.

Uso:
    python importar_pluggy.py --dry-run     # so mostra o que faria
    python importar_pluggy.py               # importa de fato (faz backup antes)
    python importar_pluggy.py --data-dir "C:/caminho/para/data"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import banco as fin

# Relativo ao arquivo, para o projeto poder ser movido sem editar codigo.
DATA_DIR_PADRAO = Path(__file__).resolve().parent / "data"

SCHEMA_PLUGGY = """
CREATE TABLE IF NOT EXISTS pluggy_itens (
  item_id TEXT PRIMARY KEY,
  conector TEXT NOT NULL DEFAULT '',
  importado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pluggy_contas (
  conta_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT '',
  subtipo TEXT NOT NULL DEFAULT '',
  nome TEXT NOT NULL DEFAULT '',
  numero TEXT NOT NULL DEFAULT '',
  titular TEXT NOT NULL DEFAULT '',
  saldo REAL NOT NULL DEFAULT 0,
  moeda TEXT NOT NULL DEFAULT 'BRL',
  limite_credito REAL,
  limite_disponivel REAL,
  fechamento TEXT NOT NULL DEFAULT '',
  vencimento TEXT NOT NULL DEFAULT '',
  criado_em TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT '',
  importado_em TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (item_id) REFERENCES pluggy_itens (item_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_transacoes (
  transacao_id TEXT PRIMARY KEY,
  conta_id TEXT NOT NULL,
  data TEXT NOT NULL,
  mes_ref TEXT NOT NULL,
  ano INTEGER NOT NULL,
  mes INTEGER NOT NULL CHECK (mes BETWEEN 1 AND 12),
  descricao TEXT NOT NULL DEFAULT '',
  valor REAL NOT NULL DEFAULT 0,
  moeda TEXT NOT NULL DEFAULT 'BRL',
  tipo TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  categoria TEXT NOT NULL DEFAULT '',
  categoria_id TEXT NOT NULL DEFAULT '',
  saldo_apos REAL,
  fatura_id TEXT NOT NULL DEFAULT '',
  parcela_numero INTEGER,
  parcela_total INTEGER,
  ordem INTEGER NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT '',
  importado_em TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (conta_id) REFERENCES pluggy_contas (conta_id) ON DELETE CASCADE
);

-- Faturas fechadas de cartao, como o banco emitiu. O fatura_id e o mesmo
-- billId que ja vem em pluggy_transacoes.fatura_id, entao da para casar 1:1
-- sem heuristica. So faturas FECHADAS existem aqui: a do ciclo corrente nao
-- e entregue pela API (ver list_bills em pluggy_sync.py).
CREATE TABLE IF NOT EXISTS pluggy_faturas (
  fatura_id TEXT PRIMARY KEY,
  conta_id TEXT NOT NULL,
  vencimento TEXT NOT NULL DEFAULT '',
  fechamento TEXT NOT NULL DEFAULT '',
  competencia TEXT NOT NULL DEFAULT '',
  valor_total REAL NOT NULL DEFAULT 0,
  moeda TEXT NOT NULL DEFAULT 'BRL',
  pagamento_minimo REAL,
  importado_em TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (conta_id) REFERENCES pluggy_contas (conta_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pluggy_sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  executado_em TEXT NOT NULL,
  origem TEXT NOT NULL,
  contas_novas INTEGER NOT NULL DEFAULT 0,
  contas_atualizadas INTEGER NOT NULL DEFAULT 0,
  transacoes_novas INTEGER NOT NULL DEFAULT 0,
  transacoes_atualizadas INTEGER NOT NULL DEFAULT 0,
  data_min TEXT NOT NULL DEFAULT '',
  data_max TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_pluggy_faturas_conta
  ON pluggy_faturas (conta_id, competencia);
CREATE INDEX IF NOT EXISTS idx_pluggy_tx_mes ON pluggy_transacoes (mes_ref);
CREATE INDEX IF NOT EXISTS idx_pluggy_tx_conta_data ON pluggy_transacoes (conta_id, data);
CREATE INDEX IF NOT EXISTS idx_pluggy_tx_categoria ON pluggy_transacoes (categoria);
CREATE INDEX IF NOT EXISTS idx_pluggy_tx_fatura ON pluggy_transacoes (fatura_id);
CREATE INDEX IF NOT EXISTS idx_pluggy_contas_item ON pluggy_contas (item_id);
"""


def texto(valor: object) -> str:
    return "" if valor is None else str(valor)


def numero(valor: object) -> float:
    try:
        return float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def opcional_numero(valor: object) -> float | None:
    if valor is None:
        return None
    try:
        return float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def opcional_inteiro(valor: object) -> int | None:
    if valor is None:
        return None
    try:
        return int(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def partes_da_data(valor: object) -> tuple[str, str, int, int]:
    """Devolve (data_iso, mes_ref, ano, mes). A Pluggy manda ISO-8601."""
    bruto = texto(valor)
    if len(bruto) < 7:
        raise ValueError(f"data invalida na transacao: {bruto!r}")
    ano = int(bruto[0:4])
    mes = int(bruto[5:7])
    return bruto, f"{ano}-{mes:02d}", ano, mes


def carregar_json(caminho: Path) -> list[dict[str, Any]]:
    with caminho.open(encoding="utf-8") as arquivo:
        dados = json.load(arquivo)
    return dados if isinstance(dados, list) else [dados]


def linha_conta(conta: dict[str, Any], agora: str) -> tuple:
    credito = conta.get("creditData") or {}
    return (
        texto(conta.get("id")),
        texto(conta.get("itemId")),
        texto(conta.get("type")),
        texto(conta.get("subtype")),
        texto(conta.get("name")),
        texto(conta.get("number")),
        texto(conta.get("owner")),
        numero(conta.get("balance")),
        texto(conta.get("currencyCode")) or "BRL",
        opcional_numero(credito.get("creditLimit")),
        opcional_numero(credito.get("availableCreditLimit")),
        texto(credito.get("balanceCloseDate")),
        texto(credito.get("balanceDueDate")),
        texto(conta.get("createdAt")),
        texto(conta.get("updatedAt")),
        agora,
        json.dumps(conta, ensure_ascii=False),
    )


def linha_transacao(tx: dict[str, Any], agora: str,
                    moedas_conta: dict[str, str] | None = None) -> tuple:
    cartao = tx.get("creditCardMetadata") or {}
    data_iso, mes_ref, ano, mes = partes_da_data(tx.get("date"))
    conta_id = texto(tx.get("accountId"))
    valor_convertido = opcional_numero(tx.get("amountInAccountCurrency"))
    valor = valor_convertido if valor_convertido is not None else numero(tx.get("amount"))
    moeda_original = texto(tx.get("currencyCode")) or "BRL"
    # Em compras internacionais, ``amount`` permanece em USD/EUR. Para todos
    # os totais do dashboard precisamos do valor efetivamente lançado na moeda
    # da conta, fornecido pela Pluggy em amountInAccountCurrency.
    moeda = (
        (moedas_conta or {}).get(conta_id, "BRL")
        if valor_convertido is not None else moeda_original
    )
    return (
        texto(tx.get("id")),
        conta_id,
        data_iso,
        mes_ref,
        ano,
        mes,
        texto(tx.get("description")),
        valor,
        moeda,
        texto(tx.get("type")),
        texto(tx.get("status")),
        texto(tx.get("category")),
        texto(tx.get("categoryId")),
        opcional_numero(tx.get("balance")),
        texto(cartao.get("billId")),
        opcional_inteiro(cartao.get("installmentNumber")),
        opcional_inteiro(cartao.get("totalInstallments")),
        opcional_inteiro(tx.get("order")) or 0,
        texto(tx.get("createdAt")),
        texto(tx.get("updatedAt")),
        agora,
        json.dumps(tx, ensure_ascii=False),
    )


def linha_fatura(bill: dict[str, Any], conta_id: str, agora: str) -> tuple:
    """Uma fatura fechada. O conta_id vem de fora: a resposta de /bills nao
    traz accountId, quem sabe a conta e o nome do arquivo (bills_<id>.json).

    A competencia e o mes do VENCIMENTO, que e como a fatura chega para pagar
    e como o resto do projeto ja datava o ciclo (ver _COMPETENCIA_FATURA em
    extrato_camada.py: mes seguinte ao ultimo lancamento). Os dois coincidiram
    em todas as faturas conferidas.
    """
    vencimento = texto(bill.get("dueDate"))
    return (
        texto(bill.get("id")),
        conta_id,
        vencimento,
        texto(bill.get("billClosingDate")),
        vencimento[:7],
        numero(bill.get("totalAmount")),
        texto(bill.get("totalAmountCurrencyCode")) or "BRL",
        opcional_numero(bill.get("minimumPaymentAmount")),
        agora,
        json.dumps(bill, ensure_ascii=False),
    )


SQL_CONTA = """
INSERT INTO pluggy_contas (
  conta_id, item_id, tipo, subtipo, nome, numero, titular, saldo, moeda,
  limite_credito, limite_disponivel, fechamento, vencimento,
  criado_em, atualizado_em, importado_em, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(conta_id) DO UPDATE SET
  item_id = excluded.item_id, tipo = excluded.tipo, subtipo = excluded.subtipo,
  nome = excluded.nome, numero = excluded.numero, titular = excluded.titular,
  saldo = excluded.saldo, moeda = excluded.moeda,
  limite_credito = excluded.limite_credito,
  limite_disponivel = excluded.limite_disponivel,
  fechamento = excluded.fechamento, vencimento = excluded.vencimento,
  criado_em = excluded.criado_em, atualizado_em = excluded.atualizado_em,
  importado_em = excluded.importado_em, raw_json = excluded.raw_json
"""

SQL_TRANSACAO = """
INSERT INTO pluggy_transacoes (
  transacao_id, conta_id, data, mes_ref, ano, mes, descricao, valor, moeda,
  tipo, status, categoria, categoria_id, saldo_apos, fatura_id,
  parcela_numero, parcela_total, ordem, criado_em, atualizado_em,
  importado_em, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(transacao_id) DO UPDATE SET
  conta_id = excluded.conta_id, data = excluded.data,
  mes_ref = excluded.mes_ref, ano = excluded.ano, mes = excluded.mes,
  descricao = excluded.descricao, valor = excluded.valor,
  moeda = excluded.moeda, tipo = excluded.tipo, status = excluded.status,
  categoria = excluded.categoria, categoria_id = excluded.categoria_id,
  saldo_apos = excluded.saldo_apos, fatura_id = excluded.fatura_id,
  parcela_numero = excluded.parcela_numero,
  parcela_total = excluded.parcela_total, ordem = excluded.ordem,
  criado_em = excluded.criado_em, atualizado_em = excluded.atualizado_em,
  importado_em = excluded.importado_em, raw_json = excluded.raw_json
"""

SQL_FATURA = """
INSERT INTO pluggy_faturas (
  fatura_id, conta_id, vencimento, fechamento, competencia, valor_total,
  moeda, pagamento_minimo, importado_em, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(fatura_id) DO UPDATE SET
  conta_id = excluded.conta_id, vencimento = excluded.vencimento,
  fechamento = excluded.fechamento, competencia = excluded.competencia,
  valor_total = excluded.valor_total, moeda = excluded.moeda,
  pagamento_minimo = excluded.pagamento_minimo,
  importado_em = excluded.importado_em, raw_json = excluded.raw_json
"""


def carregar_faturas(data_dir: Path) -> list[tuple[dict[str, Any], str]]:
    """Le os bills_<contaId>.json e devolve (fatura, conta_id).

    O conta_id sai do nome do arquivo porque a resposta de /bills nao o traz.
    Se um dia a Pluggy passar a mandar accountId, ele tem precedencia.
    """
    pares: list[tuple[dict[str, Any], str]] = []
    for arquivo in sorted(data_dir.glob("bills_*.json")):
        conta_do_arquivo = arquivo.stem[len("bills_"):]
        for bill in carregar_json(arquivo):
            conta_id = texto(bill.get("accountId")) or conta_do_arquivo
            if texto(bill.get("id")) and conta_id:
                pares.append((bill, conta_id))
    return pares


def importar(data_dir: Path, dry_run: bool = False) -> int:
    caminho_contas = data_dir / "accounts.json"
    if not caminho_contas.exists():
        print(f"Nao encontrei {caminho_contas}")
        print("Rode antes: python pluggy_sync.py sync <ITEM_ID>")
        return 1

    contas = carregar_json(caminho_contas)
    arquivos_tx = sorted(data_dir.glob("transactions_*.json"))
    transacoes: list[dict[str, Any]] = []
    for arquivo in arquivos_tx:
        transacoes.extend(carregar_json(arquivo))
    faturas = carregar_faturas(data_dir)

    print(f"Origem  : {data_dir}")
    print(f"Destino : {fin.DATABASE_PATH}")
    print(f"Lidos   : {len(contas)} conta(s), {len(transacoes)} transacao(oes) "
          f"em {len(arquivos_tx)} arquivo(s), {len(faturas)} fatura(s)\n")

    # Sem contas, toda transacao vira orfa e o import "passa" sem gravar nada.
    # Isso e sintoma de accounts.json corrompido, nao de importacao legitima.
    if not contas and transacoes:
        print("ERRO: accounts.json esta vazio, mas ha transacoes para importar.")
        print("  Importar assim descartaria todas elas como orfas.")
        print("  Nada foi gravado. Regenere o accounts.json rodando o sync de novo;")
        print("  se o sync tambem vier vazio, o problema esta do lado da Pluggy.")
        return 1

    fin.ensure_database()

    if dry_run:
        with fin.connect() as conn:
            existentes = tabelas_pluggy_existem(conn)
        print("DRY-RUN: nada foi gravado.")
        print(f"  tabelas pluggy_* ja existem? {'sim' if existentes else 'nao'}")
        print(f"  seriam gravadas {len(contas)} conta(s), "
              f"{len(transacoes)} transacao(oes) e {len(faturas)} fatura(s).")
        return 0

    backup = fin.create_database_backup("importar_pluggy", min_interval_seconds=0)
    print(f"Backup  : {backup.name if backup else '(nenhum - db ainda nao existia)'}\n")

    agora = datetime.now().isoformat(timespec="seconds")
    with fin.connect() as conn:
        conn.executescript(SCHEMA_PLUGGY)

        contas_antes = ids_existentes(conn, "pluggy_contas", "conta_id")
        tx_antes = ids_existentes(conn, "pluggy_transacoes", "transacao_id")

        itens = {texto(c.get("itemId")) for c in contas if c.get("itemId")}
        conn.executemany(
            "INSERT INTO pluggy_itens (item_id, conector, importado_em) "
            "VALUES (?, '', ?) ON CONFLICT(item_id) DO UPDATE SET "
            "importado_em = excluded.importado_em",
            [(item, agora) for item in sorted(itens)],
        )

        conn.executemany(SQL_CONTA, [linha_conta(c, agora) for c in contas])

        # Contas conhecidas = as do accounts.json atual MAIS as que ja estao no
        # banco. A pasta data/ acumula arquivos de conexoes antigas; sem isso,
        # toda importacao acusaria essas transacoes como orfas mesmo elas ja
        # tendo sido importadas com sucesso antes.
        conhecidas = {texto(c.get("id")) for c in contas} | contas_antes
        validas, orfas = [], []
        for tx in transacoes:
            (validas if texto(tx.get("accountId")) in conhecidas else orfas).append(tx)
        moedas_conta = {
            linha["conta_id"]: str(linha["moeda"] or "BRL")
            for linha in conn.execute("SELECT conta_id, moeda FROM pluggy_contas")
        }
        conn.executemany(
            SQL_TRANSACAO,
            [linha_transacao(t, agora, moedas_conta) for t in validas],
        )

        # Mesma regra de orfa das transacoes: fatura de conta que nao existe
        # mais (cartao trocado por reconexao) fica de fora.
        faturas_validas = [(b, c) for b, c in faturas if c in conhecidas]
        conn.executemany(
            SQL_FATURA,
            [linha_fatura(b, c, agora) for b, c in faturas_validas],
        )

        contas_novas = len({texto(c.get("id")) for c in contas} - contas_antes)
        tx_ids = {texto(t.get("id")) for t in validas}
        tx_novas = len(tx_ids - tx_antes)

        datas = sorted(texto(t.get("date"))[:10] for t in validas if t.get("date"))
        conn.execute(
            "INSERT INTO pluggy_sync_log (executado_em, origem, contas_novas, "
            "contas_atualizadas, transacoes_novas, transacoes_atualizadas, "
            "data_min, data_max) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (agora, str(data_dir), contas_novas, len(contas) - contas_novas,
             tx_novas, len(validas) - tx_novas,
             datas[0] if datas else "", datas[-1] if datas else ""),
        )
        conn.commit()

        print("Resultado:")
        print(f"  contas      : {contas_novas} nova(s), "
              f"{len(contas) - contas_novas} atualizada(s)")
        print(f"  transacoes  : {tx_novas} nova(s), "
              f"{len(validas) - tx_novas} atualizada(s)")
        print(f"  faturas     : {len(faturas_validas)} gravada(s)")
        if len(faturas) != len(faturas_validas):
            print(f"  IGNORADAS   : {len(faturas) - len(faturas_validas)} fatura(s) "
                  "de conta que nao existe mais")
        if orfas:
            print(f"  IGNORADAS   : {len(orfas)} transacao(oes) sem conta "
                  "correspondente em accounts.json")
        if datas:
            print(f"  periodo     : {datas[0]} a {datas[-1]}")

        print("\nTotais no banco:")
        for tabela in ("pluggy_itens", "pluggy_contas", "pluggy_transacoes",
                       "pluggy_faturas"):
            total = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            print(f"  {tabela:<20} {total}")

    return 0


def tabelas_pluggy_existem(conn: sqlite3.Connection) -> bool:
    linha = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'pluggy_%'"
    ).fetchone()
    return bool(linha[0])


def ids_existentes(conn: sqlite3.Connection, tabela: str, coluna: str) -> set[str]:
    return {
        linha[0]
        for linha in conn.execute(f"SELECT {coluna} FROM {tabela}").fetchall()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR_PADRAO,
                        help="pasta com accounts.json e transactions_*.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra o que seria feito, sem gravar nada")
    args = parser.parse_args()
    return importar(args.data_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
