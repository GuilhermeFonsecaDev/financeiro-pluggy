"""Condições E / OU / NÃO das regras (extrato, entradas e vínculo das fixas).

Descrições e números fictícios.
"""
import sqlite3
import unittest

import testes  # noqa: F401  (backend no sys.path)
import condicoes as cond
import fixas

PIX = "Pix recebido 123.456.789-00 FULANO DE TAL"


def c(termo, operador="contem", ligacao="OU"):
    return {"termo": termo, "operador": operador, "ligacao": ligacao}


class Avaliacao(unittest.TestCase):
    def test_termos_antigos_continuam_ou(self):
        self.assertTrue(cond.casa(PIX, ["enviado", "fulano"]))
        self.assertFalse(cond.casa(PIX, ["enviado", "ciclano"]))

    def test_e_exige_as_duas(self):
        self.assertTrue(cond.casa(PIX, [c("12345678900"), c("recebido", ligacao="E")]))
        self.assertFalse(cond.casa("Pix enviado 123.456.789-00", [c("12345678900"), c("recebido", ligacao="E")]))

    def test_numero_sem_pontuacao_casa_com_formatado(self):
        self.assertTrue(cond.casa(PIX, [c("12345678900")]))
        self.assertTrue(cond.casa("PIX 12345678900", [c("123.456.789-00")]))

    def test_nao_contem_exclui(self):
        self.assertFalse(cond.casa(PIX, [c("pix"), c("recebido", "nao_contem", "E")]))
        self.assertTrue(cond.casa("Pix enviado", [c("pix"), c("recebido", "nao_contem", "E")]))

    def test_so_nao_contem_nunca_casa(self):
        self.assertFalse(cond.casa("qualquer coisa", [c("pix", "nao_contem")]))
        with self.assertRaises(ValueError):
            cond.limpar([c("pix", "nao_contem")])

    def test_e_vale_antes_do_ou(self):
        # "fulano OU x1 E x2" = fulano ou (x1 e x2)
        self.assertTrue(cond.casa(PIX, [c("fulano"), c("x1"), c("x2", ligacao="E")]))
        # "x1 E x2 OU fulano" = (x1 e x2) ou fulano
        self.assertTrue(cond.casa(PIX, [c("x1"), c("x2", ligacao="E"), c("fulano")]))
        # "fulano E x1 OU x2" = (fulano e x1) ou x2
        self.assertFalse(cond.casa(PIX, [c("fulano"), c("x1", ligacao="E"), c("x2")]))

    def test_acento_maiuscula_comeca_igual(self):
        self.assertTrue(cond.casa(PIX, [c("PIX RECEBÍDO")]))
        self.assertTrue(cond.casa(PIX, [c("pix rec", "comeca_com")]))
        self.assertFalse(cond.casa(PIX, [c("pix rec", "igual")]))
        self.assertTrue(cond.casa("Pagamento", [c("pagamento", "igual")]))

    def test_condicao_vazia_ignorada(self):
        self.assertTrue(cond.casa(PIX, [c("pix"), c("   ", ligacao="E")]))

    def test_operador_padrao_da_regra(self):
        self.assertTrue(cond.casa(PIX, [("pix rec", "", "OU")], "comeca_com"))
        self.assertFalse(cond.casa(PIX, [("recebido", "", "OU")], "comeca_com"))

    def test_limpar(self):
        lista = cond.limpar([c(" A ", ligacao="E"), c("a"), c("b", "x", "?"), "c"])
        self.assertEqual([x["termo"] for x in lista], ["A", "b", "c"])
        self.assertEqual(lista[0]["ligacao"], "OU")   # a primeira não liga a nada
        self.assertEqual(lista[1]["operador"], "contem")
        self.assertEqual(lista[1]["ligacao"], "OU")

    def test_sql(self):
        conn = sqlite3.connect(":memory:")
        cond.registrar(conn)
        js = '[["12345678900","contem","OU"],["recebido","","E"]]'
        self.assertEqual(conn.execute("SELECT casa_cond('contem', ?, ?)", (js, PIX)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT casa_cond('contem', ?, ?)", (js, "Pix enviado 12345678900")).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT casa_cond('contem', NULL, ?)", (PIX,)).fetchone()[0], 0)


class VinculoFixa(unittest.TestCase):
    def test_parcela_no_termo_e_condicoes(self):
        conds = [c("LOJA EXEMPLO01/05"), c("estorno", "nao_contem", "E")]
        self.assertTrue(fixas.casa_fixa("LOJA EXEMPLO03/05", conds))
        self.assertFalse(fixas.casa_fixa("LOJA EXEMPLO03/05 ESTORNO", conds))

    def test_termos_soltos_sao_ou(self):
        self.assertTrue(fixas.casa_fixa("CLARO FIXO", [{"termo": "vivo"}, {"termo": "claro"}]))


if __name__ == "__main__":
    unittest.main()
