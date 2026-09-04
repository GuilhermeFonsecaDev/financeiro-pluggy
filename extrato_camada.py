"""Camada local sobre as transacoes da Pluggy.

A transacao importada nunca e alterada. Tudo que o usuario decide -- categoria,
regras, exclusao dos calculos -- vive em tabelas proprias com prefixo
"extrato_". Assim uma nova sincronizacao nunca apaga trabalho manual.

Categoria efetiva, em ordem de precedencia:
    edicao manual > regra do usuario > traducao da categoria Pluggy > Outros

Participacao nos calculos, em ordem de precedencia:
    decisao manual > exclusao automatica por regra > incluida

calculo_override tem tres estados: NULL (automatico), 0 (fora), 1 (forcar
inclusao). Por isso uma transferencia propria sai dos calculos sozinha mas pode
ser reincluida na mao.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import unicodedata

import banco as fin
import ciclos

CATEGORIA_PADRAO = "outros"


# --------------------------------------------------------------------------
# Normalizacao usada nas comparacoes de regra (sem caixa, sem acento)
# --------------------------------------------------------------------------

def normalizar(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return " ".join(texto.casefold().split())


def conectar() -> sqlite3.Connection:
    """Conexao do projeto + a funcao norm() usada pela view extrato_efetivo."""
    conn = fin.connect()
    conn.create_function("norm", 1, normalizar, deterministic=True)
    return conn


# --------------------------------------------------------------------------
# Categorias canonicas
# --------------------------------------------------------------------------
# As oito primeiras usam, sem alterar, os hexes da paleta categorica de
# referencia do skill de dataviz (coluna dark), cuja validacao esta documentada
# para a superficie escura: pior par adjacente CVD dE 8.4 e visao normal 19.3.
# Ficam com as categorias de despesa que mais aparecem no grafico de barras.
#
# Da nona em diante sao passos das mesmas familias de cor. Elas sao estaveis e
# distintas, mas NAO passam pelo mesmo gate de daltonismo -- por isso o chip
# sempre mostra o nome da categoria junto da cor: identidade nunca depende so
# da cor.
#   id, nome, cor, pai_id (None = e uma categoria pai)
#
# Os ids nunca mudam: extrato_ajustes.categoria_id_manual aponta para eles, e
# renomear um id apagaria edicao manual do usuario. A hierarquia foi montada por
# cima dos ids que ja existiam; so os pais que faltavam foram criados.
#
# Nenhuma cor muito escura: tom escuro sobre fundo escuro vira mancha, some no
# chip pequeno e nao alcanca contraste. Subcategoria usa um passo mais claro da
# familia do pai, para a leitura ficar obvia.
CATEGORIAS: list[tuple[str, str, str, str | None, str]] = [
    # id, nome, cor, pai_id, emoji
    # --- pais ---------------------------------------------------------------
    ("alimentacao", "Alimentação", "#e8794a", None, ""),
    ("transporte", "Transporte", "#2bb98a", None, ""),
    ("automotivo", "Automotivo", "#f0883e", None, ""),
    ("compras", "Compras", "#d9a520", None, ""),
    ("saude", "Saúde", "#e66767", None, ""),
    ("servicos", "Serviços", "#3fb98a", None, ""),
    ("servicos_digitais", "Serviços digitais", "#d55181", None, ""),
    ("moradia", "Moradia", "#4a95ea", None, ""),
    ("seguros", "Seguros", "#e0a63a", None, ""),
    ("lazer", "Lazer", "#9085e9", None, ""),
    ("impostos", "Impostos e taxas", "#c9a227", None, ""),
    ("emprestimos", "Empréstimos e financiamentos", "#c47f56", None, ""),
    ("transferencias", "Transferências", "#7aa5c4", None, ""),
    ("investimentos", "Investimentos", "#2fa36b", None, ""),
    ("receitas", "Receitas", "#4fcf7d", None, ""),
    (CATEGORIA_PADRAO, "Outros", "#9ba1ab", None, ""),

    # --- subcategorias ------------------------------------------------------
    ("supermercado", "Supermercado", "#f09468", "alimentacao", "🛒"),
    ("restaurantes", "Restaurantes", "#f5ab86", "alimentacao", "🍴"),
    ("delivery", "Delivery", "#f8c4ab", "alimentacao", "🛵"),

    ("combustivel", "Postos de combustível", "#f5a56b", "automotivo", "⛽"),
    ("veiculo", "Manutenção de veículo", "#f8c39c", "automotivo", "🔧"),

    ("vestuario", "Roupas", "#e5b843", "compras", "👕"),
    ("pets", "Pet", "#eecb70", "compras", "🐾"),
    ("educacao", "Livraria e educação", "#f4dc9c", "compras", "📚"),

    ("farmacia", "Farmácia", "#ee8585", "saude", "💊"),
    ("academia", "Academia", "#f4a5a5", "saude", "🏋️"),

    ("telecom", "Telecomunicações", "#6dc9a5", "servicos", "📶"),

    ("assinaturas", "Streaming e apps", "#e07ba1", "servicos_digitais", "🎬"),

    ("viagem", "Viagem", "#aba2ef", "lazer", "✈️"),

    ("tarifas", "Tarifas bancárias", "#d9b95c", "impostos", "🏦"),

    ("transferencia_propria", "Transferência própria", "#9dbdd4", "transferencias", "👤"),
    ("fatura", "Pagamento de cartão de crédito", "#7d8fa6", "transferencias", "💳"),

    ("rendimentos", "Rendimentos", "#5cc294", "investimentos", "💹"),

    ("renda", "Renda", "#7ddba0", "receitas", "💵"),
    ("cashback", "Cashback", "#a3e6bd", "receitas", "🎁"),
]

# Sugestões no seletor de emoji da tela de categorias.
EMOJIS_SUGERIDOS = [
    "🍽️", "🛒", "🍴", "🛵", "☕", "🍺", "🚌", "🚗", "⛽", "🔧", "🅿️", "🚕",
    "🛍️", "👕", "👟", "🐾", "📚", "🎓", "🩺", "💊", "🏋️", "🧘", "🧰", "📶",
    "💻", "🎬", "🎮", "🎵", "🏠", "💡", "💧", "🛡️", "🎉", "✈️", "🏖️", "🎁",
    "🧾", "🏦", "🏛️", "🔁", "👤", "💳", "📈", "💹", "💰", "💵", "📌", "⭐",
]

# Traducao das categorias que a Pluggy manda hoje. Categoria nova e desconhecida
# cai em Outros automaticamente (a resolucao usa COALESCE ate o padrao).
MAPEAMENTO: dict[str, str] = {
    "Groceries": "supermercado",
    "Eating out": "restaurantes",
    "Food and drinks": "restaurantes",
    "Food delivery": "delivery",
    "Taxi and ride-hailing": "transporte",
    "Transportation": "transporte",
    "Parking": "transporte",
    "Tolls and in vehicle payment": "transporte",
    "Bicycle": "transporte",
    "Gas stations": "combustivel",
    "Vehicle maintenance": "veiculo",
    "Automotive": "veiculo",
    "Vehicle ownership taxes and fees": "veiculo",
    "Shopping": "compras",
    "Online shopping": "compras",
    "Electronics": "compras",
    "Houseware": "compras",
    "Sports goods": "compras",
    "Clothing": "vestuario",
    "Healthcare": "saude",
    "Optometry": "saude",
    "Wellness": "saude",
    "Wellness and fitness": "saude",
    "Pharmacy": "farmacia",
    "Gyms and fitness centers": "academia",
    "Sports practice": "academia",
    "Digital services": "assinaturas",
    "Video streaming": "assinaturas",
    "Gaming": "assinaturas",
    "Telecommunications": "telecom",
    "Internet": "telecom",
    "Mobile": "telecom",
    "Services": "servicos",
    "Insurance": "seguros",
    "Education": "educacao",
    "School": "educacao",
    "Bookstore": "educacao",
    "Pet supplies and vet": "pets",
    "Leisure": "lazer",
    "Cinema, theater and concerts": "lazer",
    "Tickets": "lazer",
    "Gambling": "lazer",
    "Travel": "viagem",
    "Airport and airlines": "viagem",
    "Accomodation": "viagem",
    "Mileage programs": "viagem",
    "Housing": "moradia",
    "Water": "moradia",
    "Taxes": "impostos",
    "Income taxes": "impostos",
    "Tax on financial operations": "impostos",
    "Bank fees": "tarifas",
    "Credit card fees": "tarifas",
    "Late payment and overdraft costs": "tarifas",
    "Interests charged": "tarifas",
    "Loans": "emprestimos",
    "Loans and financing": "emprestimos",
    "Credit card payment": "fatura",
    "Transfers": "transferencias",
    "Transfer - PIX": "transferencias",
    "Transfer - Bank Slip": "transferencias",
    "Transfer - Internal": "transferencias",
    "Third party transfer - PIX": "transferencias",
    "Third party transfer - Debit Card": "transferencias",
    "Same person transfer": "transferencia_propria",
    "Same person transfer - CASH": "transferencia_propria",
    "Investments": "investimentos",
    "Fixed income": "investimentos",
    "Mutual funds": "investimentos",
    "Proceeds interests and dividends": "rendimentos",
    "Income": "renda",
    "Non-recurring income": "renda",
    "Entrepreneurial activities": "renda",
    "Cashback": "cashback",
}

# Regras criadas pelo sistema. Prioridade menor = aplicada primeiro.
# Aparecem no gerenciador, mas são obrigatórias: não podem ser desativadas
# nem excluídas.
REGRAS_SISTEMA: list[dict] = [
    {
        "id": "sys_same_person_transfer",
        "nome": "Transferência entre contas próprias",
        "campo": "categoria_original",
        "operador": "contem",
        "termo": "Same Person Transfer",
        "categoria_id": "transferencia_propria",
        "ignorar_calculos": 1,
        "prioridade": 10,
    },
    {
        "id": "sys_resgate_cofrinhos",
        "nome": "Resgate de cofrinhos",
        "campo": "descricao",
        "operador": "contem",
        "termo": "RESGATE COFRINHOS",
        "categoria_id": "transferencia_propria",
        "ignorar_calculos": 1,
        "prioridade": 11,
    },
    {
        "id": "sys_pagamento_fatura_cartao",
        "nome": "Pagamento de cartão de crédito",
        "campo": "descricao",
        "operador": "contem",
        "termo": "FATURA",
        "categoria_id": "fatura",
        "ignorar_calculos": 1,
        "prioridade": 12,
    },
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS extrato_categorias (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  cor TEXT NOT NULL,
  ativo INTEGER NOT NULL DEFAULT 1,
  ordem INTEGER NOT NULL DEFAULT 0,
  pai_id TEXT REFERENCES extrato_categorias (id),
  emoji TEXT NOT NULL DEFAULT '',
  -- 1 = o usuario mexeu nesta categoria. O seed roda a cada requisicao e
  -- atualiza nome/cor/emoji das categorias padrao; sem esta marca, toda
  -- personalizacao seria desfeita na chamada seguinte.
  personalizada INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS extrato_mapeamento_categorias (
  categoria_original TEXT PRIMARY KEY,
  categoria_id TEXT NOT NULL REFERENCES extrato_categorias (id)
);

CREATE TABLE IF NOT EXISTS extrato_ajustes (
  transacao_id TEXT PRIMARY KEY,
  categoria_id_manual TEXT REFERENCES extrato_categorias (id),
  calculo_override INTEGER,
  data_manual TEXT,
  descricao_manual TEXT,
  valor_manual REAL,
  tipo_manual TEXT,
  status_manual TEXT,
  conta_id_manual TEXT,
  parcela_numero_manual INTEGER,
  parcela_total_manual INTEGER,
  atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extrato_regras (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  campo TEXT NOT NULL DEFAULT 'descricao',
  operador TEXT NOT NULL DEFAULT 'contem',
  termo TEXT NOT NULL,
  valor_min REAL,
  valor_max REAL,
  categoria_id TEXT REFERENCES extrato_categorias (id),
  descricao_nova TEXT,
  ignorar_calculos INTEGER NOT NULL DEFAULT 0,
  prioridade INTEGER NOT NULL DEFAULT 100,
  ativo INTEGER NOT NULL DEFAULT 1,
  sistema INTEGER NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extrato_regras_ordem
  ON extrato_regras (ativo, prioridade, id);

CREATE TABLE IF NOT EXISTS extrato_regra_termos (
  regra_id TEXT NOT NULL REFERENCES extrato_regras (id) ON DELETE CASCADE,
  ordem INTEGER NOT NULL,
  termo TEXT NOT NULL,
  PRIMARY KEY (regra_id, ordem)
);

CREATE INDEX IF NOT EXISTS idx_extrato_regra_termos_regra
  ON extrato_regra_termos (regra_id, ordem);

CREATE TABLE IF NOT EXISTS extrato_regras_entradas (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  operador TEXT NOT NULL DEFAULT 'contem',
  termo TEXT NOT NULL,
  dia_inicio INTEGER NOT NULL DEFAULT 1,
  dia_fim INTEGER NOT NULL DEFAULT 31,
  deslocamento_meses INTEGER NOT NULL DEFAULT 0,
  ativo INTEGER NOT NULL DEFAULT 1,
  criado_em TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extrato_regras_entradas_ativas
  ON extrato_regras_entradas (ativo, id);

CREATE TABLE IF NOT EXISTS extrato_entradas_exclusoes (
  transacao_id TEXT PRIMARY KEY,
  criado_em TEXT NOT NULL,
  FOREIGN KEY (transacao_id) REFERENCES pluggy_transacoes (transacao_id) ON DELETE CASCADE
);
"""


