"""Estado por instituição, isolamento da sincronização e escopo da renovação."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import banco
import atualizar_pluggy as sync
import pluggy_conexoes as conexoes


class ConexoesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch(banco, 'DATABASE_PATH', Path(self.tmp.name) / 'teste.db')
        original_connect = banco.connect
        abertas = []
        def conectar():
            conn = original_connect()
            abertas.append(conn)
            return conn
        self.patch(banco, 'connect', conectar)
        self.addCleanup(lambda: [conn.close() for conn in abertas])
        self.patch(banco, 'ensure_database', lambda: None)
        self.patch(sync, '_trava', threading.Lock())
        self.patch(sync, '_etapas', {})
        self.patch(sync, 'itens_conhecidos', lambda: ['a', 'b'])
        with banco.connect() as conn:
            conn.execute('CREATE TABLE app_meta(chave TEXT PRIMARY KEY, valor TEXT)')
        self.patch(sync.investimentos, 'sincronizar', lambda itens: {})
        self.patch(sync.importar_pluggy, 'importar', lambda *a, **kw: 0)

    def patch(self, obj, nome, valor):
        p = patch.object(obj, nome, valor)
        p.start()
        self.addCleanup(p.stop)

    def estados(self):
        return sync._json_meta(sync._ler_meta(), sync.CHAVE_CONEXOES, {})

    def test_atualizacao_individual_nao_processa_outros_bancos(self):
        with patch.object(sync, '_rodar_sync', return_value=(True, '')) as rodar:
            self.assertEqual(sync.atualizar(forcar=True, item_id='b', verboso=False)['resultado'], 'ok')
        rodar.assert_called_once_with('b')
        self.assertEqual(set(self.estados()), {'b'})
        self.assertFalse(sync._trava.locked())

    def test_falha_nao_interrompe_outro_banco(self):
        with patch.object(sync, '_rodar_sync', side_effect=[(False, 'Sem resposta'), (True, '')]):
            self.assertEqual(sync.atualizar(forcar=True, verboso=False)['resultado'], 'ok_parcial')
        self.assertEqual(self.estados()['a']['resultado'], 'erro')
        self.assertEqual(self.estados()['b']['resultado'], 'ok')
        self.assertEqual(sync._etapas, {})

    def test_erro_preserva_ultimo_sucesso_e_limita_historico(self):
        sync._registrar_conexao('a', 'ok')
        sucesso = self.estados()['a']['sucessoEm']
        for _ in range(65):
            sync._registrar_conexao('a', 'erro', 'Indisponível')
        self.assertEqual(self.estados()['a']['sucessoEm'], sucesso)
        self.assertEqual(len(sync._json_meta(sync._ler_meta(), sync.CHAVE_HISTORICO, [])), 60)

    def test_importacao_recusada_nao_registra_sucesso(self):
        with patch.object(sync, '_rodar_sync', return_value=(True, '')), patch.object(sync.importar_pluggy, 'importar', return_value=1):
            sync.atualizar(forcar=True, item_id='a', verboso=False)
        self.assertEqual(self.estados()['a']['resultado'], 'erro')
        self.assertIsNone(self.estados()['a']['sucessoEm'])

    def test_rodada_ativa_recusa_nova_thread(self):
        sync._trava.acquire()
        self.assertIsNone(sync.atualizar_em_background(forcar=True))
        sync._trava.release()

    def test_item_desconhecido_nao_chama_rede(self):
        with patch.object(conexoes, '_post') as post:
            with self.assertRaises(ValueError):
                conexoes.criar_connect_token('inexistente')
        post.assert_not_called()

    def test_token_de_renovacao_tem_escopo_do_item(self):
        with patch.object(conexoes, '_credenciais', return_value=('teste', 'teste')), patch.object(conexoes, '_post', side_effect=[{'apiKey':'teste'}, {'accessToken':'token'}]) as post:
            self.assertEqual(conexoes.criar_connect_token('a'), 'token')
        self.assertEqual(post.call_args.args[1]['itemId'], 'a')

    def test_ressalva_nao_vira_sucesso_total(self):
        with patch.object(sync, '_rodar_sync', return_value=(True, 'Aviso: dados parciais')):
            self.assertEqual(sync.atualizar(forcar=True, item_id='a', verboso=False)['resultado'], 'ok_parcial')
        self.assertEqual(self.estados()['a']['resultado'], 'aviso')


if __name__ == '__main__':
    unittest.main()
