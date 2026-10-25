"""Classes de ativo: do que a Pluggy manda até o que a tela desenha.

A tela não conhece classe de investimento nenhuma. Ela recebe uma lista de
classes já com rótulo, ordem e colunas, e desenha uma aba por item. Quem sabe
que `FIXED_INCOME` é "Renda fixa" é este módulo -- e só ele.

A consequência que motivou o arquivo: hoje tudo que não é fundo nem renda fixa
cai num balde "Outros investimentos". Se a corretora passar a entregar ações,
COE ou previdência, o dinheiro some dentro do balde. Aqui um tipo desconhecido
**vira uma classe própria**, com rótulo tirado do próprio nome que a Pluggy
mandou e um perfil genérico de colunas. A aba nasce apresentável sem ninguém
escrever código; renomear é uma linha em `app_meta`.

O vocabulário de `formato` é fechado de propósito -- `moeda`, `percentual`,
`data`, `numero`, `texto`. Precisão e alinhamento são atributos da coluna
(`casas`, `alinhamento`), não verbos novos: assim o front continua sendo um
renderizador de tabela, e não vira um interpretador de tipos.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any

CHAVE_ROTULOS = "investimentos_classes_rotulos"

# Prefixo das classes inventadas na hora para um tipo que ainda não conhecemos.
PREFIXO_DESCONHECIDA = "outro:"
ORDEM_DESCONHECIDA = 800
ORDEM_RESTO = 900


def _coluna(id_: str, rotulo: str, campo: str = "", formato: str = "texto",
            **extra: Any) -> dict[str, Any]:
    coluna = {"id": id_, "rotulo": rotulo, "campo": campo or id_, "formato": formato}
    if formato in ("moeda", "percentual", "numero"):
        coluna["alinhamento"] = "direita"
    coluna.update(extra)
    return coluna


# Colunas mínimas que toda classe mostra. Servem sozinhas para uma classe que
# nunca vimos: nome, onde está, quanto custou, quanto vale e quanto rendeu.
COLUNAS_BASE = [
    _coluna("nome", "Investimento", principal=True),
    _coluna("instituicao", "Instituição"),
    _coluna("original", "Aplicado", formato="moeda"),
    _coluna("liquido", "Saldo", formato="moeda"),
    _coluna("rendimentoLiquido", "Ganho", formato="moeda"),
]

PERFIS: dict[str, dict[str, Any]] = {
    "generico": {
        "colunas": COLUNAS_BASE,
        "destaques": [
            {"id": "liquido", "rotulo": "Saldo", "campo": "liquido", "formato": "moeda"},
            {"id": "original", "rotulo": "Aplicado", "campo": "original", "formato": "moeda"},
        ],
    },
    "renda_fixa": {
        "colunas": [
            _coluna("nome", "Produto", principal=True),
            _coluna("emissor", "Emissor"),
            # `indexador` chega pronto do backend ("100% do CDI"), em vez de a
            # tela ter de juntar taxa, tipo e periodicidade.
            _coluna("indexador", "Indexador"),
            _coluna("vencimento", "Vencimento", formato="data", ajuda="Data de resgate do papel."),
            _coluna("liquidezRotulo", "Liquidez",
                    ajuda="Carência e disponibilidade para resgate hoje."),
            _coluna("original", "Aplicado", formato="moeda"),
            _coluna("liquido", "Saldo", formato="moeda"),
            _coluna("percentualCDI", "% do CDI", campo="benchmark.percentualDoCDI",
                    formato="percentual",
                    ajuda="Rentabilidade da posição dividida pelo CDI na mesma janela."),
        ],
        "destaques": [
            {"id": "liquido", "rotulo": "Saldo", "campo": "liquido", "formato": "moeda"},
            {"id": "vencendo90", "rotulo": "Vencendo em 90 dias",
             "campo": "liquidez.vencendo90", "formato": "moeda"},
            {"id": "disponivelHoje", "rotulo": "Resgatável hoje",
             "campo": "liquidez.disponivelHoje", "formato": "moeda"},
        ],
    },
    "fundos": {
        "colunas": [
            _coluna("nome", "Fundo", principal=True),
            _coluna("gestor", "Gestor", campo="fundo.gestor"),
            _coluna("classeAnbima", "Anbima", campo="fundo.classeAnbima"),
            # Cota tem mais casas que dinheiro comum: R$ 2,48719 vira R$ 2,49 e
            # perde justamente o que distingue uma cota da outra.
            _coluna("valorCota", "Cota", formato="moeda", casas=6),
            _coluna("quantidade", "Cotas", formato="numero", casas=8),
            _coluna("liquido", "Saldo", formato="moeda"),
            _coluna("rentabilidadeFundo12Meses", "12 meses", formato="percentual",
                    ajuda="Rentabilidade do fundo, não da sua aplicação."),
        ],
        "destaques": [
            {"id": "liquido", "rotulo": "Saldo", "campo": "liquido", "formato": "moeda"},
            {"id": "posicoes", "rotulo": "Fundos", "campo": "posicoes", "formato": "numero"},
        ],
    },
    "variavel": {
        "colunas": [
            _coluna("nome", "Ativo", principal=True),
            _coluna("codigo", "Código"),
            _coluna("quantidade", "Quantidade", formato="numero", casas=8),
            _coluna("valorCota", "Cotação", formato="moeda", casas=4),
            _coluna("original", "Custo", formato="moeda"),
            _coluna("liquido", "Saldo", formato="moeda"),
            _coluna("rendimentoLiquido", "Resultado", formato="moeda"),
        ],
        "destaques": [
            {"id": "liquido", "rotulo": "Saldo", "campo": "liquido", "formato": "moeda"},
            {"id": "rendimentoLiquido", "rotulo": "Resultado",
             "campo": "rendimentoLiquido", "formato": "moeda"},
        ],
    },
}

CLASSES: dict[str, dict[str, Any]] = {
    "renda_fixa":   {"rotulo": "Renda fixa",   "ordem": 10, "perfil": "renda_fixa"},
    "fundos":       {"rotulo": "Fundos",       "ordem": 20, "perfil": "fundos"},
    "acoes":        {"rotulo": "Ações",        "ordem": 30, "perfil": "variavel"},
    "estruturados": {"rotulo": "Estruturados", "ordem": 40, "perfil": "generico"},
    "previdencia":  {"rotulo": "Previdência",  "ordem": 50, "perfil": "generico"},
    "cripto":       {"rotulo": "Cripto",       "ordem": 60, "perfil": "variavel"},
    "imoveis":      {"rotulo": "Imóveis",      "ordem": 70, "perfil": "generico"},
    "outros":       {"rotulo": "Outros",       "ordem": ORDEM_RESTO, "perfil": "generico"},
}

# Tipo da Pluggy -> classe nossa. O que não estiver aqui vira classe própria.
TIPOS: dict[str, str] = {
    "FIXED_INCOME": "renda_fixa",
    "SECURITY": "renda_fixa",
    "MUTUAL_FUND": "fundos",
    "EQUITY": "acoes",
    "ETF": "acoes",
    "COE": "estruturados",
    "STRUCTURED_NOTE": "estruturados",
    "PENSION": "previdencia",
    "RETIREMENT": "previdencia",
    "CRYPTO": "cripto",
    "REAL_ESTATE": "imoveis",
    "OTHER": "outros",
}

# Rótulo em português do produto. Só afeta o nome do subtipo mostrado na linha;
# a classe continua vindo de TIPOS, salvo quando o subtipo diz outra coisa.
SUBTIPOS: dict[tuple[str, str], dict[str, str]] = {
    ("FIXED_INCOME", "CDB"): {"rotulo": "CDB"},
    ("FIXED_INCOME", "LCI"): {"rotulo": "LCI"},
    ("FIXED_INCOME", "LCA"): {"rotulo": "LCA"},
    ("FIXED_INCOME", "LC"): {"rotulo": "Letra de câmbio"},
    ("FIXED_INCOME", "TREASURY"): {"rotulo": "Tesouro Direto"},
    ("FIXED_INCOME", "DEBENTURES"): {"rotulo": "Debênture"},
    ("FIXED_INCOME", "CRI"): {"rotulo": "CRI"},
    ("FIXED_INCOME", "CRA"): {"rotulo": "CRA"},
    ("MUTUAL_FUND", "INVESTMENT_FUND"): {"rotulo": "Fundo de investimento"},
    ("MUTUAL_FUND", "STOCK_FUND"): {"rotulo": "Fundo de ações"},
    ("MUTUAL_FUND", "FIXED_INCOME_FUND"): {"rotulo": "Fundo de renda fixa"},
    ("MUTUAL_FUND", "MULTIMARKET_FUND"): {"rotulo": "Multimercado"},
    ("MUTUAL_FUND", "EXCHANGE_FUND"): {"rotulo": "Fundo cambial"},
    ("MUTUAL_FUND", "OFFSHORE_FUND"): {"rotulo": "Fundo offshore"},
    # FII é fundo no papel, mas quem investe o lê junto da renda variável.
    ("MUTUAL_FUND", "REAL_ESTATE_FUND"): {"rotulo": "Fundo imobiliário", "classe": "acoes"},
    ("EQUITY", "STOCK"): {"rotulo": "Ação"},
    ("EQUITY", "ETF"): {"rotulo": "ETF"},
    ("EQUITY", "BDR"): {"rotulo": "BDR"},
    ("EQUITY", "REAL_ESTATE_FUND"): {"rotulo": "Fundo imobiliário"},
}


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def humanizar(tipo: str) -> str:
    """`REAL_ESTATE` -> `Real Estate`. Serve a tipo que ainda não traduzimos.

    Não inventa tradução: mostra o nome que a instituição mandou, de forma
    legível, e deixa o usuário renomear se quiser.
    """
    limpo = re.sub(r"[_\-]+", " ", _texto(tipo)).strip()
    if not limpo:
        return "Outros"
    return " ".join(p.capitalize() if p.isupper() or p.islower() else p
                    for p in limpo.split())


def _slug(tipo: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", _texto(tipo)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", sem_acento).strip("_").upper() or "DESCONHECIDO"


def classificar(tipo: str, subtipo: str = "", classe_manual: str = "") -> dict[str, Any]:
    """A que classe pertence esta posição, e com que rótulo aparece.

    `classe_manual` vem do lançamento à mão, onde quem escolhe a classe é o
    usuário -- e por isso ela ganha da dedução por tipo.
    """
    tipo, subtipo, classe_manual = _texto(tipo), _texto(subtipo), _texto(classe_manual)
    rotulo_subtipo = ""
    conhecida = True
    classe = ""

    if classe_manual and classe_manual in CLASSES:
        classe = classe_manual
    else:
        regra = SUBTIPOS.get((tipo.upper(), subtipo.upper()))
        if regra:
            rotulo_subtipo = regra.get("rotulo", "")
            classe = regra.get("classe", "")
        classe = classe or TIPOS.get(tipo.upper(), "")

    if not classe:
        # Tipo que ainda não conhecemos vira classe própria em vez de sumir
        # dentro de "Outros": é a diferença entre ver o dinheiro e não ver.
        classe = f"{PREFIXO_DESCONHECIDA}{_slug(tipo)}" if tipo else "outros"
        conhecida = tipo == ""

    definicao = CLASSES.get(classe)
    if definicao is None:
        definicao = {"rotulo": humanizar(tipo), "ordem": ORDEM_DESCONHECIDA, "perfil": "generico"}
        conhecida = False

    return {
        "classe": classe,
        "rotulo": definicao["rotulo"],
        "rotuloSubtipo": rotulo_subtipo or humanizar(subtipo) if subtipo else "",
        "ordem": definicao["ordem"],
        "perfil": definicao["perfil"],
        "conhecida": conhecida,
    }


def colunas(perfil: str) -> list[dict[str, Any]]:
    """Colunas daquele perfil. Perfil desconhecido cai no genérico."""
    return [dict(c) for c in PERFIS.get(perfil, PERFIS["generico"])["colunas"]]


def destaques(perfil: str) -> list[dict[str, Any]]:
    return [dict(d) for d in PERFIS.get(perfil, PERFIS["generico"])["destaques"]]


def rotulos_personalizados(conn) -> dict[str, str]:
    """Renomes que o usuário deu às classes, guardados em `app_meta`.

    Banco sem `app_meta`, JSON quebrado ou formato inesperado devolvem vazio:
    um renome perdido é aborrecimento, a tela inteira cair é outra coisa.
    """
    try:
        linha = conn.execute("SELECT valor FROM app_meta WHERE chave=?",
                             (CHAVE_ROTULOS,)).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not linha:
        return {}
    try:
        dados = json.loads(linha[0])
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in dados.items()} if isinstance(dados, dict) else {}


def classes_presentes(itens: list[dict[str, Any]],
                      rotulos: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """As classes que existem no dado, na ordem canônica.

    Classe sem posição não vira aba: a tela mostra o que a pessoa tem, não o
    catálogo do que poderia ter.
    """
    rotulos = rotulos or {}
    encontradas: dict[str, dict[str, Any]] = {}
    for item in itens:
        info = classificar(item.get("tipo", ""), item.get("subtipo", ""),
                           item.get("classe", "") if item.get("origem") == "manual" else "")
        atual = encontradas.setdefault(info["classe"], {
            "id": info["classe"],
            "rotulo": rotulos.get(info["classe"], info["rotulo"]),
            "ordem": info["ordem"],
            "perfil": info["perfil"],
            "conhecida": info["conhecida"],
            "posicoes": 0,
        })
        atual["posicoes"] += 1
    # Desempate por rótulo mantém a ordem estável entre chamadas quando várias
    # classes desconhecidas dividem a mesma ordem.
    return sorted(encontradas.values(), key=lambda c: (c["ordem"], c["rotulo"], c["id"]))