# Casamento de uma regra contra uma transacao. Depende de norm() estar
# registrada na conexao -- ver conectar(). Renomeacoes sao resolvidas primeiro;
# as demais acoes podem, assim, casar contra o nome resultante.
_CAMPO_ORIGINAL = (
    "CASE r.campo WHEN 'categoria_original' THEN norm(t.categoria) "
    "             ELSE norm(COALESCE(a.descricao_manual, t.descricao)) END"
)
_FAIXA_VALOR = """
  AND (r.valor_min IS NULL OR ABS(COALESCE(a.valor_manual, t.valor)) >= r.valor_min)
  AND (r.valor_max IS NULL OR ABS(COALESCE(a.valor_manual, t.valor)) <= r.valor_max)
"""
_TERMO_CASA_ORIGINAL = f"""
  EXISTS (
    SELECT 1 FROM extrato_regra_termos rt
     WHERE rt.regra_id = r.id AND
       CASE r.operador
         WHEN 'igual'      THEN {_CAMPO_ORIGINAL} = norm(rt.termo)
         WHEN 'comeca_com' THEN {_CAMPO_ORIGINAL} LIKE norm(rt.termo) || '%'
         ELSE                   {_CAMPO_ORIGINAL} LIKE '%' || norm(rt.termo) || '%'
       END
  )
"""
_CASA_RENOMEAR = f"""
  r.ativo = 1 AND {_TERMO_CASA_ORIGINAL}{_FAIXA_VALOR}
"""
_DESCRICAO_EFETIVA = f"""
  COALESCE(
    a.descricao_manual,
    (SELECT r.descricao_nova FROM extrato_regras r
      WHERE {_CASA_RENOMEAR} AND r.descricao_nova IS NOT NULL
      ORDER BY r.prioridade, r.id LIMIT 1),
    t.descricao
  )
"""
_CAMPO = (
    "CASE r.campo WHEN 'categoria_original' THEN norm(t.categoria) "
    # "de.valor" e a MESMA coisa que _DESCRICAO_EFETIVA, ja calculada uma vez
    # so no JOIN da view (ver "de" no FROM). Inlinear a subquery aqui de novo
    # e o que fazia o rebuild da view demorar segundos: _CASA usa _CAMPO
    # dentro de um EXISTS avaliado por regra, entao cada uma das 4 vezes que
    # a view usa _CASA reavaliava _DESCRICAO_EFETIVA (que por sua vez varre
    # TODAS as regras de novo) uma vez por regra candidata -- quadratico em
    # cima de quadratico, so por reaproveitar a expressao ao inves do valor.
    "             ELSE norm(de.valor) END"
)
_TERMO_CASA_EFETIVO = f"""
  EXISTS (
    SELECT 1 FROM extrato_regra_termos rt
     WHERE rt.regra_id = r.id AND
       CASE r.operador
         WHEN 'igual'      THEN {_CAMPO} = norm(rt.termo)
         WHEN 'comeca_com' THEN {_CAMPO} LIKE norm(rt.termo) || '%'
         ELSE                   {_CAMPO} LIKE '%' || norm(rt.termo) || '%'
       END
  )
"""
_CASA = f"""
  r.ativo = 1 AND (
    {_TERMO_CASA_EFETIVO}
    OR
    (
      r.descricao_nova IS NOT NULL AND {_TERMO_CASA_ORIGINAL}
    )
  ){_FAIXA_VALOR}
"""

_ENTRADA_CASA = """
  e.ativo = 1 AND (
    CASE e.operador
      WHEN 'igual'      THEN norm(COALESCE(a.descricao_manual, t.descricao)) = norm(e.termo)
      WHEN 'comeca_com' THEN norm(COALESCE(a.descricao_manual, t.descricao)) LIKE norm(e.termo) || '%'
      ELSE                   norm(COALESCE(a.descricao_manual, t.descricao)) LIKE '%' || norm(e.termo) || '%'
    END
  ) AND CAST(SUBSTR(COALESCE(a.data_manual, t.data), 9, 2) AS INTEGER)
        BETWEEN e.dia_inicio AND e.dia_fim
"""

# Pagamento da fatura chega como CREDIT no extrato do cartao. Ele quita a
# fatura, nao e gasto dela nem compra que pertenca a um ciclo -- por isso fica
# fora tanto do total quanto da conta de qual fatura fecha quando.
EH_PAGAMENTO_FATURA = (
    "(t.tipo = 'CREDIT' AND ("
    "  t.descricao LIKE 'PAGAMENTO%'"
    "  OR t.descricao LIKE 'PGTO%'"
    "  OR t.descricao LIKE 'PAGTO%'"
    "  OR t.descricao LIKE '%PAGAMENTO DE FATURA%'"
    "))"
)

