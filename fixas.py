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
import extrato_camada as cam
import pluggy_extrato as px

TAGS = ["Contas Pessoais", "Contas Empresa"]

# Formas de pagamento. Uma forma é ou um id de conta/cartão da Pluggy, ou um
# destes dois sentinelas — que nunca colidem com um id da Pluggy (uuid).
FORMA_PIX = "pix"
FORMA_REEMBOLSO = "__reembolso__"
FORMAS_BANCOS = {
    "itau": "Itaú",
    "nubank": "Nubank",
    "inter": "Inter",
}

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

CREATE INDEX IF NOT EXISTS idx_fixas_mes ON fixas_mes (mes_ref);
CREATE INDEX IF NOT EXISTS idx_fixas_desc ON fixas_descontos (mes_ref, fixa_id);
"""

# Colunas acrescentadas depois da primeira versão. ALTER TABLE ADD COLUMN é a
# única migração que o SQLite faz barato, e serve para todas elas.
_COLUNAS_NOVAS = {
    "fixas_contas": {"desde": "TEXT NOT NULL DEFAULT ''",
                     "ate": "TEXT NOT NULL DEFAULT ''",
                     "forma_pagamento": "TEXT NOT NULL DEFAULT ''",
                     "incluir_calculos": "INTEGER NOT NULL DEFAULT 1"},
    "fixas_mes": {"forma_pagamento": "TEXT NOT NULL DEFAULT ''"},
    "fixas_descontos": {"forma_pagamento": "TEXT NOT NULL DEFAULT ''"},
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
        # Descontos antigos marcados como reembolso ganham a forma equivalente.
        conn.execute(
            "UPDATE fixas_descontos SET forma_pagamento = ? "
            "WHERE reembolso = 1 AND forma_pagamento = ''", (FORMA_REEMBOLSO,))
        conn.commit()
        _pronto = True
    return conn


# --------------------------------------------------------------------------
# Formas de pagamento
# --------------------------------------------------------------------------

def _formas(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """As quatro formas consolidadas usadas no planejamento mensal.

    Conta corrente e cartões do mesmo banco não são formas diferentes para
    esta tela. O detalhe da transação continua mostrando a conta real.
    """
    return [{"id": FORMA_PIX, "nome": "PIX"}] + [
        {"id": chave, "nome": nome} for chave, nome in FORMAS_BANCOS.items()
    ]


def _bancos_por_conta(conn: sqlite3.Connection) -> dict[str, str]:
    """Relaciona cada conta/cartão Pluggy à instituição consolidada."""
    linhas = conn.execute(
        "SELECT conta_id, item_id, tipo, nome, raw_json FROM pluggy_contas"
    ).fetchall()
    textos_item: dict[str, str] = {}
    for linha in linhas:
        textos_item[linha["item_id"]] = (
            textos_item.get(linha["item_id"], "") + " " +
            f"{linha['nome']} {linha['raw_json']}"
        ).lower()

    resultado = {}
    for linha in linhas:
        # Nesta tela os bancos representam cartões. Uma transação que saiu
        # da conta corrente do Itaú/Inter/Nubank (PIX ou boleto) continua PIX;
        # só compras vindas de uma conta CREDIT herdam o nome do banco.
        if linha["tipo"] != "CREDIT":
            continue
        texto = textos_item.get(linha["item_id"], "")
        if "itau" in texto or "itaú" in texto:
            resultado[linha["conta_id"]] = "itau"
        elif "nubank" in texto or "nu pagamentos" in texto:
            resultado[linha["conta_id"]] = "nubank"
        elif "inter" in texto:
            resultado[linha["conta_id"]] = "inter"
    return resultado


def _normalizar_forma(forma: str, bancos_por_conta: dict[str, str]) -> str:
    """Converte UUIDs antigos/automáticos para uma das formas consolidadas."""
    if forma in (FORMA_PIX, FORMA_REEMBOLSO, *FORMAS_BANCOS):
        return forma
    return bancos_por_conta.get(forma, FORMA_PIX)


def _nome_forma(forma: str, formas: list[dict[str, str]]) -> str:
    if forma == FORMA_REEMBOLSO:
        return "Reembolso"
    for f in formas:
        if f["id"] == forma:
            return f["nome"]
    return "PIX"


# --------------------------------------------------------------------------
# Vinculo pela regra CONTEM
# --------------------------------------------------------------------------

def _candidatas_do_mes(conn: sqlite3.Connection, mes_ref: str) -> list[sqlite3.Row]:
    """Saídas do mês que podem pagar uma conta fixa.

    Só DEBIT e só incluída: uma transferência própria ou um estorno nunca é o
    pagamento de uma conta fixa.
    """
    return conn.execute(
        """
        SELECT e.transacao_id, e.descricao, e.data, e.conta_id, e.status,
               e.categoria_original, e.parcela_numero, e.parcela_total,
               e.categoria_id, e.origem_categorizacao, ABS(e.valor) AS valor
        FROM extrato_efetivo_cache e
        WHERE e.mes_ref = ? AND e.tipo = 'DEBIT' AND e.incluida = 1
        ORDER BY e.data
        """,
        (mes_ref,),
    ).fetchall()


def _casar(fixas: list[sqlite3.Row], candidatas: list[sqlite3.Row],
           ja_vinculadas: set[str]) -> dict[str, sqlite3.Row]:
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
        termo = cam.normalizar(fixa["termo"])
        if not termo:
            continue
        previsto = float(fixa["valor_previsto"] or 0)
        for tx in candidatas:
            if termo not in cam.normalizar(tx["descricao"]):
                continue
            if fixa["conta_id"] and tx["conta_id"] != fixa["conta_id"]:
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
                       categorias: dict[str, sqlite3.Row]) -> dict[str, Any]:
    """Tudo que vale mostrar no painel de detalhe da transação vinculada."""
    cat = categorias.get(tx["categoria_id"] or "")
    parcela = ""
    if tx["parcela_total"]:
        parcela = f"{tx['parcela_numero'] or 1}/{tx['parcela_total']}"
    return {
        "id": tx["transacao_id"],
        "descricao": tx["descricao"],
        "data": tx["data"][:10],
        "valor": float(tx["valor"]),
        "conta": apelidos.get(tx["conta_id"], "Conta"),
        "contaId": tx["conta_id"],
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

        descontos: dict[str, list[dict]] = {}
        bancos_por_conta = _bancos_por_conta(conn)
        for l in conn.execute(
            "SELECT * FROM fixas_descontos WHERE mes_ref = ? ORDER BY descricao", (mes_ref,)
        ):
            forma = _normalizar_forma(
                l["forma_pagamento"] or
                (FORMA_REEMBOLSO if l["reembolso"] else FORMA_PIX),
                bancos_por_conta,
            )
            descontos.setdefault(l["fixa_id"], []).append({
                "id": l["id"],
                "descricao": l["descricao"],
                "valor": float(l["valor"] or 0),
                "forma": forma,
                "reembolso": forma == FORMA_REEMBOLSO,
            })

        candidatas = _candidatas_do_mes(conn, mes_ref)
        por_id = {t["transacao_id"]: t for t in candidatas}

        vinculos_manuais = {l["transacao_id"] for l in do_mes.values() if l["transacao_id"]}
        # Só as que ainda não têm vínculo manual entram na regra CONTÉM.
        sem_vinculo = [
            f for f in fixas
            if not (f["id"] in do_mes and do_mes[f["id"]]["transacao_id"])
        ]
        automaticos = _casar(sem_vinculo, candidatas, vinculos_manuais)

        formas = _formas(conn)
        apelidos = px.mapa_apelidos(conn, px.contas_ativas(conn))
        categorias = {l["id"]: l for l in conn.execute("SELECT * FROM extrato_categorias")}

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
            forma, origem_forma = _normalizar_forma(forma_mes, bancos_por_conta), "manual"
        elif tx is not None and tx["conta_id"] in bancos_por_conta:
            forma, origem_forma = bancos_por_conta[tx["conta_id"]], "transacao"
        elif f["forma_pagamento"]:
            forma, origem_forma = _normalizar_forma(
                f["forma_pagamento"], bancos_por_conta), "cadastro"
        else:
            forma, origem_forma = FORMA_PIX, "padrao"

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
            "contaId": f["conta_id"],
            "desde": f["desde"],
            "ate": f["ate"],
            "formaPadrao": (_normalizar_forma(f["forma_pagamento"], bancos_por_conta)
                             if f["forma_pagamento"] else ""),
            "incluidaCalculos": incluir_calculos,
            "categoria": {"id": f["categoria_id"], "nome": f["cat_nome"],
                          "cor": f["cat_cor"]} if f["categoria_id"] else None,
            "observacao": linha["observacao"] if linha else "",

            "valor": valor,
            "origemValor": origem_valor,
            "pago": pago,
            "origemPago": origem_pago,
            "forma": forma,
            "formaNome": _nome_forma(forma, formas),
            "origemForma": origem_forma,

            "transacao": _detalhe_transacao(tx, apelidos, categorias) if tx is not None else None,
            "origemTransacao": origem_tx,

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
            "porTag": {tag: soma("gasto", lambda i, t=tag: i["tag"] == t) for tag in TAGS},
        },
        "tags": TAGS,
        "formas": formas,
    }


def candidatas_payload(mes_ref: str, busca: str = "") -> dict[str, Any]:
    """Transações do mês que casam com um termo.

    Serve tanto para o seletor de vínculo manual quanto para a prévia da regra
    CONTÉM na hora de cadastrar — é a mesma pergunta ("o que este texto pega?")
    feita em dois lugares.
    """
    with _abrir() as conn:
        linhas = _candidatas_do_mes(conn, mes_ref)
        usadas = {
            l["transacao_id"]
            for l in conn.execute(
                "SELECT transacao_id FROM fixas_mes "
                "WHERE mes_ref = ? AND transacao_id IS NOT NULL", (mes_ref,))
        }
        apelidos = px.mapa_apelidos(conn, px.contas_ativas(conn))
        categorias = {l["id"]: l for l in conn.execute("SELECT * FROM extrato_categorias")}

    termo = cam.normalizar(busca)
    encontradas = [t for t in linhas if not termo or termo in cam.normalizar(t["descricao"])]
    return {
        "total": len(encontradas),
        "transacoes": [
            dict(_detalhe_transacao(t, apelidos, categorias),
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
    if forma and forma not in (FORMA_PIX, *FORMAS_BANCOS):
        raise ValueError("Forma de pagamento inválida.")

    return {
        "nome": nome,
        "tag": tag,
        "valor_previsto": valor,
        "dia_vencimento": dia,
        "categoria_id": dados.get("categoriaId") or None,
        "termo": str(dados.get("termo") or "").strip(),
        "conta_id": str(dados.get("contaId") or "").strip(),
        "ativo": 0 if dados.get("ativo") is False else 1,
        "desde": mes("desde"),
        "ate": mes("ate"),
        "forma_pagamento": forma,
        "incluir_calculos": 0 if dados.get("incluirCalculos") is False else 1,
    }


def criar(dados: dict) -> dict:
    d = _validar(dados)
    novo = f"fx_{uuid.uuid4().hex[:12]}"
    with _abrir() as conn:
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
        conn.commit()
    return {"ok": True, "id": novo}


def atualizar(fixa_id: str, dados: dict) -> dict:
    d = _validar(dados)
    with _abrir() as conn:
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
        conn.commit()
    return {"ok": True, "id": fixa_id}


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
        if forma and forma not in (FORMA_PIX, *FORMAS_BANCOS):
            raise ValueError("Forma de pagamento inválida.")
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
    if forma not in (FORMA_PIX, FORMA_REEMBOLSO, *FORMAS_BANCOS):
        raise ValueError("Forma de pagamento inválida.")
    desconto_id = str(dados.get("id") or "") or f"dc_{uuid.uuid4().hex[:10]}"
    with _abrir() as conn:
        if not conn.execute("SELECT 1 FROM fixas_contas WHERE id = ?",
                            (fixa_id,)).fetchone():
            raise ValueError("Conta fixa não encontrada.")
        conn.execute(
            "INSERT INTO fixas_descontos (id, fixa_id, mes_ref, descricao, valor, "
            "  reembolso, forma_pagamento) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET descricao = excluded.descricao, "
            "  valor = excluded.valor, reembolso = excluded.reembolso, "
            "  forma_pagamento = excluded.forma_pagamento",
            (desconto_id, fixa_id, mes_ref, descricao, valor,
             1 if forma == FORMA_REEMBOLSO else 0, forma),
        )
        conn.commit()
    return {"ok": True, "id": desconto_id}


def remover_desconto(desconto_id: str) -> dict:
    with _abrir() as conn:
        conn.execute("DELETE FROM fixas_descontos WHERE id = ?", (desconto_id,))
        conn.commit()
    return {"ok": True, "id": desconto_id}
