"""Em que fatura cada compra de cartão caiu.

O projeto adivinhava isso pela data da última compra ("mês da última compra +
1"). Funciona enquanto o mês corrente está vazio e quebra em TODA virada de
mês: no dia 1º chega a primeira compra do mês novo, a última data salta, e a
fatura inteira migra de coluna. Foi o que aconteceu em setembro/2026.

A resposta não se adivinha, se pergunta ao banco -- em quatro degraus, do mais
autoritativo para o mais palpite (ver `expressao_competencia`):

  1. **billId.** A fatura fechou e a API a emitiu: a competência é a dela.
  2. **billForecastDate.** A Pluggy manda, em cada transação de cartão, o mês
     em que o ciclo FECHA. Qual ciclo, dentro daquele mês, quem diz é a data da
     compra: o que fecha ali, ou o seguinte se a compra vier depois do
     fechamento. A confiabilidade disso é MEDIDA por cartão contra as
     faturas que já fecharam -- há conector que manda o campo oscilando, e nele
     o degrau 2 fica desligado.
  3. **A janela do ciclo.** Cada fatura é a janela entre o fechamento anterior
     e o dela; compra no dia do fechamento cai na fatura seguinte. Serve para
     o cartão cujo billForecastDate não é confiável.
  4. **Mês da compra + 1**, para cartão recém-conectado, sem fatura fechada.

As janelas dos ciclos futuros são projetadas a partir da última fatura
conhecida, repetindo o dia de fechamento dela. É o degrau mais fraco justamente
por isso: o dia de fechamento real oscila (fim de semana, feriado), e um erro
de um dia joga a compra daquele dia na fatura errada -- razão pela qual o
degrau 2 vem antes dele.

Nada aqui olha o nome do banco. Cartão novo passa a ter ciclo próprio na
primeira fatura que fechar, e billForecastDate confiável a partir da vigésima,
sem tocar em código.
"""

from __future__ import annotations

import sqlite3
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

# Quantos ciclos projetar além da última fatura conhecida. Precisa cobrir a
# fatura aberta, as próximas e o horizonte das parcelas longas (24x é comum).
CICLOS_PROJETADOS = 30

TABELA = """
CREATE TABLE IF NOT EXISTS pluggy_ciclos (
  conta_id    TEXT NOT NULL,
  inicio      TEXT NOT NULL,   -- primeira data que cai neste ciclo
  fim         TEXT NOT NULL,   -- fechamento: NÃO pertence a este ciclo
  competencia TEXT NOT NULL,   -- AAAA-MM em que a fatura vence
  fatura_id   TEXT NOT NULL DEFAULT '',
  origem      TEXT NOT NULL,   -- 'fatura' (o banco informou) ou 'projetado'
  PRIMARY KEY (conta_id, competencia)
);
CREATE INDEX IF NOT EXISTS idx_ciclos_janela
  ON pluggy_ciclos (conta_id, inicio, fim);

CREATE TABLE IF NOT EXISTS pluggy_cartao_previsao (
  conta_id  TEXT PRIMARY KEY,
  acertos   INTEGER NOT NULL,   -- faturas fechadas que a regra reproduz
  amostras  INTEGER NOT NULL
);
"""

# A previsao do conector so e usada quando ela e consistente nas faturas que
# ja fecharam. 90% e folgado o suficiente para tolerar um mes atipico e
# apertado o suficiente para descartar cartao onde o campo oscila.
CONFIANCA_MINIMA = 0.9

# Minimo de faturas fechadas para a medicao valer algo.
AMOSTRA_MINIMA = 20


def _dia(texto: Any) -> date | None:
    """A data em ISO que veio do banco, só a parte do dia."""
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


def _somar_meses(momento: date, meses: int) -> date:
    """Mesmo dia do mês, N meses adiante, encurtando quando o mês é curto.

    Cartão que fecha dia 30 fecha dia 28 em fevereiro. Sem o encurtamento a
    projeção de fevereiro seria inválida.
    """
    total = momento.month - 1 + meses
    ano = momento.year + total // 12
    mes = total % 12 + 1
    return date(ano, mes, min(momento.day, monthrange(ano, mes)[1]))


