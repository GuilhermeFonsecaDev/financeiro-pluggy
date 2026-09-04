"""Remove conexões Pluggy que viraram lixo, sem perder vínculo do usuário.

    python limpar_conexoes.py                       lista as conexões e o estado
    python limpar_conexoes.py <item_id> [<item_id>] mostra o que faria
    python limpar_conexoes.py <item_id> --aplicar   faz backup e executa

Candidata típica a remoção: conexão substituída por reconexão (as contas
antigas ficam para trás, já fora de todo cálculo) e conexão que não trouxe
nenhuma conta.

Uma conexão substituída por reconexão continua no banco com as contas antigas.
Elas já ficam fora de todo cálculo (`contas_ativas`), mas ainda aparecem em
lugares que listam instituições, ocupam espaço e confundem a leitura do banco.

O cuidado que justifica um script em vez de DELETE solto: **vínculo do usuário**.
Uma conta fixa pode estar ligada a uma transação da conexão antiga. Apagar sem
olhar quebra a conciliação daquele mês em silêncio. Aqui cada vínculo é primeiro
religado à transação equivalente na conexão que ficou -- mesma data, mesmo valor
e mesma descrição --, e se algum não achar par o script PARA sem apagar nada.

O que é removido, por item: transações, faturas, contas, investimentos (com
movimentos e snapshots) e o próprio item. As tabelas derivadas
(`extrato_efetivo_cache`, `pluggy_ciclos`, `pluggy_cartao_previsao`) não entram
na lista porque são reconstruídas sozinhas na primeira requisição.
"""

from __future__ import annotations

import sqlite3
import sys

import banco as fin

# Os ids das conexões vêm por argumento, nunca fixos no código: eles
# identificam as conexões bancárias de quem usa.
#
# Sobra de backup manual antigo: nenhuma linha de código do projeto a lê.
TABELAS_ORFAS = ("snapshot_antes",)


def _listar_candidatos() -> None:
    """Mostra cada conexão com contas, transações e se ainda entra nos cálculos."""
    import pluggy_extrato as px

    conn = fin.connect()
    try:
        ativas = set(px.contas_ativas(conn))
        for linha in conn.execute(
            """
            SELECT i.item_id,
                   (SELECT COUNT(*) FROM pluggy_contas a
                     WHERE a.item_id = i.item_id) contas,
                   (SELECT COUNT(*) FROM pluggy_transacoes t
                     JOIN pluggy_contas a ON a.conta_id = t.conta_id
                     WHERE a.item_id = i.item_id) tx
            FROM pluggy_itens i ORDER BY contas DESC, tx DESC
            """
        ):
            contas = conn.execute(
                "SELECT conta_id, nome FROM pluggy_contas WHERE item_id = ?",
                (linha["item_id"],),
            ).fetchall()
            vivas = sum(1 for c in contas if c["conta_id"] in ativas)
            estado = ("sem conta" if not contas
                      else "todas as contas fora dos cálculos" if not vivas
                      else f"{vivas} de {len(contas)} contas em uso")
            print(f"  {linha['item_id']}  contas={linha['contas']} "
                  f"transações={linha['tx']}  — {estado}")
    finally:
        conn.close()


def _contas(conn: sqlite3.Connection, itens: list[str]) -> list[str]:
    marcadores = ", ".join("?" for _ in itens)
    return [
        linha[0] for linha in conn.execute(
            f"SELECT conta_id FROM pluggy_contas WHERE item_id IN ({marcadores})",
            itens,
        )
    ]


def _transacoes(conn: sqlite3.Connection, contas: list[str]) -> list[str]:
    if not contas:
        return []
    marcadores = ", ".join("?" for _ in contas)
    return [
        linha[0] for linha in conn.execute(
            f"SELECT transacao_id FROM pluggy_transacoes "
            f"WHERE conta_id IN ({marcadores})",
            contas,
        )
    ]


def _equivalente(conn: sqlite3.Connection, transacao_id: str,
                 itens_removidos: list[str]) -> sqlite3.Row | None:
    """A mesma transação, na conexão que ficou.

    Mesma data, mesmo valor e mesma descrição ignorando maiúsculas -- os dois
    conectores escrevem o mesmo lançamento com caixa diferente. Sem tolerância
    de data de propósito: religar a um dia vizinho poderia trocar a cobrança
    por outra do mesmo valor.
    """
    origem = conn.execute(
        "SELECT data, valor, descricao FROM pluggy_transacoes "
        "WHERE transacao_id = ?",
        (transacao_id,),
    ).fetchone()
    if origem is None:
        return None
    marcadores = ", ".join("?" for _ in itens_removidos)
    return conn.execute(
        f"""
        SELECT t.transacao_id, t.data, t.valor, t.descricao, a.nome AS conta
        FROM pluggy_transacoes t
        JOIN pluggy_contas a USING(conta_id)
        WHERE a.item_id NOT IN ({marcadores})
          AND SUBSTR(t.data, 1, 10) = SUBSTR(?, 1, 10)
          AND ROUND(t.valor, 2) = ROUND(?, 2)
          AND UPPER(TRIM(t.descricao)) = UPPER(TRIM(?))
        LIMIT 1
        """,
        (*itens_removidos, origem["data"], origem["valor"], origem["descricao"]),
    ).fetchone()


