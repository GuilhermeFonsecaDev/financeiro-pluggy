"""Regressões de liquidez: vencimento, carência e escada de prazos.

Execute: python testes_investimentos_liquidez.py

O cuidado central é a diferença entre "não dá para resgatar" e "não sei se dá".
O primeiro é zero; o segundo não pode virar zero dentro de uma soma.
"""

import sqlite3
import unittest
from datetime import date

import investimentos_liquidez as liq

HOJE = date(2026, 9, 14)          # segunda-feira


def posicao(**campos):
    base = {"id": campos.get("id", "p1"), "nome": "Papel", "liquido": 1000.0,
            "disponivel": None, "vencimento": "", "carencia": "", "codigo": "",
            "instituicao": "BTG", "classe": "renda_fixa", "indexador": "100% do CDI"}
    base.update(campos)
    return base


def enriquecida(**campos):
    return liq.enriquecer([posicao(**campos)], campos.pop("_dias", None), HOJE)[0]


class DataResgateTests(unittest.TestCase):
    def test_conta_dias_corridos(self):
        self.assertEqual(liq.data_resgate(0, HOJE), "2026-09-14")
        self.assertEqual(liq.data_resgate(1, HOJE), "2026-09-15")
        self.assertEqual(liq.data_resgate(31, HOJE), "2026-10-15")

    def test_fim_de_semana_rola_para_segunda(self):
        # D+5 cai no sábado 19/09 e D+6 no domingo: os dois liquidam na segunda.
        self.assertEqual(liq.data_resgate(5, HOJE), "2026-09-21")
        self.assertEqual(liq.data_resgate(6, HOJE), "2026-09-21")

    def test_sem_prazo_nao_inventa_data(self):
        for valor in (None, "", "x", -3):
            self.assertEqual(liq.data_resgate(valor, HOJE), "", repr(valor))


class FaixaTests(unittest.TestCase):
    def test_limites_das_faixas(self):
        self.assertEqual(liq.faixa(0)[0], "ate_30")
        self.assertEqual(liq.faixa(30)[0], "ate_30")
        self.assertEqual(liq.faixa(31)[0], "de_31_90")
        self.assertEqual(liq.faixa(365)[0], "de_181_365")
        self.assertEqual(liq.faixa(366)[0], "de_1_2_anos")
        self.assertEqual(liq.faixa(731)[0], "acima_2_anos")

    def test_vencido_tem_degrau_proprio(self):
        self.assertEqual(liq.faixa(-1)[0], "vencido")

    def test_sem_prazo_nao_entra_na_escada_de_vencimento(self):
        self.assertEqual(liq.faixa(None)[0], "sem_vencimento")


class EnriquecerTests(unittest.TestCase):
    def test_prazo_e_faixa_a_partir_do_vencimento(self):
        p = enriquecida(vencimento="2026-10-12T03:00:00.000Z")
        self.assertEqual(p["diasParaVencer"], 28)
        self.assertEqual(p["faixaVencimento"], "ate_30")
        self.assertEqual(p["liquidezRotulo"], "no vencimento")

    def test_carencia_futura_manda_no_rotulo(self):
        p = enriquecida(vencimento="2030-01-01", carencia="2027-03-12")
        self.assertEqual(p["liquidezRotulo"], "carência até 12/03/27")
        self.assertTrue(p["emCarencia"])

    def test_carencia_no_proprio_vencimento_se_le_como_no_vencimento(self):
        """Carência que acaba junto com o papel não é espera extra."""
        p = enriquecida(vencimento="2028-09-04T03:00:00.000Z", carencia="2028-09-04")
        self.assertEqual(p["liquidezRotulo"], "no vencimento")
        self.assertTrue(p["emCarencia"])

    def test_carencia_vencida_nao_prende_mais(self):
        p = enriquecida(vencimento="2030-01-01", carencia="2026-01-05", disponivel=1000.0)
        self.assertFalse(p["emCarencia"])
        self.assertEqual(p["liquidezRotulo"], "resgatável")

    def test_fundo_recebe_d_mais_n_do_catalogo(self):
        p = liq.enriquecer([posicao(codigo="42.774.627/0001-40", vencimento="")],
                           {"42774627000140": 12}, HOJE)[0]
        self.assertEqual(p["diasResgate"], 12)
        self.assertEqual(p["liquidezRotulo"], "D+12")
        self.assertEqual(p["dataResgateEstimada"], "2026-09-28")

    def test_resgate_no_mesmo_dia_se_le_como_imediato(self):
        p = liq.enriquecer([posicao(codigo="11111111000111")], {"11111111000111": 0}, HOJE)[0]
        self.assertEqual(p["liquidezRotulo"], "resgate imediato")

    def test_sem_nada_conhecido_nao_inventa_rotulo(self):
        self.assertEqual(enriquecida()["liquidezRotulo"], "")


