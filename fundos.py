"""Identificação de fundos: do CNPJ até a página do fundo no BTG.

A assessoria indica fundos pelo CNPJ, mas o catálogo do BTG usa nomes
abreviados ("A1 D30 FICFIRF LP CrPr RL"), diferentes da denominação social
registrada na CVM. Procurar pelo nome que veio na mensagem quase nunca acha.

Duas fontes públicas resolvem isso, e nenhum dado desta máquina sai daqui: as
consultas são locais, contra cópias baixadas dos catálogos.

1. Catálogo público do BTG (693 fundos), que traz o CNPJ junto do nome usado
   na plataforma. A página de detalhe é `/fundos-de-investimento/<slug>`, com
   o slug sendo o nome slugificado -- é assim que a própria listagem monta os
   links, e ela abre sem login.
2. Cadastro da CVM, para dois casos que o catálogo do BTG não cobre: o fundo
   que o BTG não distribui (aí pelo menos sabemos nome, gestor e situação) e a
   ponte fundo/classe da RCVM 175. Esta segunda é a que mais engana: desde a
   RCVM 175 o CNPJ que a assessoria manda costuma ser o da classe, enquanto o
   BTG pode listar o CNPJ do fundo -- ou o contrário. Sem a ponte, a busca por
   CNPJ falha mesmo com o fundo disponível na plataforma.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import unicodedata
import urllib.request
import zipfile
from datetime import datetime
from typing import Any

import banco as fin

CATALOGO_BTG = ("https://investimentos.btgpactual.com/services/api/funds-public"
                "/public/funds/list/v2?page={pagina}&size=100"
                "&orderBy=NOME&sortingDirection=ASC")
PAGINA_BTG = "https://investimentos.btgpactual.com/fundos-de-investimento/{slug}"
CATALOGO_BTG_TELA = "https://investimentos.btgpactual.com/fundos-de-investimento/produtos"
CAD_FI_CVM = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"
REGISTRO_CVM = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"

# O catálogo do BTG muda pouco e o cadastro da CVM é publicado de terça a
# sábado. Um dia de validade evita baixar 25 MB a cada consulta.
VALIDADE_HORAS = 24
TIMEOUT_SEGUNDOS = 120
LIMITE_RESULTADOS = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS fundos_btg (
  cnpj TEXT PRIMARY KEY,
  nome TEXT NOT NULL DEFAULT '',
  nome_original TEXT NOT NULL DEFAULT '',
  slug TEXT NOT NULL DEFAULT '',
  categoria TEXT NOT NULL DEFAULT '',
  subcategoria TEXT NOT NULL DEFAULT '',
  risco TEXT NOT NULL DEFAULT '',
  publico TEXT NOT NULL DEFAULT '',
  aplicacao_minima REAL,
  dias_resgate INTEGER,
  atualizado_em TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS fundos_btg_nome ON fundos_btg(nome);

CREATE TABLE IF NOT EXISTS fundos_cvm (
  cnpj TEXT PRIMARY KEY,
  tipo TEXT NOT NULL DEFAULT 'fundo',
  denominacao TEXT NOT NULL DEFAULT '',
  situacao TEXT NOT NULL DEFAULT '',
  classificacao TEXT NOT NULL DEFAULT '',
  classe_anbima TEXT NOT NULL DEFAULT '',
  gestor TEXT NOT NULL DEFAULT '',
  administrador TEXT NOT NULL DEFAULT '',
  codigo_cvm TEXT NOT NULL DEFAULT '',
  registro_fundo TEXT NOT NULL DEFAULT '',
  atualizado_em TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS fundos_cvm_registro ON fundos_cvm(registro_fundo);
CREATE INDEX IF NOT EXISTS fundos_cvm_denominacao ON fundos_cvm(denominacao);
"""

_trava = threading.Lock()
_sincronizando: dict[str, bool] = {}


def garantir_tabelas(conn=None) -> None:
    if conn is not None:
        conn.executescript(SCHEMA)
        return
    with fin.connect() as conexao:
        conexao.executescript(SCHEMA)
        conexao.commit()


# ------------------------------------------------------------------- CNPJ

