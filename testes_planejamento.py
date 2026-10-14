"""Contas do Planejamento: ritmo, projeção, marcos, metas e categorias.

Execute: python -m unittest testes_planejamento -v
"""

import unittest

import planejamento as plan


class Meses(unittest.TestCase):
    def test_janela_so_meses_fechados(self):
        self.assertEqual(plan.janela("2026-02", 3), ["2025-11", "2025-12", "2026-01"])

    def test_mes_mais_e_entre(self):
        self.assertEqual(plan.mes_mais("2026-11", 3), "2027-02")
        self.assertEqual(plan.meses_entre("2026-10", "2027-01"), 3)


class Ritmo(unittest.TestCase):
    def test_mediana_ignora_resgate_grande(self):
        serie = {"2026-01": 5000, "2026-02": -40000, "2026-03": 6000}
        meses = list(serie)
        self.assertEqual(plan.ritmo(serie, meses, usar_mediana=True), 5000)
        self.assertLess(plan.ritmo(serie, meses), 0)

    def test_sem_dado(self):
        self.assertIsNone(plan.ritmo({}, ["2026-01"]))

    def test_variacao(self):
        self.assertEqual(plan.variacao(110, 100), 10)
        self.assertIsNone(plan.variacao(10, 0))


class Projecao(unittest.TestCase):
    def test_rende_e_aporta(self):
        p = plan.projetar(1000, 500, 0, "2026-10", 3)
        self.assertEqual([x["saldo"] for x in p], [1500, 2000, 2500])
        self.assertEqual(p[0]["mes"], "2026-11")
        self.assertEqual(plan.projetar(1000, 0, 1, "2026-10", 1)[0]["saldo"], 1010)

    def test_quando_atinge_e_marcos(self):
        p = plan.projetar(0, 1000, 0, "2026-10", 24)
        self.assertEqual(plan.quando_atinge(p, 5000), "2027-03")
        self.assertIsNone(plan.quando_atinge(p, 10**6))
        self.assertEqual(plan.marcos(78000), [100_000, 150_000, 200_000])


class Metas(unittest.TestCase):
    def test_chega_antes_do_prazo(self):
        r = plan.progresso_meta({"alvo": 10000, "prazo": "2027-06"}, 4000, 1000, "2026-10")
        self.assertEqual(r["chegaEm"], "2027-04")
        self.assertEqual(r["situacao"], "no_prazo")

    def test_atrasada_diz_quanto_falta_por_mes(self):
        r = plan.progresso_meta({"alvo": 10000, "prazo": "2027-01"}, 4000, 1000, "2026-10")
        self.assertEqual(r["situacao"], "atrasada")
        self.assertEqual(r["precisaPorMes"], 1500)     # 6000 em 4 meses
        self.assertEqual(r["extraPorMes"], 500)

    def test_concluida_sem_ritmo_e_vencida(self):
        self.assertEqual(plan.progresso_meta({"alvo": 100}, 150, None, "2026-10")["situacao"], "concluida")
        self.assertEqual(plan.progresso_meta({"alvo": 100}, 50, None, "2026-10")["situacao"], "sem_ritmo")
        self.assertEqual(plan.progresso_meta({"alvo": 100, "prazo": "2026-09"}, 50, 10, "2026-10")["situacao"], "vencida")

    def test_chegada_pronta_da_projecao(self):
        r = plan.progresso_meta({"alvo": 100000}, 80000, 5000, "2026-10", chega="2027-01")
        self.assertEqual(r["chegaEm"], "2027-01")
        self.assertEqual(r["situacao"], "no_ritmo")

    def test_meta_ligada_le_a_fonte(self):
        fontes = {"carteira": 50000, "liquidez": 8000, "aba": {"arrojado": 9000}, "cdb": {}}
        self.assertEqual(plan._valor_ligado({"tipo": "liquidez"}, fontes), 8000)
        self.assertEqual(plan._valor_ligado({"tipo": "aba", "referencia": "arrojado"}, fontes), 9000)
        self.assertEqual(plan._valor_ligado({"tipo": "cdb", "referencia": "X"}, fontes), 0)


