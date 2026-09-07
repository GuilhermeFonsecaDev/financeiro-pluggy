"""Acesso ao SQLite do projeto Pluggy.

Substitui o financeiro_sqlite do projeto manual. Os modulos daqui usavam
apenas quatro coisas de la -- DATABASE_PATH, connect, ensure_database e
create_database_backup --, entao este arquivo entrega exatamente isso, sem
arrastar junto o schema das planilhas manuais (contas fixas, entradas,
caixinha, dinheiro emprestado).

Os dois projetos sao independentes: bancos separados, backends separados,
portas separadas. Nada aqui conhece as tabelas do outro lado.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "pluggy.db"
BACKUP_DIR = ROOT / "backups"

_db_lock = threading.RLock()
_last_backup_at = 0.0
_leituras = ContextVar("pluggy_leituras", default=None)


def escopo_leitura(funcao):
    """Reutiliza cálculos somente durante uma carga, nunca entre requisições."""
    @wraps(funcao)
    def executar(*args, **kwargs):
        if _leituras.get() is not None:
            return funcao(*args, **kwargs)
        token = _leituras.set({})
        try:
            return funcao(*args, **kwargs)
        finally:
            _leituras.reset(token)
    return executar


def reutilizar_leitura(chave, calcular):
    cache = _leituras.get()
    if cache is None:
        return calcular()
    def revisao():
        stat = DATABASE_PATH.stat()
        return str(DATABASE_PATH), stat.st_ino, stat.st_mtime_ns, stat.st_size
    antes = revisao()
    # Não reutilizar resultados durante uma escrita ainda não confirmada.
    journal = Path(str(DATABASE_PATH) + "-journal")
    wal = Path(str(DATABASE_PATH) + "-wal")
    if journal.exists() or wal.exists():
        return calcular()
    entrada = cache.get(chave)
    if entrada and entrada[0] == antes:
        return deepcopy(entrada[1])
    resultado = calcular()
    if revisao() == antes and not journal.exists() and not wal.exists():
        cache[chave] = (antes, deepcopy(resultado))
    return resultado


class Conexao(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, factory=Conexao)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    # A atualizacao automatica grava numa thread de fundo enquanto o backend
    # atende requisicoes. Sem isto, uma leitura no meio da escrita falharia na
    # hora com "database is locked" em vez de esperar.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_database() -> None:
    """Garante que o arquivo do banco existe.

    As tabelas em si sao criadas por quem as usa: importar_pluggy cria as
    pluggy_*, extrato_camada cria as extrato_*. Aqui so precisamos do arquivo
    e da tabela de metadados que o atualizar_pluggy usa para guardar estado.
    """
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_meta ("
            "  chave TEXT PRIMARY KEY,"
            "  valor TEXT NOT NULL)"
        )
        conn.commit()


def safe_backup_reason(reason: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(reason or "manual")).strip("_")
    return cleaned[:48] or "manual"


def list_database_backups(limit: int = 30) -> list[dict[str, Any]]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backups = sorted(BACKUP_DIR.glob("pluggy_*.db"),
                     key=lambda item: item.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "path": str(p),
            "size": p.stat().st_size,
            "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for p in backups[:limit]
    ]


def prune_database_backups(keep: int = 60) -> None:
    backups = sorted(BACKUP_DIR.glob("pluggy_*.db"),
                     key=lambda item: item.stat().st_mtime, reverse=True)
    for path in backups[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


def create_database_backup(reason: str = "manual",
                           min_interval_seconds: int = 90) -> Path | None:
    global _last_backup_at
    if not DATABASE_PATH.exists():
        return None
    agora = datetime.now().timestamp()
    with _db_lock:
        if min_interval_seconds > 0 and agora - _last_backup_at < min_interval_seconds:
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        alvo = BACKUP_DIR / f"pluggy_{datetime.now():%Y%m%d_%H%M%S}_{safe_backup_reason(reason)}.db"
        shutil.copy2(DATABASE_PATH, alvo)
        _last_backup_at = agora
        prune_database_backups()
        return alvo
