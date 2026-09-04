"""Contas fixas, ligadas às transações reais da Pluggy.

Mesmo modelo do dashboard manual (nome, tag, descontos, líquido, pago, forma de
pagamento), com uma diferença central: aqui a conta fixa pode ser **casada com
uma transação de verdade**, e é isso que decide se ela foi paga — em vez de um
checkbox que alguém precisa lembrar de marcar.

O casamento é explícito, escrito por quem cadastra: uma regra "a descrição
CONTÉM este termo", igual às regras da tela de extrato. Não há detecção
automática de contas fixas; a conta só existe porque foi criada à mão.

Modelo
------
fixas_contas     cadastro, independente de mês (nome, tag, valor previsto,
                 termo da regra, forma de pagamento padrão)
fixas_mes        o que é específico de um mês (valor ajustado, override de
                 pago, vínculo manual com transação, forma de pagamento,
                 observação)
fixas_descontos  subdescontos daquele mês, cada um com sua própria forma de
                 pagamento (inclusive "Reembolso")

Resolução, na mesma lógica de precedência da camada de extrato:

    transação:  vínculo manual  >  regra CONTÉM  >  nenhuma
    valor:      valor do mês    >  valor da transação  >  valor previsto
    pago:       decisão manual  >  achou transação     >  pendente
    forma:      escolha do mês  >  conta da transação  >  padrão do cadastro

Regras de cálculo (portadas do dashboard manual)
------------------------------------------------
    líquido = max(0, valor − todos os descontos)
    gasto   = líquido + os descontos que não são reembolso

Ou seja: um subdesconto que não é reembolso continua sendo gasto seu, só muda a
forma de pagamento. Reembolso de terceiros é o único que some do total.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any

import banco as fin
import cartoes
import extrato_camada as cam
import pluggy_extrato as px

TAGS = ["Contas Pessoais", "Contas Empresa"]

# Formas de pagamento. Uma forma é ou um id de conta/cartão da Pluggy, ou um
# destes dois sentinelas — que nunca colidem com um id da Pluggy (uuid).
FORMA_PIX = "pix"
FORMA_REEMBOLSO = "__reembolso__"
# Referências do cadastro anterior, reconhecidas somente pela migração.
_FORMAS_LEGADAS = frozenset({"itau", "nubank", "inter"})

# "" no banco significa "não escolhi", e cada nível decide o que fazer com isso:
# no cadastro vira PIX, no mês vira "herda do cadastro/da transação".
FORMA_AUTO = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS fixas_contas (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  tag TEXT NOT NULL DEFAULT 'Contas Pessoais',
  valor_previsto REAL NOT NULL DEFAULT 0,
  dia_vencimento INTEGER,
  categoria_id TEXT REFERENCES extrato_categorias (id),
  -- Regra de vínculo: a descrição da transação CONTÉM este termo.
  termo TEXT NOT NULL DEFAULT '',
  conta_id TEXT NOT NULL DEFAULT '',
  tolerancia REAL NOT NULL DEFAULT 0.30,
  ativo INTEGER NOT NULL DEFAULT 1,
  ordem INTEGER NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL,
  -- Primeiro mes em que a conta existe. Sem isto ela aparecia identica em
  -- todos os meses do historico, inclusive antes de existir.
  desde TEXT NOT NULL DEFAULT '',
  -- Ultimo mes (parcelamento que acaba). Vazio = sem fim previsto.
  ate TEXT NOT NULL DEFAULT '',
  forma_pagamento TEXT NOT NULL DEFAULT '',
  -- Continua visível nos gastos por tag, mas não reduz o saldo novamente.
  incluir_calculos INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fixas_mes (
  mes_ref TEXT NOT NULL,
  fixa_id TEXT NOT NULL REFERENCES fixas_contas (id) ON DELETE CASCADE,
  valor REAL,
  pago_override INTEGER,
  transacao_id TEXT,
  observacao TEXT NOT NULL DEFAULT '',
  forma_pagamento TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (mes_ref, fixa_id)
);

CREATE TABLE IF NOT EXISTS fixas_descontos (
  id TEXT PRIMARY KEY,
  fixa_id TEXT NOT NULL REFERENCES fixas_contas (id) ON DELETE CASCADE,
  mes_ref TEXT NOT NULL,
  descricao TEXT NOT NULL,
  valor REAL NOT NULL DEFAULT 0,
  reembolso INTEGER NOT NULL DEFAULT 0,
  forma_pagamento TEXT NOT NULL DEFAULT ''
);

-- Termos alternativos de vinculo. A descricao do mesmo pagamento varia entre
-- meses ("PAG*Faculdade", "CEF MATRIZ", "FACULDADE XYZ"), e um termo so
-- deixava a conta sem vinculo justamente nos meses em que o banco mudou o
-- texto. Mesmo desenho de extrato_regra_termos, nas regras do extrato.
--
-- fixas_contas.termo continua existindo e guarda o PRIMEIRO termo: e o que
-- os payloads antigos leem, e o que mantem a migracao barata.
CREATE TABLE IF NOT EXISTS fixas_termos (
  fixa_id TEXT NOT NULL REFERENCES fixas_contas (id) ON DELETE CASCADE,
  ordem INTEGER NOT NULL,
  termo TEXT NOT NULL,
  PRIMARY KEY (fixa_id, ordem)
);

CREATE INDEX IF NOT EXISTS idx_fixas_termos ON fixas_termos (fixa_id, ordem);
CREATE INDEX IF NOT EXISTS idx_fixas_mes ON fixas_mes (mes_ref);
CREATE INDEX IF NOT EXISTS idx_fixas_desc ON fixas_descontos (mes_ref, fixa_id);

CREATE TABLE IF NOT EXISTS fixas_migracoes (
  versao INTEGER PRIMARY KEY,
  aplicado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixas_formas_migradas (
  versao INTEGER NOT NULL,
  tabela TEXT NOT NULL,
  registro_id TEXT NOT NULL,
  forma_original TEXT NOT NULL,
  forma_nova TEXT NOT NULL,
  evidencia TEXT NOT NULL,
  PRIMARY KEY (versao, tabela, registro_id)
);
"""

# Colunas acrescentadas depois da primeira versão. ALTER TABLE ADD COLUMN é a
# única migração que o SQLite faz barato, e serve para todas elas.
_COLUNAS_NOVAS = {
    "fixas_contas": {"desde": "TEXT NOT NULL DEFAULT ''",
                     "ate": "TEXT NOT NULL DEFAULT ''",
                     "forma_pagamento": "TEXT NOT NULL DEFAULT ''",
                     "incluir_calculos": "INTEGER NOT NULL DEFAULT 1"},
    "fixas_mes": {"forma_pagamento": "TEXT NOT NULL DEFAULT ''"},
    # transacao_id: o subdesconto é uma cobrança própria, muitas vezes em
    # outro cartão que a conta-pai. Sem o vínculo dele, ele herdava o "já
    # lançado" do pai -- e um pai sem termo (que nunca casa) deixava todos os
    # subdescontos eternamente como previsto, somando na fatura que já os
    # continha.
    "fixas_descontos": {"forma_pagamento": "TEXT NOT NULL DEFAULT ''",
                        "transacao_id": "TEXT"},
}

# As tags mudaram de nome para bater com o dashboard manual.
_TAGS_ANTIGAS = {"Pessoal": "Contas Pessoais", "Empresa": "Contas Empresa"}

_pronto = False


