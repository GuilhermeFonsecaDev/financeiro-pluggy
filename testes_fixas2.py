"""Contas Fixas 2.0: cada real conta uma vez.

A regra é testada com o que `fixas.mes_payload` entregaria (casamento com
transação e com parcela projetada já feito lá), e os reembolsos num banco
temporário de verdade.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import banco
import extrato_camada as cam
import fixas
import fixas2
import importar_pluggy

FORMAS = [{"id": "pix", "nome": "PIX", "tipo": "pagamento"},
          {"id": "tag:btg", "nome": "BTG", "tipo": "cartao"}]


def item(id_, valor, forma, transacao=None, projecao=None, descontos=(), incluida=True, **extra):
    return {"id": id_, "nome": id_.upper(), "valor": valor, "forma": forma,
            "formaNome": "", "transacao": transacao, "projecao": projecao,
            "incluidaCalculos": incluida, "descontos": list(descontos), **extra}


def desconto(id_, valor, forma, transacao=None, projecao=None, reembolso=False):
    return {"id": id_, "descricao": id_.upper(), "valor": valor, "forma": forma,
            "transacao": transacao, "projecao": projecao, "reembolso": reembolso}


def fatura(valor, fechada=False, cid="tag:btg", cobrado=None, parcelas=0.0, habitos=0.0):
    return {"id": cid, "nome": "BTG", "valor": valor, "origem": "", "fechada": fechada,
            "cobrado": valor - parcelas - habitos if cobrado is None else cobrado,
            "parcelas": parcelas, "habitos": habitos}


TX = {"data": "2026-10-03", "descricao": "COBRANCA", "valor": 10}
PROJ = {"descricao": "PARCELA", "parcela": "7/10", "valor": 10}


def reemb(valor, como="dinheiro"):
    return {"valor": valor, "como": como, "desconta": como != "estorno"}


class Regra(unittest.TestCase):
    def calcular(self, itens, faturas=(), reembolsos=(), mes="2026-10"):
        with mock.patch.object(fixas, "mes_payload", return_value={"itens": itens, "formas": FORMAS}), \
             mock.patch.object(fixas2, "_faturas", return_value=(list(faturas), {"tag:btg": "tag:btg"})), \
             mock.patch.object(fixas2, "_reembolsos", return_value=list(reembolsos)), \
             mock.patch.object(fixas2, "_divergencias_de_habitos", return_value=[]), \
             mock.patch.object(fixas2, "_abrir"):
            return fixas2.payload(mes)

    def test_cartao_lancado_nao_soma(self):
        p = self.calcular([item("netflix", 50, "tag:btg", transacao=TX)])
        self.assertEqual(p["itens"][0]["status"], "lancada")
        self.assertEqual(p["resumo"]["total"], 0)

    def test_cartao_sem_cobranca_soma_como_prevista(self):
        p = self.calcular([item("netflix", 50, "tag:btg")])
        self.assertEqual(p["itens"][0]["status"], "prevista")
        self.assertEqual(p["resumo"]["previstos"], 50)

    def test_cartao_com_parcela_projetada_nao_soma(self):
        p = self.calcular([item("tenis", 100, "tag:btg", projecao=PROJ)])
        self.assertEqual(p["itens"][0]["status"], "lancada")
        self.assertEqual(p["resumo"]["total"], 0)

    def test_pix_soma_mesmo_com_transacao(self):
        p = self.calcular([item("aluguel", 1000, "pix", transacao=TX)])
        self.assertEqual(p["resumo"]["foraCartao"], 1000)

    def test_informativa_nao_soma(self):
        p = self.calcular([item("inss", 355, "pix", incluida=False)])
        self.assertEqual(p["resumo"]["total"], 0)

    def test_cadastrada_pix_mas_na_fatura_projetada_nao_soma(self):
        p = self.calcular([item("servico", 80, "pix", projecao=PROJ)])
        self.assertEqual(p["itens"][0]["status"], "lancada")
        self.assertEqual(p["resumo"]["total"], 0)

    def test_pix_com_transacao_de_cartao_conta_como_cartao(self):
        p = self.calcular([item("algo", 50, "pix", transacao=dict(TX, cartaoId="cartao:x"))])
        self.assertEqual(p["itens"][0]["status"], "lancada")

    def test_mae_soma_seiscentos_uma_vez_so(self):
        mae = item("mae", 600, "pix", descontos=[
            desconto("emprestimo", 275, "pix"),
            desconto("gympass", 100, "tag:btg", transacao=TX),
            desconto("tenis", 100, "tag:btg"),
        ])
        p = self.calcular([mae], faturas=[fatura(100)])
        self.assertEqual(p["itens"][0]["liquido"], 125)
        # 100 da fatura (gympass) + 125 transferência + 275 PIX + 100 previsto
        self.assertEqual(p["resumo"]["total"], 600)

    # --- 2: fatura fechada não tem previsão --------------------------------
    def test_fatura_fechada_sem_cobranca_nao_soma_e_avisa(self):
        p = self.calcular([item("academia", 100, "tag:btg")], faturas=[fatura(500, fechada=True)])
        self.assertEqual(p["itens"][0]["status"], "nao_cobrada")
        self.assertEqual(p["resumo"]["total"], 500)
        self.assertTrue(any("ACADEMIA" in a for a in p["avisos"]))

    def test_mes_passado_sem_cobranca_nao_soma(self):
        p = self.calcular([item("academia", 100, "tag:btg")], mes="2026-06")
        self.assertEqual(p["itens"][0]["status"], "nao_cobrada")
        self.assertEqual(p["resumo"]["previstos"], 0)

    def test_fatura_aberta_continua_prevendo(self):
        p = self.calcular([item("academia", 100, "tag:btg")], faturas=[fatura(500)])
        self.assertEqual(p["resumo"]["total"], 600)

    # --- 1: como o reembolso voltou ----------------------------------------
    def test_reembolso_em_dinheiro_desconta(self):
        p = self.calcular([], faturas=[fatura(1000)], reembolsos=[reemb(149)])
        self.assertEqual(p["resumo"]["total"], 851)

    def test_estorno_no_cartao_nao_desconta_de_novo(self):
        # A fatura já veio com o estorno abatido: descontar de novo sumiria
        # com dinheiro que nunca saiu.
        p = self.calcular([], faturas=[fatura(1000)], reembolsos=[reemb(149, "estorno")])
        self.assertEqual(p["resumo"]["reembolsos"], 0)
        self.assertEqual(p["resumo"]["total"], 1000)

    # --- 3: uma fonte só de reembolso --------------------------------------
    def test_subdesconto_reembolso_nao_e_contado_na_2(self):
        gpt = item("gpt", 113, "tag:btg", descontos=[desconto("grupo", 100, fixas.FORMA_REEMBOLSO, reembolso=True)])
        p = self.calcular([gpt])
        self.assertEqual(p["itens"][0]["descontos"], [])
        self.assertEqual(p["resumo"]["total"], 113)

    # --- 6: conta-pai com subdescontos usa o valor bruto -------------------
    def test_valor_da_transferencia_nao_zera_a_conta(self):
        mae = item("mae", 60, "pix", transacao=TX, origemValor="transacao", valorPrevisto=600,
                   descontos=[desconto("emprestimo", 540, "pix")])
        p = self.calcular([mae])
        self.assertEqual(p["itens"][0]["liquido"], 60)
        self.assertEqual(p["resumo"]["total"], 600)

    # --- 7: subdescontos acima da conta ------------------------------------
    def test_subdescontos_acima_da_conta_avisa(self):
        p = self.calcular([item("mae", 100, "pix", descontos=[desconto("x", 150, "pix")])])
        self.assertTrue(any("passam do valor" in a for a in p["avisos"]))

    # --- 5: partes da fatura -----------------------------------------------
    def test_partes_da_fatura_somam_a_fatura(self):
        p = self.calcular([], faturas=[fatura(1000, parcelas=200, habitos=300)])
        r = p["resumo"]
        self.assertEqual(r["faturasCobrado"] + r["faturasParcelas"] + r["faturasHabitos"], r["faturas"])

    def test_total_e_a_soma_das_partes_menos_reembolsos(self):
        p = self.calcular(
            [item("aluguel", 1000, "pix"), item("netflix", 50, "tag:btg")],
            faturas=[fatura(2000)], reembolsos=[reemb(300)])
        r = p["resumo"]
        self.assertEqual(r["total"], r["faturas"] + r["previstos"] + r["foraCartao"] - r["reembolsos"])
        self.assertEqual(r["total"], 2750)


class Reembolsos(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.original = banco.DATABASE_PATH
        banco.DATABASE_PATH = pathlib.Path(self.pasta.name) / "teste.db"
        fixas._pronto = False
        cam._camada_pronta = False
        banco.ensure_database()
        with banco.connect() as base:
            base.executescript(importar_pluggy.SCHEMA_PLUGGY)
            base.commit()

    def tearDown(self):
        banco.DATABASE_PATH = self.original
        fixas._pronto = False
        cam._camada_pronta = False
        self.pasta.cleanup()

    def lista(self, mes):
        with fixas2._abrir() as conn:
            return fixas2._reembolsos(conn, mes, {})

    def test_cria_edita_e_remove(self):
        novo = fixas2.criar_reembolso({"mes": "2026-10", "descricao": "INGRESSO", "valor": "149,90"})["id"]
        self.assertEqual([r["valor"] for r in self.lista("2026-10")], [149.9])
        self.assertEqual(self.lista("2026-11"), [])
        fixas2.editar_reembolso(novo, {"mes": "2026-10", "descricao": "INGRESSO", "valor": 100, "como": "estorno"})
        r = self.lista("2026-10")[0]
        self.assertEqual((r["valor"], r["como"], r["desconta"]), (100, "estorno", False))
        fixas2.remover_reembolso(novo)
        self.assertEqual(self.lista("2026-10"), [])

    def test_valor_obrigatorio(self):
        with self.assertRaises(ValueError):
            fixas2.criar_reembolso({"mes": "2026-10", "descricao": "X", "valor": 0})

    def test_subdesconto_reembolso_vira_linha_da_tabela_so_na_2(self):
        with fixas._abrir() as conn:
            conn.execute("INSERT INTO fixas_contas (id, nome, valor_previsto, criado_em) VALUES ('fx1','GPT',113,'2026-01-01')")
            conn.execute("INSERT INTO fixas_descontos (id, fixa_id, mes_ref, descricao, valor, reembolso, forma_pagamento) "
                         "VALUES ('dc1','fx1','2026-11','GRUPO',100,1,?)", (fixas.FORMA_REEMBOLSO,))
            conn.commit()
        with fixas.usar_copia_2():
            fixas._abrir().close()          # cria a cópia da 2.0
        self.assertEqual([r["descricao"] for r in self.lista("2026-11")], ["GPT › GRUPO"])
        with fixas._abrir() as conn:      # a tela original não perdeu nada
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fixas_descontos").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM f2_fixas_descontos").fetchone()[0], 0)

    def test_2_recusa_subdesconto_reembolso(self):
        with fixas._abrir() as conn:
            conn.execute("INSERT INTO fixas_contas (id, nome, valor_previsto, criado_em) VALUES ('fx1','GPT',113,'2026-01-01')")
            conn.commit()
        with fixas.usar_copia_2(), self.assertRaises(ValueError):
            fixas.salvar_desconto("2026-11", "fx1", {"descricao": "GRUPO", "valor": 100, "forma": fixas.FORMA_REEMBOLSO})


if __name__ == "__main__":
    unittest.main()