def _competencia_seguinte(competencia: str, passos: int) -> str:
    ano, mes = int(competencia[:4]), int(competencia[5:7])
    total = (ano * 12 + mes - 1) + passos
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _faturas_do_cartao(conn: sqlite3.Connection,
                       conta_id: str) -> list[dict[str, Any]]:
    """As faturas fechadas do cartão, em ordem de fechamento.

    Fatura sem fechamento ou sem competência não serve de âncora -- entra no
    banco quando o conector não soube dizer o ciclo -- então fica de fora.
    """
    linhas = conn.execute(
        """
        SELECT fatura_id, competencia, fechamento
        FROM pluggy_faturas
        WHERE conta_id = ?
        ORDER BY COALESCE(fechamento, competencia)
        """,
        (conta_id,),
    ).fetchall()
    faturas = []
    for linha in linhas:
        fechamento = _dia(linha["fechamento"])
        competencia = str(linha["competencia"] or "")[:7]
        if fechamento is None or len(competencia) != 7:
            continue
        faturas.append({
            "fatura_id": str(linha["fatura_id"] or ""),
            "competencia": competencia,
            "fechamento": fechamento,
        })
    faturas.sort(key=lambda f: f["fechamento"])
    return faturas


def ciclos_do_cartao(conn: sqlite3.Connection,
                     conta_id: str) -> list[dict[str, Any]]:
    """Janelas de ciclo do cartão: as informadas e as projetadas à frente.

    Devolve vazio quando o cartão ainda não tem nenhuma fatura fechada. Quem
    chama decide o que fazer nesse caso -- não há ciclo a inventar aqui, e
    inventar um pelo nome do banco é exatamente o que se quer evitar.
    """
    faturas = _faturas_do_cartao(conn, conta_id)
    if not faturas:
        return []

    ciclos: list[dict[str, Any]] = []
    anterior: date | None = None
    for fatura in faturas:
        inicio = anterior if anterior else _somar_meses(fatura["fechamento"], -1)
        # Fatura repetida no mesmo fechamento (reconexão traz a mesma fatura
        # com id novo) viraria uma janela vazia; a competência é PK, então a
        # segunda simplesmente substitui a primeira na gravação.
        if inicio < fatura["fechamento"]:
            ciclos.append({
                "inicio": inicio,
                "fim": fatura["fechamento"],
                "competencia": fatura["competencia"],
                "fatura_id": fatura["fatura_id"],
                "origem": "fatura",
            })
        anterior = fatura["fechamento"]

    ultima = faturas[-1]
    for passo in range(1, CICLOS_PROJETADOS + 1):
        fim = _somar_meses(ultima["fechamento"], passo)
        inicio = _somar_meses(ultima["fechamento"], passo - 1)
        ciclos.append({
            "inicio": inicio,
            "fim": fim,
            "competencia": _competencia_seguinte(ultima["competencia"], passo),
            "fatura_id": "",
            "origem": "projetado",
        })
    return ciclos


def _garantir_formato(conn: sqlite3.Connection, tabela: str,
                      colunas: set[str]) -> None:
    """Derruba a tabela se ela existir com outro conjunto de colunas."""
    existe = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (tabela,),
    ).fetchone()
    if not existe:
        return
    atuais = {
        linha[1] for linha in conn.execute(f"PRAGMA table_info({tabela})")
    }
    if atuais != colunas:
        conn.execute(f"DROP TABLE {tabela}")


def reconstruir(conn: sqlite3.Connection) -> int:
    """Regrava pluggy_ciclos para todos os cartões. Devolve quantos ciclos."""
    # As duas tabelas sao derivadas: nascem inteiras daqui a cada
    # inicializacao da camada. Quando o formato muda entre versoes do codigo,
    # recriar e mais simples e mais seguro que migrar -- nao ha nada do usuario
    # aqui para preservar.
    _garantir_formato(conn, "pluggy_ciclos",
                      {"conta_id", "inicio", "fim", "competencia",
                       "fatura_id", "origem"})
    _garantir_formato(conn, "pluggy_cartao_previsao",
                      {"conta_id", "acertos", "amostras"})
    conn.executescript(TABELA)
    conn.execute("DELETE FROM pluggy_ciclos")
    conn.execute("DELETE FROM pluggy_cartao_previsao")
    total = 0
    contas = conn.execute(
        "SELECT conta_id FROM pluggy_contas WHERE subtipo = 'CREDIT_CARD'"
    ).fetchall()
    for conta in contas:
        conta_id = conta["conta_id"]
        medida = _confiabilidade_previsao(conn, conta_id)
        if medida is not None:
            acertos, amostras = medida
            conn.execute(
                "INSERT OR REPLACE INTO pluggy_cartao_previsao "
                "  (conta_id, acertos, amostras) VALUES (?, ?, ?)",
                (conta_id, acertos, amostras),
            )
        for ciclo in ciclos_do_cartao(conn, conta_id):
            conn.execute(
                "INSERT OR REPLACE INTO pluggy_ciclos "
                "  (conta_id, inicio, fim, competencia, fatura_id, origem) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conta_id, ciclo["inicio"].isoformat(), ciclo["fim"].isoformat(),
                 ciclo["competencia"], ciclo["fatura_id"], ciclo["origem"]),
            )
            total += 1
    return total