# Mes de VENCIMENTO da fatura em que a transacao caiu.
#
# Uma compra de agosto entra na fatura que vence em setembro: por data, o mes
# de setembro aparecia vazio mesmo tendo fatura para pagar.
#
# A regra inteira vive em ciclos.py, numa unica definicao usada tambem pela
# tela de Cartoes -- duas implementacoes da mesma pergunta era como a lista de
# lancamentos de um mes acabava discordando do total daquele mes.
#
# Fora do cartao a data ja e a competencia certa, entao cai no mes_ref.
_COMPETENCIA_FATURA = f"""
  CASE
    WHEN (SELECT c.subtipo FROM pluggy_contas c
           WHERE c.conta_id = COALESCE(a.conta_id_manual, t.conta_id))
         = 'CREDIT_CARD'
         AND NOT {EH_PAGAMENTO_FATURA}
    THEN {ciclos.expressao_competencia("t", "a")}
    ELSE SUBSTR(COALESCE(a.data_manual, t.data), 1, 7)
  END
"""

_COMPETENCIA_ENTRADA = f"""
  (SELECT STRFTIME('%Y-%m', DATE(
      SUBSTR(COALESCE(a.data_manual, t.data), 1, 7) || '-01',
      PRINTF('%+d month', e.deslocamento_meses)
    ))
   FROM extrato_regras_entradas e
   WHERE {_ENTRADA_CASA}
   ORDER BY e.criado_em, e.id LIMIT 1)
"""

# A view resolve categoria efetiva e participacao nos calculos para cada
# transacao. Tudo que le extrato passa por aqui, para nao existirem duas
# implementacoes da mesma regra.
#
# Categoria e exclusao sao decididas por regras SEPARADAS de proposito: uma
# regra pode so excluir dos calculos sem mexer na categoria, e uma regra de
# categoria de prioridade alta nao deve cancelar uma exclusao de prioridade
# baixa.
VIEW = f"""
DROP VIEW IF EXISTS extrato_efetivo;
CREATE VIEW extrato_efetivo AS
SELECT
  t.transacao_id,
  COALESCE(a.conta_id_manual, t.conta_id) AS conta_id,
  COALESCE(a.data_manual, t.data) AS data,
  SUBSTR(COALESCE(a.data_manual, t.data), 1, 7) AS mes_ref,
  CAST(SUBSTR(COALESCE(a.data_manual, t.data), 1, 4) AS INTEGER) AS ano,
  CAST(SUBSTR(COALESCE(a.data_manual, t.data), 6, 2) AS INTEGER) AS mes,
  de.valor AS descricao,
  COALESCE(a.valor_manual, t.valor) AS valor,
  t.moeda,
  COALESCE(a.tipo_manual, t.tipo) AS tipo,
  COALESCE(a.status_manual, t.status) AS status,
  t.categoria            AS categoria_original,
  COALESCE(a.parcela_numero_manual, t.parcela_numero) AS parcela_numero,
  COALESCE(a.parcela_total_manual, t.parcela_total) AS parcela_total,
  t.fatura_id,
  t.ordem,

  COALESCE(
    a.categoria_id_manual,
    (SELECT r.categoria_id FROM extrato_regras r WHERE r.id = rc.regra_categoria),
    m.categoria_id,
    '{CATEGORIA_PADRAO}'
  ) AS categoria_id,

  CASE
    WHEN a.categoria_id_manual IS NOT NULL THEN 'manual'
    WHEN rc.regra_categoria IS NOT NULL THEN 'regra'
    WHEN m.categoria_id IS NOT NULL THEN 'pluggy'
    ELSE 'padrao'
  END AS origem_categorizacao,

  CASE
    WHEN a.calculo_override IS NOT NULL THEN a.calculo_override
    WHEN rc.regra_ignorar IS NOT NULL THEN 0
    ELSE 1
  END AS incluida,

  CASE
    WHEN a.calculo_override = 0 THEN 'Excluída manualmente'
    WHEN a.calculo_override = 1 THEN NULL
    ELSE (SELECT r.nome FROM extrato_regras r WHERE r.id = rc.regra_ignorar)
  END AS motivo_exclusao
  ,
  CASE WHEN COALESCE(a.tipo_manual, t.tipo) = 'CREDIT'
       AND de.competencia_entrada IS NOT NULL THEN 1 ELSE 0
  END AS entrada_considerada,

  CASE WHEN COALESCE(a.tipo_manual, t.tipo) = 'CREDIT'
       THEN de.competencia_entrada ELSE NULL
  END AS competencia_entrada,

  {_COMPETENCIA_FATURA} AS competencia_fatura,

  CASE WHEN a.transacao_id IS NOT NULL THEN 1 ELSE 0 END AS editada_manualmente

FROM pluggy_transacoes t
LEFT JOIN extrato_ajustes a ON a.transacao_id = t.transacao_id
LEFT JOIN extrato_mapeamento_categorias m
       ON norm(m.categoria_original) = norm(t.categoria)
-- Descricao efetiva e competencia de entrada calculadas uma unica vez por
-- transacao aqui, e reusadas via "de.valor"/"de.competencia_entrada" em todo
-- lugar que precisar delas (ver _CAMPO). Sem este JOIN, a descricao era
-- recalculada a cada regra candidata dentro de _CASA (usado 4 vezes na
-- view), e a competencia de entrada saia duplicada em duas colunas -- o
-- rebuild do cache caiu de ~4s pra fracao de segundo depois destas mudancas.
LEFT JOIN (
  SELECT t.transacao_id AS tx_id, {_DESCRICAO_EFETIVA} AS valor,
         {_COMPETENCIA_ENTRADA} AS competencia_entrada
  FROM pluggy_transacoes t
  LEFT JOIN extrato_ajustes a ON a.transacao_id = t.transacao_id
) de ON de.tx_id = t.transacao_id
-- Mesma ideia do JOIN "de": categoria_id e origem_categorizacao faziam a
-- MESMA busca de regra (so pra saber o id numa vez e checar se achou algo na
-- outra); incluida e motivo_exclusao repetiam o mesmo par pra "ignorar
-- calculos". Eram 4 buscas iguais 2 a 2 -- aqui cada uma roda so uma vez.
LEFT JOIN (
  SELECT t.transacao_id AS tx_id,
    (SELECT r.id FROM extrato_regras r
      WHERE {_CASA} AND r.categoria_id IS NOT NULL
      ORDER BY r.prioridade, r.id LIMIT 1) AS regra_categoria,
    (SELECT r.id FROM extrato_regras r
      WHERE {_CASA} AND r.ignorar_calculos = 1
      ORDER BY r.prioridade, r.id LIMIT 1) AS regra_ignorar
  FROM pluggy_transacoes t
  LEFT JOIN extrato_ajustes a ON a.transacao_id = t.transacao_id
  LEFT JOIN (
    SELECT t.transacao_id AS tx_id, {_DESCRICAO_EFETIVA} AS valor
    FROM pluggy_transacoes t
    LEFT JOIN extrato_ajustes a ON a.transacao_id = t.transacao_id
  ) de ON de.tx_id = t.transacao_id
) rc ON rc.tx_id = t.transacao_id;
"""

# extrato_efetivo reavalia toda regra por transacao a cada leitura -- caro
# demais pra rodar em toda tela. Esta copia materializada mora aqui (e nao em
# pluggy_extrato.py) porque e sobre esta VIEW; qualquer modulo que precisar
# ler o extrato passa por garantir_extrato_materializado() e le
# extrato_efetivo_cache, nunca a view direto.
_CACHE_EXTRATO_LOCK = threading.Lock()
_CACHE_EXTRATO_CHAVE = "extrato_efetivo_cache_v1"