def _abrir() -> sqlite3.Connection:
    global _pronto
    fin.ensure_database()
    conn = cam.conectar()
    cam.garantir_camada(conn)
    # Contas fixas consultava a view extrato_efetivo direto -- ela reavalia
    # todas as regras por transacao a cada chamada, o que na pratica deixava
    # a tela sempre lenta, nao so depois de uma edicao. Transacoes ja usava
    # essa mesma copia materializada; aqui passa a usar tambem.
    cam.garantir_extrato_materializado(conn)
    if not _pronto:
        conn.executescript(SCHEMA)
        for tabela, colunas in _COLUNAS_NOVAS.items():
            existentes = {l["name"] for l in conn.execute(f"PRAGMA table_info({tabela})")}
            for coluna, tipo in colunas.items():
                if coluna not in existentes:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        # Sobra da detecção automática, que não existe mais: guardava só as
        # chaves de sugestão dispensadas, nenhum dado financeiro.
        conn.execute("DROP TABLE IF EXISTS fixas_sugestoes_ignoradas")
        for antiga, nova in _TAGS_ANTIGAS.items():
            conn.execute("UPDATE fixas_contas SET tag = ? WHERE tag = ?", (nova, antiga))
        # Contas que ja existiam entram na tabela de termos com o termo unico
        # que tinham. Sem isto elas ficariam sem termo nenhum e parariam de
        # casar com as transacoes de um dia para o outro.
        conn.execute(
            "INSERT OR IGNORE INTO fixas_termos (fixa_id, ordem, termo) "
            "SELECT id, 0, termo FROM fixas_contas WHERE TRIM(termo) <> ''")
        # Descontos antigos marcados como reembolso ganham a forma equivalente.
        conn.execute(
            "UPDATE fixas_descontos SET forma_pagamento = ? "
            "WHERE reembolso = 1 AND forma_pagamento = ''", (FORMA_REEMBOLSO,))
        _migrar_formas(conn)
        conn.commit()
        _pronto = True
    return conn


# --------------------------------------------------------------------------
# Formas de pagamento
# --------------------------------------------------------------------------

def _referencias_salvas(conn: sqlite3.Connection) -> set[str]:
    """Inclui referências antigas sem ocultá-las nem mudar o meio de pagamento."""
    return {
        str(linha[0]) for linha in conn.execute(
            "SELECT forma_pagamento FROM fixas_contas "
            "UNION SELECT forma_pagamento FROM fixas_mes "
            "UNION SELECT forma_pagamento FROM fixas_descontos")
        if linha[0]
    }


def _formas_por_conta(conn: sqlite3.Connection) -> dict[str, str]:
    """Fonte Pluggy e identidade individual resolvem para o grupo de exibição."""
    resultado: dict[str, str] = {}
    for fonte, identidade in cartoes.identidades(conn).items():
        grupo = identidade["grupo"]
        resultado[fonte] = grupo
        resultado[identidade["cartaoId"]] = grupo
        resultado[grupo] = grupo
    return resultado


def _normalizar_forma(forma: str, formas_por_conta: dict[str, str]) -> str:
    """Normaliza referências conhecidas; desconhecidas continuam identificáveis."""
    return formas_por_conta.get(forma, forma) if forma else FORMA_PIX


def _formas(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """PIX e o catálogo dinâmico de cartões/tags, separados de contas bancárias."""
    resultado = [{"id": FORMA_PIX, "nome": "PIX", "cor": "#3fbf7f",
                  "tipo": "pagamento", "pendenteAssociacao": False}]
    resultado += [dict(coluna, tipo="cartao", pendenteAssociacao=False)
                  for coluna in cartoes.colunas(conn)]
    conhecidas = {forma["id"] for forma in resultado} | {FORMA_REEMBOLSO}
    mapa = _formas_por_conta(conn)
    for referencia in sorted(_referencias_salvas(conn)):
        forma = _normalizar_forma(referencia, mapa)
        if forma not in conhecidas:
            resultado.append({
                "id": forma, "nome": f"Associar cartão · {referencia}",
                "cor": "#9aa3ad", "tipo": "pendente",
                "contas": [], "membros": [], "pendenteAssociacao": True,
            })
            conhecidas.add(forma)
    return resultado


def _forma_para_gravar(conn: sqlite3.Connection, forma: str,
                       *, reembolso: bool = False) -> str:
    """Guarda instrumento/tag estável, nunca um nome de banco inferido."""
    permitidas = {"", FORMA_PIX}
    if reembolso:
        permitidas.add(FORMA_REEMBOLSO)
    identidades = cartoes.identidades(conn)
    if forma in identidades:
        return identidades[forma]["cartaoId"]
    for identidade in identidades.values():
        permitidas.add(identidade["cartaoId"])
        permitidas.add(identidade["grupo"])
    # Editar descrição/valor não deve destruir uma associação ainda pendente.
    permitidas |= _referencias_salvas(conn)
    if forma not in permitidas or (forma == FORMA_REEMBOLSO and not reembolso):
        raise ValueError("Forma de pagamento inválida. Selecione um cartão ou PIX.")
    return forma


def _nome_forma(forma: str, formas: list[dict[str, Any]]) -> str:
    if forma == FORMA_REEMBOLSO:
        return "Reembolso"
    return next((f["nome"] for f in formas if f["id"] == forma),
                f"Associar cartão · {forma}")


def _migrar_formas(conn: sqlite3.Connection) -> None:
    """Migração auditada, idempotente e conservadora das antigas formas fixas.

    Só a origem de um vínculo real, ou o filtro individual do cadastro,
    demonstra qual instrumento foi usado. Um rótulo bancário não demonstra
    cartão nem autoriza unir instrumentos da mesma instituição.
    """
    versao = 1
    if conn.execute("SELECT 1 FROM fixas_migracoes WHERE versao = ?", (versao,)).fetchone():
        return
    identidades = cartoes.identidades(conn)
    tx_contas = {l["transacao_id"]: l["conta_id"] for l in conn.execute(
        "SELECT transacao_id, conta_id FROM pluggy_transacoes")}

    def instrumento(fonte: str) -> str:
        return identidades.get(fonte, {}).get("cartaoId", "")

    vinculos: dict[str, set[str]] = {}
    for linha in conn.execute(
        "SELECT fixa_id, transacao_id FROM fixas_mes WHERE transacao_id IS NOT NULL"
    ):
        fonte = tx_contas.get(linha["transacao_id"], "")
        # Origem bancária também conta como evidência conflitante: não permite
        # converter todo o cadastro só porque outro mês foi pago no cartão.
        if fonte:
            vinculos.setdefault(linha["fixa_id"], set()).add(instrumento(fonte) or FORMA_PIX)

    for tabela in ("fixas_contas", "fixas_mes", "fixas_descontos"):
        for linha in conn.execute(f"SELECT rowid AS registro, * FROM {tabela}").fetchall():
            original = str(linha["forma_pagamento"] or "")
            nova, evidencia = "", ""
            if original in identidades:
                nova, evidencia = instrumento(original), "fonte individual cadastrada"
            elif original in _FORMAS_LEGADAS:
                if tabela == "fixas_contas":
                    fonte = str(linha["conta_id"] or "")
                    nova = instrumento(fonte)
                    evidencia = "filtro individual do cadastro" if nova else ""
                    candidatas = vinculos.get(linha["id"], set())
                    if not nova and len(candidatas) == 1 and FORMA_PIX not in candidatas:
                        nova = next(iter(candidatas))
                        evidencia = "todos os vínculos existentes apontam para o mesmo cartão"
                else:
                    fonte = tx_contas.get(linha["transacao_id"], "")
                    nova = instrumento(fonte)
                    evidencia = "transação vinculada" if nova else ""
            else:
                continue
            if nova:
                conn.execute(f"UPDATE {tabela} SET forma_pagamento = ? WHERE rowid = ?",
                             (nova, linha["registro"]))
            conn.execute(
                "INSERT OR IGNORE INTO fixas_formas_migradas "
                "(versao, tabela, registro_id, forma_original, forma_nova, evidencia) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (versao, tabela, str(linha["registro"]), original, nova or original,
                 evidencia or "Associação pendente: nenhum cartão individual inequívoco"))
    conn.execute("INSERT INTO fixas_migracoes (versao, aplicado_em) VALUES (?, ?)",
                 (versao, datetime.now().isoformat(timespec="seconds")))


