"""Dinheiro emprestado: o que saiu daqui, o que voltou e o que peguei.

Dado 100% manual -- nada vem da Pluggy. Um Pix para o mecânico é
indistinguível de qualquer outro Pix no extrato, então quem sabe que aquilo
era empréstimo é a pessoa, não o banco. Fica em tabela própria, do lado das
outras camadas locais (extrato_*, fixas_*), e nenhuma sincronização a toca.

Três tipos no mesmo lugar de propósito: os três formam um saldo só ("quanto
ainda tenho para receber"), e a tela mostra os três juntos. O `tipo` é a única
coisa que os separa.

Pagamento não aponta para qual empréstimo quitou -- é assim no dado de origem
e continua assim aqui. O total recebido é global, não por item. Amarrar
pagamento a empréstimo mudaria o significado do número que ja existe.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import banco as fin

TIPOS = ("emprestado", "pagamento", "emprestimo_pego")

SCHEMA = """
CREATE TABLE IF NOT EXISTS emprestimos (
  id TEXT PRIMARY KEY,
  tipo TEXT NOT NULL
    CHECK (tipo IN ('emprestado', 'pagamento', 'emprestimo_pego')),
  item TEXT NOT NULL DEFAULT '',
  valor REAL NOT NULL DEFAULT 0,
  -- "emprestado" e "pagamento" usam data; "emprestimo_pego" usa o par
  -- data_inicio/data_final. Data vazia e valida: varios registros antigos
  -- nao tem data e nao inventamos uma.
  data TEXT NOT NULL DEFAULT '',
  data_inicio TEXT NOT NULL DEFAULT '',
  data_final TEXT NOT NULL DEFAULT '',
  -- So faz sentido em "emprestimo_pego"; nos outros fica no default.
  status TEXT NOT NULL DEFAULT 'em_andamento'
    CHECK (status IN ('em_andamento', 'finalizado')),
  observacao TEXT NOT NULL DEFAULT '',
  ordem INTEGER NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_emprestimos_tipo ON emprestimos (tipo, ordem);
"""

_schema_pronto = False


def _abrir() -> sqlite3.Connection:
    """Conexao com o schema garantido. Uma vez por processo: a tabela nao muda
    entre requisicoes."""
    global _schema_pronto
    fin.ensure_database()
    conn = fin.connect()
    conn.row_factory = sqlite3.Row
    if not _schema_pronto:
        conn.executescript(SCHEMA)
        conn.commit()
        _schema_pronto = True
    return conn


# --------------------------------------------------------------------------
# Normalizacao
# --------------------------------------------------------------------------

def _texto(valor: object) -> str:
    return "" if valor is None else str(valor).strip()


def _valor(valor: object) -> float:
    """Aceita number, "1.234,56" e "1234.56" -- a tela manda texto digitado."""
    if isinstance(valor, (int, float)):
        return round(float(valor), 2)
    texto = _texto(valor)
    if not texto:
        return 0.0
    limpo = re.sub(r"[^\d,.\-]", "", texto)
    # Vírgula como decimal quando ela vem depois do último ponto: em
    # "1.234,56" o ponto é separador de milhar, em "1234.56" é decimal.
    if "," in limpo and limpo.rfind(",") > limpo.rfind("."):
        limpo = limpo.replace(".", "").replace(",", ".")
    else:
        limpo = limpo.replace(",", "")
    try:
        return round(float(limpo), 2)
    except ValueError:
        return 0.0


def _data(valor: object) -> str:
    """Devolve 'YYYY-MM-DD' ou vazio. Aceita ISO com hora e dd/mm/aaaa."""
    texto = _texto(valor)
    if not texto:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto[:10]) and len(texto) >= 10:
        return texto[:10]
    br = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", texto)
    if br:
        return f"{br.group(3)}-{br.group(2)}-{br.group(1)}"
    return ""


def _status(valor: object) -> str:
    return "finalizado" if "final" in _texto(valor).casefold() else "em_andamento"


def _tipo(valor: object) -> str:
    bruto = _texto(valor).casefold()
    if bruto in TIPOS:
        return bruto
    # Apelidos que o dashboard antigo usava.
    if bruto in {"pago", "pagamentos"}:
        return "pagamento"
    if bruto in {"pego", "emprestimos_pegos", "emprestimospegos"}:
        return "emprestimo_pego"
    if bruto in {"lent", "emprestados"}:
        return "emprestado"
    raise ValueError(
        f"Tipo inválido: {valor!r}. Use um de: {', '.join(TIPOS)}."
    )


def _novo_id(tipo: str) -> str:
    # Sufixo de tempo em ms: ids criados no mesmo segundo não colidem.
    return f"{tipo}_{int(time.time() * 1000)}"


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------

def _somar_meses(iso: str, meses: int) -> str:
    """'YYYY-MM-DD' + N meses, preservando o dia quando ele existe no destino.

    Dia 31 somado a um mês de 30 cai no último dia do mês, que é como banco
    trata vencimento -- não pula para o mês seguinte.
    """
    ano, mes, dia = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    total = (ano * 12 + mes - 1) + meses
    ano_novo, mes_novo = divmod(total, 12)
    mes_novo += 1
    ultimo_dia = [31, 29 if (ano_novo % 4 == 0 and (ano_novo % 100 != 0 or ano_novo % 400 == 0))
                  else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mes_novo - 1]
    return f"{ano_novo:04d}-{mes_novo:02d}-{min(dia, ultimo_dia):02d}"


def _parcelas(item: dict[str, Any]) -> dict[str, Any] | None:
    """Parcelas de um empréstimo pego, deduzidas do período.

    O cadastro guarda início, fim e total -- não o número de parcelas. O
    número sai dos meses do intervalo contando as duas pontas: a primeira
    parcela é no mês de início e a última no mês final. Um empréstimo de
    setembro a março é 7x, não 6x.

    Devolve None quando não dá para calcular (falta uma das datas, ou o fim
    é antes do início) -- melhor não mostrar nada do que mostrar um palpite.
    """
    inicio, fim = item.get("dataInicio") or "", item.get("dataFinal") or ""
    if len(inicio) < 10 or len(fim) < 10:
        return None
    meses = ((int(fim[:4]) - int(inicio[:4])) * 12
             + (int(fim[5:7]) - int(inicio[5:7])))
    if meses < 0:
        return None
    total = meses + 1   # as duas pontas contam

    hoje = datetime.now().strftime("%Y-%m-%d")
    if item.get("status") == "finalizado":
        # Quitado é quitado: o status vale mais que a conta pelas datas, que
        # pode estar defasada se o empréstimo foi liquidado antes do prazo.
        pagas = total
    else:
        # Parcela n vence n-1 meses depois do início (a 1a é no próprio mês).
        pagas = sum(1 for n in range(total)
                    if _somar_meses(inicio, n) <= hoje)

    valor_parcela = round(float(item.get("valor") or 0) / total, 2)
    return {
        "total": total,
        "pagas": pagas,
        "restantes": total - pagas,
        "valorParcela": valor_parcela,
        "valorPago": round(valor_parcela * pagas, 2),
        "valorRestante": round(float(item.get("valor") or 0) - valor_parcela * pagas, 2),
    }


def _linha(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tipo": row["tipo"],
        "item": row["item"],
        "valor": float(row["valor"] or 0),
        "data": row["data"],
        "dataInicio": row["data_inicio"],
        "dataFinal": row["data_final"],
        "status": row["status"],
        "observacao": row["observacao"],
    }


def _mais_recente_primeiro(campo: str):
    """Ordena por data decrescente com os sem data no fim.

    Data vazia é comum aqui (vários registros antigos não têm), e jogá-los
    para o topo esconderia os lançamentos recentes.
    """
    def chave(item: dict[str, Any]) -> tuple[int, str]:
        data = item.get(campo) or ""
        # Sem data vai para o fim; entre os com data, string ISO ordena sozinha
        # e o "não" da segunda posição inverte para decrescente.
        return (1, "") if not data else (0, _inverter_iso(data))
    return chave


def _inverter_iso(data: str) -> str:
    """Chave que faz sorted() crescente devolver data decrescente."""
    return "".join(chr(ord("9") - int(c) + ord("0")) if c.isdigit() else c
                   for c in data)


def payload() -> dict[str, Any]:
    """Os três grupos e o saldo. Vazio é estado válido (tela nova, sem dado)."""
    with _abrir() as conn:
        linhas = [
            _linha(row)
            for row in conn.execute(
                # Ordem de cadastro, como no dashboard manual (que usava
                # rowid). A importação preservou essa sequência em `ordem`.
                "SELECT * FROM emprestimos ORDER BY ordem, id"
            )
        ]

    # Emprestado fica na ordem de cadastro -- é como a lista sempre foi lida,
    # e ela conta uma história cronológica que reordenar quebraria.
    emprestado = [l for l in linhas if l["tipo"] == "emprestado"]
    # Recebido e Empréstimos são consulta pontual ("quando entrou o último?"),
    # então ali o mais recente vem primeiro.
    pagamentos = sorted(
        [l for l in linhas if l["tipo"] == "pagamento"],
        key=_mais_recente_primeiro("data"),
    )
    pegos = sorted(
        [l for l in linhas if l["tipo"] == "emprestimo_pego"],
        key=_mais_recente_primeiro("dataInicio"),
    )
    for pego in pegos:
        pego["parcelas"] = _parcelas(pego)

    total_emprestado = round(sum(l["valor"] for l in emprestado), 2)
    total_recebido = round(sum(l["valor"] for l in pagamentos), 2)
    # Pego em aberto: o finalizado já foi quitado e não é mais dívida.
    pegos_abertos = [l for l in pegos if l["status"] != "finalizado"]

    return {
        "emprestado": emprestado,
        "pagamentos": pagamentos,
        "pegos": pegos,
        "totais": {
            "emprestado": total_emprestado,
            "recebido": total_recebido,
            # Pode ficar negativo: recebi mais do que o que está cadastrado
            # como emprestado. É sinal de cadastro incompleto, não de erro de
            # conta -- por isso não trava em zero.
            "aReceber": round(total_emprestado - total_recebido, 2),
            "pego": round(sum(l["valor"] for l in pegos), 2),
            "pegoAberto": round(sum(l["valor"] for l in pegos_abertos), 2),
            "qtdPegosAbertos": len(pegos_abertos),
        },
    }


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------

def _validar(dados: dict, tipo_atual: str = "") -> dict[str, Any]:
    tipo = _tipo(dados.get("tipo") or tipo_atual)
    registro = {
        "tipo": tipo,
        "item": _texto(dados.get("item")),
        "valor": _valor(dados.get("valor")),
        "data": _data(dados.get("data")),
        "data_inicio": _data(dados.get("dataInicio") or dados.get("data_inicio")),
        "data_final": _data(dados.get("dataFinal") or dados.get("data_final")),
        "status": _status(dados.get("status")),
        "observacao": _texto(dados.get("observacao")),
    }
    if registro["valor"] < 0:
        raise ValueError("Valor não pode ser negativo.")
    if (registro["data_inicio"] and registro["data_final"]
            and registro["data_inicio"] > registro["data_final"]):
        raise ValueError("Data final não pode ser anterior à data de início.")
    return registro


def criar(dados: dict) -> dict[str, Any]:
    registro = _validar(dados)
    novo_id = _texto(dados.get("id")) or _novo_id(registro["tipo"])
    with _abrir() as conn:
        if conn.execute("SELECT 1 FROM emprestimos WHERE id = ?", (novo_id,)).fetchone():
            raise ValueError(f"Já existe registro com o id {novo_id}.")
        proxima_ordem = conn.execute(
            "SELECT COALESCE(MAX(ordem), 0) + 1 FROM emprestimos WHERE tipo = ?",
            (registro["tipo"],),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO emprestimos (
              id, tipo, item, valor, data, data_inicio, data_final, status,
              observacao, ordem, criado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (novo_id, registro["tipo"], registro["item"], registro["valor"],
             registro["data"], registro["data_inicio"], registro["data_final"],
             registro["status"], registro["observacao"], proxima_ordem,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    return {"ok": True, "id": novo_id, **payload()}


def atualizar(registro_id: str, dados: dict) -> dict[str, Any]:
    with _abrir() as conn:
        atual = conn.execute(
            "SELECT tipo FROM emprestimos WHERE id = ?", (registro_id,)
        ).fetchone()
        if not atual:
            raise ValueError(f"Registro {registro_id} não encontrado.")
        registro = _validar(dados, atual["tipo"])
        conn.execute(
            """
            UPDATE emprestimos SET
              tipo = ?, item = ?, valor = ?, data = ?, data_inicio = ?,
              data_final = ?, status = ?, observacao = ?
            WHERE id = ?
            """,
            (registro["tipo"], registro["item"], registro["valor"],
             registro["data"], registro["data_inicio"], registro["data_final"],
             registro["status"], registro["observacao"], registro_id),
        )
        conn.commit()
    return {"ok": True, "id": registro_id, **payload()}


def remover(registro_id: str) -> dict[str, Any]:
    with _abrir() as conn:
        cursor = conn.execute("DELETE FROM emprestimos WHERE id = ?", (registro_id,))
        if not cursor.rowcount:
            raise ValueError(f"Registro {registro_id} não encontrado.")
        conn.commit()
    return {"ok": True, "id": registro_id, **payload()}


# --------------------------------------------------------------------------
# Importacao do dashboard manual
# --------------------------------------------------------------------------

def importar_do_financeiro(caminho: Path, aplicar: bool = False) -> dict[str, Any]:
    """Traz a tabela dinheiro_emprestado do financeiro.db (dashboard manual).

    Idempotente: o id de lá vira o id daqui, então rodar de novo atualiza em
    vez de duplicar.

    Registro sem item e sem valor é descartado, mesmo que tenha data: são
    linhas criadas ao clicar em "adicionar" e nunca preenchidas, e data
    sozinha não diz nada -- a tela mostraria uma linha em branco.
    """
    if not caminho.exists():
        raise ValueError(f"Não encontrei {caminho}")

    origem = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    origem.row_factory = sqlite3.Row
    try:
        linhas = origem.execute(
            """
            SELECT tipo, id, item, valor, data, data_inicio, data_final, status
            FROM dinheiro_emprestado ORDER BY rowid
            """
        ).fetchall()
    finally:
        origem.close()

    prontos: list[tuple] = []
    descartados: list[str] = []
    agora = datetime.now().isoformat(timespec="seconds")
    for ordem, row in enumerate(linhas, start=1):
        vazio = not _texto(row["item"]) and not _valor(row["valor"])
        if vazio:
            descartados.append(row["id"])
            continue
        prontos.append((
            row["id"], _tipo(row["tipo"]), _texto(row["item"]),
            _valor(row["valor"]), _data(row["data"]), _data(row["data_inicio"]),
            _data(row["data_final"]), _status(row["status"]), "", ordem, agora,
        ))

    resumo = {
        "lidos": len(linhas),
        "importados": len(prontos),
        "descartados": descartados,
        "porTipo": {
            tipo: sum(1 for p in prontos if p[1] == tipo) for tipo in TIPOS
        },
        "aplicado": aplicar,
    }
    if not aplicar:
        return resumo

    with _abrir() as conn:
        conn.executemany(
            """
            INSERT INTO emprestimos (
              id, tipo, item, valor, data, data_inicio, data_final, status,
              observacao, ordem, criado_em
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              tipo = excluded.tipo, item = excluded.item,
              valor = excluded.valor, data = excluded.data,
              data_inicio = excluded.data_inicio,
              data_final = excluded.data_final, status = excluded.status,
              ordem = excluded.ordem
            """,
            prontos,
        )
        conn.commit()
    resumo["totais"] = payload()["totais"]
    return resumo


def _cli() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "importar":
        print(__doc__)
        print("Uso: python emprestimos.py importar <financeiro.db> [--aplicar]")
        return 1
    caminho = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("financeiro.db")
    aplicar = "--aplicar" in sys.argv
    resumo = importar_do_financeiro(caminho, aplicar=aplicar)
    print(f"Origem   : {caminho}")
    print(f"Destino  : {fin.DATABASE_PATH}")
    print(f"Lidos    : {resumo['lidos']}")
    print(f"Prontos  : {resumo['importados']}  {resumo['porTipo']}")
    if resumo["descartados"]:
        print(f"Vazios   : {len(resumo['descartados'])} descartado(s) "
              f"-> {', '.join(resumo['descartados'])}")
    if not aplicar:
        print("\nDRY-RUN: nada foi gravado. Rode com --aplicar para gravar.")
    else:
        print(f"Totais   : {resumo['totais']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