class AgregarTests(unittest.TestCase):
    def agregar(self, posicoes, dias=None):
        return liq.agregar(liq.enriquecer(posicoes, dias, HOJE), HOJE)

    def test_carencia_futura_nao_entra_em_resgatavel_hoje(self):
        dados = self.agregar([posicao(carencia="2027-03-12", liquido=5000.0,
                                      disponivel=5000.0, vencimento="2030-01-01")])
        self.assertEqual(dados["disponivelHoje"], 0)
        self.assertEqual(dados["emCarencia"]["valor"], 5000.0)
        self.assertEqual(dados["emCarencia"]["liberaEm"],
                         [{"data": "2027-03-12", "valor": 5000.0}])

    def test_liquidez_desconhecida_nao_vira_zero_nem_imediato(self):
        """Fundo sem D+N no catálogo: não é resgatável hoje nem é bloqueado."""
        dados = self.agregar([posicao(liquido=9754.0, codigo="99999999000199")])
        self.assertEqual(dados["disponivelHoje"], 0)
        self.assertEqual(dados["liquidezDesconhecida"], {"valor": 9754.0, "posicoes": 1})
        self.assertEqual(dados["caixaEm"]["d90"], 0)

    def test_disponivel_informado_entra_no_resgatavel(self):
        dados = self.agregar([posicao(liquido=1000.0, disponivel=800.0)])
        self.assertEqual(dados["disponivelHoje"], 800.0)
        self.assertEqual(dados["liquidezDesconhecida"]["posicoes"], 0)

    def test_escada_soma_por_faixa_em_ordem_cronologica(self):
        dados = self.agregar([
            posicao(id="a", vencimento="2026-10-01", liquido=100.0),   # 17 dias
            posicao(id="b", vencimento="2026-12-20", liquido=200.0),   # 97 dias
            posicao(id="c", vencimento="2031-01-01", liquido=300.0),   # >2 anos
        ])
        self.assertEqual([(d["faixa"], d["valor"]) for d in dados["escada"]],
                         [("ate_30", 100.0), ("de_91_180", 200.0), ("acima_2_anos", 300.0)])

    def test_vencendo_em_30_e_90_dias(self):
        dados = self.agregar([
            posicao(id="a", vencimento="2026-10-01", liquido=100.0),
            posicao(id="b", vencimento="2026-11-30", liquido=200.0),
            posicao(id="c", vencimento="2028-01-01", liquido=900.0),
        ])
        self.assertEqual(dados["vencendo30"], 100.0)
        self.assertEqual(dados["vencendo90"], 300.0)

    def test_proximos_vencimentos_saem_ordenados(self):
        dados = self.agregar([
            posicao(id="longe", vencimento="2029-01-01"),
            posicao(id="perto", vencimento="2026-09-30"),
        ])
        self.assertEqual([p["id"] for p in dados["proximosVencimentos"]], ["perto", "longe"])
        self.assertEqual(dados["proximosVencimentos"][0]["dias"], 16)

    def test_vencimento_por_mes_e_janela_de_24_meses(self):
        dados = self.agregar([
            posicao(id="a", vencimento="2026-11-10", liquido=100.0),
            posicao(id="b", vencimento="2026-11-25", liquido=150.0),
            posicao(id="c", vencimento="2035-01-01", liquido=900.0),
        ])
        self.assertEqual(dados["vencimentosPorMes"],
                         [{"mes": "2026-11", "valor": 250.0, "quantidade": 2}])
        self.assertEqual(dados["alemDaJanela"], {"valor": 900.0, "quantidade": 1})

    def test_caixa_em_respeita_o_d_mais_n_do_fundo(self):
        dados = self.agregar([posicao(liquido=1000.0, codigo="42774627000140")],
                             {"42774627000140": 12})
        self.assertEqual(dados["caixaEm"]["d5"], 0)
        self.assertEqual(dados["caixaEm"]["d30"], 1000.0)
        self.assertEqual(dados["disponivelHoje"], 0)

    def test_carteira_vazia_nao_quebra(self):
        dados = liq.agregar([], HOJE)
        self.assertEqual(dados["disponivelHoje"], 0)
        self.assertEqual(dados["escada"], [])
        self.assertEqual(dados["proximosVencimentos"], [])


class CatalogoTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def criar_catalogo(self):
        self.conn.execute("CREATE TABLE fundos_btg (cnpj TEXT PRIMARY KEY, dias_resgate INTEGER)")
        self.conn.executemany("INSERT INTO fundos_btg VALUES (?,?)",
                              [("42774627000140", 12), ("47046855000117", None)])

    def test_le_o_d_mais_n_de_varios_cnpjs_de_uma_vez(self):
        self.criar_catalogo()
        mapa = liq.dias_de_resgate_por_cnpj(
            self.conn, ["42.774.627/0001-40", "47046855000117", "00000000000000"])
        self.assertEqual(mapa, {"42774627000140": 12})

    def test_sem_catalogo_a_liquidez_fica_desconhecida_em_vez_de_quebrar(self):
        self.assertEqual(liq.dias_de_resgate_por_cnpj(self.conn, ["42774627000140"]), {})

    def test_sem_codigos_nao_consulta(self):
        self.assertEqual(liq.dias_de_resgate_por_cnpj(self.conn, ["", None]), {})


if __name__ == "__main__":
    unittest.main()
