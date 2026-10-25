"""Nome de exibição das contas bancárias.

O banco pode mandar um nome que não diz de onde a conta é ("Conta
Corrente"), e aí a tela mostrava uma conta sem banco. O código COMPE no
começo do número de transferência diz qual é o banco, e é ele que dá nome à
conta nesse caso.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

import pluggy_extrato as px


def conta(conta_id, nome, subtipo="CHECKING_ACCOUNT", transferencia="208/0001/12345678-9"):
    return {
        "conta_id": conta_id, "item_id": "item", "subtipo": subtipo, "nome": nome,
        "raw_json": json.dumps({"bankData": {"transferNumber": transferencia}}),
    }


class NomeDoBanco(unittest.TestCase):
    def test_nome_generico_vira_o_nome_do_banco(self):
        self.assertEqual(px.nome_da_conta(conta("a", "Conta Corrente")), "BTG Pactual")

    def test_nome_especifico_fica_como_veio(self):
        # "BTG Investimentos" e "BTG Banking" dizem o que são; trocar pelos
        # dois por "BTG Pactual" apagaria a diferença entre eles.
        self.assertEqual(px.nome_da_conta(conta("a", "BTG Investimentos")), "BTG Investimentos")

    def test_poupanca_generica_diz_que_e_poupanca(self):
        self.assertEqual(
            px.nome_da_conta(conta("a", "Conta Poupança", subtipo="SAVINGS_ACCOUNT")),
            "BTG Pactual Poupança")

    def test_banco_desconhecido_mantem_o_nome(self):
        # Sem saber o banco, inventar um nome seria pior do que o genérico.
        self.assertEqual(
            px.nome_da_conta(conta("a", "Conta Corrente", transferencia="999/0001/1-9")),
            "Conta Corrente")

    def test_sem_numero_de_transferencia_mantem_o_nome(self):
        linha = {**conta("a", "Conta Corrente"), "raw_json": "{}"}
        self.assertEqual(px.nome_da_conta(linha), "Conta Corrente")

    def test_maiusculas_e_acentos_nao_importam(self):
        self.assertEqual(px.nome_da_conta(conta("a", "CONTA CORRENTE")), "BTG Pactual")
        self.assertEqual(px.nome_da_conta(conta("a", "conta-corrente")), "BTG Pactual")

    def test_codigo_com_zero_a_esquerda(self):
        self.assertEqual(px.nome_da_conta(conta("a", "Conta Corrente",
                                                transferencia="077/0001/1-9")), "Inter")


class NasTelas(unittest.TestCase):
    """O nome corrigido tem de chegar a mapa_apelidos, que é de onde toda
    tela tira o nome de uma conta."""

    def test_mapa_de_apelidos_usa_o_nome_do_banco(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE pluggy_contas (conta_id, item_id, subtipo, nome, raw_json)")
        for linha in (conta("a", "Conta Corrente"), conta("b", "itau", transferencia="341/1/1-9")):
            conn.execute("INSERT INTO pluggy_contas VALUES (?,?,?,?,?)",
                         tuple(linha[k] for k in ("conta_id", "item_id", "subtipo", "nome", "raw_json")))
        with patch.object(px.cartoes_id, "identidades", return_value={}):
            self.assertEqual(px.mapa_apelidos(conn), {"a": "BTG Pactual", "b": "itau"})


class RotuloDoInstrumento(unittest.TestCase):
    """O rótulo curto da coluna de Transações: "BTG - 0088", "BTG - PIX"."""

    BTG = json.dumps({"bankData": {"transferNumber": "208/0001/1-9"}})

    def test_cartao_mostra_a_tag_e_o_final(self):
        cartao = {"tag": "BTG", "numero": "0088", "nomeExibicao": "BTG"}
        self.assertEqual(px.rotulo_instrumento(cartao=cartao, conta_raw=None, metodo=None,
                                               conta_nome="BTG", tags={}), "BTG - 0088")

    def test_cartao_sem_numero_fica_so_com_o_banco(self):
        cartao = {"tag": "BTG", "numero": "", "nomeExibicao": "BTG"}
        self.assertEqual(px.rotulo_instrumento(cartao=cartao, conta_raw=None, metodo=None,
                                               conta_nome="BTG", tags={}), "BTG")

    def test_pix_na_conta_usa_a_tag_do_banco(self):
        self.assertEqual(px.rotulo_instrumento(cartao=None, conta_raw=self.BTG, metodo="PIX",
                                               conta_nome="BTG Pactual", tags={"208": "BTG"}),
                         "BTG - PIX")

    def test_sem_tag_conhecida_usa_o_nome_do_banco(self):
        self.assertEqual(px.rotulo_instrumento(cartao=None, conta_raw=self.BTG, metodo="PIX",
                                               conta_nome="Conta Corrente", tags={}),
                         "BTG Pactual - PIX")

    def test_outros_metodos_tem_nome_proprio(self):
        for metodo, esperado in (("BOLETO", "BTG - Boleto"), ("TED", "BTG - TED"),
                                 ("TEF", "BTG - Transferência")):
            self.assertEqual(px.rotulo_instrumento(cartao=None, conta_raw=self.BTG, metodo=metodo,
                                                   conta_nome="x", tags={"208": "BTG"}), esperado)

    def test_metodo_generico_nao_vira_pix(self):
        # OTHER é débito automático, tarifa, resgate: chamar de PIX mentiria.
        self.assertEqual(px.rotulo_instrumento(cartao=None, conta_raw=self.BTG, metodo="OTHER",
                                               conta_nome="x", tags={"208": "BTG"}), "BTG")


class TagPorBanco(unittest.TestCase):
    def conn(self, contas):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE pluggy_contas (item_id, subtipo, raw_json)")
        for item, codigo in contas:
            conn.execute("INSERT INTO pluggy_contas VALUES (?, 'CHECKING_ACCOUNT', ?)",
                         (item, json.dumps({"bankData": {"transferNumber": f"{codigo}/1/1-9"}})))
        return conn

    def test_a_conexao_liga_o_codigo_a_tag(self):
        conn = self.conn([("antiga", "208"), ("nova", "208")])
        identidades = {"cartao": {"itemId": "antiga", "tag": "BTG"}}
        # A conexão nova não tem cartão, mas herda "BTG" pelo código.
        self.assertEqual(px.tags_por_banco(conn, identidades), {"208": "BTG"})

    def test_codigo_com_duas_tags_fica_de_fora(self):
        conn = self.conn([("a", "208"), ("b", "208")])
        identidades = {"x": {"itemId": "a", "tag": "BTG"}, "y": {"itemId": "b", "tag": "Outro"}}
        self.assertEqual(px.tags_por_banco(conn, identidades), {})

    def test_conexao_com_duas_tags_nao_decide(self):
        conn = self.conn([("a", "341")])
        identidades = {"x": {"itemId": "a", "tag": "ITAU"}, "y": {"itemId": "a", "tag": "Empresa"}}
        self.assertEqual(px.tags_por_banco(conn, identidades), {})


if __name__ == "__main__":
    unittest.main()
