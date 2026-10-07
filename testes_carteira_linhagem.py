"""Linhagem de fundos e calculadora de rebalanceamento só com aportes.

Execute: python -m unittest testes_carteira_linhagem -v

Um fundo fecha para novos aportes e outro ocupa o lugar dele ("vaga"). A
posição do fechado continua aplicada e soma na vaga; o dinheiro novo vai para
o fundo aberto da ponta, priorizando as vagas mais abaixo do % alvo.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import banco
import carteira


def cnpj(base: str) -> str:
    """CNPJ com dígitos verificadores corretos a partir de 12 dígitos."""
    def dv(numeros, pesos):
        soma = sum(int(n) * p for n, p in zip(numeros, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)
    d1 = dv(base, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = dv(base + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return base + d1 + d2


A, B, C, X = cnpj("111111110001"), cnpj("222222220001"), cnpj("333333330001"), cnpj("444444440001")


def item(cnpj_, pct, atual=0.0, fechado="", iq=False, ordem=0):
    return {"cnpj": cnpj_, "percentual": pct, "atualVaga": atual, "fechadoEm": fechado,
            "qualificado": "sim" if iq else "nao", "ordem": ordem}


class Rebalanceamento(unittest.TestCase):
    """A conta pura, sem banco."""

    def test_aporte_vai_para_a_vaga_mais_abaixo_do_alvo(self):
        # 50/50, mas A já tem 1000 e B nada: 400 de aporte vão todos para B.
        lista = [item(A, 50, 1000), item(B, 50, 0, ordem=1)]
        carteira._distribuir_rebalanceando(lista, 400)
        self.assertEqual([i["aporte"] for i in lista], [0, 400])

    def test_aporte_maior_que_o_deficit_reparte_a_sobra_pelo_alvo(self):
        # Déficit de B = 500 (alvo 50% de 2000+...); o que passa segue 50/50.
        lista = [item(A, 50, 1000), item(B, 50, 0, ordem=1)]
        resumo = carteira._distribuir_rebalanceando(lista, 2000)
        self.assertEqual(round(sum(i["aporte"] for i in lista), 2), 2000)
        # Depois do aporte as duas vagas ficam iguais (1500 cada).
        self.assertEqual([i["aporte"] for i in lista], [500, 1500])
        self.assertEqual(resumo["faltaParaAlvo"], 0)

    def test_nunca_sugere_resgate(self):
        lista = [item(A, 10, 5000), item(B, 90, 0, ordem=1)]
        carteira._distribuir_rebalanceando(lista, 100)
        self.assertTrue(all(i["aporte"] >= 0 for i in lista))
        self.assertEqual(round(sum(i["aporte"] for i in lista), 2), 100)

    def test_fundo_fechado_nao_recebe_aporte(self):
        lista = [item(A, 0, 300, fechado="2026-01-01"), item(B, 100, 0, ordem=1)]
        carteira._distribuir_rebalanceando(lista, 250)
        self.assertEqual([i["aporte"] for i in lista], [0, 250])

    def test_iq_nao_recebe_e_sai_do_alvo(self):
        lista = [item(A, 50, 0, iq=True), item(B, 50, 0, ordem=1)]
        carteira._distribuir_rebalanceando(lista, 100)
        self.assertEqual([i["aporte"] for i in lista], [0, 100])
        self.assertTrue(lista[0]["redistribuido"])

    def test_centavos_batem(self):
        for aporte in (0.03, 333.33, 1000.01):
            lista = [item(A, 33.33, 10), item(B, 33.33, 20, ordem=1), item(C, 33.34, 0, ordem=2)]
            carteira._distribuir_rebalanceando(lista, aporte)
            self.assertEqual(round(sum(i["aporte"] for i in lista), 2), round(aporte, 2))


    def test_cdb_na_aba_soma_no_total_e_ocupa_parte_do_alvo(self):
        # Aba com A (50%) e um CDB de 1000 com meta 50%: o aporte de 1000 vai
        # todo para A, que fica com metade da aba (1000 de 2000).
        lista = [item(A, 50, 0)]
        resumo = carteira._distribuir_rebalanceando(lista, 1000, fixo_atual=1000, fixo_pct=50)
        self.assertEqual(lista[0]["aporte"], 1000)
        self.assertEqual(resumo["totalAtual"], 1000)
        self.assertEqual(lista[0]["alvoVaga"], 1000)


class Vagas(unittest.TestCase):
    def test_cadeia_forma_uma_vaga_so(self):
        itens = [{"cnpj": c} for c in (A, B, C, X)]
        arestas = [{"origem": A, "destino": B}, {"origem": B, "destino": C}]
        vagas = carteira._vagas(itens, arestas)
        self.assertEqual(vagas, {C: [A, B, C], X: [X]})


class SubstituicaoNoBanco(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.original = banco.DATABASE_PATH
        banco.DATABASE_PATH = Path(self.pasta.name) / "teste.db"
        banco.ensure_database()
        carteira.adicionar(A, 60, "conservador")
        carteira.adicionar(X, 40, "conservador")

    def tearDown(self):
        banco.DATABASE_PATH = self.original
        self.pasta.cleanup()

    def itens(self, p):
        return {i["cnpj"]: i for i in p["itens"]}

    def test_substituir_passa_o_percentual_e_fecha_a_origem(self):
        p = carteira.substituir(A, B, "2026-05-10", "fechou para captação")
        it = self.itens(p)
        self.assertEqual(it[A]["fechadoEm"], "2026-05-10")
        self.assertEqual(it[A]["percentual"], 0)
        self.assertEqual(it[B]["percentual"], 60)
        self.assertEqual(it[B]["perfil"], "conservador")
        self.assertEqual(p["substituicoes"][0]["motivo"], "fechou para captação")

    def test_vaga_soma_posicoes_da_cadeia_e_aporte_vai_para_o_aberto(self):
        carteira.substituir(A, B, "2026-05-10")
        carteira.substituir(B, C, "2026-08-01")
        posicoes = {A: {"posicao": 100.0, "primeiroAporte": "2025-01-02"},
                    B: {"posicao": 50.0, "primeiroAporte": "2026-05-11"},
                    X: {"posicao": 500.0, "primeiroAporte": "2025-01-02"}}
        with patch.object(carteira, "_posicoes_por_cnpj", return_value=posicoes):
            p = carteira.payload(1000)
        it = self.itens(p)
        self.assertEqual(it[C]["atualVaga"], 150)
        self.assertEqual(it[C]["cadeia"], [A, B, C])
        self.assertEqual(it[A]["aporte"], 0)
        self.assertEqual(it[B]["aporte"], 0)
        self.assertGreater(it[C]["aporte"], 0)
        vaga = next(v for v in p["vagas"] if v["ponta"] == C)
        self.assertEqual(vaga["cadeia"], [A, B, C])

    def test_desfazer_volta_ao_estado_anterior(self):
        carteira.substituir(A, B, "2026-05-10")
        p = carteira.desfazer_substituicao(A)
        it = self.itens(p)
        self.assertEqual(it[A]["fechadoEm"], "")
        self.assertEqual(it[A]["percentual"], 60)
        self.assertNotIn(B, it)            # entrou só pela substituição
        self.assertEqual(p["substituicoes"], [])

    def test_so_desfaz_a_ponta(self):
        carteira.substituir(A, B, "2026-05-10")
        carteira.substituir(B, C, "2026-08-01")
        with self.assertRaises(ValueError):
            carteira.desfazer_substituicao(A)

    def test_remover_fundo_com_linhagem_e_recusado(self):
        carteira.substituir(A, B, "2026-05-10")
        for cnpj_ in (A, B):
            with self.assertRaises(ValueError):
                carteira.excluir(cnpj_)
        carteira.excluir(X)            # fora de linhagem continua removível

    def test_salvar_pela_tela_preserva_o_fechamento(self):
        carteira.substituir(A, B, "2026-05-10")
        p = carteira.payload()
        itens = [{"cnpj": i["cnpj"], "percentual": i["percentual"], "perfil": i["perfil"]} for i in p["itens"]]
        p2 = carteira.salvar(itens, 0, percentuais={a["id"]: a["percentual"] for a in p["abas"]})
        self.assertEqual(self.itens(p2)[A]["fechadoEm"], "2026-05-10")
        self.assertEqual(len(p2["substituicoes"]), 1)


if __name__ == "__main__":
    unittest.main()
