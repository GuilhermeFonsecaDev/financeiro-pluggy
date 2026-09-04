"""Reconexão tardia de cartões, usando somente SQLite temporário."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import banco
import cartoes
from importar_pluggy import SCHEMA_PLUGGY


class ReconexaoTardiaTest(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory(prefix="reconexao_cartoes_")
        self.caminho = patch.object(banco, "DATABASE_PATH", Path(self.temporario.name) / "teste.db")
        self.caminho.start()
        self.conn = banco.connect()
        self.conn.execute("CREATE TABLE app_meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
        self.conn.executescript(SCHEMA_PLUGGY)
        self.preparado = patch.object(banco, "ensure_database", lambda: None)
        self.preparado.start()

    def tearDown(self):
        self.conn.close()
        self.preparado.stop()
        self.caminho.stop()
        self.temporario.cleanup()

    def fonte(self, nome, item=None, banco_nome="Banco Teste"):
        item = item or f"item-{nome}"
        self.conn.execute("INSERT OR IGNORE INTO pluggy_itens VALUES (?,?,?)",
                          (item, banco_nome, "2026-09-01"))
        self.conn.execute(
            "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,"
            "titular,raw_json,importado_em) VALUES (?,?,'CREDIT','CREDIT_CARD',"
            "'VISA','1234','Teste',?,'2026-09-01')",
            (nome, item, json.dumps({"creditData": {"brand": "VISA"}})),
        )
        self.conn.commit()

    def historico(self, fonte):
        for dia in (1, 2, 3):
            self.conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,"
                "descricao,valor,tipo,importado_em) VALUES (?,?,?,'2026-08',2026,8,"
                "'Compra de teste',10,'DEBIT','2026-09-01')",
                (f"{fonte}-{dia}", fonte, f"2026-08-{dia:02d}"),
            )
        self.conn.commit()

    def identidade(self, fonte):
        return cartoes.identidades(self.conn)[fonte]["cartaoId"]

    def preparar_tardia(self):
        self.fonte("antiga")
        self.historico("antiga")
        antiga = self.identidade("antiga")
        self.fonte("nova")
        nova = self.identidade("nova")
        self.assertNotEqual(antiga, nova)
        return antiga, nova

    def tag(self, cartao_id, nome):
        cartoes.salvar({"cartoes": [{"cartaoId": cartao_id, "tag": nome}]})

    def test_historico_tardio_preserva_tag_id_e_referencias(self):
        antiga, nova = self.preparar_tardia()
        self.tag(antiga, "Pessoal")
        for tabela in ("fixas_contas", "fixas_mes", "fixas_descontos"):
            self.conn.execute(f"CREATE TABLE {tabela} (forma_pagamento TEXT, conta_id TEXT)")
            self.conn.execute(f"INSERT INTO {tabela} VALUES (?,?)", (nova, "nova"))
        self.conn.commit()
        self.historico("nova")
        mapa = cartoes.identidades(self.conn)
        self.assertEqual({i["cartaoId"] for i in mapa.values()}, {antiga})
        self.assertEqual({i["tag"] for i in mapa.values()}, {"Pessoal"})
        self.assertEqual(len(cartoes.fontes_ativas(self.conn)), 1)
        for tabela in ("fixas_contas", "fixas_mes", "fixas_descontos"):
            linha = self.conn.execute(f"SELECT * FROM {tabela}").fetchone()
            self.assertEqual(linha["forma_pagamento"], antiga)
            self.assertEqual(linha["conta_id"], "nova")  # Vínculo da fonte é imutável.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pluggy_transacoes").fetchone()[0], 6)
        self.assertEqual(self.identidade("nova"), antiga)  # Repetir é idempotente.

    def test_preferencia_unica_na_fonte_nova_acompanha_identidade_antiga(self):
        antiga, nova = self.preparar_tardia()
        self.tag(nova, "Empresa")
        self.historico("nova")
        mapa = cartoes.identidades(self.conn)
        self.assertEqual(mapa["nova"]["cartaoId"], antiga)
        self.assertEqual(mapa["antiga"]["tag"], "Empresa")

    def test_tags_conflitantes_nao_sao_unidas(self):
        antiga, nova = self.preparar_tardia()
        self.tag(antiga, "Pessoal")
        self.tag(nova, "Empresa")
        self.historico("nova")
        self.assertEqual({i["cartaoId"] for i in cartoes.identidades(self.conn).values()}, {antiga, nova})

    def test_remocao_explicita_da_tag_nao_e_sobrescrita(self):
        antiga, nova = self.preparar_tardia()
        self.tag(antiga, "Pessoal")
        self.tag(nova, "")
        self.historico("nova")
        self.assertEqual({i["cartaoId"] for i in cartoes.identidades(self.conn).values()}, {antiga, nova})

    def test_instituicoes_distintas_nao_unem_cartoes_com_mesmo_final(self):
        for fonte, instituicao in (("a", "Banco Alpha"), ("b", "Banco Beta")):
            self.fonte(fonte, banco_nome=instituicao)
            self.historico(fonte)
        self.assertEqual(len(cartoes.colunas(self.conn)), 2)

    def test_candidatos_ambiguos_na_mesma_conexao_nao_se_fundem(self):
        for fonte, item in (("a", "item-1"), ("b", "item-1"), ("c", "item-2")):
            self.fonte(fonte, item=item)
            self.identidade(fonte)
        for fonte in ("a", "b", "c"):
            self.historico(fonte)
        self.assertEqual(len(cartoes.colunas(self.conn)), 3)

    def test_identidade_reconectada_nao_absorve_cartao_que_coexiste(self):
        self.fonte("antiga", item="item-antigo")
        self.historico("antiga")
        id_antiga = self.identidade("antiga")

        self.fonte("reconectada", item="item-novo")
        self.historico("reconectada")
        self.assertEqual(self.identidade("reconectada"), id_antiga)

        self.fonte("outro", item="item-novo")
        self.historico("outro")
        mapa = cartoes.identidades(self.conn)
        self.assertNotEqual(mapa["outro"]["cartaoId"], id_antiga)
        self.assertEqual(len({i["cartaoId"] for i in mapa.values()}), 2)

    def test_reconciliacao_nao_confirma_transacao_do_chamador(self):
        antiga, nova = self.preparar_tardia()
        self.historico("nova")
        self.conn.execute("INSERT INTO app_meta VALUES ('marcador_externo','1')")
        mapa = cartoes.identidades(self.conn)
        self.assertEqual({i["cartaoId"] for i in mapa.values()}, {antiga})
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM app_meta WHERE chave='marcador_externo'").fetchone())
        fontes = self.conn.execute("SELECT DISTINCT cartao_id FROM cartoes_fontes").fetchall()
        self.assertEqual({r[0] for r in fontes}, {antiga, nova})


if __name__ == "__main__":
    unittest.main()
