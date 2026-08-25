#!/usr/bin/env python3
"""
pluggy_sync.py
==============

Script pessoal para conectar sua conta bancaria via Open Finance usando a API
da Pluggy (https://pluggy.ai) e baixar contas/transacoes para arquivos locais
(CSV e JSON) dentro da pasta ./data/.

Fluxo de uso
------------

1) Copie ".env.example" para ".env" e preencha PLUGGY_CLIENT_ID e
   PLUGGY_CLIENT_SECRET (obtidos em https://dashboard.pluggy.ai).

2) Instale as dependencias:
       pip install -r requirements.txt

3) Gere a pagina de conexao:
       python pluggy_sync.py connect

   Isso cria um arquivo "connect.html". Abra-o no navegador, escolha seu
   banco (ou "Pluggy Bank" para testar no sandbox) e autorize o acesso.
   Ao final, a propria pagina mostra o ITEM_ID da conexao criada -- copie-o.

   O token de conexao expira em 30 minutos, entao gere a pagina de novo caso
   demore para concluir a autorizacao.

4) Baixe os dados dessa conexao:
       python pluggy_sync.py sync <ITEM_ID>

   Opcionalmente informe o periodo das transacoes:
       python pluggy_sync.py sync <ITEM_ID> --from 2025-01-01 --to 2026-08-15

   Os arquivos serao salvos em ./data/ (accounts.csv/json e
   transactions_<accountId>.csv/json). Essa pasta e o .env NAO devem ser
   versionados nem compartilhados -- contem dados financeiros pessoais.

Este script guarda as credenciais apenas em variaveis de ambiente (.env
local) e nunca as expoe em texto fixo no codigo.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, timedelta
from typing import Any, Optional

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Preso ao diretorio do script, nao ao diretorio atual: o backend chama este
# arquivo como subprocesso e o usuario pode roda-lo de qualquer lugar.
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_URL = "https://api.pluggy.ai"
# Endpoint de configuracao da aplicacao, consumido pelo proprio widget.
# Autentica com "Authorization: Bearer <connectToken>" (nao aceita X-API-KEY).
CONNECT_CONFIG_URL = "https://auth.pluggy.ai/connect/config"
CLIENT_ID = os.getenv("PLUGGY_CLIENT_ID")
CLIENT_SECRET = os.getenv("PLUGGY_CLIENT_SECRET")

DATA_DIR = os.path.join(BASE_DIR, "data")

# Widget version do Pluggy Connect servido via CDN.
# Pode trocar por "latest" se preferir sempre a versao mais recente.
PLUGGY_CONNECT_JS = "https://cdn.pluggy.ai/pluggy-connect/v2.7.0/pluggy-connect.js"


# --------------------------------------------------------------------------
# Autenticacao
# --------------------------------------------------------------------------

def get_api_key() -> str:
    """Troca CLIENT_ID/CLIENT_SECRET por um apiKey (valido por ~2h)."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit(
            "Defina PLUGGY_CLIENT_ID e PLUGGY_CLIENT_SECRET no arquivo .env "
            "(veja .env.example). Crie suas credenciais em "
            "https://dashboard.pluggy.ai"
        )
    resp = requests.post(
        f"{API_URL}/auth",
        json={"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["apiKey"]


def create_connect_token(api_key: str, item_id: Optional[str] = None) -> str:
    """Cria um connect token (escopo limitado, valido por ~30min) para o
    widget PluggyConnect. Se item_id for informado, o token permite
    atualizar/reautorizar aquela conexao especifica."""
    payload: dict[str, Any] = {}
    if item_id:
        payload["itemId"] = item_id
    resp = requests.post(
        f"{API_URL}/connect_token",
        headers={"X-API-KEY": api_key},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


# --------------------------------------------------------------------------
# Comando: connect  -> gera o connect.html
# --------------------------------------------------------------------------

CONNECT_HTML_TEMPLATE = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8" />
<title>Conectar banco via Pluggy</title>
<script src="__PLUGGY_CONNECT_JS__"></script>
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 48px auto; padding: 0 16px; }
  button { font-size: 16px; padding: 10px 18px; cursor: pointer; }
  pre { background: #f4f4f4; padding: 12px; border-radius: 6px; white-space: pre-wrap; }
  .hint { color: #555; }
</style>
</head>
<body>
  <h1>Conectar minha conta bancaria (Open Finance)</h1>
  <p class="hint">Clique no botao, escolha o seu banco e siga o fluxo de autorizacao.
  Este token expira em 30 minutos.</p>
  <button id="start">Conectar banco</button>
  <h2>Resultado</h2>
  <pre id="result">(aguardando...)</pre>

  <script>
    const resultEl = document.getElementById('result');

    document.getElementById('start').addEventListener('click', () => {
      const pluggyConnect = new PluggyConnect({
        connectToken: '__CONNECT_TOKEN__',
        includeSandbox: true, // mostra tambem os conectores de teste "Pluggy Bank"
        language: 'pt',
        __EXTRA_OPTIONS__
        onSuccess: (itemData) => {
          resultEl.textContent =
            'Conexao criada com sucesso!\\n\\n' +
            'ITEM_ID: ' + itemData.item.id + '\\n\\n' +
            'Copie o ITEM_ID acima e rode no terminal:\\n' +
            'python pluggy_sync.py sync ' + itemData.item.id;
        },
        onError: (error) => {
          resultEl.textContent = 'Erro na conexao: ' + JSON.stringify(error, null, 2);
        },
      });
      pluggyConnect.init();
    });
  </script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Comando: check -> testa credenciais e mostra o que a conta ainda permite
# --------------------------------------------------------------------------

def cmd_check() -> None:
    """Diagnostico: valida as credenciais e mostra quais conectores estao
    disponiveis para a sua conta. Util para descobrir, apos o fim do trial,
    se ainda da para conectar bancos reais ou apenas o sandbox."""
    print("1) Testando /auth com as credenciais do .env ...")
    try:
        api_key = get_api_key()
    except requests.HTTPError as exc:
        print(f"   FALHOU (HTTP {exc.response.status_code if exc.response is not None else '?'})")
        if exc.response is not None:
            print(f"   Resposta da API: {exc.response.text[:500]}")
        print(
            "\n   Possiveis causas:\n"
            "     401/403 -> CLIENT_ID ou CLIENT_SECRET incorretos, ou aplicacao\n"
            "                desativada/sem plano ativo (trial expirado).\n"
            "     4xx     -> verifique se copiou os valores completos, sem espacos."
        )
        return
    except requests.RequestException as exc:
        print(f"   FALHOU: nao foi possivel alcancar {API_URL}")
        print(f"   Detalhe: {type(exc).__name__}: {exc}")
        print(
            "\n   Isso e erro de REDE, nao de credencial. Verifique conexao,\n"
            "   proxy corporativo, VPN ou firewall bloqueando api.pluggy.ai."
        )
        return
    print("   OK - apiKey gerada com sucesso (credenciais validas).")

    for label, params in (("reais (Open Finance / bancos)", {}),
                          ("sandbox (dados ficticios)", {"sandbox": "true"})):
        print(f"\n2) Listando conectores {label} ...")
        try:
            resp = requests.get(
                f"{API_URL}/connectors",
                headers={"X-API-KEY": api_key},
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"   FALHOU: {type(exc).__name__}: {exc}")
            resp_obj = getattr(exc, "response", None)
            if resp_obj is not None:
                print(f"   Resposta da API: {resp_obj.text[:500]}")
            continue
        results = resp.json().get("results", [])
        print(f"   {len(results)} conector(es) disponivel(is).")
        for c in results[:10]:
            flags = []
            if c.get("isSandbox"):
                flags.append("sandbox")
            if c.get("isOpenFinance"):
                flags.append("open finance")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"     - {c.get('id')}: {c.get('name')}{suffix}")
        if len(results) > 10:
            print(f"     ... e mais {len(results) - 10}")

    print(
        "\nInterpretacao:\n"
        "  - Se aparecem conectores mas a criacao de item falhar com erro de\n"
        "    plano/assinatura, o trial expirado esta bloqueando conexoes reais.\n"
        "  - O conector 'Pluggy Bank' (sandbox) normalmente continua liberado\n"
        "    e serve para validar o script de ponta a ponta sem custo.\n"
        "  - Para dados bancarios reais e pessoais sem plano pago, veja a secao\n"
        "    'Meu Pluggy' no README."
    )


CONNECTOR_MEUPLUGGY = 200
CONNECTOR_PLUGGY_BANK = 2


# --------------------------------------------------------------------------
# Comando: inspect -> dump dos metadados de um conector
# --------------------------------------------------------------------------

def cmd_inspect(connector_id: Optional[int]) -> None:
    """Mostra os metadados completos de um conector (type, products, etc).
    Serve para descobrir por que o widget recusa um connector id."""
    api_key = get_api_key()

    if connector_id is None:
        print("Conectores visiveis para esta aplicacao (com o campo 'type'):\n")
        for params in ({}, {"sandbox": "true"}):
            resp = requests.get(
                f"{API_URL}/connectors",
                headers={"X-API-KEY": api_key},
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            for c in resp.json().get("results", []):
                print(
                    f"  id={c.get('id'):<5} type={str(c.get('type')):<20} "
                    f"sandbox={str(c.get('isSandbox')):<6} "
                    f"openFinance={str(c.get('isOpenFinance')):<6} {c.get('name')}"
                )
        return

    print(f"Metadados completos do conector {connector_id}:\n")
    resp = requests.get(
        f"{API_URL}/connectors/{connector_id}",
        headers={"X-API-KEY": api_key},
        timeout=30,
    )
    if resp.status_code == 404:
        print(f"  Conector {connector_id} nao encontrado para esta aplicacao.")
        return
    resp.raise_for_status()
    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
    print("\n--- resumo ---")
    print(f"  type         : {data.get('type')}")
    print(f"  isSandbox    : {data.get('isSandbox')}")
    print(f"  isOpenFinance: {data.get('isOpenFinance')}")
    print(f"  products     : {data.get('products')}")
    print(
        "\nSe 'type' for diferente de PERSONAL_BANK/BUSINESS_BANK, o widget pode\n"
        "estar filtrando esse conector. Nesse caso rode:\n"
        f"  python pluggy_sync.py connect --connector {connector_id} --types {data.get('type')}"
    )


# --------------------------------------------------------------------------
# Comando: config -> mostra a configuracao da aplicacao usada pelo widget
# --------------------------------------------------------------------------

def cmd_config() -> None:
    """Baixa a configuracao que o widget PluggyConnect carrega para esta
    aplicacao. E aqui que mora o 'connectorsFilters.ids': uma allowlist de
    conectores definida no lado do servidor. Conector fora dessa lista some
    do widget mesmo aparecendo em GET /connectors."""
    api_key = get_api_key()
    token = create_connect_token(api_key)

    resp = requests.get(
        CONNECT_CONFIG_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if not resp.ok:
        print(f"FALHOU (HTTP {resp.status_code}): {resp.text[:400]}")
        return
    cfg = resp.json()

    ids = (cfg.get("connectorsFilters") or {}).get("ids") or []
    print("Configuracao da aplicacao (a mesma que o widget carrega):\n")
    print(f"  environment        : {cfg.get('environment')}")
    print(f"  isProductionEnabled: {cfg.get('isProductionEnabled')}")
    print(f"  companyName        : {cfg.get('companyName')}")
    print(f"  itemsCreateLimit   : {cfg.get('itemsCreateLimit')}")
    print(f"  totalItems         : {cfg.get('totalItems')}")
    print(f"  connectorsFilters  : {len(ids)} id(s) permitido(s)")
    if ids:
        print(f"    faixa: {min(ids)} a {max(ids)}")
        liberado = CONNECTOR_MEUPLUGGY in ids
    else:
        print("    allowlist vazia -> filtro desligado, todo conector passa")
        liberado = True
    print(f"    MeuPluggy ({CONNECTOR_MEUPLUGGY}) liberado no widget? "
          f"{'SIM' if liberado else 'NAO'}")

    print(
        "\nComo o widget filtra (logica extraida do bundle do Connect):\n"
        "    passaNaApp   = allowlist vazia OU allowlist contem o id\n"
        "    passaNaProp  = sem connectorIds OU connectorIds contem o id\n"
        "    passaSandbox = includeSandbox E conector e sandbox\n"
        "    exibe        = (passaNaApp E passaNaProp) OU passaSandbox\n"
        "\nOu seja: conectores sandbox furam a allowlist; conectores reais nao.\n"
        "Se a allowlist for nao-vazia e nao contiver o seu conector, nenhuma\n"
        "opcao do widget (selectedConnectorId, connectorIds, connectorTypes)\n"
        "resolve -- a allowlist precisa ser alterada no dashboard."
    )


# --------------------------------------------------------------------------
# Comando: item -> sonda o estado de uma conexao
# --------------------------------------------------------------------------

def cmd_item(item_id: str) -> None:
    """Mostra o que a API responde para um item AGORA: status, quantas contas
    e quantas transacoes.

    Serve para o teste de retencao: rode logo depois de autorizar e de novo
    umas horas depois. Se as contas cairem para 0 sem o item mudar de status,
    e a plataforma removendo os dados, nao problema de codigo.
    """
    api_key = get_api_key()
    agora = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Sonda de {agora}\n")

    resp = requests.get(f"{API_URL}/items/{item_id}",
                        headers={"X-API-KEY": api_key}, timeout=30)
    if not resp.ok:
        print(f"  /items/{item_id} -> HTTP {resp.status_code}")
        print(f"  {resp.text[:300]}")
        return
    item = resp.json()
    conector = item.get("connector") or {}
    print(f"  item            : {item_id}")
    print(f"  status          : {item.get('status')} / {item.get('executionStatus')}")
    print(f"  erro            : {item.get('error')}")
    print(f"  conector        : {conector.get('id')} {conector.get('name')}")
    print(f"  ultima atualiz. : {item.get('lastUpdatedAt')}")

    resp = requests.get(f"{API_URL}/accounts", headers={"X-API-KEY": api_key},
                        params={"itemId": item_id}, timeout=30)
    contas = resp.json().get("results", []) if resp.ok else []
    print(f"\n  contas          : {len(contas)}")

    total_tx = 0
    for conta in contas:
        r = requests.get(f"{API_URL}/transactions", headers={"X-API-KEY": api_key},
                         params={"accountId": conta["id"], "pageSize": 1}, timeout=30)
        n = r.json().get("total", 0) if r.ok else 0
        total_tx += n
        print(f"    - {conta.get('type')}/{conta.get('subtype')}: {n} transacao(oes)")

    print("\n  VEREDITO:", end=" ")
    if contas and total_tx:
        print("dados presentes e acessiveis.")
    elif item.get("status") == "UPDATED":
        print("item saudavel MAS sem dados -- a plataforma removeu o acesso.")
        print("  Nao e problema do script. Ver secao 5.4-bis do HANDOFF.md.")
    else:
        print("item ainda nao terminou de sincronizar; rode de novo em instantes.")


def _serve_connect_page(html: str, port: int) -> None:
    """Serve a pagina em http://localhost:<port> a partir de um diretorio
    temporario isolado.

    Importante: NAO servimos a pasta do projeto, que contem o .env com as
    credenciais. Apenas um diretorio temporario com o connect.html dentro.
    """
    import http.server
    import socketserver
    import tempfile
    import threading
    import webbrowser

    tmpdir = tempfile.mkdtemp(prefix="pluggy_connect_")
    page = os.path.join(tmpdir, "index.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=tmpdir, **kw)  # noqa: E731

    class QuietTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        httpd = QuietTCPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"Nao foi possivel abrir a porta {port}: {exc}")
        print("Tente outra porta com --serve <PORTA>.")
        return

    url = f"http://localhost:{port}/"
    print(f"Servindo em {url}  (origin http, compativel com OAuth)")
    print("Diretorio servido (isolado, sem o .env):", tmpdir)
    print("Pressione Ctrl+C para encerrar quando terminar a autorizacao.\n")

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        webbrowser.open(url)
    except Exception:
        print(f"Abra manualmente: {url}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nEncerrando servidor.")
        httpd.shutdown()


def cmd_connect(
    connector_id: Optional[int] = None,
    update_item: Optional[str] = None,
    connector_types: Optional[list[str]] = None,
    filter_only: bool = False,
    serve_port: Optional[int] = None,
) -> None:
    api_key = get_api_key()
    token = create_connect_token(api_key, item_id=update_item)

    extra: list[str] = []
    if connector_types:
        types_js = ", ".join(f"'{t}'" for t in connector_types)
        extra.append(f"connectorTypes: [{types_js}],")
    if connector_id:
        if filter_only:
            # Restringe a lista a esse conector, sem pular a tela de selecao.
            extra.append(f"connectorIds: [{connector_id}],")
        else:
            # Pre-seleciona o conector, pulando a tela de busca de instituicao.
            extra.append(f"selectedConnectorId: {connector_id},")
    if update_item:
        extra.append(f"updateItem: '{update_item}',")

    html = (
        CONNECT_HTML_TEMPLATE
        .replace("__CONNECT_TOKEN__", token)
        .replace("__PLUGGY_CONNECT_JS__", PLUGGY_CONNECT_JS)
        .replace("__EXTRA_OPTIONS__", "\n        ".join(extra))
    )
    out_path = os.path.join(BASE_DIR, "connect.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    if serve_port:
        _serve_connect_page(html, serve_port)
        return

    print(f"Arquivo gerado: {out_path}")
    print(
        "ATENCAO: abrir por duplo clique usa origin 'file://', que quebra\n"
        "conectores OAuth (como o MeuPluggy). Prefira:\n"
        "    python pluggy_sync.py connect --meupluggy --serve"
    )
    if connector_id == CONNECTOR_MEUPLUGGY:
        print("Conector pre-selecionado: MeuPluggy (200).")
        print("Faca login com a conta que voce criou em https://meu.pluggy.ai")
        print("-> Conecte seus bancos reais LA primeiro; aqui voce so puxa o que ja agregou.")
    elif connector_id == CONNECTOR_PLUGGY_BANK:
        print("Conector pre-selecionado: Pluggy Bank (sandbox, dados ficticios).")
        print("Use as credenciais de teste indicadas pelo proprio widget.")
    else:
        print("Abra o arquivo no navegador e escolha a instituicao.")
    print("O token expira em 30 minutos -- rode 'connect' de novo se precisar.")


# --------------------------------------------------------------------------
# Comando: sync -> baixa contas e transacoes de um item ja conectado
# --------------------------------------------------------------------------

TERMINAL_STATUSES = {"UPDATED", "LOGIN_ERROR", "OUTDATED", "INVALID_CREDENTIALS"}
WAITING_STATUSES = {"UPDATING", "CREATING", "LOGIN_IN_PROGRESS", "WAITING_USER_INPUT"}


def wait_item_ready(api_key: str, item_id: str, timeout: int = 180, interval: int = 5) -> dict:
    """Espera o item sair de status transitorio (sincronizando) antes de
    buscar contas/transacoes."""
    deadline = time.time() + timeout
    while True:
        resp = requests.get(
            f"{API_URL}/items/{item_id}",
            headers={"X-API-KEY": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        item = resp.json()
        status = item.get("status")
        print(f"  status do item: {status}")
        if status not in WAITING_STATUSES or time.time() > deadline:
            return item
        time.sleep(interval)


def _paginated_get(api_key: str, path: str, params: dict) -> list[dict]:
    results: list[dict] = []
    page = 1
    while True:
        query = dict(params, page=page, pageSize=100)
        resp = requests.get(
            f"{API_URL}{path}",
            headers={"X-API-KEY": api_key},
            params=query,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        page_results = body.get("results", [])
        results.extend(page_results)
        total_pages = body.get("totalPages", 1)
        if page >= total_pages or not page_results:
            break
        page += 1
    return results


def list_accounts(api_key: str, item_id: str) -> list[dict]:
    return _paginated_get(api_key, "/accounts", {"itemId": item_id})


def list_transactions(api_key: str, account_id: str, date_from: str, date_to: str) -> list[dict]:
    return _paginated_get(
        api_key,
        "/transactions",
        {"accountId": account_id, "from": date_from, "to": date_to},
    )


def _tem_conteudo(path: str) -> bool:
    """True se o arquivo ja existe e guarda pelo menos um registro."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            return bool(json.load(f))
    except (ValueError, OSError):
        return False


def _save_json(records: list[dict] | dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _save_csv(records: list[dict], path: str) -> None:
    if not records:
        return
    # Usa a uniao de todas as chaves encontradas, ja que o schema pode variar
    # entre bancos/conectores.
    fieldnames: list[str] = []
    seen = set()
    for r in records:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            flat = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                    for k, v in r.items()}
            writer.writerow(flat)


def cmd_sync(item_id: str, date_from: str, date_to: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    api_key = get_api_key()

    print(f"Verificando status do item {item_id} ...")
    item = wait_item_ready(api_key, item_id)
    if item.get("status") not in {"UPDATED", "OUTDATED"}:
        print(f"Aviso: item terminou com status '{item.get('status')}'.")
        error = item.get("error")
        if error:
            print(f"Detalhes: {error}")

    print("Baixando contas ...")
    accounts = list_accounts(api_key, item_id)

    # Guarda contra perda de dados: ja aconteceu de a API responder 200 com zero
    # contas para um item saudavel (status UPDATED). Se gravassemos isso, o
    # accounts.json bom seria substituido por [] e todas as transacoes ja
    # baixadas ficariam orfas na importacao. Melhor abortar sem tocar em nada.
    if not accounts:
        raise SystemExit(
            "ERRO: a API retornou 0 contas para esse item.\n"
            "  Nada foi gravado -- os arquivos em ./data/ continuam intactos.\n"
            "  O item pode estar sem contas do lado do Meu Pluggy, ou o plano\n"
            "  perdeu acesso aos dados. Rode 'python pluggy_sync.py check' e\n"
            "  confira a conexao em https://meu.pluggy.ai"
        )

    _save_json(accounts, os.path.join(DATA_DIR, "accounts.json"))
    _save_csv(accounts, os.path.join(DATA_DIR, "accounts.csv"))
    print(f"  {len(accounts)} conta(s) salva(s) em ./data/accounts.json e accounts.csv")

    for account in accounts:
        account_id = account["id"]
        name = account.get("name", account_id)
        print(f"Baixando transacoes de '{name}' ({date_from} a {date_to}) ...")
        transactions = list_transactions(api_key, account_id, date_from, date_to)

        json_path = os.path.join(DATA_DIR, f"transactions_{account_id}.json")
        # Mesma guarda: nao trocar um arquivo com dados por um vazio.
        if not transactions and _tem_conteudo(json_path):
            print("  AVISO: a API nao retornou transacoes agora, mas ja existe "
                  "arquivo com dados.")
            print("  Mantive o arquivo anterior em vez de sobrescrever com vazio.")
            continue

        _save_json(transactions, json_path)
        _save_csv(transactions, os.path.join(DATA_DIR, f"transactions_{account_id}.csv"))
        print(f"  {len(transactions)} transacao(oes) salva(s)")

    print("\nConcluido. Dados em:", DATA_DIR)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Testa as credenciais e lista conectores disponiveis")

    sub.add_parser(
        "config",
        help="Mostra a config da aplicacao usada pelo widget (allowlist de conectores)",
    )

    connect_parser = sub.add_parser("connect", help="Gera connect.html para autorizar seu banco no navegador")
    connect_group = connect_parser.add_mutually_exclusive_group()
    connect_group.add_argument(
        "--meupluggy", action="store_true",
        help="Pre-seleciona o conector MeuPluggy (200) - dados reais, uso pessoal gratuito",
    )
    connect_group.add_argument(
        "--sandbox", action="store_true",
        help="Pre-seleciona o Pluggy Bank (2) - dados ficticios, para testar",
    )
    connect_group.add_argument(
        "--connector", type=int, metavar="ID",
        help="Pre-seleciona um conector pelo ID (veja 'check')",
    )
    connect_parser.add_argument(
        "--update-item", metavar="ITEM_ID",
        help="Reautoriza/atualiza uma conexao existente em vez de criar outra",
    )
    connect_parser.add_argument(
        "--types", nargs="+", metavar="TYPE",
        help="connectorTypes do widget (ex: PERSONAL_BANK BUSINESS_BANK OTHER)",
    )
    connect_parser.add_argument(
        "--filter-only", action="store_true",
        help="Usa connectorIds (filtra a lista) em vez de selectedConnectorId (pre-seleciona)",
    )
    connect_parser.add_argument(
        "--serve", nargs="?", type=int, const=8080, metavar="PORTA",
        help="Serve a pagina em http://localhost:PORTA (padrao 8080). "
             "Necessario para conectores OAuth como o MeuPluggy.",
    )

    item_parser = sub.add_parser(
        "item", help="Sonda uma conexao: status, contas e transacoes agora")
    item_parser.add_argument("item_id", help="ITEM_ID da conexao")

    inspect_parser = sub.add_parser("inspect", help="Mostra metadados de um conector (diagnostico)")
    inspect_parser.add_argument(
        "connector_id", nargs="?", type=int,
        help="ID do conector. Sem valor, lista todos com seus tipos.",
    )

    sync_parser = sub.add_parser("sync", help="Baixa contas e transacoes de um item ja conectado")
    sync_parser.add_argument("item_id", help="ITEM_ID mostrado apos concluir a autorizacao em connect.html")
    default_to = date.today().isoformat()
    default_from = (date.today() - timedelta(days=365)).isoformat()
    sync_parser.add_argument("--from", dest="date_from", default=default_from, help=f"Data inicial (YYYY-MM-DD). Padrao: {default_from}")
    sync_parser.add_argument("--to", dest="date_to", default=default_to, help=f"Data final (YYYY-MM-DD). Padrao: {default_to}")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check()
    elif args.command == "config":
        cmd_config()
    elif args.command == "connect":
        connector_id = args.connector
        if args.meupluggy:
            connector_id = CONNECTOR_MEUPLUGGY
        elif args.sandbox:
            connector_id = CONNECTOR_PLUGGY_BANK
        cmd_connect(
            connector_id=connector_id,
            update_item=args.update_item,
            connector_types=args.types,
            filter_only=args.filter_only,
            serve_port=args.serve,
        )
    elif args.command == "item":
        cmd_item(args.item_id)
    elif args.command == "inspect":
        cmd_inspect(args.connector_id)
    elif args.command == "sync":
        cmd_sync(args.item_id, args.date_from, args.date_to)


if __name__ == "__main__":
    main()
