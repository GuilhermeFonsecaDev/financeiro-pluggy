"""Séries do Banco Central (CDI, IPCA) guardadas aqui para comparar carteira.

"Rendeu 1,8%" não diz nada sozinho: 1,8% em um mês é ótimo, em um ano é ruim.
A régua é o CDI do mesmo intervalo, e ela vem do SGS do Banco Central -- de
graça, sem cadastro e sem mandar nada nosso para lá: a requisição é o número
da série e o intervalo de datas.

Duas decisões atravessam o arquivo:

1. **O payload nunca espera a rede.** Quem lê a tela lê a tabela local; baixar
   é trabalho de fundo, junto da sincronização da Pluggy. Banco Central fora do
   ar vira um comparativo ausente com motivo legível, nunca uma tela travada.
2. **Série incompleta não é extrapolada.** Se o CDI só existe até anteontem, a
   comparação é feita até anteontem e a janela real volta junto do número. O
   contrário -- repetir o último valor para "fechar" o período -- inventaria
   rendimento que ninguém teve.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import banco as fin

URL = ("https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados"
       "?formato=json&dataInicial={de}&dataFinal={ate}")

SERIES: dict[str, dict[str, str]] = {
    "12": {"nome": "CDI", "periodicidade": "diaria",
           "descricao": "CDI diário, em % ao dia"},
    "4389": {"nome": "CDI anualizado", "periodicidade": "diaria",
             "descricao": "CDI em % ao ano, base 252"},
    "433": {"nome": "IPCA", "periodicidade": "mensal",
            "descricao": "IPCA mensal, em %"},
}

PADRAO = "12"
TIMEOUT_SEGUNDOS = 60
# O SGS recusa janelas muito longas; dez anos por requisição passa folgado.
ANOS_POR_PEDIDO = 10
# Série diária só muda em dia útil. Tentar de novo a cada 6h evita insistir na
# API do Banco Central enquanto ela está fora.
HORAS_ENTRE_TENTATIVAS = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS indices_series (
  serie TEXT NOT NULL,
  data  TEXT NOT NULL,
  valor REAL NOT NULL,
  PRIMARY KEY (serie, data)
);
CREATE INDEX IF NOT EXISTS idx_indices_serie_data ON indices_series(serie, data);
"""

_trava = threading.Lock()


def garantir_tabelas(conn=None) -> None:
    if conn is not None:
        conn.executescript(SCHEMA)
        return
    with fin.connect() as conexao:
        conexao.executescript(SCHEMA)
        conexao.commit()


# ------------------------------------------------------------------- datas

def _hoje() -> date:
    return date.today()


def _iso(valor: Any) -> str:
    return str(valor or "")[:10]


def dia_util_anterior(base: date | None = None) -> date:
    """Último dia em que o CDI pode ter sido publicado.

    Não conhecemos feriados, então em semana de feriado a série parece atrasada
    e o estado fica "parcial" -- que é justamente o que ela é.
    """
    dia = (base or _hoje()) - timedelta(days=1)
    while dia.weekday() >= 5:
        dia -= timedelta(days=1)
    return dia


def _br(valor: date) -> str:
    return valor.strftime("%d/%m/%Y")


# ------------------------------------------------------------------ coleta

def _baixar(url: str) -> bytes:
    pedido = urllib.request.Request(url, headers={"User-Agent": "FinanceiroPluggy/1.0"})
    with urllib.request.urlopen(pedido, timeout=TIMEOUT_SEGUNDOS) as resposta:
        return resposta.read()


def _ponto(bruto: dict[str, Any]) -> tuple[str, float] | None:
    """Um ponto do SGS vira (ISO, número) -- ou nada, se vier estranho.

    O SGS manda `valor` como texto e a data em dd/MM/yyyy. Ponto malformado é
    descartado em silêncio: uma linha ruim não pode derrubar a série inteira.
    """
    try:
        quando = datetime.strptime(str(bruto["data"]), "%d/%m/%Y").date()
        valor = float(str(bruto["valor"]).replace(",", "."))
    except (KeyError, TypeError, ValueError):
        return None
    return (quando.isoformat(), valor)


