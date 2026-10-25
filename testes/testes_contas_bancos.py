"""Tela Contas: um card por banco, com o que está parado dito como parado.

Banco temporário de verdade, no molde de `testes_reconexao_cartoes.py`: as
funções abrem conexões próprias e leem o estado das conexões em app_meta.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import banco
import cartoes
import contas_bancos as cb
import pluggy_extrato as px
from importar_pluggy import SCHEMA_PLUGGY

HOJE = date(2026, 9, 24)


class ProximoVencimento(unittest.TestCase):
    def test_data_no_futuro_fica(self):
        self.assertEqual(cb.proximo_vencimento("2026-10-07", HOJE), "2026-10-07")

    def test_data_que_passou_avanca_para_o_mes_seguinte(self):
        self.assertEqual(cb.proximo_vencimento("2026-09-07", HOJE), "2026-10-07")

    def test_dia_31_num_mes_de_30_cai_no_ultimo_dia(self):
        self.assertEqual(cb.proximo_vencimento("2026-08-31", date(2026, 9, 1)), "2026-09-30")

    def test_sem_data_nao_inventa(self):
        self.assertIsNone(cb.proximo_vencimento("", HOJE))


class Mascara(unittest.TestCase):
    def test_so_os_quatro_ultimos(self):
        self.assertEqual(cb.mascarar("0012345678-9"), "···· 6789")

    def test_vazio_fica_vazio(self):
        self.assertEqual(cb.mascarar(""), "")


class ContasPorBanco(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory(prefix="contas_bancos_")
        self.caminho = patch.object(banco, "DATABASE_PATH", Path(self.temporario.name) / "teste.db")
        self.caminho.start()
        self.conn = banco.connect()
        self.conn.execute("CREATE TABLE app_meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
        self.conn.executescript(SCHEMA_PLUGGY)
        self.conn.commit()
        self.preparado = patch.object(banco, "ensure_database", lambda: None)
        self.preparado.start()
        # A fatura do mês é da tela de Cartões e tem teste próprio; aqui ela
        # só precisa existir.
        self.faturas = patch.object(px, "cartoes_payload",
                                    lambda ano, grupo: {"cartoes": [], "valores": {}, "origens": {}})
        self.faturas.start()

    def tearDown(self):
        self.faturas.stop()
        self.conn.close()
        self.preparado.stop()
        self.caminho.stop()
        self.temporario.cleanup()

    def item(self, item_id, importado="2026-09-01"):
        self.conn.execute("INSERT OR IGNORE INTO pluggy_itens VALUES (?,?,?)",
                          (item_id, "MeuPluggy", importado))

    def conta(self, conta_id, item_id, nome="Conta Corrente", codigo="208", numero="12345678", saldo=100):
        self.item(item_id)
        self.conn.execute(
            "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,titular,saldo,"
            "raw_json,importado_em) VALUES (?,?,'BANK','CHECKING_ACCOUNT',?,?,'T',?,?,'2026-09-01')",
            (conta_id, item_id, nome, numero, saldo,
             json.dumps({"bankData": {"transferNumber": f"{codigo}/0001/{numero}-9"}})))

    def cartao(self, conta_id, item_id, final="0846", vencimento="2026-10-05"):
        self.item(item_id)
        self.conn.execute(
            "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,titular,"
            "limite_credito,limite_disponivel,vencimento,raw_json,importado_em) VALUES "
            "(?,?,'CREDIT','CREDIT_CARD','Cartão',?,'T',1000,400,?,?,'2026-09-01')",
            (conta_id, item_id, final, vencimento, json.dumps({"creditData": {"brand": "MASTERCARD"}})))

    def estado(self, arquivadas=(), falhando=(), produtos=None):
        estados = {i: {"resultado": "erro"} for i in falhando}
        self.conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('pluggy_conexoes_arquivadas',?)",
                          (json.dumps(list(arquivadas)),))
        self.conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('pluggy_conexoes_estado',?)",
                          (json.dumps(estados),))
        self.conn.execute("INSERT OR REPLACE INTO app_meta VALUES ('pluggy_conexoes_produtos',?)",
                          (json.dumps(produtos or {}),))

    def tag(self, conta_id, nome):
        self.conn.commit()
        cartao_id = cartoes.identidades(self.conn)[conta_id]["cartaoId"]
        cartoes.salvar({"cartoes": [{"cartaoId": cartao_id, "tag": nome}]})

    def bancos(self):
        self.conn.commit()
        return {b["chave"]: b for b in cb.payload(HOJE)["bancos"]}

    def test_varias_conexoes_do_mesmo_banco_viram_um_card(self):
        self.conta("c1", "velho", numero="11111111")
        self.conta("c2", "novo", numero="22222222")
        self.estado()
        bancos = self.bancos()
        self.assertEqual(list(bancos), ["cod:208"])
        self.assertEqual(len(bancos["cod:208"]["conexoes"]), 2)

    def test_conexao_so_com_cartao_entra_no_banco_pela_tag(self):
        self.conta("corrente", "com-conta")
        self.cartao("cartao-a", "com-conta")
        self.cartao("cartao-b", "so-cartao", final="7777")
        self.tag("cartao-a", "BTG")
        self.tag("cartao-b", "BTG")
        self.estado()
        bancos = self.bancos()
        self.assertEqual(set(bancos), {"cod:208"})
        self.assertEqual(bancos["cod:208"]["nome"], "BTG")

    def test_conexao_vazia_nao_some(self):
        self.conta("c1", "cheio")
        self.item("vazio-de-tudo")
        self.estado()
        bancos = self.bancos()
        self.assertIn("vazio", bancos)
        self.assertEqual(bancos["vazio"]["nome"], "Sem dados")

    def test_instrumento_de_conexao_falhando_sai_congelado(self):
        self.conta("c1", "falha")
        self.cartao("k1", "falha")
        self.estado(falhando=["falha"])
        b = self.bancos()["cod:208"]
        self.assertTrue(b["contas"][0]["congelada"])
        self.assertTrue(b["cartoes"][0]["congelado"])
        self.assertEqual(b["estado"], "erro")

    def test_aviso_quando_o_cartao_so_vem_de_conexao_parada(self):
        # O caso do BTG: a conexão nova só trouxe a conta; o cartão ficou na velha.
        self.conta("c-velha", "velha", numero="11111111")
        self.cartao("k-velho", "velha")
        self.conta("c-nova", "nova", numero="22222222")
        self.estado(falhando=["velha"])
        b = self.bancos()["cod:208"]
        # Consentimento desconhecido: as duas hipóteses, sem mandar reconectar.
        self.assertEqual([a["codigo"] for a in b["avisos"]], ["parou"])
        self.assertIn("Evite reconectar", b["avisos"][0]["texto"])
        self.assertEqual(b["estado"], "aviso")

    def test_dois_produtos_parados_viram_um_aviso_so(self):
        self.conta("c-velha", "velha", numero="11111111")
        self.cartao("k-velho", "velha")
        self.conn.execute("CREATE TABLE pluggy_investimentos (item_id, status, saldo_liquido, "
                          "disponivel_resgate, importado_em)")
        self.conn.execute("INSERT INTO pluggy_investimentos VALUES ('velha','ACTIVE',10,10,'2026-09-01')")
        self.conta("c-nova", "nova", numero="22222222")
        self.estado(falhando=["velha"])
        avisos = self.bancos()["cod:208"]["avisos"]
        self.assertEqual(len(avisos), 1)
        self.assertTrue(avisos[0]["texto"].startswith("Cartão e investimentos pararam"))

    def test_sem_aviso_quando_tudo_vem_da_conexao_viva(self):
        self.conta("c1", "boa")
        self.cartao("k1", "boa")
        self.estado()
        b = self.bancos()["cod:208"]
        self.assertEqual(b["avisos"], [])
        self.assertEqual(b["estado"], "ok")

    def test_resumo_da_faixa(self):
        self.conta("c1", "boa", saldo=250)
        self.cartao("k1", "boa")
        self.estado()
        r = self.bancos()["cod:208"]["resumo"]
        self.assertEqual(r["emConta"], 250)
        self.assertEqual(r["limiteUsadoPct"], 60.0)   # 1000 de limite, 400 disponível
        self.assertEqual(r["proximoVencimento"], "2026-10-05")

    def test_numero_da_conta_nunca_sai_inteiro(self):
        self.conta("c1", "boa", numero="0012345678")
        self.estado()
        conta = self.bancos()["cod:208"]["contas"][0]
        self.assertEqual(conta["final"], "···· 5678")
        self.assertNotIn("0012345678", json.dumps(cb.payload(HOJE)))

    def caso_btg(self, autorizados, avisos_pluggy=()):
        self.conta("c-velha", "velha", numero="11111111")
        self.cartao("k-velho", "velha")
        self.conta("c-nova", "nova", numero="22222222")
        self.estado(falhando=["velha"], produtos={
            "nova": {"autorizados": autorizados, "avisos": list(avisos_pluggy)}})
        return self.bancos()["cod:208"]

    def test_autorizado_e_nao_entregue_aponta_o_limite_sem_afirmar(self):
        # O consentimento do Meu Pluggy lista tudo para todo banco: "autorizado"
        # não prova que o banco compartilhou. O aviso aponta o provável.
        b = self.caso_btg(["ACCOUNTS", "CREDIT_CARDS", "TRANSACTIONS"])
        aviso = next(a for a in b["avisos"] if a["codigo"] == "parou")
        self.assertIn("provável limite mensal", aviso["texto"])
        self.assertIn("Evite reconectar", aviso["texto"])

    def test_fora_do_consentimento_manda_reconectar_marcando(self):
        b = self.caso_btg(["ACCOUNTS", "TRANSACTIONS"])
        aviso = next(a for a in b["avisos"] if a["codigo"] == "fora_do_consentimento")
        self.assertIn("Reconecte", aviso["texto"])
        self.assertNotIn("limite", aviso["texto"])

    def test_autorizado_que_nunca_veio_nao_avisa(self):
        # O falso positivo que apareceu no Nubank: consentimento com
        # investimentos, banco sem investimento nenhum. Nunca veio, não parou.
        self.conta("c1", "nova")
        self.estado(produtos={"nova": {"autorizados": ["CREDIT_CARDS", "INVESTMENTS"], "avisos": []}})
        self.assertEqual(self.bancos()["cod:208"]["avisos"], [])

    def test_sem_cartao_e_sem_autorizacao_nao_avisa(self):
        self.conta("c1", "nova")
        self.estado(produtos={"nova": {"autorizados": ["ACCOUNTS"], "avisos": []}})
        self.assertEqual(self.bancos()["cod:208"]["avisos"], [])

    def test_aviso_da_pluggy_aparece_com_o_texto_dela(self):
        b = self.caso_btg(["CREDIT_CARDS"], [{"produto": "cartões", "codigo": "X",
                                               "texto": "O limite mensal foi atingido."}])
        self.assertIn("Cartões: O limite mensal foi atingido.", [a["texto"] for a in b["avisos"]])

    def test_ultima_transacao_vista_e_informacao(self):
        self.conta("c1", "boa")
        self.conn.execute(
            "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,"
            "valor,tipo,criado_em,importado_em) VALUES ('t','c1','2026-09-10','2026-09',2026,9,'x',1,"
            "'DEBIT','2026-09-24T08:00:00Z','2026-09-24')")
        self.estado()
        b = self.bancos()["cod:208"]
        # A data da compra, não o createdAt: este zera a cada reconexão.
        self.assertEqual(b["contas"][0]["ultimaVista"], "2026-09-10")
        self.assertEqual(b["avisos"], [])     # não vira alarme

    def test_investimentos_e_poupanca_ficam_fora_da_lista_e_da_soma(self):
        self.conta("corrente", "item", nome="Conta Corrente", numero="11111111", saldo=100)
        self.conta("corretora", "item", nome="BTG Investimentos", numero="22222222", saldo=50)
        self.conn.execute("UPDATE pluggy_contas SET subtipo='SAVINGS_ACCOUNT' WHERE conta_id='corretora'")
        self.conta("poup", "item", nome="Poupança", numero="33333333", saldo=30)
        self.conn.execute("UPDATE pluggy_contas SET subtipo='SAVINGS_ACCOUNT' WHERE conta_id='poup'")
        self.estado()
        b = self.bancos()["cod:208"]
        self.assertEqual([c["contaId"] for c in b["contas"]], ["corrente"])
        self.assertEqual(b["resumo"]["emConta"], 100)

class LerProdutos(unittest.TestCase):
    """O que a Pluggy devolve em /items e /consents, lido sem rede."""

    def test_status_detail_vazio_nao_inventa_aviso(self):
        estado = __import__("atualizar_pluggy").ler_produtos({"statusDetail": None}, [])
        self.assertEqual(estado["avisos"], [])
        self.assertEqual(estado["autorizados"], [])

    def test_aviso_por_produto_prefere_o_texto_do_banco(self):
        item = {"statusDetail": {"creditCards": {"isUpdated": False, "warnings": [
            {"code": "001", "message": "genérico", "providerMessage": "Limite mensal atingido"}]}}}
        estado = __import__("atualizar_pluggy").ler_produtos(item, None)
        self.assertEqual(estado["avisos"], [{"produto": "cartões", "codigo": "001",
                                             "texto": "Limite mensal atingido"}])
        self.assertIsNone(estado["autorizados"])   # None = não consegui consultar

    def test_produto_nao_atualizado_sem_texto_ainda_avisa(self):
        item = {"statusDetail": {"investments": {"isUpdated": False, "warnings": []}}}
        estado = __import__("atualizar_pluggy").ler_produtos(item, [])
        self.assertEqual(estado["avisos"][0]["codigo"], "nao_atualizado")

    def test_consentimento_revogado_nao_conta(self):
        estado = __import__("atualizar_pluggy").ler_produtos({}, [
            {"products": ["CREDIT_CARDS"], "revokedAt": "2026-09-01"},
            {"products": ["ACCOUNTS"], "revokedAt": None}])
        self.assertEqual(estado["autorizados"], ["ACCOUNTS"])


class SilencioForaDoPadrao(unittest.TestCase):
    """O laranja do "Dado mais recente", relativo ao uso do banco."""

    def test_banco_diario_acende_em_poucos_dias(self):
        r = cb.silencio_fora_do_padrao(dias_com_movimento=80, dias_sem_novidade=5)
        self.assertTrue(r["fora"])
        self.assertLessEqual(r["esperadoAte"], 4)

    def test_banco_pouco_usado_nao_acende_em_tres_dias(self):
        # O caso da pergunta: não usar um banco que já é pouco usado.
        r = cb.silencio_fora_do_padrao(dias_com_movimento=4, dias_sem_novidade=21)
        self.assertFalse(r["fora"])

    def test_nunca_antes_do_minimo(self):
        r = cb.silencio_fora_do_padrao(dias_com_movimento=90, dias_sem_novidade=3)
        self.assertEqual(r["esperadoAte"], 3)
        self.assertFalse(r["fora"])

    def test_sem_historico_nao_acende(self):
        r = cb.silencio_fora_do_padrao(dias_com_movimento=0, dias_sem_novidade=40)
        self.assertIsNone(r["esperadoAte"])
        self.assertFalse(r["fora"])


class LogoDoBanco(unittest.TestCase):
    """Logo colorida ou branca, pela cor do próprio arquivo."""

    def test_logo_escura_vira_branca(self):
        self.assertTrue(cb.logo_do_banco("208")["branca"])     # BTG, azul-marinho

    def test_logo_sem_cor_explicita_conta_como_preta(self):
        self.assertTrue(cb.logo_do_banco("336")["branca"])     # C6, sem fill

    def test_logo_com_contraste_fica_colorida(self):
        self.assertFalse(cb.logo_do_banco("341")["branca"])    # Itaú, laranja

    def test_banco_sem_arquivo_nao_tem_logo(self):
        self.assertIsNone(cb.logo_do_banco("422"))             # Safra
        self.assertIsNone(cb.logo_do_banco(""))

    def test_toda_logo_mapeada_existe(self):
        for codigo, slug in cb.LOGO_POR_CODIGO.items():
            self.assertTrue((cb.PASTA_LOGOS / f"{slug}.svg").exists(), f"{codigo}: {slug}.svg")



if __name__ == "__main__":
    unittest.main()