def competencia_de(conn: sqlite3.Connection, conta_id: str,
                   data: str) -> str | None:
    """A competência do ciclo em que esta data cai, ou None se não há ciclo."""
    linha = conn.execute(
        """
        SELECT competencia FROM pluggy_ciclos
        WHERE conta_id = ? AND inicio <= ? AND fim > ?
        ORDER BY inicio LIMIT 1
        """,
        (conta_id, str(data)[:10], str(data)[:10]),
    ).fetchone()
    return linha["competencia"] if linha else None


def conferir(conn: sqlite3.Connection,
             contas: set[str] | None = None) -> list[str]:
    """Aponta transação pendente que não caiu em nenhum ciclo.

    A janela só decide a competência das PENDENTES: quem já tem billId
    pergunta direto à fatura, que é o próprio banco respondendo. Então o que
    precisa de conferência é a cobertura -- toda compra pendente tem de achar
    um ciclo, senão ela desaparece de todas as colunas.

    `contas` limita a conferência às contas que os cálculos usam; a conta
    substituída por reconexão tem faturas que o conector não devolve mais e
    apareceria aqui como problema sem ser.
    """
    onde = ""
    parametros: list[Any] = []
    if contas is not None:
        if not contas:
            return []
        onde = f" AND t.conta_id IN ({', '.join('?' for _ in contas)})"
        parametros = sorted(contas)
    linhas = conn.execute(
        f"""
        SELECT a.nome, t.data, t.descricao, t.valor
        FROM pluggy_transacoes t
        JOIN pluggy_contas a ON a.conta_id = t.conta_id
                            AND a.subtipo = 'CREDIT_CARD'
        WHERE t.fatura_id = '' AND t.tipo = 'DEBIT'{onde}
          AND NOT EXISTS (
                SELECT 1 FROM pluggy_ciclos c
                WHERE c.conta_id = t.conta_id
                  AND c.inicio <= SUBSTR(t.data, 1, 10)
                  AND c.fim > SUBSTR(t.data, 1, 10))
        ORDER BY a.nome, t.data
        """,
        parametros,
    ).fetchall()
    return [
        f"{linha['nome']} {str(linha['data'])[:10]} sem ciclo: "
        f"{str(linha['descricao'])[:34]}"
        for linha in linhas
    ]


# ---------------------------------------------------------------------------
# Competencia de fatura de uma transacao de cartao
# ---------------------------------------------------------------------------
#
# Uma unica definicao, usada pela view do extrato E pela tela de Cartoes. Ter
# duas implementacoes da mesma pergunta era como a lista de lancamentos de um
# mes acabava discordando do total daquele mes.


def _confiabilidade_previsao(conn: sqlite3.Connection,
                             conta_id: str) -> tuple[int, int] | None:
    """Quanto a previsao do conector acerta neste cartao.

    A Pluggy manda em cada transacao de cartao um `creditCardMetadata.
    billForecastDate` no formato AAAA-MM. Medido contra as faturas que ja
    fecharam, ele e o mes em que o ciclo FECHA -- nao o mes em que a fatura
    vence. A diferenca entre os dois depende do cartao: um que fecha dia 30 e
    vence dia 7 tem vencimento no mes seguinte ao fechamento; um que fecha dia
    2 e vence dia 9 vence no mesmo mes.

    Sabendo o mes do fechamento, quem termina de responder e a data da compra:
    dentro do mes apontado existe UM fechamento, e a compra cai no ciclo que
    fecha ali ou, se ela vier DEPOIS do fechamento, no seguinte. Foi isso que
    explicou as 11 excecoes que um deslocamento fixo de meses nao explicava --
    todas eram compras nos ultimos dias do mes, passado o fechamento.

    Compra NO dia do fechamento entra no ciclo que fecha naquele dia. Note que
    a janela do degrau 3 usa a fronteira oposta ([inicio, fim), ou seja compra
    no dia do fechamento vai para o ciclo seguinte). Nao e descuido: as duas
    fronteiras foram medidas contra as faturas fechadas e cada mecanismo
    acertou mais com a sua. A leitura provavel e que o `fechamento` informado e
    o dia em que a fatura foi EMITIDA, um dia depois da ultima compra que ela
    contem -- o que tornaria as duas coerentes. Enquanto isso for suposicao,
    ficam como estao, cada uma com a fronteira que os dados sustentam.

    Aqui so se mede se a regra funciona neste cartao. Devolve None quando nao
    ha amostra suficiente ou quando a previsao nao e confiavel -- ha conector
    que manda o campo oscilando, e nesse caso quem responde e a janela do
    ciclo (degrau 3).
    """
    linhas = conn.execute(
        """
        SELECT
          SUBSTR(JSON_EXTRACT(
            t.raw_json, '$.creditCardMetadata.billForecastDate'), 1, 7
          ) AS previsao,
          SUBSTR(t.data, 1, 10) AS dia,
          f.competencia
        FROM pluggy_transacoes t
        JOIN pluggy_faturas f ON f.conta_id = t.conta_id
                             AND f.fatura_id = t.fatura_id
        WHERE t.conta_id = ?
          AND JSON_EXTRACT(t.raw_json, '$.creditCardMetadata.billForecastDate')
              GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*'
        """,
        (conta_id,),
    ).fetchall()
    if len(linhas) < AMOSTRA_MINIMA:
        return None

    janelas = ciclos_do_cartao(conn, conta_id)
    if not janelas:
        return None
    fechamentos = sorted(
        (ciclo["fim"].isoformat(), ciclo["competencia"]) for ciclo in janelas
    )

    acertos = 0
    for linha in linhas:
        resposta = _competencia_pela_previsao(
            fechamentos, linha["previsao"], linha["dia"]
        )
        if resposta == linha["competencia"]:
            acertos += 1
    if acertos / len(linhas) < CONFIANCA_MINIMA:
        return None
    return acertos, len(linhas)


