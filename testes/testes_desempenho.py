"""Desempenho sem cache financeiro entre cargas; usa somente SQLite temporário."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import banco
import extrato_camada as camada


class DesempenhoTests(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix='pluggy_perf_test_')
        self.addCleanup(pasta.cleanup)
        mock = patch.object(banco, 'DATABASE_PATH', Path(pasta.name) / 'test.db')
        mock.start()
        self.addCleanup(mock.stop)
        banco.ensure_database()

    def test_cache_so_dura_uma_carga_e_nao_compartilha_objetos(self):
        chamadas = []
        def calcular():
            chamadas.append(1)
            return {'valores': [123]}
        @banco.escopo_leitura
        def carregar():
            primeiro = banco.reutilizar_leitura('faturas', calcular)
            primeiro['valores'][0] = 0
            return banco.reutilizar_leitura('faturas', calcular)
        self.assertEqual(carregar(), {'valores': [123]})
        self.assertEqual(len(chamadas), 1)
        carregar()
        self.assertEqual(len(chamadas), 2)

    def test_gravacao_invalida_reutilizacao(self):
        def ler():
            with banco.connect() as conn:
                return conn.execute('SELECT COUNT(*) FROM app_meta').fetchone()[0]
        @banco.escopo_leitura
        def carregar():
            antes = banco.reutilizar_leitura('contagem', ler)
            with banco.connect() as conn:
                conn.execute("INSERT INTO app_meta VALUES ('teste', 'alterado')")
            depois = banco.reutilizar_leitura('contagem', ler)
            self.assertEqual(depois, antes + 1)
        carregar()

    def test_cargas_simultaneas_sao_isoladas(self):
        @banco.escopo_leitura
        def carregar(valor):
            return banco.reutilizar_leitura('mesma_chave', lambda: valor)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(carregar, range(20))), list(range(20)))

    def test_conexao_fecha_e_desfaz_transacao_com_erro(self):
        conn = banco.connect()
        with self.assertRaises(ValueError):
            with conn:
                conn.execute("INSERT INTO app_meta VALUES ('teste', 'alterado')")
                raise ValueError('desfazer')
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
        with banco.connect() as nova:
            self.assertIsNone(nova.execute("SELECT valor FROM app_meta WHERE chave='teste'").fetchone())

    def test_normalizacao_preserva_contrato_e_reutiliza_textos(self):
        camada._normalizar_texto.cache_clear()
        for valor, esperado in [('  ASSINATÚRA  Mês ', 'assinatura mes'), (None, ''), (123, '123'), ('ação', 'acao')]:
            self.assertEqual(camada.normalizar(valor), esperado)
            self.assertEqual(camada.normalizar(valor), esperado)
        self.assertGreaterEqual(camada._normalizar_texto.cache_info().hits, 4)


if __name__ == '__main__':
    unittest.main()