def baixar(serie: str, de: date, ate: date) -> list[tuple[str, float]]:
    """Pontos da série no intervalo, em pedaços que o SGS aceita."""
    pontos: list[tuple[str, float]] = []
    inicio = de
    while inicio <= ate:
        fim = min(ate, inicio.replace(year=inicio.year + ANOS_POR_PEDIDO))
        bruto = _baixar(URL.format(serie=serie, de=_br(inicio), ate=_br(fim)))
        texto = bruto.decode("utf-8").strip()
        # Intervalo sem publicação devolve corpo vazio, não JSON vazio.
        dados = json.loads(texto) if texto else []
        for item in dados if isinstance(dados, list) else []:
            ponto = _ponto(item)
            if ponto:
                pontos.append(ponto)
        inicio = fim + timedelta(days=1)
    return pontos


def _meta(conn, chave: str) -> str:
    linha = conn.execute("SELECT valor FROM app_meta WHERE chave=?", (chave,)).fetchone()
    return linha[0] if linha else ""


def _gravar_meta(conn, chave: str, valor: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_meta VALUES (?,?)", (chave, valor))


def ultima_data(conn, serie: str) -> str:
    linha = conn.execute("SELECT MAX(data) FROM indices_series WHERE serie=?", (serie,)).fetchone()
    return linha[0] or "" if linha else ""


def sincronizar(series: list[str] | None = None, desde: date | None = None) -> dict[str, Any]:
    """Completa as séries até hoje. Incremental: só pede o que falta."""
    if not _trava.acquire(blocking=False):
        return {"ok": True, "resultado": "ja_rodando"}
    try:
        garantir_tabelas()
        resultado: dict[str, Any] = {}
        hoje = _hoje()
        for serie in series or [PADRAO]:
            if serie not in SERIES:
                resultado[serie] = {"erro": "série desconhecida"}
                continue
            with fin.connect() as conn:
                garantir_tabelas(conn)
                ultima = ultima_data(conn, serie)
            inicio = (datetime.strptime(ultima, "%Y-%m-%d").date() + timedelta(days=1)
                      if ultima else (desde or hoje.replace(year=hoje.year - 5)))
            if inicio > hoje:
                resultado[serie] = {"pontos": 0, "resultado": "em_dia"}
                continue
            agora = datetime.now().isoformat(timespec="seconds")
            try:
                pontos = baixar(serie, inicio, hoje)
            except Exception as erro:               # rede, formato, indisponibilidade
                with fin.connect() as conn:
                    garantir_tabelas(conn)
                    _gravar_meta(conn, f"indices_{serie}_tentativa_em", agora)
                    _gravar_meta(conn, f"indices_{serie}_erro", str(erro)[:300])
                    conn.commit()
                resultado[serie] = {"erro": str(erro)}
                continue
            with fin.connect() as conn:
                garantir_tabelas(conn)
                conn.executemany(
                    "INSERT OR REPLACE INTO indices_series (serie, data, valor) VALUES (?,?,?)",
                    [(serie, d, v) for d, v in pontos])
                _gravar_meta(conn, f"indices_{serie}_em", agora)
                _gravar_meta(conn, f"indices_{serie}_tentativa_em", agora)
                _gravar_meta(conn, f"indices_{serie}_erro", "")
                conn.commit()
            resultado[serie] = {"pontos": len(pontos)}
        return {"ok": all("erro" not in r for r in resultado.values()), "resultado": resultado}
    finally:
        _trava.release()


def precisa_atualizar(conn, serie: str = PADRAO) -> bool:
    """Vale bater na API agora?

    Só quando a série está atrasada em relação ao último dia útil **e** a última
    tentativa já tem algumas horas -- senão uma indisponibilidade do Banco
    Central viraria uma rajada de pedidos a cada carga de tela.
    """
    if ultima_data(conn, serie) >= dia_util_anterior().isoformat():
        return False
    tentativa = _meta(conn, f"indices_{serie}_tentativa_em")
    if not tentativa:
        return True
    try:
        quando = datetime.fromisoformat(tentativa)
    except ValueError:
        return True
    return (datetime.now() - quando).total_seconds() / 3600 >= HORAS_ENTRE_TENTATIVAS


def atualizar_em_background(series: list[str] | None = None) -> threading.Thread | None:
    """Dispara a coleta sem segurar quem chamou. Já rodando, não empilha."""
    if _trava.locked():
        return None
    thread = threading.Thread(target=sincronizar, args=(series,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- consulta

def estado(conn=None) -> dict[str, Any]:
    def ler(conexao):
        garantir_tabelas(conexao)
        saida = {}
        for serie, info in SERIES.items():
            ultima = ultima_data(conexao, serie)
            total = conexao.execute("SELECT COUNT(*) FROM indices_series WHERE serie=?",
                                    (serie,)).fetchone()[0]
            saida[serie] = {
                "nome": info["nome"], "pontos": total, "ultimaData": ultima,
                "atualizadoEm": _meta(conexao, f"indices_{serie}_em"),
                "erro": _meta(conexao, f"indices_{serie}_erro"),
                "vencido": ultima < dia_util_anterior().isoformat() if ultima else True,
            }
        return saida

    if conn is not None:
        return ler(conn)
    with fin.connect() as conexao:
        return ler(conexao)


def fator(conn, serie: str, de: str, ate: str) -> dict[str, Any]:
    """Quanto 1 real viraria na série, no intervalo.

    `status`: `ok` quando a série cobre o pedido, `parcial` quando termina
    antes, `indisponivel` quando não há ponto nenhum. A janela devolvida é a
    que foi de fato usada -- é ela que a tela deve mostrar ao lado do número.
    """
    de, ate = _iso(de), _iso(ate)
    try:
        garantir_tabelas(conn)
        linhas = list(conn.execute(
            "SELECT data, valor FROM indices_series WHERE serie=? AND data>=? AND data<=? "
            "ORDER BY data", (serie, de, ate)))
    except Exception:
        linhas = []
    if not linhas:
        return {"fator": None, "pontos": 0, "status": "indisponivel",
                "janela": {"de": de, "ate": ate},
                "motivo": "sem série do índice no período"}
    acumulado = 1.0
    for _, valor in linhas:
        acumulado *= 1 + (valor or 0) / 100
    ultima = linhas[-1][0]
    # Faltando o fim do intervalo, comparamos até onde a série vai. Repetir o
    # último valor para cobrir o resto inventaria rendimento.
    parcial = ultima < ate
    return {
        "fator": acumulado,
        "variacao": round((acumulado - 1) * 100, 4),
        "pontos": len(linhas),
        "status": "parcial" if parcial else "ok",
        "janela": {"de": linhas[0][0], "ate": ultima},
        "motivo": f"série do índice disponível até {ultima}" if parcial else "",
    }


def comparar(retorno_carteira: float | None, fator_indice: dict[str, Any]) -> dict[str, Any]:
    """Compara o retorno da carteira com o do índice, no mesmo intervalo.

    Devolve sempre `excessoPP` (diferença em pontos percentuais) porque ele
    funciona também com retorno negativo -- situação em que "% do CDI" perde o
    sentido e por isso vem `None`.
    """
    variacao = fator_indice.get("variacao")
    base = {"indice": fator_indice.get("nome") or SERIES.get(PADRAO, {}).get("nome", "CDI"),
            "status": fator_indice.get("status", "indisponivel"),
            "janela": fator_indice.get("janela"),
            "noPeriodo": variacao,
            "motivo": fator_indice.get("motivo", "")}
    if retorno_carteira is None or variacao is None:
        return {**base, "percentualDoIndice": None, "excessoPP": None}
    excesso = round(retorno_carteira - variacao, 4)
    percentual = (round(retorno_carteira / variacao * 100, 2)
                  if variacao > 0 and retorno_carteira > 0 else None)
    return {**base, "percentualDoIndice": percentual, "excessoPP": excesso}