def _competencia_pela_previsao(fechamentos: list[tuple[str, str]],
                               previsao: str, dia: str) -> str | None:
    """A competencia que a previsao do conector aponta, resolvida pela data."""
    for indice, (fim, competencia) in enumerate(fechamentos):
        if fim[:7] != previsao[:7]:
            continue
        if dia[:10] > fim:
            seguinte = fechamentos[indice + 1:]
            return seguinte[0][1] if seguinte else None
        return competencia
    return None


def _serial(competencia: str) -> int:
    return int(competencia[:4]) * 12 + int(competencia[5:7]) - 1


def expressao_competencia(t: str = "t", a: str | None = "a") -> str:
    """SQL que devolve a competencia de fatura de uma transacao de cartao.

    Quatro degraus, do mais autoritativo para o mais palpite:

      1. billId -> a competencia da propria fatura;
      2. billForecastDate do conector, corrigido pelo deslocamento medido
         daquele cartao (so existe linha em pluggy_cartao_previsao quando a
         medicao foi consistente);
      3. a janela do ciclo em que a data cai;
      4. mes da compra + 1, para cartao recem conectado, sem fatura fechada.

    `t` e o alias da tabela de transacoes. `a` e o alias de extrato_ajustes,
    para a consulta que faz JOIN com ela e portanto respeita conta e data
    editadas a mao; passe None na consulta que le pluggy_transacoes crua.
    """
    conta = (f"COALESCE({a}.conta_id_manual, {t}.conta_id)" if a
             else f"{t}.conta_id")
    data = f"COALESCE({a}.data_manual, {t}.data)" if a else f"{t}.data"
    previsao = (
        f"JSON_EXTRACT({t}.raw_json, "
        "'$.creditCardMetadata.billForecastDate')"
    )
    return f"""
    COALESCE(
      (SELECT f.competencia FROM pluggy_faturas f
        WHERE {t}.fatura_id <> ''
          AND f.conta_id = {conta}
          AND f.fatura_id = {t}.fatura_id),
      (SELECT CASE
                WHEN SUBSTR({data}, 1, 10) > x.fim
                  -- compra DEPOIS do fechamento: proximo ciclo
                  THEN (SELECT y.competencia FROM pluggy_ciclos y
                         WHERE y.conta_id = x.conta_id AND y.fim > x.fim
                         ORDER BY y.fim LIMIT 1)
                ELSE x.competencia
              END
         FROM pluggy_ciclos x
        WHERE x.conta_id = {conta}
          AND SUBSTR(x.fim, 1, 7) = SUBSTR({previsao}, 1, 7)
          AND {previsao} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*'
          AND EXISTS (SELECT 1 FROM pluggy_cartao_previsao p
                       WHERE p.conta_id = x.conta_id)
        ORDER BY x.fim LIMIT 1),
      (SELECT x.competencia FROM pluggy_ciclos x
        WHERE x.conta_id = {conta}
          AND x.inicio <= SUBSTR({data}, 1, 10)
          AND x.fim > SUBSTR({data}, 1, 10)
        ORDER BY x.inicio LIMIT 1),
      STRFTIME('%Y-%m', DATE(SUBSTR({data}, 1, 7) || '-01', '+1 month'))
    )
    """
