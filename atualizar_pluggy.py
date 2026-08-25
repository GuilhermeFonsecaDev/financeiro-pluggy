"""Baixa os dados novos da Pluggy e importa para o financeiro.db.

Roda sozinho quando o backend sobe (ver backend_financeiro.run_server), sempre
em segundo plano: abrir o dashboard nunca espera a rede. Tambem da para chamar
na mao:

    python atualizar_pluggy.py            # atualiza se ja passou do intervalo
    python atualizar_pluggy.py --forcar   # atualiza mesmo se acabou de rodar
    python atualizar_pluggy.py --status   # so mostra o estado atual

Como funciona
-------------
1. Descobre os ITEM_IDs olhando a tabela pluggy_itens (eles chegam la na
   primeira importacao manual). Da para sobrescrever com a variavel de
   ambiente PLUGGY_ITEM_IDS, separada por virgula.
2. Para cada item, roda "pluggy_sync.py sync <ITEM_ID>" (aqui mesmo no
   projeto), que regrava os JSON/CSV em ./data.
3. Importa esses arquivos com importar_pluggy.importar().

Por que existe um intervalo minimo
----------------------------------
Os itens do Meu Pluggy sao atualizados uma vez por dia e nao da para forcar
atualizacao pela API. Sincronizar a cada abertura do dashboard gastaria
chamadas sem trazer nada novo, entao por padrao so busca se a ultima
importacao bem-sucedida tem mais de INTERVALO_PADRAO_HORAS.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import banco as fin
import importar_pluggy
import investimentos

# Download e importacao vivem no mesmo projeto: pluggy_sync.py fica aqui do lado
# e grava em ./data. Caminho relativo ao arquivo, nao absoluto, para a pasta
# poder ser movida ou clonada sem editar codigo.
RAIZ = Path(__file__).resolve().parent
INTERVALO_PADRAO_HORAS = float(os.getenv("PLUGGY_INTERVALO_HORAS", "6"))
TIMEOUT_SYNC_SEGUNDOS = int(os.getenv("PLUGGY_TIMEOUT_SEGUNDOS", "300"))

# Uma atualizacao por vez: o backend e multi-thread e o usuario pode disparar
# manualmente enquanto a automatica ainda roda.
_trava = threading.Lock()


# --------------------------------------------------------------------------
# Estado (guardado em app_meta, que ja existe)
# --------------------------------------------------------------------------

CHAVE_TENTATIVA = "pluggy_ultima_tentativa"
CHAVE_RESULTADO = "pluggy_ultimo_resultado"
CHAVE_DETALHE = "pluggy_ultimo_detalhe"


def _gravar_meta(valores: dict[str, str]) -> None:
    with fin.connect() as conn:
        conn.executemany(
            "INSERT INTO app_meta (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            list(valores.items()),
        )
        conn.commit()


def _ler_meta() -> dict[str, str]:
    fin.ensure_database()
    with fin.connect() as conn:
        return {
            linha["chave"]: linha["valor"]
            for linha in conn.execute(
                "SELECT chave, valor FROM app_meta WHERE chave LIKE 'pluggy_%'"
            )
        }


def ultima_importacao() -> datetime | None:
    fin.ensure_database()
    with fin.connect() as conn:
        importar_pluggy_tabelas(conn)
        # Só conta importação que de fato trouxe algo. Uma rodada que gravou
        # zero linhas não deve marcar "atualizado agora" nem segurar a próxima
        # tentativa pelo intervalo mínimo.
        linha = conn.execute(
            "SELECT executado_em FROM pluggy_sync_log "
            "WHERE (contas_novas + contas_atualizadas + transacoes_novas "
            "       + transacoes_atualizadas) > 0 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not linha:
        return None
    try:
        return datetime.fromisoformat(linha["executado_em"])
    except (TypeError, ValueError):
        return None


def importar_pluggy_tabelas(conn) -> None:
    conn.executescript(importar_pluggy.SCHEMA_PLUGGY)


def itens_conhecidos() -> list[str]:
    override = os.getenv("PLUGGY_ITEM_IDS", "").strip()
    fin.ensure_database()
    with fin.connect() as conn:
        importar_pluggy_tabelas(conn)
        itens = {
            linha["item_id"]
            for linha in conn.execute(
                "SELECT item_id FROM pluggy_itens ORDER BY item_id"
            )
        }
    itens.update(
        pedaco.strip() for pedaco in override.split(",") if pedaco.strip()
    )
    return sorted(itens)


def registrar_item(item_id: str, conector: str = "") -> dict[str, Any]:
    item_id = item_id.strip()
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        item_id,
    ):
        raise ValueError("ITEM_ID invalido.")

    agora = datetime.now().isoformat(timespec="seconds")
    fin.ensure_database()
    with fin.connect() as conn:
        importar_pluggy_tabelas(conn)
        conn.execute(
            "INSERT INTO pluggy_itens (item_id, conector, importado_em) "
            "VALUES (?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
            "conector = CASE WHEN excluded.conector <> '' "
            "THEN excluded.conector ELSE pluggy_itens.conector END, "
            "importado_em = excluded.importado_em",
            (item_id, conector.strip(), agora),
        )
        conn.commit()
    return {"itemId": item_id, "conector": conector.strip(), "registradoEm": agora}


def conexoes_conhecidas() -> list[dict[str, Any]]:
    fin.ensure_database()
    with fin.connect() as conn:
        importar_pluggy_tabelas(conn)
        return [
            {
                "id": linha["item_id"],
                "idCurto": linha["item_id"][:8],
                "conector": linha["conector"] or "",
                "contas": [
                    nome for nome in (linha["contas"] or "").split(",") if nome
                ],
                "importadoEm": linha["importado_em"],
            }
            for linha in conn.execute(
                "SELECT i.item_id, i.conector, i.importado_em, "
                "GROUP_CONCAT(DISTINCT c.nome) AS contas "
                "FROM pluggy_itens i LEFT JOIN pluggy_contas c "
                "ON c.item_id = i.item_id GROUP BY i.item_id "
                "ORDER BY COALESCE(NULLIF(i.conector, ''), contas, i.item_id)"
            )
        ]


def status() -> dict[str, Any]:
    meta = _ler_meta()
    ultima = ultima_importacao()
    return {
        "itens": len(itens_conhecidos()),
        "conexoes": conexoes_conhecidas(),
        "ultimaImportacao": ultima.isoformat(timespec="seconds") if ultima else None,
        "ultimaTentativa": meta.get(CHAVE_TENTATIVA),
        "ultimoResultado": meta.get(CHAVE_RESULTADO),
        "ultimoDetalhe": meta.get(CHAVE_DETALHE),
        "emAndamento": _trava.locked(),
        "intervaloHoras": INTERVALO_PADRAO_HORAS,
    }


# --------------------------------------------------------------------------
# Atualizacao
# --------------------------------------------------------------------------

def _rodar_sync(item_id: str) -> tuple[bool, str]:
    """Roda o pluggy_sync.py para um item."""
    script = RAIZ / "pluggy_sync.py"
    if not script.exists():
        return False, f"nao encontrei {script}"

    ambiente = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        processo = subprocess.run(
            [sys.executable, "pluggy_sync.py", "sync", item_id],
            cwd=str(RAIZ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SYNC_SEGUNDOS,
            env=ambiente,
        )
    except subprocess.TimeoutExpired:
        return False, f"sync passou de {TIMEOUT_SYNC_SEGUNDOS}s e foi abortado"
    except OSError as exc:
        return False, f"nao consegui executar o pluggy_sync.py: {exc}"

    saida = f"{processo.stdout}\n{processo.stderr}"
    if processo.returncode != 0:
        # O motivo da falha vai para stderr (SystemExit / traceback); o stdout
        # so tem o log de progresso. Preferir stderr, senao as ultimas linhas
        # do stdout.
        bruto = processo.stderr.strip() or processo.stdout.strip()
        linhas = [l.strip() for l in bruto.splitlines() if l.strip()]
        if not processo.stderr.strip():
            linhas = linhas[-3:]
        return False, " / ".join(linhas[:4])[:300] if linhas else "sync falhou"

    # cmd_sync sai com 0 mesmo quando o item volta degradado; o sintoma so
    # aparece no texto. Sem isto, uma sincronizacao vazia passaria por sucesso.
    if re.search(r"^\s*0 conta\(s\)", saida, re.MULTILINE):
        return False, "a API retornou 0 contas para o item"
    aviso = re.search(r"^(?:Aviso|AVISO):.*$", saida, re.MULTILINE)
    return True, aviso.group(0).strip()[:300] if aviso else ""


def precisa_atualizar(intervalo_horas: float = INTERVALO_PADRAO_HORAS) -> bool:
    ultima = ultima_importacao()
    if ultima is None:
        return True
    return datetime.now() - ultima >= timedelta(hours=intervalo_horas)


def atualizar(
    forcar: bool = False,
    intervalo_horas: float = INTERVALO_PADRAO_HORAS,
    verboso: bool = True,
) -> dict[str, Any]:
    def registrar(resultado: str, detalhe: str) -> dict[str, Any]:
        agora = datetime.now().isoformat(timespec="seconds")
        try:
            _gravar_meta({
                CHAVE_TENTATIVA: agora,
                CHAVE_RESULTADO: resultado,
                CHAVE_DETALHE: detalhe,
            })
        except Exception as exc:  # noqa: BLE001 - nunca derrubar quem chamou
            if verboso:
                print(f"[pluggy] nao consegui gravar o estado: {exc}")
        if verboso:
            print(f"[pluggy] {resultado}{f': {detalhe}' if detalhe else ''}")
        return {"resultado": resultado, "detalhe": detalhe, "quando": agora}

    if not _trava.acquire(blocking=False):
        return {"resultado": "ja_rodando", "detalhe": "", "quando": None}

    try:
        fin.ensure_database()

        itens = itens_conhecidos()
        if not itens:
            return registrar(
                "sem_itens",
                "nenhum ITEM_ID conhecido. Rode 'pluggy_sync.py connect --meupluggy "
                "--serve' e depois 'importar_pluggy.py' uma vez",
            )

        if not forcar and not precisa_atualizar(intervalo_horas):
            # De proposito nao grava no app_meta: pular nao e uma tentativa, e
            # sobrescrever o estado aqui apagaria um erro anterior que ainda
            # nao foi resolvido -- a falha ficaria invisivel na tela ate o
            # intervalo minimo vencer.
            detalhe = f"ultima importacao ha menos de {intervalo_horas:g}h"
            if verboso:
                print(f"[pluggy] pulado: {detalhe}")
            return {"resultado": "pulado", "detalhe": detalhe, "quando": None}

        # Um item morto nao pode impedir os outros de sincronizar: da para ter
        # uma conexao antiga quebrada convivendo com uma nova saudavel.
        avisos: list[str] = []
        falhas: list[str] = []
        sucessos = 0
        for item_id in itens:
            if verboso:
                print(f"[pluggy] sincronizando item {item_id[:8]}...")
            ok, detalhe = _rodar_sync(item_id)
            if not ok:
                falhas.append(f"{item_id[:8]}: {detalhe}")
                if verboso:
                    print(f"[pluggy]   falhou: {detalhe}")
                continue
            if detalhe:
                avisos.append(detalhe)

            # Cada sync regrava accounts.json somente com as contas daquele
            # item. Importar imediatamente evita que o item seguinte apague
            # essa lista antes de ela chegar ao SQLite.
            try:
                codigo = importar_pluggy.importar(
                    importar_pluggy.DATA_DIR_PADRAO, dry_run=False
                )
            except Exception as exc:  # noqa: BLE001 - manter os demais itens
                falhas.append(f"{item_id[:8]}: import falhou: {exc}")
                continue
            if codigo != 0:
                falhas.append(f"{item_id[:8]}: import recusou os arquivos")
                continue
            sucessos += 1

        # Investimentos são um produto separado de contas/transações na API.
        # Coletamos depois dos extratos para manter posições, lotes, movimentos
        # e snapshots de variação sempre na mesma rodada de atualização.
        if sucessos:
            try:
                resultado_invest = investimentos.sincronizar(itens)
                if resultado_invest.get("falhas"):
                    avisos.append("investimentos: " + " | ".join(resultado_invest["falhas"])[:220])
            except Exception as exc:  # noqa: BLE001 - extrato atualizado continua válido
                avisos.append(f"investimentos: {exc}"[:240])

        if sucessos == 0:
            return registrar("erro", " || ".join(falhas)[:300])

        if falhas:
            return registrar("ok_parcial", " || ".join(falhas + avisos)[:300])
        return registrar("ok", " | ".join(avisos)[:300])
    finally:
        _trava.release()


def atualizar_em_background(
    forcar: bool = False,
    intervalo_horas: float = INTERVALO_PADRAO_HORAS,
) -> threading.Thread:
    """Dispara a atualizacao numa thread daemon. Usado na subida do backend
    para que abrir o dashboard nunca espere a rede."""
    thread = threading.Thread(
        target=atualizar,
        kwargs={"forcar": forcar, "intervalo_horas": intervalo_horas},
        name="atualizar-pluggy",
        daemon=True,
    )
    thread.start()
    return thread


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--forcar", action="store_true",
                        help="ignora o intervalo minimo e atualiza agora")
    parser.add_argument("--intervalo", type=float, default=INTERVALO_PADRAO_HORAS,
                        metavar="HORAS",
                        help=f"intervalo minimo entre atualizacoes (padrao: {INTERVALO_PADRAO_HORAS:g})")
    parser.add_argument("--status", action="store_true",
                        help="mostra o estado da ultima atualizacao e sai")
    args = parser.parse_args()

    if args.status:
        for chave, valor in status().items():
            print(f"  {chave:<18} {valor}")
        return 0

    resultado = atualizar(forcar=args.forcar, intervalo_horas=args.intervalo)
    return 0 if resultado["resultado"] in {"ok", "ok_parcial", "pulado", "ja_rodando"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