class Tendencia(unittest.TestCase):
    def test_copia_mensal_nao_esta_acabando(self):
        linhas = [
            {"nome": "Aluguel", "desde": "2026-01", "ate": "2026-10", "valor": 1000},   # cadastro antigo
            {"nome": "Aluguel", "desde": "2026-12", "ate": "2026-12", "valor": 1000},   # cópia mensal
            {"nome": "Curso", "desde": "2026-10", "ate": "2026-12", "valor": 173},      # parcelado
            {"nome": "Velha", "desde": "2026-01", "ate": "2026-05", "valor": 50},       # já acabou
        ]
        r = plan.contas_que_acabam(linhas, "2026-10")
        self.assertEqual([c["nome"] for c in r], ["Curso"])

    def test_custo_segue_as_fixas_de_cada_mes(self):
        futuro = {"2026-11": {"parcelas": 700, "fixas": 5000},
                  "2026-12": {"parcelas": 1400, "fixas": 5000},
                  "2027-01": {"parcelas": 800, "fixas": 3500},     # contas removidas em janeiro
                  "2027-02": {"parcelas": 600, "fixas": 0},        # mês ainda não gerado
                  "2027-04": {"parcelas": 500, "fixas": 0}}
        acabam = [{"nome": "Curso", "ate": "2027-02", "valor": 173}]
        pontos, eventos = plan.projetar_custo(9000, "2026-10", futuro, "2027-01", acabam)
        valor = {p["mes"]: p["valor"] for p in pontos}
        self.assertEqual(valor["2026-11"], 9000)
        self.assertEqual(valor["2026-12"], 9700)                   # parcela nova
        self.assertEqual(valor["2027-01"], 9000 + 100 - 1500)      # fixas de janeiro de verdade
        self.assertEqual(valor["2027-02"], 9000 - 100 - 1500)      # repete janeiro
        self.assertEqual(valor["2027-04"], 9000 - 200 - 1500 - 173)
        self.assertIn({"mes": "2027-01", "texto": "contas fixas diminuem", "valor": -1500}, eventos)


class Fluxo(unittest.TestCase):
    def test_sobra_e_folga(self):
        pontos = [{"mes": "2026-11", "fixas": 5000, "parcelas": 700, "valor": 9000},
                  {"mes": "2026-12", "fixas": 5000, "parcelas": 1400, "valor": 9700}]
        eventos = [{"mes": "2026-12", "texto": "parcelas de cartão aumentam", "valor": 700}]
        f = plan.montar_fluxo(18000, 9000, pontos, eventos, 6000)
        self.assertEqual(f[0]["variavel"], 3300)          # 9000 - 5000 - 700
        self.assertEqual(f[0]["sobra"], 9000)
        self.assertEqual(f[1]["custo"], 9700)
        self.assertEqual(f[1]["folga"], 18000 - 9700 - 6000)
        self.assertEqual(len(f[1]["eventos"]), 1)


class Parcelas(unittest.TestCase):
    def test_agenda_junta_real_e_projetada(self):
        p = lambda compra, mes, atual, total=3, valor=100, cartao="A": {
            "compra": compra, "mes": mes, "valor": valor, "atual": atual, "total": total,
            "descricao": compra, "cartao": cartao}
        reais = [p("tv", "2026-11", 2)]
        proj = [p("tv", "2026-11", 2), p("tv", "2026-12", 3),           # nov repetido: vale o real
                p("sofa", "2026-12", 1, 10, 50, "B"), p("sofa", "2027-01", 2, 10, 50, "B"),
                p("velha", "2026-10", 3)]                               # fora da janela
        a = plan.agenda_parcelas(reais, proj, ["2026-11", "2026-12", "2027-01"])
        tv = next(c for c in a["compras"] if c["id"] == "tv")
        self.assertEqual((tv["restantes"], tv["saldo"], tv["terminaEm"]), (2, 200, "2026-12"))
        sofa = next(c for c in a["compras"] if c["id"] == "sofa")
        self.assertIsNone(sofa["terminaEm"])                           # passa da janela
        dez = a["meses"][1]
        self.assertEqual((dez["total"], dez["compras"], dez["terminam"]), (150, 2, 1))
        self.assertEqual(dez["porCartao"], {"A": 100, "B": 50})
        self.assertEqual(a["saldo"], 300)


class Validacao(unittest.TestCase):
    def test_meta_invalida(self):
        for dados in ({"nome": "", "alvo": 10}, {"nome": "X", "alvo": 0},
                      {"nome": "X", "alvo": 10, "tipo": "aba"}, {"nome": "X", "alvo": 10, "prazo": "13/2027"}):
            with self.assertRaises(ValueError):
                plan._validar_meta(dados)


if __name__ == "__main__":
    unittest.main()
