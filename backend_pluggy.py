"""Backend local do projeto Pluggy (Open Finance).

Separado do dashboard manual de propósito: banco próprio, porta própria,
processo próprio. Os dois podem rodar ao mesmo tempo sem se enxergar.

    python backend_pluggy.py --open
"""

from __future__ import annotations

import cartoes as cartoes_id
import argparse
import json
import re
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from atualizar_pluggy import atualizar_em_background, registrar_item
from atualizar_pluggy import status as pluggy_status
from banco import DATABASE_PATH, ROOT, create_database_backup, ensure_database, list_database_backups
from extrato_camada import (
    ajustar_transacao,
    atualizar_regra_entrada,
    atualizar_categoria,
    atualizar_regra,
    categorias_payload,
    criar_categoria,
    criar_regra_entrada,
    criar_regra,
    definir_entrada_ativa,
    definir_valor_esperado_entradas,
    entradas_payload,
    regras_payload,
    remover_categoria,
    remover_regra_entrada,
    remover_regra,
)
import emprestimos
import fixas
import investimentos
import recorrentes
import visao_geral
from pluggy_conexoes import criar_connect_token
from pluggy_extrato import (
    cartoes_payload,
    categorias_resumo_payload,
    extrato_payload,
    filtros_payload,
)

HOST = "127.0.0.1"
# 8766, não 8765: o dashboard manual continua na 8765 e os dois sobem juntos.
PORT = 8766