def _assinatura_extrato(conn: sqlite3.Connection) -> str:
    """Assinatura barata e determinística de tudo que altera a view efetiva."""
    consultas = (
        "SELECT transacao_id, conta_id, data, descricao, valor, moeda, tipo, "
        "status, categoria, parcela_numero, parcela_total, fatura_id, ordem "
        "FROM pluggy_transacoes ORDER BY transacao_id",
        "SELECT * FROM extrato_ajustes ORDER BY transacao_id",
        "SELECT id, campo, operador, termo, valor_min, valor_max, categoria_id, "
        "ignorar_calculos, prioridade, ativo, descricao_nova "
        "FROM extrato_regras ORDER BY id",
        # A regra pode ter varios termos (extrato_regra_termos), e so a coluna
        # legada "termo" (o primeiro/unico) entrava aqui -- editar a lista sem
        # tocar mais nada deixava o cache achando que nada mudou, e a
        # descricao renomeada antiga ficava presa ate outra coisa invalidar
        # por acaso.
        "SELECT regra_id, ordem, termo FROM extrato_regra_termos "
        "ORDER BY regra_id, ordem",
        "SELECT categoria_original, categoria_id FROM extrato_mapeamento_categorias "
        "ORDER BY categoria_original",
        "SELECT id, operador, termo, ativo, dia_inicio, dia_fim, deslocamento_meses "
        "FROM extrato_regras_entradas ORDER BY id",
        # A competencia de fatura sai do ciclo do cartao, que nasce dos
        # fechamentos informados pelo banco. Sem estas duas consultas aqui, a
        # sincronizacao trazia uma fatura nova (ou reprojetava um ciclo) e o
        # cache continuava respondendo com a competencia antiga.
        "SELECT conta_id, fatura_id, competencia, fechamento, vencimento "
        "FROM pluggy_faturas ORDER BY conta_id, fatura_id",
        "SELECT conta_id, inicio, fim, competencia, fatura_id, origem "
        "FROM pluggy_ciclos ORDER BY conta_id, inicio",
        "SELECT conta_id, acertos, amostras FROM pluggy_cartao_previsao "
        "ORDER BY conta_id",
    )
    digest = hashlib.sha256()
    digest.update(VIEW.encode("utf-8"))
    for consulta in consultas:
        for linha in conn.execute(consulta):
            digest.update(repr(tuple(linha)).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def garantir_extrato_materializado(conn: sqlite3.Connection) -> None:
    """Mantém uma cópia da view e só a refaz quando sua origem mudou."""
    with _CACHE_EXTRATO_LOCK:
        assinatura = _assinatura_extrato(conn)
        salva = conn.execute(
            "SELECT valor FROM app_meta WHERE chave = ?", (_CACHE_EXTRATO_CHAVE,)
        ).fetchone()
        existe = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'extrato_efetivo_cache'"
        ).fetchone()
        if existe and salva and salva[0] == assinatura:
            return

        conn.execute("DROP TABLE IF EXISTS extrato_efetivo_cache_novo")
        conn.execute(
            "CREATE TABLE extrato_efetivo_cache_novo AS "
            "SELECT * FROM extrato_efetivo"
        )
        conn.execute("DROP TABLE IF EXISTS extrato_efetivo_cache")
        conn.execute(
            "ALTER TABLE extrato_efetivo_cache_novo RENAME TO extrato_efetivo_cache"
        )
        conn.execute(
            "INSERT INTO app_meta(chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (_CACHE_EXTRATO_CHAVE, assinatura),
        )
        conn.commit()


CAMPOS_VALIDOS = {"descricao", "categoria_original"}
OPERADORES_VALIDOS = {"contem", "igual", "comeca_com"}


def _abrir() -> sqlite3.Connection:
    fin.ensure_database()
    conn = conectar()
    garantir_camada(conn)
    return conn


def categorias_payload() -> dict:
    """Lista achatada, já na ordem da árvore: cada pai seguido dos seus filhos.

    O seletor de categoria mostra nessa ordem e recua as subcategorias, então a
    ordenação precisa vir pronta daqui.
    """
    with _abrir() as conn:
        linhas = [
            {
                "id": l["id"],
                "nome": l["nome"],
                "cor": l["cor"],
                "emoji": l["emoji"] or "",
                "paiId": l["pai_id"],
                "paiNome": l["pai_nome"],
                "ordem": l["ordem"],
                "personalizada": bool(l["personalizada"]),
                "padrao": l["id"] in {c[0] for c in CATEGORIAS},
            }
            for l in conn.execute(
                "SELECT c.id, c.nome, c.cor, c.emoji, c.pai_id, c.ordem, "
                "       c.personalizada, p.nome AS pai_nome "
                "FROM extrato_categorias c "
                "LEFT JOIN extrato_categorias p ON p.id = c.pai_id "
                "WHERE c.ativo = 1 ORDER BY c.ordem"
            )
        ]

    pais = [c for c in linhas if not c["paiId"]]
    filhos_de: dict[str, list] = {}
    for c in linhas:
        if c["paiId"]:
            filhos_de.setdefault(c["paiId"], []).append(c)

    ordenadas = []
    for pai in pais:
        ordenadas.append(pai)
        ordenadas.extend(filhos_de.get(pai["id"], []))
    # Órfã (pai desativado) não pode sumir do seletor.
    vistos = {c["id"] for c in ordenadas}
    ordenadas.extend(c for c in linhas if c["id"] not in vistos)

    return {"categorias": ordenadas, "emojisSugeridos": EMOJIS_SUGERIDOS}


# --------------------------------------------------------------------------
# CRUD de categorias
# --------------------------------------------------------------------------

