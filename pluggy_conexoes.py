"""Autenticacao local para abrir o Pluggy Connect dentro do dashboard.

As credenciais ficam no .env do proprio projeto. Este modulo apenas as le em
memoria para gerar tokens curtos; nunca devolve client secret ao browser.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.pluggy.ai"
ENV_PATH = Path(__file__).resolve().parent / ".env"


def _ler_env_local() -> dict[str, str]:
    valores: dict[str, str] = {}
    if not ENV_PATH.exists():
        return valores
    for linha in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in {'"', "'"}:
            valor = valor[1:-1]
        valores[chave.strip()] = valor
    return valores


def _credenciais() -> tuple[str, str]:
    arquivo = _ler_env_local()
    client_id = os.getenv("PLUGGY_CLIENT_ID") or arquivo.get("PLUGGY_CLIENT_ID", "")
    client_secret = os.getenv("PLUGGY_CLIENT_SECRET") or arquivo.get("PLUGGY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            f"PLUGGY_CLIENT_ID/PLUGGY_CLIENT_SECRET nao encontrados em {ENV_PATH}"
        )
    return client_id, client_secret


def _post(caminho: str, payload: dict[str, Any], api_key: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key
    requisicao = Request(
        f"{API_URL}{caminho}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(requisicao, timeout=30) as resposta:  # noqa: S310 - URL fixa
            corpo = json.loads(resposta.read().decode("utf-8"))
    except HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Pluggy respondeu HTTP {exc.code}: {detalhe}") from exc
    except URLError as exc:
        raise RuntimeError(f"Nao consegui acessar a Pluggy: {exc.reason}") from exc
    if not isinstance(corpo, dict):
        raise RuntimeError("Resposta inesperada da Pluggy.")
    return corpo


def criar_connect_token() -> str:
    client_id, client_secret = _credenciais()
    autenticacao = _post(
        "/auth", {"clientId": client_id, "clientSecret": client_secret}
    )
    api_key = str(autenticacao.get("apiKey") or "")
    if not api_key:
        raise RuntimeError("A Pluggy nao devolveu apiKey.")

    conexao = _post(
        "/connect_token",
        {
            "options": {
                "clientUserId": "dashboard-financeiro-guilherme",
            }
        },
        api_key,
    )
    token = str(conexao.get("accessToken") or "")
    if not token:
        raise RuntimeError("A Pluggy nao devolveu connectToken.")
    return token
