"""Reconexão de conta bancária, usando somente SQLite temporário.

O caso que motivou: ao reconectar o BTG, a conta corrente voltou com outro nome
("BTG Banking" virou "Conta Corrente") e sem titular. Como o nome faz parte da
chave da conta, as duas versões ficavam ativas e toda transação aparecia duas
vezes. E a "BTG Investimentos", de outra conexão, tem o MESMO número -- então
número sozinho não serve: é o histórico que diz quem é quem.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import banco
import pluggy_extrato as px
from importar_pluggy import SCHEMA_PLUGGY


class ReconexaoDeContaTest(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory(prefix="reconexao_contas_")
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

    def conta(self, conta_id, item, nome, numero="12345678", titular="Titular",
              importada="2026-09-01"):
        self.conn.execute("INSERT OR IGNORE INTO pluggy_itens VALUES (?,?,?)",
                          (item, "MeuPluggy", importada))
        self.conn.execute(
            "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,"
            "titular,raw_json,importado_em) VALUES (?,?,'BANK','CHECKING_ACCOUNT',?,?,?,'{}',?)",
            (conta_id, item, nome, numero, titular, importada))
        self.conn.commit()

    def historico(self, conta_id, descricao="Pix enviado", dias=(1, 2, 3), valor=50):
        for dia in dias:
            self.conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,"
                "descricao,valor,tipo,importado_em) VALUES (?,?,?,'2026-09',2026,9,?,?,'DEBIT','2026-09-01')",
                (f"{conta_id}-{descricao}-{dia}", conta_id, f"2026-09-{dia:02d}", descricao, valor))
        self.conn.commit()

    def ativas(self):
        return px.contas_ativas(self.conn)

    def test_conta_renomeada_na_reconexao_fica_uma_so(self):
        self.conta("antiga", "item-velho", "BTG Banking")
        self.historico("antiga")
        self.conta("nova", "item-novo", "Conta Corrente", importada="2026-09-20")
        self.historico("nova")
        self.historico("nova", descricao="Pix recebido", dias=(4,))  # a nova é mais recente
        self.assertEqual(self.ativas(), {"nova"})

    def test_titular_vazio_na_versao_nova_nao_impede(self):
        # Foi assim que o BTG voltou: sem titular. Vazio não é evidência contra.
        self.conta("antiga", "item-velho", "BTG Banking")
        self.historico("antiga")
        self.conta("nova", "item-novo", "Conta Corrente", titular="", importada="2026-09-20")
        self.historico("nova")
        self.historico("nova", descricao="Pix recebido", dias=(4,))
        self.assertEqual(self.ativas(), {"nova"})

    def test_titulares_diferentes_continuam_duas(self):
        self.conta("a", "item-1", "Conta", titular="Fulano")
        self.historico("a")
        self.conta("b", "item-2", "Conta Corrente", titular="Beltrano")
        self.historico("b")
        self.assertEqual(self.ativas(), {"a", "b"})

    def test_mesmo_numero_com_historico_diferente_continuam_duas(self):
        # A "BTG Investimentos" divide número com a corrente, em outra conexão.
        self.conta("corrente", "item-1", "BTG Banking")
        self.historico("corrente")
        self.conta("invest", "item-2", "BTG Investimentos")
        self.historico("invest", descricao="Resgate CDB", dias=(10, 11, 12), valor=900)
        self.assertEqual(self.ativas(), {"corrente", "invest"})

    def test_tres_com_o_mesmo_numero_so_as_iguais_se_juntam(self):
        # O caso real: Investimentos, Banking e Conta Corrente, todas com o mesmo
        # número. Só as duas versões da corrente viram uma; a de investimentos fica.
        self.conta("invest", "item-a", "BTG Investimentos")
        self.historico("invest", descricao="Resgate CDB", dias=(10, 11, 12), valor=900)
        self.conta("banking", "item-b", "BTG Banking")
        self.historico("banking")
        self.conta("corrente", "item-c", "Conta Corrente", titular="", importada="2026-09-20")
        self.historico("corrente")
        self.historico("corrente", descricao="Pix recebido", dias=(4,))
        self.assertEqual(self.ativas(), {"invest", "corrente"})

    def test_mesma_conexao_nunca_se_junta(self):
        # Uma conexão pode ter duas contas de mesmo número de verdade.
        self.conta("a", "item-1", "BTG Banking")
        self.historico("a")
        self.conta("b", "item-1", "BTG Investimentos")
        self.historico("b")
        self.assertEqual(self.ativas(), {"a", "b"})

    def test_sem_historico_suficiente_continuam_duas(self):
        self.conta("a", "item-1", "BTG Banking")
        self.historico("a", dias=(1, 2))
        self.conta("b", "item-2", "Conta Corrente")
        self.historico("b", dias=(1, 2))
        self.assertEqual(self.ativas(), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
