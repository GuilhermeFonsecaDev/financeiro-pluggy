"""Testes automáticos. Rodar da raiz do projeto:

    python -m unittest discover -s testes -t .
"""
import sys
from pathlib import Path

# Os testes importam uns aos outros pelo nome (from testes_recorrentes import ...).
sys.path.insert(0, str(Path(__file__).resolve().parent))
# O código do app fica em backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
