"""Regressões de conexão substituída e descrição de Pix, sem dados reais."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import banco
import atualizar_pluggy as sync
from importar_pluggy import SCHEMA_PLUGGY, descricao_transacao, linha_transacao


class PixTest(unittest.TestCase):
    def test_documento_da_contraparte_quando_nome_ausente(self):
        tx = {'description': 'Pix', 'type': 'DEBIT', 'paymentData': {
            'receiver': {'name': ' ', 'documentNumber': {'type': 'CPF', 'value': '***.123.456-**'}},
            'payer': {'documentNumber': {'type': 'CNPJ', 'value': '00.000.000/0001-00'}}}}
        self.assertEqual(descricao_transacao(tx), 'Pix enviado para CPF ***.123.456-**')
        tx['paymentData']['receiver']['name'] = 'Loja'
        self.assertEqual(descricao_transacao(tx), 'Pix enviado para Loja')
        tx['type'] = 'CREDIT'
        self.assertEqual(descricao_transacao(tx), 'Pix recebido de CNPJ 00.000.000/0001-00')

    def test_documento_vazio_ou_invalido_preserva_descricao(self):
        for documento in [None, {}, {'value': None}, {'value': ' '}, {'value': 123}]:
            tx = {'description': 'Pix', 'type': 'DEBIT', 'paymentData': {
                'receiver': {'documentNumber': documento},
                'payer': {'documentNumber': {'value': '123'}}}}
            self.assertEqual(descricao_transacao(tx), 'Pix')

    def test_direcao_e_raw_preservado(self):
        tx = {'id': 'tx', 'date': '2026-09-09', 'description': 'Pix', 'type': 'DEBIT',
              'paymentData': {'payer': {'name': 'Titular'}, 'receiver': {'name': 'Loja'}}}
        linha = linha_transacao(tx, 'agora')
        self.assertEqual(linha[6], 'Pix enviado para Loja')
        self.assertEqual(json.loads(linha[-1]), tx)
        tx['type'] = 'CREDIT'
        self.assertEqual(descricao_transacao(tx), 'Pix recebido de Titular')

    def test_nao_inventa_contraparte_nem_substitui_descricao_completa(self):
        for pagamento in [None, {}, {'receiver': None}, {'receiver': {'name': None}, 'payer': {'name': 'Titular'}}]:
            self.assertEqual(descricao_transacao({'description': 'PIX', 'type': 'DEBIT', 'paymentData': pagamento}), 'PIX')
        self.assertEqual(descricao_transacao({'description': 'Pix para Maria', 'type': 'DEBIT', 'paymentData': {'receiver': {'name': 'Outra'}}}), 'Pix para Maria')


class ArquivamentoTest(unittest.TestCase):
    def test_preserva_dados_ignora_override_e_permite_reativar(self):
        with tempfile.TemporaryDirectory() as tmp:
            conexoes = []
            original = banco.connect
            def conectar():
                c = original()
                conexoes.append(c)
                return c
            try:
                with patch.object(banco, 'DATABASE_PATH', Path(tmp) / 'teste.db'), patch.object(banco, 'connect', conectar), patch.object(banco, 'ensure_database', lambda: None), patch.object(sync, '_trava', threading.Lock()), patch.dict(os.environ, {'PLUGGY_ITEM_IDS': 'antiga'}):
                    with banco.connect() as c:
                        c.executescript(SCHEMA_PLUGGY)
                        c.execute('CREATE TABLE app_meta(chave TEXT PRIMARY KEY, valor TEXT)')
                        c.execute("INSERT INTO pluggy_itens VALUES ('antiga','BTG','agora')")
                        c.execute("INSERT INTO pluggy_contas(conta_id,item_id,importado_em) VALUES ('conta','antiga','agora')")
                    sync.definir_arquivada('antiga', True)
                    self.assertEqual(sync.itens_conhecidos(), [])
                    with banco.connect() as c:
                        self.assertEqual(c.execute('SELECT COUNT(*) FROM pluggy_contas').fetchone()[0], 1)
                    with self.assertRaises(ValueError):
                        sync.validar_item('antiga')
                    sync.definir_arquivada('antiga', False)
                    self.assertEqual(sync.itens_conhecidos(), ['antiga'])
                    sync._trava.acquire()
                    with self.assertRaises(ValueError):
                        sync.definir_arquivada('antiga', True)
                    sync._trava.release()
                    with self.assertRaises(ValueError):
                        sync.definir_arquivada('inexistente', True)
            finally:
                for c in conexoes:
                    c.close()


if __name__ == '__main__':
    unittest.main()