def _vinculos(conn: sqlite3.Connection,
              transacoes: list[str]) -> list[sqlite3.Row]:
    """Vínculos de conta fixa que apontam para as transações que vão sair."""
    if not transacoes:
        return []
    marcadores = ", ".join("?" for _ in transacoes)
    return conn.execute(
        f"""
        SELECT m.mes_ref, m.fixa_id, m.transacao_id, f.nome AS fixa
        FROM fixas_mes m
        LEFT JOIN fixas_contas f ON f.id = m.fixa_id
        WHERE m.transacao_id IN ({marcadores})
        ORDER BY m.mes_ref
        """,
        transacoes,
    ).fetchall()


def main() -> int:
    aplicar = "--aplicar" in sys.argv
    itens = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not itens:
        print(__doc__)
        print("Informe os item_id a remover. Candidatos no banco:\n")
        _listar_candidatos()
        return 1

    fin.ensure_database()
    conn = fin.connect()
    try:
        presentes = [
            linha[0] for linha in conn.execute(
                "SELECT item_id FROM pluggy_itens WHERE item_id IN "
                f"({', '.join('?' for _ in itens)})",
                itens,
            )
        ]
        ausentes = [item for item in itens if item not in presentes]
        for item in ausentes:
            print(f"item já removido, ignorando: {item[:8]}")
        itens = presentes
        if not itens and not aplicar:
            print("nada a remover.")

        contas = _contas(conn, itens) if itens else []
        transacoes = _transacoes(conn, contas)

        print(f"\nconexões a remover: {len(itens)}")
        for item in itens:
            print(f"  {item[:8]}")
        print(f"contas: {len(contas)} | transações: {len(transacoes)}")

        # --- vínculos do usuário -------------------------------------------
        pendentes = _vinculos(conn, transacoes)
        religar: list[tuple[str, str, str]] = []
        sem_par: list[sqlite3.Row] = []
        for vinculo in pendentes:
            par = _equivalente(conn, vinculo["transacao_id"], itens)
            if par is None:
                sem_par.append(vinculo)
                continue
            religar.append(
                (par["transacao_id"], vinculo["mes_ref"], vinculo["fixa_id"])
            )
            print(f"\nvínculo a religar: {vinculo['fixa']} · {vinculo['mes_ref']}")
            print(f"  para: {str(par['data'])[:10]} R$ {abs(par['valor']):.2f} "
                  f"{str(par['descricao'])[:34]} ({par['conta']})")

        if sem_par:
            print(f"\nPARANDO: {len(sem_par)} vínculo(s) sem equivalente na "
                  f"conexão que fica. Religue à mão antes de remover:")
            for vinculo in sem_par:
                print(f"  {vinculo['fixa']} · {vinculo['mes_ref']}")
            return 2

        orfas = [
            tabela for tabela in TABELAS_ORFAS
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (tabela,),
            ).fetchone()
        ]
        for tabela in orfas:
            total = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
            print(f"\ntabela órfã a remover: {tabela} ({total} linhas)")

        if not aplicar:
            print("\n(simulação — rode com --aplicar para executar)")
            return 0

        caminho = fin.create_database_backup("limpar_conexoes",
                                             min_interval_seconds=0)
        print(f"\nbackup: {caminho}")

        for transacao_id, mes_ref, fixa_id in religar:
            conn.execute(
                "UPDATE fixas_mes SET transacao_id = ? "
                "WHERE mes_ref = ? AND fixa_id = ?",
                (transacao_id, mes_ref, fixa_id),
            )

        if contas:
            marc_contas = ", ".join("?" for _ in contas)
            conn.execute(
                f"DELETE FROM pluggy_transacoes WHERE conta_id IN ({marc_contas})",
                contas,
            )
            conn.execute(
                f"DELETE FROM pluggy_faturas WHERE conta_id IN ({marc_contas})",
                contas,
            )
        if itens:
            marc_itens = ", ".join("?" for _ in itens)
            conn.execute(
                "DELETE FROM pluggy_investimento_movimentos WHERE "
                "investimento_chave IN (SELECT investimento_chave FROM "
                f"pluggy_investimentos WHERE item_id IN ({marc_itens}))",
                itens,
            )
            conn.execute(
                "DELETE FROM pluggy_investimento_snapshots WHERE "
                "investimento_chave IN (SELECT investimento_chave FROM "
                f"pluggy_investimentos WHERE item_id IN ({marc_itens}))",
                itens,
            )
            conn.execute(
                f"DELETE FROM pluggy_investimentos WHERE item_id IN ({marc_itens})",
                itens,
            )
            conn.execute(
                f"DELETE FROM pluggy_contas WHERE item_id IN ({marc_itens})",
                itens,
            )
            conn.execute(
                f"DELETE FROM pluggy_itens WHERE item_id IN ({marc_itens})",
                itens,
            )
        for tabela in orfas:
            conn.execute(f"DROP TABLE {tabela}")

        # O cache do extrato é derivado: invalidar força a reconstrução na
        # próxima requisição, já sem as contas removidas.
        conn.execute("DROP TABLE IF EXISTS extrato_efetivo_cache")
        conn.execute(
            "DELETE FROM app_meta WHERE chave = 'extrato_efetivo_cache_v1'"
        )
        conn.commit()
        print("removido.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