def _slug(texto: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", normalizar(texto)).strip("_")
    return base[:40] or "categoria"


def _validar_categoria(dados: dict, conn: sqlite3.Connection,
                       id_atual: str | None = None) -> dict:
    nome = str(dados.get("nome") or "").strip()
    if not nome:
        raise ValueError("Informe o nome da categoria.")

    cor = str(dados.get("cor") or "").strip() or "#9ba1ab"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", cor):
        raise ValueError("Cor inválida. Use o formato #rrggbb.")

    # Um emoji costuma ter varios code points (pele, variacao, ZWJ). Limitar por
    # caractere quebraria; o teto generoso so evita alguem colar um texto.
    emoji = str(dados.get("emoji") or "").strip()[:16]

    pai_id = dados.get("paiId") or None
    # So subcategoria tem emoji. Categoria (sem pai) vira sempre o quadrado
    # colorido -- reforcado aqui pra valer mesmo se algum cliente antigo ainda
    # mandar emoji num payload de categoria-pai.
    if not pai_id:
        emoji = ""
    if pai_id:
        if pai_id == id_atual:
            raise ValueError("Uma categoria não pode ser pai dela mesma.")
        pai = conn.execute(
            "SELECT pai_id FROM extrato_categorias WHERE id = ?", (pai_id,)
        ).fetchone()
        if not pai:
            raise ValueError("Categoria pai não encontrada.")
        # Só dois níveis: pai e subcategoria. Permitir neto tornaria a soma da
        # tela de categorias ambígua.
        if pai["pai_id"]:
            raise ValueError(
                "Só existem dois níveis: escolha uma categoria pai, não uma subcategoria."
            )
        if id_atual and conn.execute(
            "SELECT 1 FROM extrato_categorias WHERE pai_id = ?", (id_atual,)
        ).fetchone():
            raise ValueError(
                "Esta categoria já tem subcategorias, então não pode virar subcategoria."
            )

    return {"nome": nome, "cor": cor, "emoji": emoji, "pai_id": pai_id}


def criar_categoria(dados: dict) -> dict:
    with _abrir() as conn:
        c = _validar_categoria(dados, conn)
        base = _slug(c["nome"])
        novo_id, n = base, 2
        while conn.execute("SELECT 1 FROM extrato_categorias WHERE id = ?",
                           (novo_id,)).fetchone():
            novo_id, n = f"{base}_{n}", n + 1

        ordem = (conn.execute(
            "SELECT COALESCE(MAX(ordem), 0) FROM extrato_categorias"
        ).fetchone()[0]) + 1

        conn.execute(
            "INSERT INTO extrato_categorias "
            "  (id, nome, cor, ativo, ordem, pai_id, emoji, personalizada) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 1)",
            (novo_id, c["nome"], c["cor"], ordem, c["pai_id"], c["emoji"]),
        )
        conn.commit()
    return {"ok": True, "id": novo_id, "nome": c["nome"], "cor": c["cor"],
            "emoji": c["emoji"], "paiId": c["pai_id"]}


def atualizar_categoria(categoria_id: str, dados: dict) -> dict:
    with _abrir() as conn:
        if not conn.execute("SELECT 1 FROM extrato_categorias WHERE id = ?",
                            (categoria_id,)).fetchone():
            raise ValueError("Categoria não encontrada.")
        c = _validar_categoria(dados, conn, id_atual=categoria_id)
        conn.execute(
            "UPDATE extrato_categorias SET nome = ?, cor = ?, emoji = ?, pai_id = ?, "
            "  personalizada = 1 WHERE id = ?",
            (c["nome"], c["cor"], c["emoji"], c["pai_id"], categoria_id),
        )
        conn.commit()
    return {"ok": True, "id": categoria_id, **c}


def remover_categoria(categoria_id: str) -> dict:
    """Só remove se ninguém depender dela.

    Apagar em cascata levaria junto edições manuais e regras do usuário, que é
    exatamente o que esta camada existe para proteger.
    """
    if categoria_id == CATEGORIA_PADRAO:
        raise ValueError("A categoria Outros é o destino padrão e não pode ser removida.")

    with _abrir() as conn:
        if not conn.execute("SELECT 1 FROM extrato_categorias WHERE id = ?",
                            (categoria_id,)).fetchone():
            raise ValueError("Categoria não encontrada.")

        usos = {
            "subcategorias": conn.execute(
                "SELECT COUNT(*) FROM extrato_categorias WHERE pai_id = ?",
                (categoria_id,)).fetchone()[0],
            "transações editadas à mão": conn.execute(
                "SELECT COUNT(*) FROM extrato_ajustes WHERE categoria_id_manual = ?",
                (categoria_id,)).fetchone()[0],
            "regras": conn.execute(
                "SELECT COUNT(*) FROM extrato_regras WHERE categoria_id = ?",
                (categoria_id,)).fetchone()[0],
            "traduções da Pluggy": conn.execute(
                "SELECT COUNT(*) FROM extrato_mapeamento_categorias WHERE categoria_id = ?",
                (categoria_id,)).fetchone()[0],
        }
        presos = [f"{n} {rotulo}" for rotulo, n in usos.items() if n]
        if presos:
            raise ValueError(
                "Não dá para remover: a categoria ainda é usada por "
                + ", ".join(presos) + "."
            )

        conn.execute("DELETE FROM extrato_categorias WHERE id = ?", (categoria_id,))
        conn.commit()
    return {"ok": True, "id": categoria_id}


def regras_payload() -> dict:
    with _abrir() as conn:
        linhas = list(conn.execute(
            """
            SELECT r.*, c.nome AS categoria_nome, c.cor AS categoria_cor,
                   (SELECT COUNT(*) FROM pluggy_transacoes t
                     LEFT JOIN extrato_ajustes a
                       ON a.transacao_id = t.transacao_id
                     WHERE EXISTS (
                               SELECT 1 FROM extrato_regra_termos rt
                                WHERE rt.regra_id = r.id AND
                                  CASE r.operador
                                    WHEN 'igual' THEN
                                      (CASE r.campo WHEN 'categoria_original'
                                         THEN norm(t.categoria) ELSE norm(t.descricao) END)
                                      = norm(rt.termo)
                                    WHEN 'comeca_com' THEN
                                      (CASE r.campo WHEN 'categoria_original'
                                         THEN norm(t.categoria) ELSE norm(t.descricao) END)
                                      LIKE norm(rt.termo) || '%'
                                    ELSE
                                      (CASE r.campo WHEN 'categoria_original'
                                         THEN norm(t.categoria) ELSE norm(t.descricao) END)
                                      LIKE '%' || norm(rt.termo) || '%'
                                  END
                             )
                       AND (r.valor_min IS NULL OR
                            ABS(COALESCE(a.valor_manual, t.valor)) >= r.valor_min)
                       AND (r.valor_max IS NULL OR
                            ABS(COALESCE(a.valor_manual, t.valor)) <= r.valor_max)
                   ) AS afetadas
              FROM extrato_regras r
              LEFT JOIN extrato_categorias c ON c.id = r.categoria_id
             ORDER BY r.prioridade, r.id
            """
        ))
        termos_por_regra: dict[str, list[str]] = {}
        for termo in conn.execute(
            "SELECT regra_id, termo FROM extrato_regra_termos ORDER BY regra_id, ordem"
        ):
            termos_por_regra.setdefault(termo["regra_id"], []).append(termo["termo"])
        return {
            "regras": [
                {
                    "id": l["id"],
                    "nome": l["nome"],
                    "campo": l["campo"],
                    "operador": l["operador"],
                    "termo": l["termo"],
                    "termos": termos_por_regra.get(l["id"], [l["termo"]]),
                    "valorMin": l["valor_min"],
                    "valorMax": l["valor_max"],
                    "categoriaId": l["categoria_id"],
                    "categoriaNome": l["categoria_nome"],
                    "categoriaCor": l["categoria_cor"],
                    "descricaoNova": l["descricao_nova"] or "",
                    "ignorarCalculos": bool(l["ignorar_calculos"]),
                    "prioridade": l["prioridade"],
                    "ativo": bool(l["ativo"]),
                    "sistema": bool(l["sistema"]),
                    "criadoEm": l["criado_em"],
                    "afetadas": l["afetadas"],
                }
                for l in linhas
                # Regras de sistema ficam fora: nao sao editaveis nem
                # removiveis, entao listar so daria a impressao de que sao. O
                # que elas fazem esta documentado no README.
                if not l["sistema"]
            ],
            "sistema": sum(1 for l in linhas if l["sistema"]),
        }


def _validar_regra(dados: dict) -> dict:
    recebidos = dados.get("termos")
    if not isinstance(recebidos, list):
        recebidos = [dados.get("termo")]
    termos: list[str] = []
    termos_normalizados: set[str] = set()
    for recebido in recebidos:
        termo_atual = str(recebido or "").strip()[:240]
        chave = normalizar(termo_atual)
        if termo_atual and chave not in termos_normalizados:
            termos.append(termo_atual)
            termos_normalizados.add(chave)
    if not termos:
        raise ValueError("Informe pelo menos um termo a procurar.")
    termo = termos[0]

    campo = str(dados.get("campo") or "descricao")
    if campo not in CAMPOS_VALIDOS:
        raise ValueError(f"Campo inválido: {campo}")

    operador = str(dados.get("operador") or "contem")
    if operador not in OPERADORES_VALIDOS:
        raise ValueError(f"Operador inválido: {operador}")

    categoria_id = dados.get("categoriaId") or None
    descricao_nova = str(dados.get("descricaoNova") or "").strip()[:240] or None
    ignorar = 1 if dados.get("ignorarCalculos") else 0
    if not categoria_id and not descricao_nova and not ignorar:
        raise ValueError(
            "A regra precisa definir uma categoria, renomear a descrição, "
            "marcar 'fora dos cálculos' ou combinar essas ações."
        )

    try:
        prioridade = int(dados.get("prioridade", 100))
    except (TypeError, ValueError):
        prioridade = 100

    def valor_opcional(chave: str) -> float | None:
        bruto = dados.get(chave)
        if bruto is None or str(bruto).strip() == "":
            return None
        texto = str(bruto).replace("R$", "").replace(" ", "").strip()
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            valor = float(texto)
        except (TypeError, ValueError):
            raise ValueError("Informe valores mínimo e máximo válidos.")
        if valor < 0:
            raise ValueError("A faixa de valores não pode ser negativa.")
        return round(valor, 2)

    valor_min = valor_opcional("valorMin")
    valor_max = valor_opcional("valorMax")
    if valor_min is not None and valor_max is not None and valor_min > valor_max:
        raise ValueError("O valor mínimo não pode ser maior que o valor máximo.")

    return {
        "nome": str(dados.get("nome") or termo).strip()[:120],
        "campo": campo,
        "operador": operador,
        "termo": termo,
        "termos": termos,
        "valor_min": valor_min,
        "valor_max": valor_max,
        "categoria_id": categoria_id,
        "descricao_nova": descricao_nova,
        "ignorar_calculos": ignorar,
        "prioridade": prioridade,
        "ativo": 0 if dados.get("ativo") is False else 1,
    }


def criar_regra(dados: dict) -> dict:
    import uuid
    from datetime import datetime

    r = _validar_regra(dados)
    novo_id = f"usr_{uuid.uuid4().hex[:12]}"
    with _abrir() as conn:
        if r["categoria_id"] and not conn.execute(
            "SELECT 1 FROM extrato_categorias WHERE id = ?", (r["categoria_id"],)
        ).fetchone():
            raise ValueError(f"Categoria inexistente: {r['categoria_id']}")
        conn.execute(
            "INSERT INTO extrato_regras (id, nome, campo, operador, termo, "
            "valor_min, valor_max, categoria_id, descricao_nova, ignorar_calculos, "
            "prioridade, ativo, sistema, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (novo_id, r["nome"], r["campo"], r["operador"], r["termo"],
             r["valor_min"], r["valor_max"], r["categoria_id"],
             r["descricao_nova"], r["ignorar_calculos"],
             r["prioridade"], r["ativo"],
             datetime.now().isoformat(timespec="seconds")),
        )
        _substituir_termos_regra(conn, novo_id, r["termos"])
        conn.commit()
    return {"ok": True, "id": novo_id}


def atualizar_regra(regra_id: str, dados: dict) -> dict:
    r = _validar_regra(dados)
    with _abrir() as conn:
        existente = conn.execute(
            "SELECT sistema FROM extrato_regras WHERE id = ?", (regra_id,)
        ).fetchone()
        if not existente:
            raise ValueError("Regra não encontrada.")
        # Regra de sistema e infraestrutura de calculo, nao preferencia do
        # usuario: mexer no termo dela quebra a exclusao de transferencia
        # propria e de pagamento de fatura, e o estrago aparece longe daqui,
        # como total negativo numa categoria.
        if existente["sistema"]:
            raise ValueError("Regras de sistema não podem ser editadas.")
        conn.execute(
            "UPDATE extrato_regras SET nome = ?, campo = ?, operador = ?, termo = ?, "
            "valor_min = ?, valor_max = ?, categoria_id = ?, descricao_nova = ?, ignorar_calculos = ?, "
            "prioridade = ?, ativo = ? "
            "WHERE id = ?",
            (r["nome"], r["campo"], r["operador"], r["termo"],
             r["valor_min"], r["valor_max"], r["categoria_id"],
             r["descricao_nova"], r["ignorar_calculos"], r["prioridade"],
             r["ativo"], regra_id),
        )
        _substituir_termos_regra(conn, regra_id, r["termos"])
        conn.commit()
    return {"ok": True, "id": regra_id}


def remover_regra(regra_id: str) -> dict:
    with _abrir() as conn:
        linha = conn.execute(
            "SELECT sistema FROM extrato_regras WHERE id = ?", (regra_id,)
        ).fetchone()
        if not linha:
            raise ValueError("Regra não encontrada.")
        if linha["sistema"]:
            raise ValueError("Regras de sistema não podem ser excluídas.")
        conn.execute("DELETE FROM extrato_regra_termos WHERE regra_id = ?", (regra_id,))
        conn.execute("DELETE FROM extrato_regras WHERE id = ?", (regra_id,))
        conn.commit()
    return {"ok": True, "id": regra_id, "removida": True}


# --------------------------------------------------------------------------
# Regras de entradas: somente créditos reconhecidos aqui compõem o salário.
# --------------------------------------------------------------------------