# --------------------------------------------------------------------------
# Vinculo pela regra CONTEM
# --------------------------------------------------------------------------

def _candidatas_do_mes(conn: sqlite3.Connection, mes_ref: str) -> list[sqlite3.Row]:
    """Saídas do mês que podem pagar uma conta fixa.

    Só DEBIT e só incluída: uma transferência própria ou um estorno nunca é o
    pagamento de uma conta fixa.

    O mês é a COMPETÊNCIA, não a data da transação -- mesma convenção do resto
    do projeto (ver "Mês do cartão é o mês em que a fatura vence" no README).
    Para conta corrente as duas são iguais; no cartão, não: o seguro comprado
    em 12/08 entra na fatura de setembro, e é em setembro que ele é pago.

    Usar a data aqui criava um descasamento com o total da fatura, que sempre
    foi por competência: a conta casava em agosto (data) e era descontada da
    fatura de agosto, que não a continha; e em setembro -- a fatura que de
    fato a continha -- ela aparecia como "ainda não lançada" e era somada de
    novo. Ou seja, contava duas vezes.

    Só contas ativas, como no resto do projeto (ver _onde em
    pluggy_extrato.py): uma reconexão cria uma conta nova para o mesmo cartão
    e a antiga fica com as transações duplicadas. Sem este filtro o vínculo
    podia casar com a duplicata da conta substituída -- e como a competência
    da fatura dela é calculada a partir do último lançamento que ela tem, o
    mês saía deslocado.
    """
    ativas = sorted(px.contas_ativas(conn))
    marcadores = ", ".join("?" for _ in ativas) or "NULL"
    return conn.execute(
        f"""
        SELECT e.transacao_id, e.descricao, e.data, e.conta_id, e.status,
               e.categoria_original, e.parcela_numero, e.parcela_total,
               e.categoria_id, e.origem_categorizacao, ABS(e.valor) AS valor
        FROM extrato_efetivo_cache e
        WHERE e.competencia_fatura = ? AND e.tipo = 'DEBIT' AND e.incluida = 1
          AND e.conta_id IN ({marcadores})
        ORDER BY e.data
        """,
        (mes_ref, *ativas),
    ).fetchall()


def _projecoes_do_mes(mes_ref: str) -> dict[str, list[dict[str, Any]]]:
    """Parcelas que a fatura do mês vai cobrar, por cartão ou tag.

    Existe só para o mês corrente e os futuros -- em mês passado a fatura já
    fechou e o que vale são as transações reais. Fora dessa janela devolve
    vazio sem custo, porque cartoes_payload não é barato.
    """
    if mes_ref < datetime.now().strftime("%Y-%m"):
        return {}
    try:
        payload = px.cartoes_payload(int(mes_ref[:4]), "fatura")
    except Exception:
        return {}
    numero_mes = int(mes_ref[5:7])
    por_grupo: dict[str, list[dict[str, Any]]] = {}
    for cartao in payload["cartoes"]:
        grupo = cartao["id"]
        for item in payload["itens"].get(cartao["id"], []):
            if item["mes"] == numero_mes:
                por_grupo.setdefault(grupo, []).append(dict(item, grupoId=grupo))
    return por_grupo


def _casar_projecao(termos: list[str], projecoes: dict[str, list[dict[str, Any]]],
                    usadas: set[int], referencia: str = "") -> dict[str, Any] | None:
    """A parcela projetada que estes termos identificam, se houver.

    Serve para não contar o mesmo gasto duas vezes num mês futuro: a fatura
    projetada JÁ inclui a parcela, e a conta fixa somaria em cima dela. É o
    mesmo papel que a transação vinculada faz no mês fechado -- só que aqui a
    "transação" ainda não existe, então quem identifica é o termo.

    Só contas fixas entram nisso. Subdesconto tem descrição livre ("GYMPASS"),
    não uma regra de casamento -- forçar match ali seria adivinhação.
    """
    if not termos:
        return None
    for grupo, itens in projecoes.items():
        for item in itens:
            if id(item) in usadas:
                continue
            if referencia and referencia not in (
                grupo, item.get("cartaoId"), item.get("contaId")):
                continue
            descricao = cam.normalizar(item["descricao"])
            if any(t in descricao for t in termos):
                usadas.add(id(item))
                return {
                    "forma": grupo,
                    "grupoId": grupo,
                    "cartaoId": item.get("cartaoId", ""),
                    "banco": grupo,  # alias de leitura para versões anteriores da tela
                    "descricao": item["descricao"],
                    "valor": round(float(item["valor"]), 2),
                    "parcela": f"{item['parcelaAtual']}/{item['parcelaTotal']}",
                }
    return None


def _termo_de_regra(termo: str) -> str:
    """Termo normalizado, sem o contador de parcela.

    Vários lojistas colocam a parcela dentro da descrição ("GRUPO CASAS
    BAHIAR01/05"), e quem cadastra a conta fixa copia a descrição inteira --
    incluindo o contador. Aí a regra casa só com a primeira parcela e a conta
    aparece como não paga de lá em diante.

    Tirar o contador do TERMO basta: a descrição continua com o dela, e
    "grupo casas bahiar" é substring de "grupo casas bahiar02/05". Termo sem
    contador não muda de comportamento.
    """
    return re.sub(r"\s+", " ",
                  re.sub(r"\d+\s*/\s*\d+", " ", cam.normalizar(termo))).strip()


