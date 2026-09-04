"""Contratos financeiros de formas genéricas; usa somente SQLite em memória."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import cartoes
import fixas


class FormasGenericas(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.patch_path = patch.object(fixas.fin, "DATABASE_PATH", Path(self.pasta.name) / "teste.db")
        self.patch_path.start()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE pluggy_contas (
                conta_id TEXT PRIMARY KEY, item_id TEXT, tipo TEXT, subtipo TEXT,
                nome TEXT, numero TEXT, raw_json TEXT, titular TEXT, moeda TEXT,
                limite_credito REAL, limite_disponivel REAL, vencimento TEXT,
                fechamento TEXT, importado_em TEXT);
            CREATE TABLE pluggy_transacoes (
                transacao_id TEXT PRIMARY KEY, conta_id TEXT, data TEXT, valor REAL,
                descricao TEXT, tipo TEXT);
            CREATE TABLE extrato_categorias (id TEXT PRIMARY KEY, nome TEXT, cor TEXT);
        """)
        self.conn.executescript(fixas.SCHEMA)
        self.conn.execute("ALTER TABLE fixas_descontos ADD COLUMN transacao_id TEXT")
        for fonte, tipo, numero in (("fonte-a", "CREDIT", "8113"),
                                    ("fonte-b", "CREDIT", "7412"),
                                    ("conta-corrente", "BANK", "1234")):
            self.conn.execute(
                "INSERT INTO pluggy_contas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fonte, "conexao-" + fonte, tipo, "CREDIT_CARD" if tipo == "CREDIT" else "CHECKING_ACCOUNT",
                 "Instituição nova", numero, json.dumps({"creditData": {"brand": "MASTERCARD"}}),
                 "", "BRL", 5000, 4000, "2099-09-10", "2099-09-03", "2099-09-01"))
        self.identidades = cartoes.identidades(self.conn)
        self.a = self.identidades["fonte-a"]["cartaoId"]
        self.b = self.identidades["fonte-b"]["cartaoId"]

    def tearDown(self):
        self.conn.close()
        self.patch_path.stop()
        self.pasta.cleanup()

    def fixa(self, chave, forma, conta="", valor=100):
        self.conn.execute(
            "INSERT INTO fixas_contas (id,nome,forma_pagamento,conta_id,valor_previsto,criado_em) "
            "VALUES (?,?,?,?,?,'2099-01-01')", (chave, chave, forma, conta, valor))

    def tag_compartilhada(self):
        tag = cartoes._obter_tag(self.conn, "Viagens")
        self.conn.execute("UPDATE cartoes_catalogo SET tag_id=?", (tag,))
        return tag

    def test_catalogo_descobre_cartoes_novos_sem_confundir_conta_bancaria(self):
        formas = fixas._formas(self.conn)
        self.assertEqual({f["id"] for f in formas}, {"pix", self.a, self.b})
        self.assertEqual({f["nome"] for f in formas}, {"PIX", "MASTERCARD 8113", "MASTERCARD 7412"})

    def test_tag_agrupa_exibicao_e_preserva_referencia_fisica(self):
        tag = self.tag_compartilhada()
        formas = fixas._formas(self.conn)
        self.assertEqual({f["id"] for f in formas}, {"pix", tag})
        mapa = fixas._formas_por_conta(self.conn)
        self.assertEqual(fixas._normalizar_forma(self.a, mapa), tag)
        self.assertEqual(fixas._forma_para_gravar(self.conn, "fonte-a"), self.a)
        self.assertEqual(fixas._forma_para_gravar(self.conn, self.a), self.a)
        self.assertEqual(fixas._forma_para_gravar(self.conn, tag), tag)
        self.conn.execute("UPDATE cartoes_catalogo SET tag_id=NULL WHERE cartao_id=?", (self.a,))
        self.assertEqual(fixas._normalizar_forma(self.a, fixas._formas_por_conta(self.conn)), self.a)

    def test_referencia_desconhecida_nao_vira_pix(self):
        self.fixa("pendente", "referencia-antiga")
        formas = fixas._formas(self.conn)
        pendente = next(f for f in formas if f["id"] == "referencia-antiga")
        self.assertTrue(pendente["pendenteAssociacao"])
        self.assertIn("Associar cartão", pendente["nome"])
        self.assertEqual(fixas._normalizar_forma("referencia-antiga", {}), "referencia-antiga")
        with self.assertRaises(ValueError):
            fixas._forma_para_gravar(self.conn, "qualquer-id-inventado")

    def test_migracao_audita_resolvidos_e_preserva_ambiguos_sem_mudar_valores(self):
        self.tag_compartilhada()
        for chave, fonte in (("tx-a", "fonte-a"), ("tx-b", "fonte-b")):
            self.conn.execute("INSERT INTO pluggy_transacoes VALUES (?,?,'2099-01-01',100,'Compra','DEBIT')",
                              (chave, fonte))
        self.fixa("individual", "inter", "fonte-a")
        self.fixa("com-vinculo", "nubank")
        self.fixa("ambigua", "itau", valor=300)
        self.fixa("sem-vinculo", "inter")
        for fixa, mes, tx in (("com-vinculo", "2099-01", "tx-b"),
                              ("ambigua", "2099-01", "tx-a"),
                              ("ambigua", "2099-02", "tx-b")):
            self.conn.execute("INSERT INTO fixas_mes (fixa_id,mes_ref,transacao_id,forma_pagamento) VALUES (?,?,?,'itau')",
                              (fixa, mes, tx))
        self.conn.execute("INSERT INTO fixas_descontos (id,fixa_id,mes_ref,descricao,valor,forma_pagamento,transacao_id) "
                          "VALUES ('d','individual','2099-01','Parcela',30,'inter','tx-a')")
        antes = self.conn.execute("SELECT SUM(valor_previsto) FROM fixas_contas").fetchone()[0]
        fixas._migrar_formas(self.conn)
        formas = dict(self.conn.execute("SELECT id,forma_pagamento FROM fixas_contas"))
        self.assertEqual(formas, {"individual": self.a, "com-vinculo": self.b,
                                 "ambigua": "itau", "sem-vinculo": "inter"})
        self.assertEqual(self.conn.execute("SELECT forma_pagamento FROM fixas_descontos").fetchone()[0], self.a)
        self.assertEqual(self.conn.execute("SELECT SUM(valor_previsto) FROM fixas_contas").fetchone()[0], antes)
        auditoria = self.conn.execute("SELECT COUNT(*) FROM fixas_formas_migradas").fetchone()[0]
        fixas._migrar_formas(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixas_formas_migradas").fetchone()[0], auditoria)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fixas_migracoes").fetchone()[0], 1)

    def test_reembolso_so_e_permitido_em_desconto(self):
        self.assertEqual(fixas._forma_para_gravar(self.conn, "__reembolso__", reembolso=True), "__reembolso__")
        with self.assertRaises(ValueError):
            fixas._forma_para_gravar(self.conn, "__reembolso__")

    def test_projecoes_nao_dependem_do_nome_da_instituicao(self):
        tag = self.tag_compartilhada()
        payload = {"cartoes": [{"id": tag, "nome": "Viagens"}], "itens": {tag: [
            {"mes": 9, "cartaoId": self.a, "descricao": "Loja 1/3", "valor": 42,
             "parcelaAtual": 1, "parcelaTotal": 3},
            {"mes": 9, "cartaoId": self.b, "descricao": "Loja 1/3", "valor": 85,
             "parcelaAtual": 1, "parcelaTotal": 3}]}}
        with patch.object(fixas.px, "cartoes_payload", return_value=payload):
            projecoes = fixas._projecoes_do_mes("2099-09")
        self.assertEqual(set(projecoes), {tag})
        achado = fixas._casar_projecao(["loja"], projecoes, set(), self.b)
        self.assertEqual((achado["cartaoId"], achado["grupoId"], achado["valor"]), (self.b, tag, 85))
        self.assertIsNone(fixas._casar_projecao(["loja"], projecoes, set(), "pix"))


if __name__ == "__main__":
    unittest.main()
