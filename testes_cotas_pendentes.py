"""Compras de cotas vistas no extrato antes da API de investimentos.

Execute: python -m unittest testes_cotas_pendentes -v
"""

import unittest

import investimentos as inv

ATIVOS = [{"nome": n, "investimento_id": n} for n in (
    "V8 CASH PLATINUM FIC RF CrPr", "EVEREST 90 FIC FIDC", "Absolute Hidra CDI Plus P Infra RF RL",
    "WESTERN ASSET FIA BDR NIVEL I", "ARBOR GL EQUITIES FIA - BDR NIVEL I", "MAG CASH 10 FIRF CrPr",
    "Solis Capital Antares Pioneiro FICFIDC")]


class FundoDoExtrato(unittest.TestCase):
    def fundo(self, abreviado):
        achado = inv._fundo_do_extrato(abreviado, ATIVOS)
        return achado["nome"] if achado else None

    def test_nomes_abreviados_do_btg(self):
        self.assertEqual(self.fundo("v8 cas plt fc rf pco"), "V8 CASH PLATINUM FIC RF CrPr")
        self.assertEqual(self.fundo("everest 90 fidc pco"), "EVEREST 90 FIC FIDC")
        self.assertEqual(self.fundo("abs hidra infra pco"), "Absolute Hidra CDI Plus P Infra RF RL")
        self.assertEqual(self.fundo("west asset fia pco"), "WESTERN ASSET FIA BDR NIVEL I")
        self.assertEqual(self.fundo("arbor fic fia pco"), "ARBOR GL EQUITIES FIA - BDR NIVEL I")
        self.assertEqual(self.fundo("mag cash firf pco"), "MAG CASH 10 FIRF CrPr")
        self.assertEqual(self.fundo("solis a pioneiro pco"), "Solis Capital Antares Pioneiro FICFIDC")

    def test_fundo_sem_posicao_nao_chuta(self):
        self.assertIsNone(self.fundo("mapfre plus firf pco"))

    def test_padrao_da_descricao(self):
        achado = inv._COTAS.search(inv._norm("004541878 - AQUISICAO DE COTAS NO FUNDO EVEREST 90 FIDC PCO"))
        self.assertEqual(achado[1], "aquisicao")
        self.assertEqual(achado[2], "everest 90 fidc pco")


if __name__ == "__main__":
    unittest.main()
