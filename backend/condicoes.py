"""Condições de casamento com E / OU / NÃO.

Uma regra (do extrato, de entrada ou o vínculo de uma conta fixa) é uma lista
ordenada de condições. Cada uma tem termo, operador e a ligação com a
ANTERIOR ("E" ou "OU"; a primeira ignora a sua).

Leitura igual à do SQL: o E vale antes do OU. "A E B OU C" é "(A e B) ou C".

Regras antigas não mudam: os termos delas ficam ligados por OU, que é como
sempre funcionaram.

Duas proteções:
- Precisa existir ao menos uma condição positiva (contém, começa com,
  igual). Uma regra só de "não contém" casaria com quase tudo.
- Número com pontuação ("123.456.789-00") casa com o mesmo número sem ela.
  O banco manda documentos formatados e quem cadastra costuma digitar só os
  dígitos.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

OPERADORES = ("contem", "nao_contem", "comeca_com", "igual")
LIGACOES = ("E", "OU")

_SEPARADOR_ENTRE_DIGITOS = re.compile(r"(?<=\d)[.\-/](?=\d)")


def _normalizar(texto: object) -> str:
    # Importado aqui para o módulo não depender da ordem de import do backend.
    from extrato_camada import normalizar
    return normalizar(texto)


@lru_cache(maxsize=32768)
def preparar(texto: str) -> str:
    """Texto normalizado e com os separadores entre dígitos removidos."""
    return _SEPARADOR_ENTRE_DIGITOS.sub("", _normalizar(texto))


def _casa_uma(alvo: str, operador: str, termo: str) -> bool:
    if operador == "igual":
        return alvo == termo
    if operador == "comeca_com":
        return alvo.startswith(termo)
    return termo in alvo          # contem e nao_contem (a negação vem depois)


def casa(texto: object, condicoes, operador_padrao: str = "contem",
         preparar_termo=None) -> bool:
    """Avalia as condições contra o texto.

    condicoes: [(termo, operador, ligacao)] ou [{"termo", "operador", "ligacao"}].
    Operador vazio usa operador_padrao (o da regra, nas regras antigas).
    preparar_termo: ajuste extra do termo (as fixas tiram o "01/05" da parcela).
    """
    alvo = preparar(str(texto or ""))
    grupos: list[list[bool]] = []
    positiva = False
    for item in condicoes or []:
        if isinstance(item, dict):
            termo, operador, ligacao = item.get("termo"), item.get("operador"), item.get("ligacao")
        elif isinstance(item, str):
            termo, operador, ligacao = item, None, None
        else:
            termo, operador, ligacao = (list(item) + [None, None])[:3]
        termo = str(termo or "")
        if preparar_termo:
            termo = preparar_termo(termo)
        termo = preparar(termo)
        if not termo:
            continue                                  # condição vazia não conta
        operador = operador if operador in OPERADORES else operador_padrao
        if operador not in OPERADORES:
            operador = "contem"
        resultado = _casa_uma(alvo, operador, termo)
        if operador == "nao_contem":
            resultado = not resultado
        else:
            positiva = True
        if grupos and ligacao == "E":
            grupos[-1].append(resultado)
        else:
            grupos.append([resultado])
    if not positiva:
        return False
    return any(all(g) for g in grupos)


@lru_cache(maxsize=4096)
def _ler_json(texto: str) -> tuple:
    try:
        bruto = json.loads(texto)
    except (TypeError, ValueError):
        return ()
    saida = []
    for item in bruto if isinstance(bruto, list) else []:
        if isinstance(item, dict):
            saida.append((item.get("termo"), item.get("operador"), item.get("ligacao")))
        elif isinstance(item, (list, tuple)):
            saida.append(tuple((list(item) + [None, None, None])[:3]))
    return tuple(saida)


def casa_sql(operador_padrao, condicoes_json, texto) -> int:
    """Versão para o SQLite: casa_cond(operador_da_regra, json, texto)."""
    if not condicoes_json:
        return 0
    return 1 if casa(texto, _ler_json(condicoes_json), operador_padrao or "contem") else 0


def registrar(conn) -> None:
    conn.create_function("casa_cond", 3, casa_sql, deterministic=True)


def limpar(recebidas, operador_padrao: str = "contem", limite: int = 240) -> list[dict]:
    """Valida o que veio da tela: sem vazios, operador e ligação conhecidos,
    sem repetir (termo, operador). A primeira fica sempre com OU."""
    saida, vistos = [], set()
    for item in recebidas or []:
        if isinstance(item, str):
            item = {"termo": item}
        if not isinstance(item, dict):
            continue
        termo = str(item.get("termo") or "").strip()[:limite]
        if not termo:
            continue
        operador = item.get("operador") if item.get("operador") in OPERADORES else operador_padrao
        ligacao = item.get("ligacao") if item.get("ligacao") in LIGACOES else "OU"
        chave = (_normalizar(termo), operador)
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append({"termo": termo, "operador": operador, "ligacao": ligacao if saida else "OU"})
    if saida and not any(c["operador"] != "nao_contem" for c in saida):
        raise ValueError("Inclua pelo menos uma condição que não seja \"não contém\".")
    return saida


def descrever(condicoes) -> str:
    """Frase curta para listas: contém "A" E não contém "B"."""
    nomes = {"contem": "contém", "nao_contem": "não contém", "comeca_com": "começa com", "igual": "é igual a"}
    partes = []
    for i, c in enumerate(condicoes or []):
        if i:
            partes.append(c.get("ligacao") or "OU")
        partes.append(f'{nomes.get(c.get("operador"), "contém")} "{c.get("termo")}"')
    return " ".join(partes)