def _validar_regra_entrada(dados: dict) -> dict:
    termo = str(dados.get("termo") or "").strip()
    if not termo:
        raise ValueError("Informe o termo a procurar na descrição.")
    operador = str(dados.get("operador") or "contem")
    if operador not in OPERADORES_VALIDOS:
        raise ValueError(f"Operador inválido: {operador}")
    nome = str(dados.get("nome") or termo).strip()[:120]
    try:
        dia_inicio = int(dados.get("diaInicio", 1))
        dia_fim = int(dados.get("diaFim", 31))
        deslocamento = int(dados.get("deslocamentoMeses", 0))
    except (TypeError, ValueError):
        raise ValueError("Período ou competência inválidos.") from None
    if not (1 <= dia_inicio <= dia_fim <= 31):
        raise ValueError("Informe um período de dias entre 1 e 31.")
    if deslocamento not in {-1, 0, 1}:
        raise ValueError("A competência deve ser o mês anterior, atual ou seguinte.")
    return {
        "nome": nome,
        "operador": operador,
        "termo": termo,
        "dia_inicio": dia_inicio,
        "dia_fim": dia_fim,
        "deslocamento_meses": deslocamento,
        "ativo": 0 if dados.get("ativo") is False else 1,
    }


def _substituir_termos_regra(
    conn: sqlite3.Connection, regra_id: str, termos: list[str]
) -> None:
    conn.execute("DELETE FROM extrato_regra_termos WHERE regra_id = ?", (regra_id,))
    conn.executemany(
        "INSERT INTO extrato_regra_termos (regra_id, ordem, termo) VALUES (?, ?, ?)",
        [(regra_id, ordem, termo) for ordem, termo in enumerate(termos)],
    )


# Um valor só, sem escopo por mês ou ano: é "quanto eu espero receber por
# mês", não uma previsão específica de uma competência. Guardado em app_meta
# por ser um ajuste manual isolado, no mesmo esquema de fatura_confirmada em
# pluggy_extrato.py -- não precisa de tabela própria para um único número.
_CHAVE_VALOR_ESPERADO = "entradas_valor_esperado"


def valor_esperado_entradas(conn: sqlite3.Connection) -> float | None:
    linha = conn.execute(
        "SELECT valor FROM app_meta WHERE chave = ?", (_CHAVE_VALOR_ESPERADO,)
    ).fetchone()
    if not linha:
        return None
    try:
        return float(linha["valor"])
    except (TypeError, ValueError):
        return None


def definir_valor_esperado_entradas(dados: dict) -> dict:
    """Valor manual que passa a valer no lugar da média em mês incompleto.

    Enviar null/vazio remove o ajuste e devolve o cálculo à média automática.
    """
    bruto = dados.get("valor")
    with _abrir() as conn:
        if bruto in (None, ""):
            conn.execute("DELETE FROM app_meta WHERE chave = ?", (_CHAVE_VALOR_ESPERADO,))
            conn.commit()
            return {"ok": True, "valor": None}
        try:
            valor = max(0.0, float(bruto))
        except (TypeError, ValueError):
            raise ValueError("Valor esperado inválido.") from None
        conn.execute(
            "INSERT INTO app_meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (_CHAVE_VALOR_ESPERADO, str(valor)),
        )
        conn.commit()
    return {"ok": True, "valor": valor}


def entradas_payload(mes: str) -> dict:
    with _abrir() as conn:
        # Cada regra escaneia todo credito do historico 3x (afetadas,
        # afetadas_mes, total_mes); direto na view isso reavaliava a
        # categorizacao inteira de toda transacao so pra checar tipo/data/
        # descricao, que nem sao colunas caras. So a copia materializada,
        # nao a view.
        garantir_extrato_materializado(conn)
        valor_esperado = valor_esperado_entradas(conn)
        regras = [
            {
                "id": l["id"],
                "nome": l["nome"],
                "operador": l["operador"],
                "termo": l["termo"],
                "diaInicio": l["dia_inicio"],
                "diaFim": l["dia_fim"],
                "deslocamentoMeses": l["deslocamento_meses"],
                "ativo": bool(l["ativo"]),
                "afetadas": l["afetadas"],
                "afetadasMes": l["afetadas_mes"],
                "totalMes": float(l["total_mes"] or 0),
            }
            for l in conn.execute(
                """
                SELECT e.*,
                       (SELECT COUNT(*) FROM extrato_efetivo_cache t
                         WHERE t.tipo = 'CREDIT'
                           AND NOT EXISTS (SELECT 1 FROM extrato_entradas_exclusoes x
                                           WHERE x.transacao_id = t.transacao_id)
                           AND CAST(SUBSTR(t.data, 9, 2) AS INTEGER)
                               BETWEEN e.dia_inicio AND e.dia_fim AND (
                           CASE e.operador
                             WHEN 'igual' THEN norm(t.descricao) = norm(e.termo)
                             WHEN 'comeca_com' THEN norm(t.descricao) LIKE norm(e.termo) || '%'
                             ELSE norm(t.descricao) LIKE '%' || norm(e.termo) || '%'
                           END
                         )) AS afetadas,
                       (SELECT COUNT(*) FROM extrato_efetivo_cache t
                         WHERE t.tipo = 'CREDIT'
                           AND NOT EXISTS (SELECT 1 FROM extrato_entradas_exclusoes x
                                           WHERE x.transacao_id = t.transacao_id)
                           AND STRFTIME('%Y-%m', DATE(SUBSTR(t.data, 1, 7) || '-01',
                               PRINTF('%+d month', e.deslocamento_meses))) = ?
                           AND CAST(SUBSTR(t.data, 9, 2) AS INTEGER)
                               BETWEEN e.dia_inicio AND e.dia_fim AND (
                           CASE e.operador
                             WHEN 'igual' THEN norm(t.descricao) = norm(e.termo)
                             WHEN 'comeca_com' THEN norm(t.descricao) LIKE norm(e.termo) || '%'
                             ELSE norm(t.descricao) LIKE '%' || norm(e.termo) || '%'
                           END
                         )) AS afetadas_mes,
                       (SELECT COALESCE(SUM(ABS(t.valor)), 0) FROM extrato_efetivo_cache t
                         WHERE t.tipo = 'CREDIT'
                           AND NOT EXISTS (SELECT 1 FROM extrato_entradas_exclusoes x
                                           WHERE x.transacao_id = t.transacao_id)
                           AND STRFTIME('%Y-%m', DATE(SUBSTR(t.data, 1, 7) || '-01',
                               PRINTF('%+d month', e.deslocamento_meses))) = ?
                           AND CAST(SUBSTR(t.data, 9, 2) AS INTEGER)
                               BETWEEN e.dia_inicio AND e.dia_fim AND (
                           CASE e.operador
                             WHEN 'igual' THEN norm(t.descricao) = norm(e.termo)
                             WHEN 'comeca_com' THEN norm(t.descricao) LIKE norm(e.termo) || '%'
                             ELSE norm(t.descricao) LIKE '%' || norm(e.termo) || '%'
                           END
                         )) AS total_mes
                FROM extrato_regras_entradas e
                ORDER BY e.criado_em, e.id
                """,
                (mes, mes),
            )
        ]

        transacoes = [
            {
                "id": l["transacao_id"],
                "data": l["data"][:10],
                "descricao": l["descricao"],
                "valor": float(abs(l["valor"])),
                "conta": l["conta_nome"],
                "ativa": not bool(l["excluida_entrada"]),
            }
            for l in conn.execute(
                """
                SELECT t.transacao_id, t.data, t.descricao, t.valor,
                       COALESCE(c.nome, 'Conta') AS conta_nome,
                       CASE WHEN x.transacao_id IS NULL THEN 0 ELSE 1 END AS excluida_entrada
                FROM extrato_efetivo_cache t
                LEFT JOIN pluggy_contas c ON c.conta_id = t.conta_id
                LEFT JOIN extrato_entradas_exclusoes x ON x.transacao_id = t.transacao_id
                WHERE t.competencia_entrada = ? AND t.tipo = 'CREDIT'
                  AND t.incluida = 1 AND t.entrada_considerada = 1
                ORDER BY t.data DESC, t.ordem DESC, t.transacao_id
                """,
                (mes,),
            )
        ]

    return {
        "mes": mes,
        "total": sum(t["valor"] for t in transacoes if t["ativa"]),
        "quantidade": sum(1 for t in transacoes if t["ativa"]),
        "quantidadeExcluida": sum(1 for t in transacoes if not t["ativa"]),
        "regrasAtivas": sum(1 for r in regras if r["ativo"]),
        "regras": regras,
        "transacoes": transacoes,
        "valorEsperado": valor_esperado,
    }