def digitos(texto: str) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def formatar_cnpj(cnpj: str) -> str:
    d = digitos(cnpj)
    if len(d) != 14:
        return d
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def cnpj_valido(cnpj: str) -> bool:
    """Confere os dígitos verificadores.

    A assessoria manda o CNPJ por mensagem e um dígito trocado leva a uma
    busca que não acha nada -- vale distinguir "não existe" de "veio errado".
    """
    d = digitos(cnpj)
    if len(d) != 14 or len(set(d)) == 1:
        return False
    for tamanho in (12, 13):
        pesos = [(i % 8) + 2 for i in range(tamanho - 1, -1, -1)]
        soma = sum(int(d[i]) * pesos[i] for i in range(tamanho))
        resto = soma % 11
        if int(d[tamanho]) != (0 if resto < 2 else 11 - resto):
            return False
    return True


def slugificar(nome: str) -> str:
    """Mesma regra que o catálogo do BTG usa nos links da listagem."""
    sem_acento = unicodedata.normalize("NFKD", str(nome or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", sem_acento).strip("-").lower()


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9 ]+", " ", sem_acento)).strip().lower()


def cnpjs_do_texto(texto: str) -> list[str]:
    """Extrai CNPJs de um texto colado, na ordem em que aparecem.

    A mensagem da assessoria vem com nome, CNPJ e comentários no meio; aceitar
    o texto inteiro poupa o trabalho de separar um por um.
    """
    achados: list[str] = []
    for bruto in re.findall(r"\d[\d.\-/]{11,}\d", str(texto or "")):
        d = digitos(bruto)
        if len(d) == 14 and d not in achados:
            achados.append(d)
    return achados


# --------------------------------------------------------- catálogo do BTG

def _baixar(url: str) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": "FinanceiroPluggy/1.0"})
    with urllib.request.urlopen(pedido, timeout=TIMEOUT_SEGUNDOS) as resposta:
        return resposta.read()


def _itens_btg() -> list[dict[str, Any]]:
    itens: list[dict[str, Any]] = []
    pagina = 1
    while True:
        # A API responde utf-8, como declara, e recusa página maior que 100.
        dados = json.loads(_baixar(CATALOGO_BTG.format(pagina=pagina)).decode("utf-8"))
        itens += dados.get("items") or []
        total_paginas = int(dados.get("total_pages") or 1)
        total = int(dados.get("total") or 0)
        if pagina >= total_paginas or len(itens) >= total or pagina >= 40:
            return itens
        pagina += 1


def sincronizar_btg() -> int:
    agora = datetime.now().isoformat(timespec="seconds")
    linhas = []
    for item in _itens_btg():
        cnpj = digitos(item.get("CNPJ"))
        nome = str(item.get("Name") or "").strip()
        if len(cnpj) != 14 or not nome:
            continue
        detalhe = item.get("Detail") or {}
        linhas.append((
            cnpj, nome, str(item.get("NameOriginal") or "").strip(), slugificar(nome),
            str(detalhe.get("categoryBTG") or ""), str(detalhe.get("subcategoryBTG") or ""),
            str(detalhe.get("riskName") or ""), str(detalhe.get("investmentType") or ""),
            detalhe.get("minimumInitialInvestment"), detalhe.get("numeroDiaFinanceiroResgate"),
            agora,
        ))
    if not linhas:
        raise RuntimeError("O catálogo do BTG voltou vazio.")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        # Fundo que saiu do catálogo não deve continuar respondendo com link.
        conn.execute("DELETE FROM fundos_btg")
        conn.executemany(
            "INSERT OR REPLACE INTO fundos_btg (cnpj,nome,nome_original,slug,categoria,"
            "subcategoria,risco,publico,aplicacao_minima,dias_resgate,atualizado_em) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", linhas)
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('fundos_btg_em',?)", (agora,))
        conn.commit()
    return len(linhas)


# --------------------------------------------------------- cadastro da CVM

def _csv_cvm(texto: str):
    return csv.DictReader(io.StringIO(texto), delimiter=";")


def _registros_cvm() -> list[tuple]:
    agora = datetime.now().isoformat(timespec="seconds")
    registros: dict[str, tuple] = {}

    # Marco antigo: um fundo por linha, com gestor e administrador na linha.
    for linha in _csv_cvm(_baixar(CAD_FI_CVM).decode("latin-1")):
        cnpj = digitos(linha.get("CNPJ_FUNDO"))
        if len(cnpj) != 14:
            continue
        registros[cnpj] = (
            cnpj, "fundo", (linha.get("DENOM_SOCIAL") or "").strip(),
            (linha.get("SIT") or "").strip(), (linha.get("CLASSE") or "").strip(),
            (linha.get("CLASSE_ANBIMA") or "").strip(), (linha.get("GESTOR") or "").strip(),
            (linha.get("ADMIN") or "").strip(), (linha.get("CD_CVM") or "").strip(), "", agora,
        )

    # RCVM 175: fundo e classes em arquivos separados, ligados pelo registro.
    with zipfile.ZipFile(io.BytesIO(_baixar(REGISTRO_CVM))) as pacote:
        fundos: dict[str, tuple[str, str, str]] = {}
        with pacote.open("registro_fundo.csv") as arquivo:
            for linha in _csv_cvm(arquivo.read().decode("latin-1")):
                registro = (linha.get("ID_Registro_Fundo") or "").strip()
                cnpj = digitos(linha.get("CNPJ_Fundo"))
                gestor = (linha.get("Gestor") or "").strip()
                admin = (linha.get("Administrador") or "").strip()
                fundos[registro] = (cnpj, gestor, admin)
                if len(cnpj) == 14:
                    registros[cnpj] = (
                        cnpj, "fundo", (linha.get("Denominacao_Social") or "").strip(),
                        (linha.get("Situacao") or "").strip(),
                        (linha.get("Tipo_Fundo") or "").strip(), "", gestor, admin,
                        (linha.get("Codigo_CVM") or "").strip(), registro, agora,
                    )
        with pacote.open("registro_classe.csv") as arquivo:
            for linha in _csv_cvm(arquivo.read().decode("latin-1")):
                cnpj = digitos(linha.get("CNPJ_Classe"))
                if len(cnpj) != 14:
                    continue
                registro = (linha.get("ID_Registro_Fundo") or "").strip()
                _, gestor, admin = fundos.get(registro, ("", "", ""))
                registros[cnpj] = (
                    cnpj, "classe", (linha.get("Denominacao_Social") or "").strip(),
                    (linha.get("Situacao") or "").strip(),
                    (linha.get("Classificacao") or "").strip(),
                    (linha.get("Classificacao_Anbima") or "").strip(), gestor, admin,
                    (linha.get("Codigo_CVM") or "").strip(), registro, agora,
                )
    return list(registros.values())


def sincronizar_cvm() -> int:
    linhas = _registros_cvm()
    if not linhas:
        raise RuntimeError("O cadastro da CVM voltou vazio.")
    agora = datetime.now().isoformat(timespec="seconds")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        conn.execute("DELETE FROM fundos_cvm")
        conn.executemany(
            "INSERT OR REPLACE INTO fundos_cvm (cnpj,tipo,denominacao,situacao,classificacao,"
            "classe_anbima,gestor,administrador,codigo_cvm,registro_fundo,atualizado_em) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", linhas)
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('fundos_cvm_em',?)", (agora,))
        conn.commit()
    return len(linhas)


# ------------------------------------------------------------ sincronização

FONTES = {"btg": ("fundos_btg", sincronizar_btg), "cvm": ("fundos_cvm", sincronizar_cvm)}


def _meta(conn, chave: str) -> str:
    linha = conn.execute("SELECT valor FROM app_meta WHERE chave=?", (chave,)).fetchone()
    return linha["valor"] if linha else ""


def _estado(conn) -> dict[str, Any]:
    estado = {}
    for fonte, (tabela, _) in FONTES.items():
        atualizado = _meta(conn, f"fundos_{fonte}_em")
        total = conn.execute(f"SELECT COUNT(*) AS n FROM {tabela}").fetchone()["n"]
        horas = None
        if atualizado:
            try:
                horas = (datetime.now() - datetime.fromisoformat(atualizado)).total_seconds() / 3600
            except ValueError:
                horas = None
        estado[fonte] = {
            "atualizadoEm": atualizado,
            "total": total,
            "vencido": not total or horas is None or horas > VALIDADE_HORAS,
            "sincronizando": bool(_sincronizando.get(fonte)),
        }
    return estado


def sincronizar(fontes: list[str] | None = None) -> dict[str, Any]:
    """Baixa os catálogos pedidos. Uma fonte por vez, sem enfileirar chamadas."""
    resultado: dict[str, Any] = {}
    for fonte in fontes or list(FONTES):
        if fonte not in FONTES:
            raise ValueError(f"Fonte desconhecida: {fonte}")
        with _trava:
            if _sincronizando.get(fonte):
                resultado[fonte] = {"jaEmAndamento": True}
                continue
            _sincronizando[fonte] = True
        try:
            resultado[fonte] = {"total": FONTES[fonte][1]()}
        except Exception as erro:                    # rede, formato do arquivo, disco
            resultado[fonte] = {"erro": str(erro)}
        finally:
            _sincronizando[fonte] = False
    return resultado


def _garantir_fonte(fonte: str) -> None:
    """Sincroniza sob demanda quando a cópia local está vazia ou vencida."""
    with fin.connect() as conn:
        garantir_tabelas(conn)
        vencido = _estado(conn)[fonte]["vencido"]
    if vencido and not _sincronizando.get(fonte):
        sincronizar([fonte])


# ---------------------------------------------------------------- consulta

def _linha_btg(conn, cnpj: str):
    return conn.execute("SELECT * FROM fundos_btg WHERE cnpj=?", (cnpj,)).fetchone()


def _btg_payload(linha, como: str) -> dict[str, Any]:
    return {
        "cnpj": linha["cnpj"],
        "cnpjFormatado": formatar_cnpj(linha["cnpj"]),
        "nome": linha["nome"],
        "nomeOriginal": linha["nome_original"],
        "url": PAGINA_BTG.format(slug=linha["slug"]),
        "categoria": linha["categoria"],
        "subcategoria": linha["subcategoria"],
        "risco": linha["risco"],
        "publico": linha["publico"],
        "aplicacaoMinima": linha["aplicacao_minima"],
        "diasResgate": linha["dias_resgate"],
        "como": como,
    }


def _cvm_payload(linha) -> dict[str, Any]:
    return {
        "cnpj": linha["cnpj"],
        "cnpjFormatado": formatar_cnpj(linha["cnpj"]),
        "tipo": linha["tipo"],
        "denominacao": linha["denominacao"],
        "situacao": linha["situacao"],
        "classificacao": linha["classificacao"],
        "classeAnbima": linha["classe_anbima"],
        "gestor": linha["gestor"],
        "administrador": linha["administrador"],
        "codigoCvm": linha["codigo_cvm"],
    }


def _parentes_cvm(conn, cvm) -> list[Any]:
    """Fundo e classes que compartilham o mesmo registro na CVM."""
    if not cvm or not cvm["registro_fundo"]:
        return []
    return list(conn.execute(
        "SELECT * FROM fundos_cvm WHERE registro_fundo=? AND cnpj<>? ORDER BY tipo,denominacao",
        (cvm["registro_fundo"], cvm["cnpj"])))


def _por_nome(conn, termo: str, limite: int = 8) -> list[Any]:
    """Fundos do catálogo do BTG cujo nome contém todas as palavras do termo."""
    palavras = [p for p in normalizar(termo).split() if len(p) > 2][:6]
    if not palavras:
        return []
    achados = []
    for linha in conn.execute("SELECT * FROM fundos_btg ORDER BY nome"):
        alvo = normalizar(f"{linha['nome']} {linha['nome_original']}")
        if all(p in alvo for p in palavras):
            achados.append(linha)
        if len(achados) >= limite:
            break
    return achados


def _resolver_cnpj(conn, cnpj: str, entrada: str) -> dict[str, Any]:
    resultado: dict[str, Any] = {
        "entrada": entrada,
        "cnpj": cnpj,
        "cnpjFormatado": formatar_cnpj(cnpj),
        "tipoEntrada": "cnpj",
        "cnpjInvalido": not cnpj_valido(cnpj),
        "cvm": None,
        "btg": None,
        "alternativas": [],
        "aviso": "",
    }
    cvm = conn.execute("SELECT * FROM fundos_cvm WHERE cnpj=?", (cnpj,)).fetchone()
    if cvm:
        resultado["cvm"] = _cvm_payload(cvm)
        # Um dígito trocado costuma cair num fundo antigo e encerrado. Dizer a
        # situação evita que um cadastro cancelado passe por indicação válida.
        if cvm["situacao"] and "normal" not in cvm["situacao"].lower():
            resultado["situacaoAtencao"] = cvm["situacao"]

    direto = _linha_btg(conn, cnpj)
    if direto:
        resultado["btg"] = _btg_payload(direto, "cnpj")
        return resultado

    # Sem acerto direto, a ponte fundo/classe da RCVM 175 costuma explicar.
    for parente in _parentes_cvm(conn, cvm):
        linha = _linha_btg(conn, parente["cnpj"])
        if not linha:
            continue
        como = "classe" if parente["tipo"] == "classe" else "fundo"
        payload_btg = _btg_payload(linha, como)
        payload_btg["parente"] = _cvm_payload(parente)
        if resultado["btg"] is None:
            resultado["btg"] = payload_btg
            resultado["aviso"] = (
                "O BTG lista a classe deste fundo, com outro CNPJ."
                if como == "classe" else
                "O BTG lista o fundo, não a classe deste CNPJ.")
        else:
            resultado["alternativas"].append(payload_btg)
    if resultado["btg"]:
        return resultado

    if cvm:
        resultado["alternativas"] = [_btg_payload(linha, "nome")
                                     for linha in _por_nome(conn, cvm["denominacao"])]
        resultado["aviso"] = (
            "Nenhum acerto por CNPJ. Os nomes abaixo são apenas semelhantes -- confirme o "
            "CNPJ na página antes de investir."
            if resultado["alternativas"] else
            "Não está no catálogo público do BTG por este CNPJ. Procure pela denominação da "
            "CVM na plataforma; fundo restrito a investidor qualificado pode aparecer só na "
            "área logada.")
    else:
        resultado["aviso"] = ("CNPJ não encontrado nem no catálogo do BTG nem no cadastro da "
                              "CVM. Confira se o número veio completo e correto.")
    return resultado


def _resolver_nome(conn, termo: str) -> dict[str, Any]:
    achados = _por_nome(conn, termo, limite=LIMITE_RESULTADOS)
    resultado: dict[str, Any] = {
        "entrada": termo,
        "tipoEntrada": "nome",
        "cvm": None,
        "btg": None,
        "alternativas": [_btg_payload(linha, "nome") for linha in achados],
        "aviso": "",
    }
    if not achados:
        parecidos = list(conn.execute(
            "SELECT * FROM fundos_cvm WHERE denominacao LIKE ? AND situacao NOT LIKE 'CANCELADA%' "
            "ORDER BY denominacao LIMIT 8", (f"%{termo.strip().upper()}%",)))
        resultado["parecidosCvm"] = [_cvm_payload(linha) for linha in parecidos]
        resultado["aviso"] = ("Nenhum fundo do catálogo do BTG com todas essas palavras."
                              if not parecidos else
                              "Nada no catálogo do BTG. A CVM tem nomes parecidos; "
                              "consulte pelo CNPJ deles.")
    elif len(achados) == 1:
        resultado["btg"] = resultado["alternativas"][0]
        resultado["alternativas"] = []
    return resultado


@fin.escopo_leitura
def payload(consulta: str = "") -> dict[str, Any]:
    """Resolve CNPJs (ou um nome) em páginas de fundo do BTG."""
    consulta = str(consulta or "").strip()
    _garantir_fonte("btg")
    if consulta:
        _garantir_fonte("cvm")
    cnpjs = cnpjs_do_texto(consulta)
    with fin.connect() as conn:
        garantir_tabelas(conn)
        resultados = []
        if cnpjs:
            for cnpj in cnpjs[:LIMITE_RESULTADOS]:
                resultados.append(_resolver_cnpj(conn, cnpj, formatar_cnpj(cnpj)))
        elif consulta:
            resultados.append(_resolver_nome(conn, consulta))
        return {
            "consulta": consulta,
            "resultados": resultados,
            "fontes": _estado(conn),
            "catalogoBtg": CATALOGO_BTG_TELA,
        }


def atualizar_payload(fontes: list[str] | None = None) -> dict[str, Any]:
    resultado = sincronizar(fontes)
    with fin.connect() as conn:
        garantir_tabelas(conn)
        return {"ok": all("erro" not in r for r in resultado.values()),
                "resultado": resultado, "fontes": _estado(conn)}