def _termos_por_fixa(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Termos crus de cada conta, na ordem em que foram cadastrados."""
    saida: dict[str, list[str]] = {}
    for linha in conn.execute(
        "SELECT fixa_id, termo FROM fixas_termos ORDER BY fixa_id, ordem"
    ):
        saida.setdefault(linha["fixa_id"], []).append(linha["termo"])
    return saida


def _termos_de_regra(termos: list[str]) -> list[str]:
    """Lista normalizada e sem vazios, pronta para o CONTÉM."""
    return [t for t in (_termo_de_regra(x) for x in termos or []) if t]


def _casar(fixas: list[sqlite3.Row], candidatas: list[sqlite3.Row],
           ja_vinculadas: set[str],
           termos_por_fixa: dict[str, list[str]] | None = None,
           fontes_por_referencia: dict[str, set[str]] | None = None) -> dict[str, sqlite3.Row]:
    """Aplica a regra CONTÉM de cada conta fixa sobre as transações do mês.

    A regra é só texto — é o termo que a pessoa escreveu que manda, não uma
    faixa de valor. Quando mais de uma conta disputa a mesma transação, ganha
    primeiro a que tem a mesma categoria do lançamento; depois, a de valor mais
    próximo do previsto. Isso evita uma regra de teste em outra categoria roubar
    o pagamento da conta correta.

    Uma transação já usada por outra fixa (ou vinculada à mão) sai do páreo:
    senão duas contas de valor parecido apontariam para o mesmo lançamento e o
    mês contaria pago duas vezes.
    """
    usadas = set(ja_vinculadas)
    resultado: dict[str, sqlite3.Row] = {}

    pares = []
    for fixa in fixas:
        # Qualquer um dos termos serve (OU), igual às regras do extrato: a
        # descrição do mesmo pagamento muda de mês para mês.
        brutos = (termos_por_fixa or {}).get(fixa["id"]) or [fixa["termo"]]
        termos = _termos_de_regra(brutos)
        if not termos:
            continue
        previsto = float(fixa["valor_previsto"] or 0)
        for tx in candidatas:
            descricao = cam.normalizar(tx["descricao"])
            if not any(t in descricao for t in termos):
                continue
            referencia = fixa["conta_id"]
            permitidas = (fontes_por_referencia or {}).get(referencia, {referencia})
            if referencia and tx["conta_id"] not in permitidas:
                continue
            distancia = abs(float(tx["valor"] or 0) - previsto) if previsto > 0 else 0.0
            categoria_fixa = fixa["categoria_id"] or ""
            categoria_tx = tx["categoria_id"] or ""
            prioridade_categoria = (
                0 if categoria_fixa and categoria_fixa == categoria_tx
                else 1 if not categoria_fixa
                else 2
            )
            pares.append((prioridade_categoria, distancia, fixa["id"], tx))

    for _, _, fixa_id, tx in sorted(pares, key=lambda p: (p[0], p[1], p[2])):
        if fixa_id in resultado or tx["transacao_id"] in usadas:
            continue
        resultado[fixa_id] = tx
        usadas.add(tx["transacao_id"])
    return resultado


def _detalhe_transacao(tx: sqlite3.Row, apelidos: dict[str, str],
                       categorias: dict[str, sqlite3.Row],
                       identidades: dict[str, dict] | None = None) -> dict[str, Any]:
    """Tudo que vale mostrar no painel de detalhe da transação vinculada."""
    cat = categorias.get(tx["categoria_id"] or "")
    parcela = ""
    if tx["parcela_total"]:
        parcela = f"{tx['parcela_numero'] or 1}/{tx['parcela_total']}"
    identidade = (identidades or {}).get(tx["conta_id"], {})
    return {
        "id": tx["transacao_id"],
        "descricao": tx["descricao"],
        "data": tx["data"][:10],
        "valor": float(tx["valor"]),
        "conta": apelidos.get(tx["conta_id"], "Conta"),
        "contaId": tx["conta_id"],
        "cartaoId": identidade.get("cartaoId", ""),
        "grupoId": identidade.get("grupo", ""),
        "cartaoOriginal": identidade.get("nomeOriginal", ""),
        "status": tx["status"] or "",
        "categoria": cat["nome"] if cat else "",
        "categoriaCor": cat["cor"] if cat else "",
        "origemCategoria": tx["origem_categorizacao"] or "",
        "categoriaOriginal": tx["categoria_original"] or "",
        "parcela": parcela,
    }


# --------------------------------------------------------------------------
# Leitura do mes
# --------------------------------------------------------------------------

def mes_payload(mes_ref: str) -> dict[str, Any]:
    with _abrir() as conn:
        # Comparação de texto funciona porque mes_ref é sempre "AAAA-MM".
        # Uma conta não existe antes do mês em que começou nem depois do fim.
        fixas = conn.execute(
            "SELECT f.*, c.nome AS cat_nome, c.cor AS cat_cor "
            "FROM fixas_contas f "
            "LEFT JOIN extrato_categorias c ON c.id = f.categoria_id "
            "WHERE f.ativo = 1 "
            "  AND (f.desde = '' OR f.desde <= ?) "
            "  AND (f.ate = '' OR f.ate >= ?) "
            "ORDER BY f.ordem, f.nome",
            (mes_ref, mes_ref),
        ).fetchall()

        do_mes = {
            l["fixa_id"]: l
            for l in conn.execute("SELECT * FROM fixas_mes WHERE mes_ref = ?", (mes_ref,))
        }

        formas_por_conta = _formas_por_conta(conn)
        identidades = cartoes.identidades(conn)
        fontes_por_referencia = {
            referencia: set(cartoes.resolver_contas(conn, referencia)) or {referencia}
            for referencia in {f["conta_id"] for f in fixas if f["conta_id"]}
        }
        linhas_desconto = conn.execute(
            "SELECT * FROM fixas_descontos WHERE mes_ref = ? ORDER BY descricao",
            (mes_ref,),
        ).fetchall()

        candidatas = _candidatas_do_mes(conn, mes_ref)
        por_id = {t["transacao_id"]: t for t in candidatas}

        # Carregados aqui, antes dos descontos, porque o detalhe da transação
        # vinculada a um subdesconto também precisa deles.
        formas = _formas(conn)
        formas_pendentes = {f["id"] for f in formas if f.get("pendenteAssociacao")}
        apelidos = px.mapa_apelidos(conn, px.contas_ativas(conn))
        categorias = {l["id"]: l for l in conn.execute("SELECT * FROM extrato_categorias")}

        descontos: dict[str, list[dict]] = {}
        for l in linhas_desconto:
            tx_desc = por_id.get(l["transacao_id"]) if l["transacao_id"] else None
            referencia = l["forma_pagamento"] or (FORMA_REEMBOLSO if l["reembolso"] else FORMA_PIX)
            referencia = identidades.get(referencia, {}).get("cartaoId", referencia)
            forma = _normalizar_forma(
                l["forma_pagamento"] or
                (FORMA_REEMBOLSO if l["reembolso"] else FORMA_PIX),
                formas_por_conta,
            )
            # Uma referência legada ambígua pode ser resolvida para ESTE mês
            # pelo próprio vínculo, sem alterar o padrão dos demais meses.
            if forma in formas_pendentes and tx_desc is not None:
                forma = formas_por_conta.get(tx_desc["conta_id"], forma)
                referencia = identidades.get(tx_desc["conta_id"], {}).get("cartaoId", referencia)
            descontos.setdefault(l["fixa_id"], []).append({
                "id": l["id"],
                "descricao": l["descricao"],
                "valor": float(l["valor"] or 0),
                "forma": forma,
                "formaReferencia": referencia,
                "formaNome": _nome_forma(forma, formas),
                "pendenteAssociacao": forma in formas_pendentes,
                "reembolso": forma == FORMA_REEMBOLSO,
                # Vínculo próprio: é o que diz se ESTA cobrança já está na
                # fatura. Herdar do pai errava sempre que o pai não casava.
                "transacaoId": l["transacao_id"] or "",
                "transacao": _detalhe_transacao(tx_desc, apelidos, categorias, identidades)
                             if tx_desc is not None else None,
                # Vinculado mas fora do mês: mantém o id sem fingir que achou.
                "vinculoPerdido": bool(l["transacao_id"]) and tx_desc is None,
                "projecao": None,   # preenchido depois, fora do "with"
            })

        # Vínculo que este subdesconto teve em MESES ANTERIORES. Serve para
        # reconhecer a parcela projetada do mês corrente: a projeção carrega o
        # id da compra que a originou, então "mesma compra" é comparação de
        # id, não adivinhação por descrição.
        vinculo_anterior: dict[tuple[str, str], str] = {}
        for l in conn.execute(
            "SELECT fixa_id, descricao, transacao_id, mes_ref FROM fixas_descontos "
            "WHERE transacao_id IS NOT NULL AND mes_ref < ? ORDER BY mes_ref",
            (mes_ref,),
        ):
            vinculo_anterior[(l["fixa_id"], cam.normalizar(l["descricao"]))] = l["transacao_id"]

        # Transação usada por um subdesconto sai do páreo das contas fixas:
        # senão a mesma cobrança pagaria a conta e o desconto dela.
        vinculos_manuais = {l["transacao_id"] for l in do_mes.values() if l["transacao_id"]}
        vinculos_manuais |= {
            l["transacao_id"] for l in linhas_desconto if l["transacao_id"]
        }
        # Só as que ainda não têm vínculo manual entram na regra CONTÉM.
        sem_vinculo = [
            f for f in fixas
            if not (f["id"] in do_mes and do_mes[f["id"]]["transacao_id"])
        ]
        termos_por_fixa = _termos_por_fixa(conn)
        automaticos = _casar(sem_vinculo, candidatas, vinculos_manuais,
                             termos_por_fixa, fontes_por_referencia)

    # Fora do "with": não usa o banco, e cartoes_payload abre a conexão dele.
    # Só o que não casou com transação real disputa a projeção.
    projecoes = _projecoes_do_mes(mes_ref)
    projecao_usada: set[int] = set()
    projecao_por_fixa: dict[str, dict[str, Any]] = {}
    if projecoes:
        for f in fixas:
            linha = do_mes.get(f["id"])
            tem_tx = bool(
                (linha and linha["transacao_id"] and por_id.get(linha["transacao_id"]))
                or automaticos.get(f["id"])
            )
            if tem_tx:
                continue
            referencia = ((linha["forma_pagamento"] if linha else "")
                          or f["forma_pagamento"] or f["conta_id"])
            # Legados ainda ambíguos não autorizam escolher uma projeção pelo
            # nome do banco. A associação deve ser feita na edição da conta.
            if referencia in formas_pendentes:
                continue
            if referencia in identidades:
                referencia = identidades[referencia]["cartaoId"]
            achado = _casar_projecao(
                _termos_de_regra(termos_por_fixa.get(f["id"]) or [f["termo"]]),
                projecoes, projecao_usada, referencia)
            if achado:
                projecao_por_fixa[f["id"]] = achado

        # Subdesconto não tem termo (a descrição é rótulo livre), então o que
        # o identifica é a COMPRA a que ele foi vinculado antes: se a parcela
        # projetada deste mês veio da mesma compra, é o mesmo dinheiro.
        #
        # Compara compraId, não transacaoBaseId: o "base" é a última parcela
        # cobrada e muda todo mês, então o casamento duraria um mês só. A
        # identidade da compra não muda -- ver chave_compra em pluggy_extrato.
        por_compra = {
            item["compraId"]: (grupo, item)
            for grupo, itens in projecoes.items() for item in itens
        }
        if por_compra:
            with _abrir() as conn2:
                for fixa_id, lista in descontos.items():
                    for desconto in lista:
                        if desconto["transacao"] or desconto["forma"] == FORMA_REEMBOLSO:
                            continue
                        tx_antiga = vinculo_anterior.get(
                            (fixa_id, cam.normalizar(desconto["descricao"])))
                        if not tx_antiga:
                            continue
                        compra = px.compra_da_transacao(conn2, tx_antiga)
                        achado = por_compra.get(
                            "|".join(str(p) for p in compra)) if compra else None
                        if not achado or id(achado[1]) in projecao_usada:
                            continue
                        grupo, item = achado
                        projecao_usada.add(id(item))
                        desconto["projecao"] = {
                            "forma": grupo,
                            "grupoId": grupo,
                            "cartaoId": item.get("cartaoId", ""),
                            "banco": grupo,
                            "descricao": item["descricao"],
                            "valor": round(float(item["valor"]), 2),
                            "parcela": f"{item['parcelaAtual']}/{item['parcelaTotal']}",
                        }

    itens = []
    for f in fixas:
        linha = do_mes.get(f["id"])
        manual_tx = linha["transacao_id"] if linha else None
        tx = por_id.get(manual_tx) if manual_tx else automaticos.get(f["id"])
        origem_tx = "manual" if manual_tx and tx else ("regra" if tx else None)

        # Vínculo manual apontando para transação fora do mês/filtro: mantém o
        # id para não perder a informação, mas não finge que achou.
        if manual_tx and not tx:
            origem_tx = "manual_perdido"

        override = linha["pago_override"] if linha else None
        valor_mes = linha["valor"] if linha else None

        valor = (
            float(valor_mes) if valor_mes is not None
            else float(tx["valor"]) if tx is not None
            else float(f["valor_previsto"] or 0)
        )
        origem_valor = ("manual" if valor_mes is not None
                        else "transacao" if tx is not None else "previsto")

        pago = bool(override) if override is not None else tx is not None
        origem_pago = ("manual" if override is not None
                       else "transacao" if tx is not None else "pendente")

        # Forma: escolha do mês > conta da transação > padrão do cadastro > PIX.
        forma_mes = (linha["forma_pagamento"] if linha else "") or FORMA_AUTO
        if forma_mes:
            forma, origem_forma = _normalizar_forma(forma_mes, formas_por_conta), "manual"
            referencia = forma_mes
            if forma in formas_pendentes and tx is not None:
                forma = formas_por_conta.get(tx["conta_id"], forma)
                referencia = identidades.get(tx["conta_id"], {}).get("cartaoId", referencia)
        elif tx is not None:
            forma = formas_por_conta.get(tx["conta_id"], FORMA_PIX)
            referencia = identidades.get(tx["conta_id"], {}).get("cartaoId", FORMA_PIX)
            origem_forma = "transacao"
        elif f["forma_pagamento"]:
            forma, origem_forma = _normalizar_forma(
                f["forma_pagamento"], formas_por_conta), "cadastro"
            referencia = f["forma_pagamento"]
        else:
            forma, origem_forma = FORMA_PIX, "padrao"
            referencia = FORMA_PIX
        referencia = identidades.get(referencia, {}).get("cartaoId", referencia)

        desc = descontos.get(f["id"], [])
        total_desc = sum(d["valor"] for d in desc)
        reembolsos = sum(d["valor"] for d in desc if d["reembolso"])
        liquido = max(0.0, valor - total_desc)
        gasto = liquido + (total_desc - reembolsos)
        incluir_calculos = bool(f["incluir_calculos"])

        itens.append({
            "id": f["id"],
            "nome": f["nome"],
            "tag": f["tag"],
            "valorPrevisto": float(f["valor_previsto"] or 0),
            "diaVencimento": f["dia_vencimento"],
            "termo": f["termo"],
            "termos": termos_por_fixa.get(f["id"]) or ([f["termo"]] if f["termo"] else []),
            "contaId": f["conta_id"],
            "desde": f["desde"],
            "ate": f["ate"],
            "formaPadrao": (_normalizar_forma(f["forma_pagamento"], formas_por_conta)
                             if f["forma_pagamento"] else ""),
            "formaPadraoReferencia": f["forma_pagamento"] or "",
            "incluidaCalculos": incluir_calculos,
            "categoria": {"id": f["categoria_id"], "nome": f["cat_nome"],
                          "cor": f["cat_cor"]} if f["categoria_id"] else None,
            "observacao": linha["observacao"] if linha else "",

            "valor": valor,
            "origemValor": origem_valor,
            "pago": pago,
            "origemPago": origem_pago,
            "forma": forma,
            "formaReferencia": referencia,
            "formaNome": _nome_forma(forma, formas),
            "pendenteAssociacao": forma in formas_pendentes,
            "origemForma": origem_forma,

            "transacao": _detalhe_transacao(tx, apelidos, categorias, identidades) if tx is not None else None,
            "origemTransacao": origem_tx,
            # Preenchido quando a fatura projetada do mês já inclui esta
            # cobrança: a tela desconta em vez de somar como previsto.
            "projecao": projecao_por_fixa.get(f["id"]),

            "descontos": desc,
            "totalDescontos": total_desc,
            "reembolsos": reembolsos,
            # Portadas do dashboard manual: ver docstring do módulo. O gasto é
            # somado a partir do líquido, e não calculado como
            # "valor − reembolsos": as duas contas dão o mesmo resultado, menos
            # quando os descontos passam do valor — aí só esta trava em zero,
            # como o dashboard manual faz.
            "liquido": liquido,
            "gasto": gasto,
            "gastoCalculo": gasto if incluir_calculos else 0.0,
        })

    # Contas Pessoais primeiro, Contas Empresa depois; dentro de cada grupo,
    # da conta de maior valor líquido para a menor. Substitui a ordem antiga
    # (f.ordem, f.nome), que era só a ordem de cadastro.
    itens.sort(key=lambda i: (i["tag"] != "Contas Pessoais", -i["liquido"]))

    def soma(chave: str, filtro=lambda i: True) -> float:
        return sum(i[chave] for i in itens if filtro(i))

    return {
        "mesRef": mes_ref,
        "itens": itens,
        "resumo": {
            "quantidade": len(itens),
            "gasto": soma("gastoCalculo"),
            "gastoInformativo": soma("gasto"),
            "liquido": soma("liquido", lambda i: i["incluidaCalculos"]),
            "pago": soma("gastoCalculo", lambda i: i["pago"]),
            "pendente": soma("gastoCalculo", lambda i: not i["pago"]),
            "descontos": soma("totalDescontos"),
            "reembolsos": soma("reembolsos"),
            "conciliadas": sum(1 for i in itens if i["transacao"]),
            "associacoesPendentes": sum(
                int(i["pendenteAssociacao"]) + sum(int(d["pendenteAssociacao"]) for d in i["descontos"])
                for i in itens),
            "porTag": {tag: soma("gasto", lambda i, t=tag: i["tag"] == t) for tag in TAGS},
        },
        "tags": TAGS,
        "formas": formas,
    }


def candidatas_payload(mes_ref: str, busca: str = "",
                       como_regra: bool = False) -> dict[str, Any]:
    """Transações do mês que casam com um termo.

    Serve tanto para o seletor de vínculo manual quanto para a prévia da regra
    CONTÉM na hora de cadastrar — é a mesma pergunta ("o que este texto pega?")
    feita em dois lugares.

    como_regra=True aplica a mesma normalização do motor de vínculo (ignora o
    contador de parcela). Só a prévia usa isso: no seletor a busca é texto
    livre, e ali procurar "3/12" tem que procurar "3/12" mesmo.
    """
    with _abrir() as conn:
        linhas = _candidatas_do_mes(conn, mes_ref)
        # Inclui os subdescontos: uma transação já usada por um deles não
        # deve parecer livre para virar vínculo de outra coisa.
        usadas = {
            l["transacao_id"]
            for l in conn.execute(
                "SELECT transacao_id FROM fixas_mes "
                "WHERE mes_ref = ? AND transacao_id IS NOT NULL", (mes_ref,))
        } | {
            l["transacao_id"]
            for l in conn.execute(
                "SELECT transacao_id FROM fixas_descontos "
                "WHERE mes_ref = ? AND transacao_id IS NOT NULL", (mes_ref,))
        }
        apelidos = px.mapa_apelidos(conn, px.contas_ativas(conn))
        identidades = cartoes.identidades(conn)
        categorias = {l["id"]: l for l in conn.execute("SELECT * FROM extrato_categorias")}

    termo = _termo_de_regra(busca) if como_regra else cam.normalizar(busca)
    encontradas = [t for t in linhas if not termo or termo in cam.normalizar(t["descricao"])]
    return {
        "total": len(encontradas),
        "transacoes": [
            dict(_detalhe_transacao(t, apelidos, categorias, identidades),
                 jaVinculada=t["transacao_id"] in usadas)
            for t in encontradas[:80]
        ],
    }


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------

def _validar(dados: dict) -> dict:
    nome = str(dados.get("nome") or "").strip()
    if not nome:
        raise ValueError("Informe o nome da conta.")

    tag = str(dados.get("tag") or TAGS[0])
    if tag not in TAGS:
        raise ValueError(f"Tag inválida. Use {' ou '.join(TAGS)}.")

    try:
        valor = max(0.0, float(dados.get("valorPrevisto") or 0))
    except (TypeError, ValueError):
        raise ValueError("Valor previsto inválido.")

    dia = dados.get("diaVencimento")
    try:
        dia = int(dia) if dia not in (None, "") else None
    except (TypeError, ValueError):
        dia = None
    if dia is not None and not 1 <= dia <= 31:
        raise ValueError("Dia de vencimento deve estar entre 1 e 31.")

    def mes(chave: str) -> str:
        v = str(dados.get(chave) or "").strip()
        if v and not re.fullmatch(r"\d{4}-\d{2}", v):
            raise ValueError(f"{chave} deve estar no formato AAAA-MM.")
        return v

    forma = str(dados.get("forma") or "").strip()

    # Aceita a lista nova e o campo antigo, para uma chamada velha (ou o
    # importador) continuar funcionando sem mudanca.
    brutos = dados.get("termos")
    if not isinstance(brutos, list):
        brutos = [dados.get("termo") or ""]
    termos, vistos = [], set()
    for item in brutos:
        texto = str(item or "").strip()
        chave = cam.normalizar(texto)
        if not texto or chave in vistos:
            continue
        vistos.add(chave)
        termos.append(texto[:200])
    if len(termos) > 20:
        raise ValueError("No máximo 20 termos por conta fixa.")

    return {
        "nome": nome,
        "tag": tag,
        "valor_previsto": valor,
        "dia_vencimento": dia,
        "categoria_id": dados.get("categoriaId") or None,
        # `termos` e a lista; `termo` continua sendo o primeiro dela, porque
        # varios payloads e o importador ainda leem a coluna antiga.
        "termos": termos,
        "termo": termos[0] if termos else "",
        "conta_id": str(dados.get("contaId") or "").strip(),
        "ativo": 0 if dados.get("ativo") is False else 1,
        "desde": mes("desde"),
        "ate": mes("ate"),
        "forma_pagamento": forma,
        "incluir_calculos": 0 if dados.get("incluirCalculos") is False else 1,
    }


def _gravar_termos(conn: sqlite3.Connection, fixa_id: str,
                   termos: list[str]) -> None:
    """Reescreve a lista inteira: e mais simples que diferenciar, e a lista
    tem no maximo algumas unidades."""
    conn.execute("DELETE FROM fixas_termos WHERE fixa_id = ?", (fixa_id,))
    conn.executemany(
        "INSERT INTO fixas_termos (fixa_id, ordem, termo) VALUES (?, ?, ?)",
        [(fixa_id, ordem, termo) for ordem, termo in enumerate(termos)])


def criar(dados: dict) -> dict:
    d = _validar(dados)
    novo = f"fx_{uuid.uuid4().hex[:12]}"
    with _abrir() as conn:
        d["forma_pagamento"] = _forma_para_gravar(conn, d["forma_pagamento"])
        ordem = conn.execute(
            "SELECT COALESCE(MAX(ordem), 0) + 1 FROM fixas_contas").fetchone()[0]
        # Sem "desde" explícito, a conta passa a existir no mês em que foi
        # criada — não retroage por todo o histórico.
        desde = d["desde"] or datetime.now().strftime("%Y-%m")
        conn.execute(
            "INSERT INTO fixas_contas (id, nome, tag, valor_previsto, dia_vencimento, "
            "  categoria_id, termo, conta_id, ativo, ordem, criado_em, "
            "  desde, ate, forma_pagamento, incluir_calculos) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (novo, d["nome"], d["tag"], d["valor_previsto"], d["dia_vencimento"],
             d["categoria_id"], d["termo"], d["conta_id"], d["ativo"], ordem,
             datetime.now().isoformat(timespec="seconds"), desde, d["ate"],
             d["forma_pagamento"], d["incluir_calculos"]),
        )
        _gravar_termos(conn, novo, d["termos"])
        conn.commit()
    return {"ok": True, "id": novo}


def atualizar(fixa_id: str, dados: dict) -> dict:
    d = _validar(dados)
    with _abrir() as conn:
        d["forma_pagamento"] = _forma_para_gravar(conn, d["forma_pagamento"])
        if not conn.execute("SELECT 1 FROM fixas_contas WHERE id = ?",
                            (fixa_id,)).fetchone():
            raise ValueError("Conta fixa não encontrada.")
        conn.execute(
            "UPDATE fixas_contas SET nome = ?, tag = ?, valor_previsto = ?, "
            "  dia_vencimento = ?, categoria_id = ?, termo = ?, conta_id = ?, "
            "  ativo = ?, desde = ?, ate = ?, forma_pagamento = ?, "
            "  incluir_calculos = ? WHERE id = ?",
            (d["nome"], d["tag"], d["valor_previsto"], d["dia_vencimento"],
             d["categoria_id"], d["termo"], d["conta_id"], d["ativo"],
             d["desde"], d["ate"], d["forma_pagamento"],
             d["incluir_calculos"], fixa_id),
        )
        _gravar_termos(conn, fixa_id, d["termos"])
        conn.commit()
    return {"ok": True, "id": fixa_id}


def historico_payload(fixa_id: str) -> dict[str, Any]:
    """Todas as datas em que esta conta fixa foi paga, mês a mês.

    Sai da transação vinculada, não de um registro de "pago" -- é a data em
    que o dinheiro saiu de verdade. Usa a mesma regra do mês (vínculo manual
    tem prioridade, senão a regra CONTÉM), aplicada mês por mês, para o
    histórico não contar uma coisa e a tela do mês outra.

    Mês em que a conta existia e nada casou aparece como não pago: a lacuna
    é informação (esqueci de pagar? o termo parou de casar?), esconder ela
    faria o histórico parecer completo quando não está.
    """
    with _abrir() as conn:
        fixa = conn.execute(
            "SELECT * FROM fixas_contas WHERE id = ?", (fixa_id,)
        ).fetchone()
        if not fixa:
            raise ValueError(f"Conta fixa {fixa_id} não encontrada.")
        termos_da_fixa = _termos_por_fixa(conn).get(fixa_id) or [fixa["termo"]]

        # Janela: da vigência da conta, limitada ao que existe de extrato.
        limites = conn.execute(
            "SELECT MIN(mes_ref) AS min_mes, MAX(mes_ref) AS max_mes "
            "FROM extrato_efetivo_cache"
        ).fetchone()
        primeiro = max(fixa["desde"] or "", limites["min_mes"] or "")
        ultimo = min(fixa["ate"] or "9999-12", limites["max_mes"] or "0000-01")
        if not primeiro or primeiro > ultimo:
            return {"fixa": {"id": fixa["id"], "nome": fixa["nome"],
                             "termo": fixa["termo"],
                             "termos": termos_da_fixa,
                             "valorPrevisto": float(fixa["valor_previsto"] or 0)},
                    "pagamentos": [], "resumo": {}}

        manuais = {
            l["mes_ref"]: l["transacao_id"]
            for l in conn.execute(
                "SELECT mes_ref, transacao_id FROM fixas_mes "
                "WHERE fixa_id = ? AND transacao_id IS NOT NULL", (fixa_id,))
        }
        apelidos = px.mapa_apelidos(conn, px.contas_ativas(conn))
        categorias = {l["id"]: l for l in conn.execute("SELECT * FROM extrato_categorias")}
        identidades = cartoes.identidades(conn)
        fontes_por_referencia = {
            fixa["conta_id"]: set(cartoes.resolver_contas(conn, fixa["conta_id"])) or {fixa["conta_id"]}
        } if fixa["conta_id"] else {}

        pagamentos = []
        termos_da_fixa = _termos_por_fixa(conn).get(fixa_id) or [fixa["termo"]]
        for mes_ref in _meses_entre(primeiro, ultimo):
            candidatas = _candidatas_do_mes(conn, mes_ref)
            por_id = {t["transacao_id"]: t for t in candidatas}
            tx = por_id.get(manuais.get(mes_ref, ""))
            origem = "manual" if tx is not None else None
            if tx is None:
                # Mesma regra do mês, mas só para esta conta: as outras contas
                # não disputam aqui porque a pergunta é "o que casou com ESTA".
                achado = _casar([fixa], candidatas, set(),
                                {fixa_id: termos_da_fixa}, fontes_por_referencia)
                tx = achado.get(fixa_id)
                origem = "regra" if tx is not None else None
            pagamentos.append({
                "mes": mes_ref,
                "pago": tx is not None,
                "origem": origem,
                "transacao": _detalhe_transacao(tx, apelidos, categorias, identidades)
                             if tx is not None else None,
            })

    pagos = [p for p in pagamentos if p["pago"]]
    valores = [p["transacao"]["valor"] for p in pagos]
    return {
        "fixa": {"id": fixa["id"], "nome": fixa["nome"], "termo": fixa["termo"],
                 "termos": termos_da_fixa,
                 "valorPrevisto": float(fixa["valor_previsto"] or 0)},
        "pagamentos": list(reversed(pagamentos)),   # mais recente primeiro
        "resumo": {
            "meses": len(pagamentos),
            "pagos": len(pagos),
            "semPagamento": len(pagamentos) - len(pagos),
            "total": round(sum(valores), 2),
            "media": round(sum(valores) / len(valores), 2) if valores else 0.0,
            "menor": round(min(valores), 2) if valores else 0.0,
            "maior": round(max(valores), 2) if valores else 0.0,
        },
    }


def _meses_entre(de: str, ate: str) -> list[str]:
    serial_de = int(de[:4]) * 12 + int(de[5:7]) - 1
    serial_ate = int(ate[:4]) * 12 + int(ate[5:7]) - 1
    return [f"{s // 12:04d}-{s % 12 + 1:02d}"
            for s in range(serial_de, serial_ate + 1)]


def remover(fixa_id: str) -> dict:
    """Remove o cadastro e o histórico dele (fixas_mes e descontos, via cascade).

    Diferente das categorias, aqui a cascata é o comportamento desejado: o
    histórico mensal de uma conta fixa não faz sentido sem a conta.
    """
    with _abrir() as conn:
        if not conn.execute("SELECT 1 FROM fixas_contas WHERE id = ?",
                            (fixa_id,)).fetchone():
            raise ValueError("Conta fixa não encontrada.")
        # O PRAGMA vive por conexão, e sem ele o ON DELETE CASCADE do schema é
        # decorativo: as linhas de fixas_mes e fixas_descontos ficariam órfãs.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM fixas_descontos WHERE fixa_id = ?", (fixa_id,))
        conn.execute("DELETE FROM fixas_mes WHERE fixa_id = ?", (fixa_id,))
        conn.execute("DELETE FROM fixas_contas WHERE id = ?", (fixa_id,))
        conn.commit()
    return {"ok": True, "id": fixa_id}


def clonar_mes(origem: str, destino: str) -> dict:
    """Clona um mês vazio sem carregar pagamento ou vínculo de transação.

    Os novos cadastros existem somente no mês de destino. Valores ajustados,
    formas de pagamento e subdescontos são preservados; pagamento e vínculo
    voltam ao automático para não inventar conciliações no novo mês.
    """
    if not re.fullmatch(r"\d{4}-\d{2}", origem or ""):
        raise ValueError("Mês de origem inválido.")
    if not re.fullmatch(r"\d{4}-\d{2}", destino or ""):
        raise ValueError("Mês de destino inválido.")
    if origem == destino:
        raise ValueError("Origem e destino devem ser meses diferentes.")

    with _abrir() as conn:
        existentes = conn.execute(
            "SELECT COUNT(*) FROM fixas_contas WHERE ativo = 1 "
            "AND (desde = '' OR desde <= ?) AND (ate = '' OR ate >= ?)",
            (destino, destino),
        ).fetchone()[0]
        if existentes:
            raise ValueError("O mês de destino já possui contas fixas.")

        contas = conn.execute(
            "SELECT * FROM fixas_contas WHERE ativo = 1 "
            "AND (desde = '' OR desde <= ?) AND (ate = '' OR ate >= ?) "
            "ORDER BY ordem, nome",
            (origem, origem),
        ).fetchall()
        if not contas:
            raise ValueError("O mês anterior também não possui contas fixas.")

        ajustes = {
            linha["fixa_id"]: linha
            for linha in conn.execute("SELECT * FROM fixas_mes WHERE mes_ref = ?", (origem,))
        }
        descontos = {}
        for linha in conn.execute(
            "SELECT * FROM fixas_descontos WHERE mes_ref = ? ORDER BY descricao", (origem,)
        ):
            descontos.setdefault(linha["fixa_id"], []).append(linha)

        proxima_ordem = conn.execute(
            "SELECT COALESCE(MAX(ordem), 0) + 1 FROM fixas_contas"
        ).fetchone()[0]
        for indice, conta in enumerate(contas):
            ajuste = ajustes.get(conta["id"])
            valor = (ajuste["valor"] if ajuste and ajuste["valor"] is not None
                     else conta["valor_previsto"])
            forma = (ajuste["forma_pagamento"] if ajuste and ajuste["forma_pagamento"]
                     else conta["forma_pagamento"])
            nova_id = f"fx_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO fixas_contas (id, nome, tag, valor_previsto, dia_vencimento, "
                "categoria_id, termo, conta_id, tolerancia, ativo, ordem, criado_em, "
                "desde, ate, forma_pagamento, incluir_calculos) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                (nova_id, conta["nome"], conta["tag"], valor, conta["dia_vencimento"],
                 conta["categoria_id"], conta["termo"], conta["conta_id"],
                 conta["tolerancia"], proxima_ordem + indice,
                 datetime.now().isoformat(timespec="seconds"), destino, destino, forma,
                 conta["incluir_calculos"]),
            )
            for desconto in descontos.get(conta["id"], []):
                conn.execute(
                    "INSERT INTO fixas_descontos (id, fixa_id, mes_ref, descricao, valor, "
                    "reembolso, forma_pagamento) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"dc_{uuid.uuid4().hex[:10]}", nova_id, destino,
                     desconto["descricao"], desconto["valor"], desconto["reembolso"],
                     desconto["forma_pagamento"]),
                )
        conn.commit()
    return {"ok": True, "origem": origem, "destino": destino, "quantidade": len(contas)}


def ajustar_mes(mes_ref: str, fixa_id: str, dados: dict) -> dict:
    """Grava o que é específico daquele mês.

    Campos ausentes ficam como estão; enviar null volta ao automático.
    """
    with _abrir() as conn:
        if not conn.execute("SELECT 1 FROM fixas_contas WHERE id = ?",
                            (fixa_id,)).fetchone():
            raise ValueError("Conta fixa não encontrada.")

        atual = conn.execute(
            "SELECT * FROM fixas_mes WHERE mes_ref = ? AND fixa_id = ?",
            (mes_ref, fixa_id)).fetchone()

        def escolher(chave: str, coluna: str):
            if chave not in dados:
                return atual[coluna] if atual else None
            return dados[chave]

        valor = escolher("valor", "valor")
        if valor in ("", "auto"):
            valor = None
        if valor is not None:
            try:
                valor = float(valor)
            except (TypeError, ValueError):
                raise ValueError("Valor inválido.")

        pago = escolher("pago", "pago_override")
        if pago in ("", "auto"):
            pago = None
        if pago is not None:
            pago = 1 if pago in (True, 1, "1", "true") else 0

        transacao = escolher("transacaoId", "transacao_id")
        if transacao in ("", "auto"):
            transacao = None
        if transacao is not None and not conn.execute(
            "SELECT 1 FROM pluggy_transacoes WHERE transacao_id = ?", (transacao,)
        ).fetchone():
            raise ValueError("Transação não encontrada.")

        forma = str(escolher("forma", "forma_pagamento") or "")
        forma = _forma_para_gravar(conn, forma)
        observacao = str(escolher("observacao", "observacao") or "")

        vazio = (valor is None and pago is None and not transacao
                 and not forma and not observacao)
        if vazio:
            conn.execute("DELETE FROM fixas_mes WHERE mes_ref = ? AND fixa_id = ?",
                         (mes_ref, fixa_id))
        else:
            conn.execute(
                "INSERT INTO fixas_mes (mes_ref, fixa_id, valor, pago_override, "
                "  transacao_id, observacao, forma_pagamento) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mes_ref, fixa_id) DO UPDATE SET valor = excluded.valor, "
                "  pago_override = excluded.pago_override, "
                "  transacao_id = excluded.transacao_id, "
                "  observacao = excluded.observacao, "
                "  forma_pagamento = excluded.forma_pagamento",
                (mes_ref, fixa_id, valor, pago, transacao, observacao, forma),
            )
        conn.commit()
    return {"ok": True, "mesRef": mes_ref, "id": fixa_id}


def salvar_desconto(mes_ref: str, fixa_id: str, dados: dict) -> dict:
    descricao = str(dados.get("descricao") or "").strip()
    if not descricao:
        raise ValueError("Informe a descrição do desconto.")
    try:
        valor = max(0.0, float(dados.get("valor") or 0))
    except (TypeError, ValueError):
        raise ValueError("Valor do desconto inválido.")

    forma = str(dados.get("forma") or FORMA_PIX).strip() or FORMA_PIX
    # A transação vinculada é o que diz se esta cobrança já entrou na fatura.
    # "" limpa o vínculo; ausente preserva o que já estava (a tela manda só o
    # que o usuário mexeu).
    tem_tx = "transacaoId" in dados
    transacao_id = str(dados.get("transacaoId") or "").strip() or None

    desconto_id = str(dados.get("id") or "") or f"dc_{uuid.uuid4().hex[:10]}"
    with _abrir() as conn:
        forma = _forma_para_gravar(conn, forma, reembolso=True)
        if not conn.execute("SELECT 1 FROM fixas_contas WHERE id = ?",
                            (fixa_id,)).fetchone():
            raise ValueError("Conta fixa não encontrada.")
        if transacao_id and not conn.execute(
            "SELECT 1 FROM pluggy_transacoes WHERE transacao_id = ?",
            (transacao_id,),
        ).fetchone():
            raise ValueError("Transação não encontrada.")
        conn.execute(
            "INSERT INTO fixas_descontos (id, fixa_id, mes_ref, descricao, valor, "
            "  reembolso, forma_pagamento, transacao_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET descricao = excluded.descricao, "
            "  valor = excluded.valor, reembolso = excluded.reembolso, "
            "  forma_pagamento = excluded.forma_pagamento"
            + (", transacao_id = excluded.transacao_id" if tem_tx else ""),
            (desconto_id, fixa_id, mes_ref, descricao, valor,
             1 if forma == FORMA_REEMBOLSO else 0, forma, transacao_id),
        )
        conn.commit()
    return {"ok": True, "id": desconto_id}


def remover_desconto(desconto_id: str) -> dict:
    with _abrir() as conn:
        conn.execute("DELETE FROM fixas_descontos WHERE id = ?", (desconto_id,))
        conn.commit()
    return {"ok": True, "id": desconto_id}