def definir_entrada_ativa(transacao_id: str, ativa: bool) -> dict:
    """Inclui ou exclui um único crédito dos totais de salário."""
    from datetime import datetime

    with _abrir() as conn:
        linha = conn.execute(
            "SELECT transacao_id, entrada_considerada FROM extrato_efetivo WHERE transacao_id = ?",
            (transacao_id,),
        ).fetchone()
        if not linha:
            raise ValueError("Lançamento não encontrado.")
        if not linha["entrada_considerada"]:
            raise ValueError("Este lançamento não é reconhecido por uma regra de entrada.")
        if ativa:
            conn.execute("DELETE FROM extrato_entradas_exclusoes WHERE transacao_id = ?",
                         (transacao_id,))
        else:
            conn.execute(
                "INSERT INTO extrato_entradas_exclusoes (transacao_id, criado_em) VALUES (?, ?) "
                "ON CONFLICT(transacao_id) DO NOTHING",
                (transacao_id, datetime.now().isoformat(timespec="seconds")),
            )
        conn.commit()
    return {"ok": True, "id": transacao_id, "ativa": bool(ativa)}


def criar_regra_entrada(dados: dict) -> dict:
    import uuid
    from datetime import datetime

    regra = _validar_regra_entrada(dados)
    regra_id = f"ent_{uuid.uuid4().hex[:12]}"
    with _abrir() as conn:
        conn.execute(
            "INSERT INTO extrato_regras_entradas "
            "(id, nome, operador, termo, dia_inicio, dia_fim, deslocamento_meses, ativo, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (regra_id, regra["nome"], regra["operador"], regra["termo"],
             regra["dia_inicio"], regra["dia_fim"], regra["deslocamento_meses"],
             regra["ativo"], datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    return {"ok": True, "id": regra_id}


def atualizar_regra_entrada(regra_id: str, dados: dict) -> dict:
    regra = _validar_regra_entrada(dados)
    with _abrir() as conn:
        if not conn.execute(
            "SELECT 1 FROM extrato_regras_entradas WHERE id = ?", (regra_id,)
        ).fetchone():
            raise ValueError("Regra de entrada não encontrada.")
        conn.execute(
            "UPDATE extrato_regras_entradas SET nome = ?, operador = ?, termo = ?, "
            "dia_inicio = ?, dia_fim = ?, deslocamento_meses = ?, ativo = ? WHERE id = ?",
            (regra["nome"], regra["operador"], regra["termo"],
             regra["dia_inicio"], regra["dia_fim"], regra["deslocamento_meses"], regra["ativo"],
             regra_id),
        )
        conn.commit()
    return {"ok": True, "id": regra_id}


def remover_regra_entrada(regra_id: str) -> dict:
    with _abrir() as conn:
        cursor = conn.execute(
            "DELETE FROM extrato_regras_entradas WHERE id = ?", (regra_id,)
        )
        if not cursor.rowcount:
            raise ValueError("Regra de entrada não encontrada.")
        conn.commit()
    return {"ok": True, "id": regra_id}


def _parcelas_irmas(conn: sqlite3.Connection, original: sqlite3.Row) -> list[str]:
    """Ids das outras parcelas da mesma compra parcelada.

    A Pluggy não manda um id de compra em comum entre as parcelas -- cada
    parcela é uma transação própria, só ligada às outras pelo mesmo cartão,
    a mesma descrição e o mesmo total de parcelas. É o sinal que dá pra usar.
    """
    total = int(original["parcela_total"] or 0)
    if total <= 1:
        return []
    linhas = conn.execute(
        "SELECT transacao_id FROM pluggy_transacoes "
        "WHERE conta_id = ? AND parcela_total = ? AND transacao_id != ? "
        "  AND norm(descricao) = norm(?)",
        (original["conta_id"], total, original["transacao_id"], original["descricao"]),
    ).fetchall()
    return [l["transacao_id"] for l in linhas]


def _definir_categoria_manual(conn: sqlite3.Connection, transacao_id: str,
                              categoria_id: str | None) -> None:
    """Aplica só a categoria manual a uma transação, preservando qualquer
    outro ajuste que ela já tivesse (valor ou data corrigidos à parte, etc)."""
    from datetime import datetime

    existente = conn.execute(
        "SELECT * FROM extrato_ajustes WHERE transacao_id = ?", (transacao_id,)
    ).fetchone()
    valores = (
        categoria_id,
        existente["calculo_override"] if existente else None,
        existente["data_manual"] if existente else None,
        existente["descricao_manual"] if existente else None,
        existente["valor_manual"] if existente else None,
        existente["tipo_manual"] if existente else None,
        existente["status_manual"] if existente else None,
        existente["conta_id_manual"] if existente else None,
        existente["parcela_numero_manual"] if existente else None,
        existente["parcela_total_manual"] if existente else None,
    )
    if all(v is None for v in valores):
        conn.execute("DELETE FROM extrato_ajustes WHERE transacao_id = ?", (transacao_id,))
        return
    conn.execute(
        "INSERT INTO extrato_ajustes "
        "(transacao_id, categoria_id_manual, calculo_override, data_manual, "
        " descricao_manual, valor_manual, tipo_manual, status_manual, "
        " conta_id_manual, parcela_numero_manual, parcela_total_manual, atualizado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(transacao_id) DO UPDATE SET "
        "categoria_id_manual = excluded.categoria_id_manual, "
        "atualizado_em = excluded.atualizado_em",
        (transacao_id, *valores, datetime.now().isoformat(timespec="seconds")),
    )


def ajustar_transacao(transacao_id: str, dados: dict) -> dict:
    """Grava substituições locais sem alterar a transação importada da Pluggy."""
    from datetime import datetime

    with _abrir() as conn:
        original = conn.execute(
            "SELECT * FROM pluggy_transacoes WHERE transacao_id = ?", (transacao_id,)
        ).fetchone()
        if not original:
            raise ValueError("Transação não encontrada.")

        if dados.get("restaurarTudo"):
            conn.execute("DELETE FROM extrato_ajustes WHERE transacao_id = ?",
                         (transacao_id,))
            conn.commit()
            return {"ok": True, "id": transacao_id, "restaurada": True}

        existente = conn.execute(
            "SELECT * FROM extrato_ajustes WHERE transacao_id = ?", (transacao_id,)
        ).fetchone()

        def atual(chave: str, coluna: str):
            if chave in dados:
                valor = dados.get(chave)
                return None if valor in ("", "auto") else valor
            return existente[coluna] if existente else None

        categoria_id = atual("categoriaId", "categoria_id_manual")
        override = atual("incluidaNosCalculos", "calculo_override")
        if override is not None:
            override = 1 if override in (True, 1, "1", "true", "sim") else 0

        data_manual = atual("data", "data_manual")
        if data_manual is not None:
            data_manual = str(data_manual).strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_manual):
                raise ValueError("Data inválida. Use o formato AAAA-MM-DD.")

        descricao_manual = atual("descricao", "descricao_manual")
        if descricao_manual is not None:
            descricao_manual = str(descricao_manual).strip()
            if not descricao_manual:
                raise ValueError("A descrição não pode ficar vazia.")

        valor_manual = atual("valor", "valor_manual")
        if valor_manual is not None:
            try:
                valor_manual = abs(float(valor_manual))
            except (TypeError, ValueError):
                raise ValueError("Valor inválido.") from None

        tipo_manual = atual("tipo", "tipo_manual")
        if tipo_manual is not None:
            tipo_manual = str(tipo_manual).upper()
            if tipo_manual not in {"DEBIT", "CREDIT"}:
                raise ValueError("Tipo inválido.")

        status_manual = atual("status", "status_manual")
        if status_manual is not None:
            status_manual = str(status_manual).upper()
            if status_manual not in {"POSTED", "PENDING"}:
                raise ValueError("Situação inválida.")

        conta_id_manual = atual("contaId", "conta_id_manual")
        if conta_id_manual and not conn.execute(
            "SELECT 1 FROM pluggy_contas WHERE conta_id = ?", (conta_id_manual,)
        ).fetchone():
            raise ValueError("Conta não encontrada.")

        parcela_numero = atual("parcelaNumero", "parcela_numero_manual")
        parcela_total = atual("parcelaTotal", "parcela_total_manual")
        try:
            parcela_numero = None if parcela_numero is None else max(1, int(parcela_numero))
            parcela_total = None if parcela_total is None else max(1, int(parcela_total))
        except (TypeError, ValueError):
            raise ValueError("Número de parcela inválido.") from None
        numero_efetivo = parcela_numero or int(original["parcela_numero"] or 1)
        total_efetivo = parcela_total or int(original["parcela_total"] or 1)
        if numero_efetivo > total_efetivo:
            raise ValueError("A parcela atual não pode ser maior que o total de parcelas.")

        if categoria_id and not conn.execute(
            "SELECT 1 FROM extrato_categorias WHERE id = ?", (categoria_id,)
        ).fetchone():
            raise ValueError(f"Categoria inexistente: {categoria_id}")

        valores = (
            categoria_id, override, data_manual, descricao_manual, valor_manual,
            tipo_manual, status_manual, conta_id_manual, parcela_numero, parcela_total,
        )
        if all(v is None for v in valores):
            conn.execute("DELETE FROM extrato_ajustes WHERE transacao_id = ?",
                         (transacao_id,))
        else:
            conn.execute(
                "INSERT INTO extrato_ajustes "
                "(transacao_id, categoria_id_manual, calculo_override, data_manual, "
                " descricao_manual, valor_manual, tipo_manual, status_manual, "
                " conta_id_manual, parcela_numero_manual, parcela_total_manual, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(transacao_id) DO UPDATE SET "
                "categoria_id_manual = excluded.categoria_id_manual, "
                "calculo_override = excluded.calculo_override, "
                "data_manual = excluded.data_manual, "
                "descricao_manual = excluded.descricao_manual, "
                "valor_manual = excluded.valor_manual, "
                "tipo_manual = excluded.tipo_manual, "
                "status_manual = excluded.status_manual, "
                "conta_id_manual = excluded.conta_id_manual, "
                "parcela_numero_manual = excluded.parcela_numero_manual, "
                "parcela_total_manual = excluded.parcela_total_manual, "
                "atualizado_em = excluded.atualizado_em",
                (transacao_id, *valores,
                 datetime.now().isoformat(timespec="seconds")),
            )

        # Parcelamento: a mesma compra virou uma transação por mês, e mudar a
        # categoria de uma sem mudar as outras deixava o gasto espalhado em
        # categorias diferentes só por acaso de qual parcela foi revisada. Só
        # dispara quando o pedido é exclusivamente a categoria -- o popover
        # rápido do chip manda só isso; a edição completa manda o formulário
        # inteiro, e ali valor/data de uma parcela não deve arrastar as outras.
        irmas_atualizadas = 0
        if set(dados.keys()) == {"categoriaId"}:
            for irma_id in _parcelas_irmas(conn, original):
                _definir_categoria_manual(conn, irma_id, categoria_id)
                irmas_atualizadas += 1

        conn.commit()

        linha = conn.execute(
            "SELECT e.categoria_id, e.origem_categorizacao, e.incluida, "
            "       e.motivo_exclusao, c.nome, c.cor "
            "FROM extrato_efetivo e "
            "LEFT JOIN extrato_categorias c ON c.id = e.categoria_id "
            "WHERE e.transacao_id = ?",
            (transacao_id,),
        ).fetchone()

    return {
        "ok": True,
        "id": transacao_id,
        "categoria": {"id": linha["categoria_id"], "nome": linha["nome"],
                      "cor": linha["cor"]},
        "origemCategorizacao": linha["origem_categorizacao"],
        "incluidaNosCalculos": bool(linha["incluida"]),
        "motivoExclusao": linha["motivo_exclusao"],
        "parcelasIrmasAtualizadas": irmas_atualizadas,
    }