class PluggyHandler(SimpleHTTPRequestHandler):
    server_version = "BackendPluggy/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Dados nunca ficam em cache do navegador -- quem cacheia é a tela, que
        # sabe invalidar depois de uma escrita. Já o tema e o app.js são
        # estáticos e eram rebaixados a cada troca de tela; um cache curto com
        # revalidação resolve sem risco de servir versão velha.
        caminho = urlsplit(self.path).path
        if caminho.endswith((".css", ".js")):
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/transacoes.html")
            self.end_headers()
            return
        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        super().do_GET()

    def do_POST(self) -> None:
        self.handle_api_write()

    def do_PUT(self) -> None:
        self.handle_api_write()

    def do_DELETE(self) -> None:
        parsed = urlsplit(self.path)
        try:
            ensure_database()
            regra = re.fullmatch(r"/api/extrato/regras/([\w-]+)", parsed.path)
            if regra:
                self.send_json(remover_regra(regra.group(1)))
                return
            regra_entrada = re.fullmatch(r"/api/extrato/entradas/regras/([\w-]+)", parsed.path)
            if regra_entrada:
                self.send_json(remover_regra_entrada(regra_entrada.group(1)))
                return
            categoria = re.fullmatch(r"/api/extrato/categorias/([\w-]+)", parsed.path)
            if categoria:
                self.send_json(remover_categoria(categoria.group(1)))
                return
            desconto = re.fullmatch(r"/api/fixas/descontos/([\w-]+)", parsed.path)
            if desconto:
                self.send_json(fixas.remover_desconto(desconto.group(1)))
                return
            fixa = re.fullmatch(r"/api/fixas/([\w-]+)", parsed.path)
            if fixa:
                self.send_json(fixas.remover(fixa.group(1)))
                return
            emprestimo = re.fullmatch(r"/api/emprestimos/([\w-]+)", parsed.path)
            if emprestimo:
                self.send_json(emprestimos.remover(emprestimo.group(1)))
                return
            self.send_error_json("Endpoint não encontrado.", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - API local deve retornar erro legível.
            self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Corpo JSON deve ser um objeto.")
        return data

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, mensagem: str,
                        status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"ok": False, "error": mensagem}, status)

    def query_extrato(self, query: dict[str, list[str]]) -> dict[str, Any]:
        def texto(chave: str) -> str:
            return (query.get(chave, [""])[0] or "").strip()

        def inteiro(chave: str, padrao: int) -> int:
            try:
                return int(query.get(chave, [str(padrao)])[0])
            except (TypeError, ValueError):
                return padrao

        # O periodo e um intervalo de meses (from/to). "month" continua aceito
        # como atalho de mes unico -- e o que os links de outras telas mandam.
        def mes_valido(chave: str) -> str:
            valor = texto(chave)
            if valor and not re.fullmatch(r"\d{4}-\d{2}", valor):
                raise ValueError(
                    f"Parâmetro {chave} deve estar no formato YYYY-MM."
                )
            return valor

        mes = mes_valido("month")
        mes_de = mes_valido("from") or mes
        mes_ate = mes_valido("to") or mes

        modo = texto("modo").lower()
        return {
            "mes": "",
            "mesDe": mes_de,
            "mesAte": mes_ate,
            "conta": texto("account"),
            "cartao": texto("card"),
            "tipo": texto("type").upper(),
            "categoria": texto("category"),
            "status": texto("status").upper(),
            "busca": texto("q"),
            "modo": modo if modo in ("fatura", "mes") else "fatura",
            "limite": inteiro("limit", 200),
            "offset": inteiro("offset", 0),
        }

    def query_mes(self, query: dict[str, list[str]]) -> str:
        mes = (query.get("month", [""])[0] or "").strip()
        if not mes:
            return datetime.now().strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", mes):
            raise ValueError("Parâmetro month deve estar no formato YYYY-MM.")
        return mes

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        try:
            ensure_database()
            if path == "/api/health":
                self.send_json({"ok": True, "database": str(DATABASE_PATH)})
            elif path == "/api/extrato/filtros":
                modo = (query.get("modo", [""])[0] or "").strip().lower()
                self.send_json(filtros_payload(modo if modo in ("fatura", "mes") else "fatura"))
            elif path == "/api/extrato/categorias":
                self.send_json(categorias_payload())
            elif path == "/api/extrato/categorias-resumo":
                self.send_json(categorias_resumo_payload(self.query_extrato(query)))
            elif path == "/api/extrato/regras":
                self.send_json(regras_payload())
            elif path == "/api/extrato/entradas":
                self.send_json(entradas_payload(self.query_mes(query)))
            elif path == "/api/extrato/categoria-transacoes":
                filtros = self.query_extrato(query)
                categoria = (query.get("category", [""])[0] or "").strip()
                if not categoria:
                    raise ValueError("Informe a categoria em 'category'.")
                filtros["categoria"] = categoria
                filtros["limite"] = filtros.get("limite") or 500
                self.send_json(extrato_payload(filtros))
            elif path == "/api/extrato":
                self.send_json(extrato_payload(self.query_extrato(query)))
            elif path == "/api/cartoes-identidade":
                self.send_json(cartoes_id.payload())
            elif path == "/api/visao-geral":
                self.send_json(visao_geral.payload(
                    (query.get("periodo", ["mes"])[0] or "mes").strip(),
                    (query.get("ref", [""])[0] or "").strip()))
            elif path == "/api/emprestimos":
                self.send_json(emprestimos.payload())
            elif path == "/api/fixas/recorrentes":
                self.send_json(recorrentes.sugestoes_payload())
            elif re.fullmatch(r"/api/fixas/[\w-]+/historico", path):
                self.send_json(fixas.historico_payload(path.split("/")[3]))
            elif path == "/api/fixas":
                self.send_json(fixas.mes_payload(self.query_mes(query)))
            elif path == "/api/fixas/candidatas":
                self.send_json(fixas.candidatas_payload(
                    self.query_mes(query), (query.get("q", [""])[0] or "").strip(),
                    como_regra=(query.get("regra", [""])[0] or "") == "1"))
            elif path == "/api/pluggy-status":
                self.send_json(pluggy_status())
            elif path == "/api/investimentos":
                self.send_json(investimentos.payload())
            elif path == "/api/pluggy-connect-token":
                self.send_json({"connectToken": criar_connect_token()})
            elif path == "/api/pluggy-cartoes":
                try:
                    ano = int(query.get("year", ["0"])[0])
                except (TypeError, ValueError):
                    raise ValueError("Parâmetro year inválido.")
                grupo = (query.get("group", ["fatura"])[0] or "fatura").strip()
                self.send_json(cartoes_payload(ano, grupo))
            elif path == "/api/backups":
                self.send_json({"backups": list_database_backups()})
            else:
                self.send_error_json("Endpoint não encontrado.", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - API local deve retornar erro legível.
            self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_api_write(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)   # as rotas de contas fixas leem ?month=
        try:
            ensure_database()
            payload = self.read_json_body()

            regra = re.fullmatch(r"/api/extrato/regras/([\w-]+)", parsed.path)
            if regra:
                self.send_json(atualizar_regra(regra.group(1), payload))
                return
            regra_entrada = re.fullmatch(r"/api/extrato/entradas/regras/([\w-]+)", parsed.path)
            if regra_entrada:
                self.send_json(atualizar_regra_entrada(regra_entrada.group(1), payload))
                return
            if parsed.path == "/api/cartoes-identidade":
                self.send_json(cartoes_id.salvar(payload))
                return
            if parsed.path == "/api/extrato/entradas/valor-esperado":
                self.send_json(definir_valor_esperado_entradas(payload))
                return
            entrada_transacao = re.fullmatch(
                r"/api/extrato/entradas/transacoes/([\w-]+)", parsed.path
            )
            if entrada_transacao:
                self.send_json(definir_entrada_ativa(
                    entrada_transacao.group(1), payload.get("ativa") is not False
                ))
                return
            categoria = re.fullmatch(r"/api/extrato/categorias/([\w-]+)", parsed.path)
            if categoria:
                self.send_json(atualizar_categoria(categoria.group(1), payload))
                return
            transacao = re.fullmatch(r"/api/extrato/transacoes/([\w-]+)", parsed.path)
            if transacao:
                self.send_json(ajustar_transacao(transacao.group(1), payload))
                return

            # Recorrentes detectados: recusar tira a sugestão do painel para
            # sempre; reconsiderar traz de volta.
            if parsed.path == "/api/fixas/recorrentes/ignorar":
                self.send_json(recorrentes.ignorar(
                    str(payload.get("chave") or ""),
                    str(payload.get("descricao") or "")))
                return
            if parsed.path == "/api/fixas/recorrentes/reconsiderar":
                self.send_json(recorrentes.reconsiderar(
                    str(payload.get("chave") or "")))
                return
            if parsed.path == "/api/fixas/recorrentes/corrigir-termo":
                self.send_json(recorrentes.corrigir_termo(
                    str(payload.get("fixaId") or ""),
                    str(payload.get("termo") or "")))
                return

            # Contas fixas: /api/fixas/{id} edita o cadastro,
            # /api/fixas/{id}/mes edita só o que é daquele mês.
            if parsed.path == "/api/fixas/clonar-mes":
                self.send_json(fixas.clonar_mes(
                    str(payload.get("origem") or ""), str(payload.get("destino") or "")))
                return
            fixa_mes = re.fullmatch(r"/api/fixas/([\w-]+)/mes", parsed.path)
            if fixa_mes:
                self.send_json(fixas.ajustar_mes(
                    self.query_mes(query), fixa_mes.group(1), payload))
                return
            fixa_desc = re.fullmatch(r"/api/fixas/([\w-]+)/descontos", parsed.path)
            if fixa_desc:
                self.send_json(fixas.salvar_desconto(
                    self.query_mes(query), fixa_desc.group(1), payload))
                return
            fixa = re.fullmatch(r"/api/fixas/([\w-]+)", parsed.path)
            if fixa:
                self.send_json(fixas.atualizar(fixa.group(1), payload))
                return
            emprestimo = re.fullmatch(r"/api/emprestimos/([\w-]+)", parsed.path)
            if emprestimo:
                self.send_json(emprestimos.atualizar(emprestimo.group(1), payload))
                return

            if parsed.path == "/api/emprestimos":
                self.send_json(emprestimos.criar(payload))
            elif parsed.path == "/api/fixas":
                self.send_json(fixas.criar(payload))
            elif parsed.path == "/api/extrato/regras":
                self.send_json(criar_regra(payload))
            elif parsed.path == "/api/extrato/entradas/regras":
                self.send_json(criar_regra_entrada(payload))
            elif parsed.path == "/api/extrato/categorias":
                self.send_json(criar_categoria(payload))
            elif parsed.path == "/api/pluggy-items":
                item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
                item_id = str(item.get("id") or item.get("itemId") or "")
                conector_obj = item.get("connector") if isinstance(item.get("connector"), dict) else {}
                conector = str(
                    conector_obj.get("name")
                    or item.get("connectorName")
                    or payload.get("connectorName")
                    or ""
                )
                registro = registrar_item(item_id, conector)
                atualizar_em_background(forcar=True, intervalo_horas=0)
                self.send_json({"ok": True, "registro": registro, "sincronizando": True})
            elif parsed.path == "/api/pluggy-sync":
                atualizar_em_background(forcar=True, intervalo_horas=0)
                self.send_json({"ok": True, "sincronizando": True})
            elif parsed.path == "/api/backup":
                backup = create_database_backup(str(payload.get("reason") or "manual"),
                                                min_interval_seconds=0)
                self.send_json({"ok": True, "backup": str(backup) if backup else None})
            else:
                self.send_error_json("Endpoint não encontrado.", HTTPStatus.NOT_FOUND)
        except PermissionError:
            self.send_error_json(
                "Não consegui salvar o banco SQLite. Feche ferramentas que estejam "
                "usando pluggy.db e tente novamente.",
                HTTPStatus.CONFLICT,
            )
        except ValueError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - API local deve retornar erro legível.
            self.send_error_json(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)


class ServidorUnico(ThreadingHTTPServer):
    """Recusa subir se a porta já estiver ocupada.

    O padrão do ThreadingHTTPServer é allow_reuse_address = 1, e no Windows
    isso deixa um segundo processo se ligar a uma porta em uso em vez de
    falhar — o resultado é backend empilhado servindo código velho.
    """

    allow_reuse_address = False


def run_server(host: str = HOST, port: int = PORT, open_browser: bool = False,
               auto_sync: bool = True) -> None:
    ensure_database()
    handler = lambda *a, **kw: PluggyHandler(*a, directory=str(ROOT), **kw)  # noqa: E731
    try:
        server = ServidorUnico((host, port), handler)
    except OSError:
        print(f"Já existe um backend Pluggy rodando em http://{host}:{port}")
        print(f"Abra: http://{host}:{port}/transacoes.html")
        if open_browser:
            webbrowser.open(f"http://{host}:{port}/transacoes.html")
        return

    url = f"http://{host}:{port}/transacoes.html"
    print(f"Backend Pluggy em http://{host}:{port}")
    print(f"Banco SQLite: {DATABASE_PATH}")
    print(f"Abra: {url}")

    if auto_sync:
        # Em segundo plano: abrir a tela nunca espera a rede.
        print("Atualizacao da Pluggy rodando em segundo plano...")
        atualizar_em_background()

    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando backend.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backend local do projeto Pluggy.")
    parser.add_argument("--init", action="store_true", help="Só cria/valida o banco e sai.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--open", action="store_true", help="Abre a tela no navegador.")
    parser.add_argument("--sem-sync", action="store_true",
                        help="Não atualiza os dados da Pluggy ao subir.")
    args = parser.parse_args()

    if args.init:
        ensure_database()
        print(f"Banco pronto: {DATABASE_PATH}")
        return

    run_server(args.host, args.port, open_browser=args.open, auto_sync=not args.sem_sync)


if __name__ == "__main__":
    main()
