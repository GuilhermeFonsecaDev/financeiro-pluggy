"""Rede de segurança da grade de faturas.

    python testes_faturas.py            confere invariantes e compara com o snapshot
    python testes_faturas.py --aceitar  regrava o snapshot com o resultado atual

Existe porque toda correção na competência de fatura mexe em dezenas de
células ao mesmo tempo, em anos diferentes, e conferir isso na tela não
funciona: dá para consertar um mês e quebrar outro sem perceber. Aqui uma
mudança aparece como uma lista de células alteradas, com a origem de cada
valor -- que é o que se revisa.

Duas partes independentes:

* **Invariantes** -- valem sempre, não dependem de snapshot, e uma delas
  falhando é bug e não escolha (por exemplo: mês marcado como `oficial` que
  não é idêntico ao valor que a API entregou).
* **Snapshot** -- a grade inteira, gravada em `data/` porque contém valores
  reais e o repositório é público. Na primeira execução ele é criado; nas
  seguintes, qualquer diferença é listada e o teste falha, para a mudança ser
  deliberada.

Roda sobre uma CÓPIA do banco (`data/teste_faturas.db`). O banco de verdade
não é tocado, nem para reconstruir cache.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
DESTINO = RAIZ / "data"
COPIA = DESTINO / "teste_faturas.db"
SNAPSHOT = DESTINO / "snapshot_faturas.json"

# Anos conferidos: os que têm movimento mais o horizonte das parcelas longas.
ANOS = (2025, 2026, 2027)

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out",
         "nov", "dez"]


def preparar():
    """Copia o banco e devolve os módulos apontados para a cópia."""
    origem = RAIZ / "pluggy.db"
    if not origem.exists():
        print(f"banco não encontrado em {origem}")
        raise SystemExit(2)
    DESTINO.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origem, COPIA)

    sys.path.insert(0, str(RAIZ))
    import banco

    banco.DATABASE_PATH = COPIA
    # O backup automático copiaria a cópia para backups/ a cada execução.
    banco.create_database_backup = lambda *a, **k: None

    import extrato_camada as cam
    import pluggy_extrato as px

    conn = cam._abrir()
    cam.garantir_camada(conn, forcar=True)
    conn.commit()
    cam.garantir_extrato_materializado(conn)
    return conn, cam, px


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------

def grade(px) -> dict:
    """A grade de todos os anos, por nome de coluna (o id muda entre versões)."""
    saida = {}
    for ano in ANOS:
        dados = px.cartoes_payload(ano, "fatura")
        for cartao in dados["cartoes"]:
            chave = f"{cartao['nome']}|{ano}"
            saida[chave] = {
                "valores": [round(v, 2) for v in dados["valores"][cartao["id"]]],
                "origens": list(dados["origens"][cartao["id"]]),
            }
        saida[f"TOTAL|{ano}"] = {
            "valores": [round(v, 2) for v in dados["totalMes"]],
            "origens": [""] * 12,
        }
    return saida


# ---------------------------------------------------------------------------
# invariantes
# ---------------------------------------------------------------------------

def contas_por_coluna(conn, px, dados) -> dict[str, list[str] | None]:
    """Quais contas cada coluna da grade representa.

    A coluna pode ser consolidada -- vários cartões numa fatura só --, e aí a
    fatura oficial da coluna é a soma das faturas das contas dela.

    Quando o registro de identidade existe, ele responde. Sem ele, uma coluna
    cujo id não é um `conta_id` de verdade é consolidada por alguma regra que
    daqui não se enxerga: devolve None, e a conferência a trata como não
    verificável em vez de inventar que a API mandou zero.
    """
    try:
        import cartoes

        ativas = set(px.contas_ativas(conn))
        return {c["id"]: c["contas"] for c in cartoes.colunas(conn, ativas)}
    except ImportError:
        pass
    reais = {
        linha["conta_id"] for linha in conn.execute(
            "SELECT conta_id FROM pluggy_contas"
        )
    }
    return {
        c["id"]: ([c["id"]] if c["id"] in reais else None)
        for c in dados["cartoes"]
    }


def invariante_oficial_igual_api(conn, px,
                                 problemas: list[str]) -> tuple[int, int]:
    """Mês marcado como `oficial` é exatamente o que a API entregou.

    É a invariante mais importante: fatura fechada não é reconstruída, é
    consumida. Se um mês `oficial` divergir do `valor_total`, alguma etapa
    posterior está mexendo no que já era resposta final.
    """
    faturas = {}
    for linha in conn.execute(
        "SELECT conta_id, competencia, valor_total FROM pluggy_faturas"
    ):
        faturas[(linha["conta_id"], linha["competencia"])] = round(
            linha["valor_total"] or 0, 2
        )
    conferidos = 0
    nao_verificaveis = 0
    for ano in ANOS:
        dados = px.cartoes_payload(ano, "fatura")
        colunas = contas_por_coluna(conn, px, dados)
        for cartao in dados["cartoes"]:
            contas = colunas.get(cartao["id"], [cartao["id"]])
            if contas is None:
                nao_verificaveis += sum(
                    1 for origem in dados["origens"][cartao["id"]]
                    if origem == "oficial"
                )
                continue
            for indice in range(12):
                if dados["origens"][cartao["id"]][indice] != "oficial":
                    continue
                competencia = f"{ano}-{indice + 1:02d}"
                esperado = round(sum(
                    faturas[(conta, competencia)] for conta in contas
                    if (conta, competencia) in faturas
                ), 2)
                obtido = round(dados["valores"][cartao["id"]][indice], 2)
                conferidos += 1
                if abs(esperado - obtido) > 0.005:
                    problemas.append(
                        f"mês 'oficial' diverge da API: {cartao['nome']} "
                        f"{MESES[indice]}/{ano} API={esperado:.2f} "
                        f"grade={obtido:.2f}"
                    )
    return conferidos, nao_verificaveis


def invariante_toda_compra_tem_competencia(conn, problemas: list[str]) -> int:
    """Compra de cartão sem competência desaparece de todas as colunas."""
    linhas = conn.execute(
        """
        SELECT a.nome, e.data, e.descricao
        FROM extrato_efetivo_cache e
        JOIN pluggy_contas a USING(conta_id)
        WHERE a.subtipo = 'CREDIT_CARD' AND e.tipo = 'DEBIT'
          AND (e.competencia_fatura IS NULL OR e.competencia_fatura = '')
        LIMIT 20
        """
    ).fetchall()
    for linha in linhas:
        problemas.append(
            f"sem competência: {linha['nome']} {str(linha['data'])[:10]} "
            f"{str(linha['descricao'])[:30]}"
        )
    return len(linhas)


def invariante_grade_soma_colunas(px, problemas: list[str]) -> None:
    """O total do mês é a soma das colunas -- pega coluna órfã na grade."""
    for ano in ANOS:
        dados = px.cartoes_payload(ano, "fatura")
        for indice in range(12):
            soma = round(sum(
                dados["valores"][c["id"]][indice] for c in dados["cartoes"]
            ), 2)
            total = round(dados["totalMes"][indice], 2)
            if abs(soma - total) > 0.005:
                problemas.append(
                    f"total do mês não é a soma das colunas: {MESES[indice]}/"
                    f"{ano} colunas={soma:.2f} total={total:.2f}"
                )


def invariante_competencia_dentro_do_horizonte(conn, problemas: list[str]) -> None:
    """Competência absurda denuncia aritmética de mês errada.

    Uma compra não pode cair numa fatura anterior à própria compra, nem mais de
    quatro meses depois dela -- cartão nenhum tem ciclo tão longo. Quando a
    regra de competência erra a conta de mês, é aqui que aparece.
    """
    linhas = conn.execute(
        """
        SELECT a.nome, e.data, e.descricao, e.competencia_fatura
        FROM extrato_efetivo_cache e
        JOIN pluggy_contas a USING(conta_id)
        WHERE a.subtipo = 'CREDIT_CARD' AND e.tipo = 'DEBIT'
          AND e.competencia_fatura IS NOT NULL
          AND (e.competencia_fatura < SUBSTR(e.data, 1, 7)
               OR e.competencia_fatura >
                  STRFTIME('%Y-%m', DATE(SUBSTR(e.data, 1, 7) || '-01',
                                         '+4 month')))
        LIMIT 20
        """
    ).fetchall()
    for linha in linhas:
        problemas.append(
            f"competência fora do horizonte: {linha['nome']} "
            f"compra {str(linha['data'])[:10]} -> fatura "
            f"{linha['competencia_fatura']} ({str(linha['descricao'])[:24]})"
        )


def invariante_pendente_cai_em_um_ciclo(conn, px, problemas: list[str]) -> None:
    """Toda pendente acha um ciclo. Só roda quando pluggy_ciclos existe."""
    existe = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'pluggy_ciclos'"
    ).fetchone()
    if not existe:
        return
    try:
        import ciclos
    except ImportError:
        return
    for aviso in ciclos.conferir(conn, set(px.contas_ativas(conn))):
        problemas.append(f"pendente sem ciclo: {aviso}")


# ---------------------------------------------------------------------------
# comparação com o snapshot
# ---------------------------------------------------------------------------

def comparar(atual: dict, salvo: dict) -> list[str]:
    diferencas = []
    for chave in sorted(set(atual) | set(salvo)):
        nome, ano = chave.split("|")
        a = salvo.get(chave)
        b = atual.get(chave)
        if a is None:
            diferencas.append(f"coluna nova: {nome} em {ano}")
            continue
        if b is None:
            diferencas.append(f"coluna desapareceu: {nome} em {ano}")
            continue
        for indice in range(12):
            antes, agora = a["valores"][indice], b["valores"][indice]
            if abs(antes - agora) <= 0.005:
                continue
            origem = b["origens"][indice] or "-"
            diferencas.append(
                f"{nome:<10} {MESES[indice]}/{ano[2:]}  {antes:>10.2f} -> "
                f"{agora:>10.2f}  ({origem})"
            )
    return diferencas


def main() -> int:
    aceitar = "--aceitar" in sys.argv
    conn, _cam, px = preparar()

    problemas: list[str] = []
    conferidos, nao_verificaveis = invariante_oficial_igual_api(
        conn, px, problemas)
    sem_competencia = invariante_toda_compra_tem_competencia(conn, problemas)
    invariante_grade_soma_colunas(px, problemas)
    invariante_competencia_dentro_do_horizonte(conn, problemas)
    invariante_pendente_cai_em_um_ciclo(conn, px, problemas)

    print(f"meses conferidos contra a API: {conferidos}")
    if nao_verificaveis:
        print(f"meses 'oficial' em coluna consolidada sem registro de "
              f"identidade (não verificáveis): {nao_verificaveis}")
    print(f"compras de cartão sem competência: {sem_competencia}")

    if problemas:
        print(f"\nINVARIANTES VIOLADAS ({len(problemas)}):")
        for problema in problemas[:40]:
            print(f"  - {problema}")
        if len(problemas) > 40:
            print(f"  ... e mais {len(problemas) - 40}")
    else:
        print("invariantes: todas ok")

    atual = grade(px)
    if aceitar or not SNAPSHOT.exists():
        SNAPSHOT.write_text(
            json.dumps(atual, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        acao = "regravado" if aceitar else "criado"
        print(f"\nsnapshot {acao}: {SNAPSHOT.relative_to(RAIZ)} "
              f"({len(atual)} colunas-ano)")
        return 1 if problemas else 0

    salvo = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    diferencas = comparar(atual, salvo)
    if diferencas:
        print(f"\nCÉLULAS QUE MUDARAM ({len(diferencas)}):")
        for linha in diferencas:
            print(f"  {linha}")
        print("\nSe a mudança é esperada, revise a lista e rode com --aceitar.")
    else:
        print("\ngrade idêntica ao snapshot")

    return 1 if (problemas or diferencas) else 0


if __name__ == "__main__":
    raise SystemExit(main())