_camada_pronta = False


def garantir_camada(conn: sqlite3.Connection, forcar: bool = False) -> None:
    """Cria/atualiza a camada — uma vez por processo.

    Isto roda no começo de toda requisição e faz DROP/CREATE da view mais o
    seed de 35 categorias, 74 traduções e as regras de sistema. Nada disso muda
    entre duas requisições do mesmo processo, então repetir só custava tempo
    (~14 ms por chamada, várias por tela).

    "forcar" existe para os testes e para quando o banco é recriado no meio da
    execução.
    """
    global _camada_pronta
    if _camada_pronta and not forcar:
        return
    _aplicar_camada(conn)
    _camada_pronta = True


def _aplicar_camada(conn: sqlite3.Connection) -> None:
    """Cria tabelas, semeia categorias/mapeamento/regras e (re)cria a view.

    Idempotente: pode rodar a cada requisicao. As categorias sao atualizadas em
    nome/cor, mas o que o usuario criou (regras nao-sistema e ajustes) nunca e
    tocado.
    """
    from datetime import datetime

    conn.executescript(SCHEMA)

    # Migracoes de bancos criados antes destas colunas.
    colunas = {l["name"] for l in conn.execute("PRAGMA table_info(extrato_categorias)")}
    for coluna, ddl in (
        ("pai_id", "ALTER TABLE extrato_categorias ADD COLUMN pai_id TEXT"),
        ("emoji", "ALTER TABLE extrato_categorias ADD COLUMN emoji TEXT NOT NULL DEFAULT ''"),
        ("personalizada",
         "ALTER TABLE extrato_categorias ADD COLUMN personalizada INTEGER NOT NULL DEFAULT 0"),
    ):
        if coluna not in colunas:
            conn.execute(ddl)

    colunas_ajustes = {
        l["name"] for l in conn.execute("PRAGMA table_info(extrato_ajustes)")
    }
    for coluna, ddl in (
        ("data_manual", "ALTER TABLE extrato_ajustes ADD COLUMN data_manual TEXT"),
        ("descricao_manual", "ALTER TABLE extrato_ajustes ADD COLUMN descricao_manual TEXT"),
        ("valor_manual", "ALTER TABLE extrato_ajustes ADD COLUMN valor_manual REAL"),
        ("tipo_manual", "ALTER TABLE extrato_ajustes ADD COLUMN tipo_manual TEXT"),
        ("status_manual", "ALTER TABLE extrato_ajustes ADD COLUMN status_manual TEXT"),
        ("conta_id_manual", "ALTER TABLE extrato_ajustes ADD COLUMN conta_id_manual TEXT"),
        ("parcela_numero_manual", "ALTER TABLE extrato_ajustes ADD COLUMN parcela_numero_manual INTEGER"),
        ("parcela_total_manual", "ALTER TABLE extrato_ajustes ADD COLUMN parcela_total_manual INTEGER"),
    ):
        if coluna not in colunas_ajustes:
            conn.execute(ddl)

    colunas_regras = {
        l["name"] for l in conn.execute("PRAGMA table_info(extrato_regras)")
    }
    for coluna, ddl in (
        ("descricao_nova", "ALTER TABLE extrato_regras ADD COLUMN descricao_nova TEXT"),
        ("valor_min", "ALTER TABLE extrato_regras ADD COLUMN valor_min REAL"),
        ("valor_max", "ALTER TABLE extrato_regras ADD COLUMN valor_max REAL"),
    ):
        if coluna not in colunas_regras:
            conn.execute(ddl)

    colunas_entradas = {
        l["name"] for l in conn.execute("PRAGMA table_info(extrato_regras_entradas)")
    }
    for coluna, ddl in (
        ("dia_inicio", "ALTER TABLE extrato_regras_entradas ADD COLUMN dia_inicio INTEGER NOT NULL DEFAULT 1"),
        ("dia_fim", "ALTER TABLE extrato_regras_entradas ADD COLUMN dia_fim INTEGER NOT NULL DEFAULT 31"),
        ("deslocamento_meses", "ALTER TABLE extrato_regras_entradas ADD COLUMN deslocamento_meses INTEGER NOT NULL DEFAULT 0"),
    ):
        if coluna not in colunas_entradas:
            conn.execute(ddl)

    agora = datetime.now().isoformat(timespec="seconds")

    # Duas passadas: os pais primeiro, senao a referencia pai_id apontaria para
    # uma linha que ainda nao existe.
    #
    # O UPDATE so toca em quem NAO foi personalizado. Assim continuo podendo
    # corrigir nome ou cor das categorias padrao, sem desfazer o que o usuario
    # editou -- e este seed roda a cada requisicao.
    for so_pais in (True, False):
        conn.executemany(
            "INSERT INTO extrato_categorias "
            "  (id, nome, cor, ativo, ordem, pai_id, emoji, personalizada) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, 0) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  nome = excluded.nome, cor = excluded.cor, ordem = excluded.ordem, "
            "  pai_id = excluded.pai_id, emoji = excluded.emoji "
            "WHERE extrato_categorias.personalizada = 0",
            [
                (cid, nome, cor, i, pai, emoji)
                for i, (cid, nome, cor, pai, emoji) in enumerate(CATEGORIAS)
                if (pai is None) == so_pais
            ],
        )

    conn.executemany(
        "INSERT INTO extrato_mapeamento_categorias (categoria_original, categoria_id) "
        "VALUES (?, ?) ON CONFLICT(categoria_original) DO UPDATE SET "
        "categoria_id = excluded.categoria_id",
        list(MAPEAMENTO.items()),
    )

    # Regras de sistema: inseridas se faltarem e mantidas obrigatoriamente
    # ativas. Os demais campos continuam editáveis.
    for regra in REGRAS_SISTEMA:
        conn.execute(
            "INSERT OR IGNORE INTO extrato_regras "
            "(id, nome, campo, operador, termo, categoria_id, ignorar_calculos, "
            " prioridade, ativo, sistema, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
            (regra["id"], regra["nome"], regra["campo"], regra["operador"],
             regra["termo"], regra["categoria_id"], regra["ignorar_calculos"],
             regra["prioridade"], agora),
        )
        conn.execute(
            "UPDATE extrato_regras SET ativo = 1 WHERE id = ? AND sistema = 1",
            (regra["id"],),
        )

    # Migração transparente das regras antigas: o campo legado `termo` passa
    # a ser o primeiro item da nova lista, sem alterar nenhuma configuração.
    conn.execute(
        "INSERT INTO extrato_regra_termos (regra_id, ordem, termo) "
        "SELECT r.id, 0, r.termo FROM extrato_regras r "
        "WHERE NOT EXISTS (SELECT 1 FROM extrato_regra_termos rt "
        "                  WHERE rt.regra_id = r.id)"
    )

    # Os ciclos de fatura precisam existir ANTES da view: e deles que sai a
    # competencia_fatura de cada transacao de cartao. Reconstruir e barato
    # (algumas dezenas de linhas) e mantem a janela projetada em dia quando o
    # banco fecha uma fatura nova.
    ciclos.reconstruir(conn)

    conn.executescript(VIEW)
