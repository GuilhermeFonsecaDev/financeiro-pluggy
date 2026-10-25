"""Regressões de rentabilidade: TWR, XIRR e o casamento de rolagem.

Execute: python testes_investimentos_rentabilidade.py

O teste que mais importa é o do aporte no meio do período: é exatamente o erro
que o ganho simples comete, e a razão de existir TWR aqui.
"""

import unittest
from unittest.mock import patch
from datetime import date

import investimentos_rentabilidade as rent


def mov(data, tipo, valor, instituicao="BTG"):
    return {"data": data, "tipo": tipo, "valor": valor, "instituicao": instituicao}


class PontosDiariosTests(unittest.TestCase):
    def test_varias_coletas_no_dia_viram_a_ultima(self):
        serie = [("2026-09-01T09:00", 100.0), ("2026-09-01T18:00", 110.0),
                 ("2026-09-02T10:00", 120.0)]
        self.assertEqual(rent.pontos_diarios(serie),
                         [("2026-09-01", 110.0), ("2026-09-02", 120.0)])

    def test_sai_ordenado_mesmo_com_entrada_bagunçada(self):
        serie = [("2026-09-03T10:00", 3.0), ("2026-09-01T10:00", 1.0), ("2026-09-02T10:00", 2.0)]
        self.assertEqual([d for d, _ in rent.pontos_diarios(serie)],
                         ["2026-09-01", "2026-09-02", "2026-09-03"])

    def test_serie_vazia_nao_quebra(self):
        self.assertEqual(rent.pontos_diarios([]), [])


class TwrTests(unittest.TestCase):
    def test_aporte_no_meio_nao_vira_rentabilidade(self):
        """O erro clássico: 100 -> aporte 100 -> 210 é 5%, não 110%."""
        pontos = [("2026-09-01", 100.0), ("2026-09-02", 210.0)]
        resultado = rent.twr(pontos, {"2026-09-02": 100.0})
        self.assertAlmostEqual(resultado["valor"], 5.0, places=4)
        self.assertTrue(resultado["confiavel"])

    def test_resgate_no_meio_tambem_e_neutralizado(self):
        pontos = [("2026-09-01", 200.0), ("2026-09-02", 105.0)]
        resultado = rent.twr(pontos, {"2026-09-02": -100.0})
        self.assertAlmostEqual(resultado["valor"], 5.0, places=4)

    def test_sem_fluxo_e_a_variacao_simples(self):
        resultado = rent.twr([("2026-09-01", 100.0), ("2026-09-30", 102.0)])
        self.assertAlmostEqual(resultado["valor"], 2.0, places=4)

    def test_encadeia_subperiodos(self):
        pontos = [("2026-09-01", 100.0), ("2026-09-02", 110.0), ("2026-09-03", 121.0)]
        resultado = rent.twr(pontos)
        self.assertAlmostEqual(resultado["valor"], 21.0, places=4)
        self.assertEqual(resultado["subperiodos"], 2)

    def test_um_ponto_so_nao_vira_zero(self):
        resultado = rent.twr([("2026-09-01", 100.0)])
        self.assertIsNone(resultado["valor"])
        self.assertEqual(resultado["motivo"], "serie_curta")
        self.assertFalse(resultado["confiavel"])

    def test_carteira_zerada_no_meio_pula_o_subperiodo(self):
        """Zerou e voltou a receber aporte: o trecho sem base fica de fora."""
        pontos = [("2026-09-01", 100.0), ("2026-09-02", 0.0), ("2026-09-03", 50.0)]
        resultado = rent.twr(pontos, {"2026-09-02": -100.0, "2026-09-03": 50.0})
        self.assertTrue(resultado["aproximado"])
        self.assertEqual(resultado["subperiodosPulados"], 1)
        self.assertEqual(resultado["subperiodos"], 1)
        self.assertFalse(resultado["confiavel"])

    def test_todo_subperiodo_invalido_nao_vira_zero(self):
        pontos = [("2026-09-01", 100.0), ("2026-09-02", 0.0)]
        resultado = rent.twr(pontos, {"2026-09-02": -100.0})
        self.assertIsNone(resultado["valor"])
        self.assertEqual(resultado["motivo"], "sem_subperiodo_valido")
        self.assertEqual(resultado["subperiodosPulados"], 1)

    def test_buraco_na_coleta_com_fluxo_dentro_marca_aproximado(self):
        pontos = [("2026-09-01", 100.0), ("2026-09-20", 220.0)]
        resultado = rent.twr(pontos, {"2026-09-10": 100.0})
        self.assertTrue(resultado["aproximado"])
        self.assertEqual(resultado["motivo"], "periodo_com_lacuna")

    def test_buraco_sem_fluxo_nao_estraga_a_confianca(self):
        resultado = rent.twr([("2026-09-01", 100.0), ("2026-09-20", 102.0)])
        self.assertFalse(resultado["aproximado"])
        self.assertTrue(resultado["confiavel"])

    def test_janela_e_dias_acompanham_o_numero(self):
        resultado = rent.twr([("2026-08-16", 100.0), ("2026-09-14", 102.0)])
        self.assertEqual(resultado["janela"], {"de": "2026-08-16", "ate": "2026-09-14"})
        self.assertEqual(resultado["dias"], 29)


class AnualizarTests(unittest.TestCase):
    def test_periodo_curto_nao_anualiza(self):
        """Anualizar 20 dias de CDB é fabricar precisão."""
        self.assertIsNone(rent.anualizar(1.0, 20))
        self.assertIsNone(rent.twr([("2026-09-01", 100.0), ("2026-09-10", 101.0)])["anualizado"])

    def test_um_ano_devolve_o_proprio_retorno(self):
        self.assertAlmostEqual(rent.anualizar(10.0, 365), 10.0, places=2)

    def test_meio_ano_compoe(self):
        self.assertAlmostEqual(rent.anualizar(5.0, 182), 10.28, places=1)


class XirrTests(unittest.TestCase):
    def test_caso_simples_de_um_ano(self):
        taxa = rent.xirr([("2025-09-14", -1000.0), ("2026-09-14", 1100.0)])
        self.assertAlmostEqual(taxa, 10.0, places=1)

    def test_aportes_em_datas_diferentes(self):
        taxa = rent.xirr([("2026-01-01", -1000.0), ("2026-07-01", -1000.0),
                          ("2027-01-01", 2150.0)])
        self.assertIsNotNone(taxa)
        self.assertGreater(taxa, 0)

    def test_prejuizo_da_taxa_negativa(self):
        taxa = rent.xirr([("2025-09-14", -1000.0), ("2026-09-14", 900.0)])
        self.assertLess(taxa, 0)

    def test_fluxos_de_um_sinal_so_nao_tem_taxa(self):
        self.assertIsNone(rent.xirr([("2026-01-01", -100.0), ("2026-06-01", -100.0)]))
        self.assertIsNone(rent.xirr([("2026-01-01", 100.0), ("2026-06-01", 100.0)]))

    def test_entrada_insuficiente_devolve_none_em_vez_de_excecao(self):
        self.assertIsNone(rent.xirr([]))
        self.assertIsNone(rent.xirr([("2026-01-01", -100.0)]))
        self.assertIsNone(rent.xirr([("data ruim", -100.0), ("outra", 100.0)]))


class RentabilidadeXirrTests(unittest.TestCase):
    def test_historico_com_compra_e_saldo_atual(self):
        resultado = rent.rentabilidade_xirr(
            [mov("2025-09-14", "BUY", 1000.0)], 1100.0, "2026-09-14")
        self.assertAlmostEqual(resultado["valor"], 10.0, places=1)
        self.assertTrue(resultado["confiavel"])

    def test_posicao_encerrada_so_com_resgate_nao_inventa_resultado(self):
        """65 papéis da carteira real estão assim: só SELL, sem a compra."""
        resultado = rent.rentabilidade_xirr([mov("2026-03-01", "SELL", 500.0)], 0)
        self.assertIsNone(resultado["valor"])
        self.assertEqual(resultado["motivo"], "historico_de_compras_incompleto")

    def test_sem_movimento_nenhum(self):
        resultado = rent.rentabilidade_xirr([], 1000.0)
        self.assertIsNone(resultado["valor"])
        self.assertEqual(resultado["motivo"], "sem_movimentos")


class RolagemTests(unittest.TestCase):
    def test_venda_e_recompra_no_mesmo_dia_sao_rolagem(self):
        movs = rent.casar_rolagens([mov("2026-09-01", "SELL", 10000.0),
                                    mov("2026-09-01", "BUY", 10000.0)])
        self.assertTrue(all(m.get("rolagem") for m in movs))
        self.assertEqual(rent.fluxos_por_dia(movs), {})

    def test_recompra_no_dia_seguinte_tambem(self):
        movs = rent.casar_rolagens([mov("2026-09-01", "SELL", 10000.0),
                                    mov("2026-09-02", "BUY", 10000.0)])
        self.assertTrue(all(m.get("rolagem") for m in movs))

    def test_valor_diferente_nao_e_rolagem(self):
        movs = rent.casar_rolagens([mov("2026-09-01", "SELL", 10000.0),
                                    mov("2026-09-01", "BUY", 8000.0)])
        self.assertFalse(any(m.get("rolagem") for m in movs))
        self.assertEqual(rent.fluxos_por_dia(movs), {"2026-09-01": -2000.0})

    def test_instituicao_diferente_nao_e_rolagem(self):
        movs = rent.casar_rolagens([mov("2026-09-01", "SELL", 10000.0, "BTG"),
                                    mov("2026-09-01", "BUY", 10000.0, "Itaú")])
        self.assertFalse(any(m.get("rolagem") for m in movs))

    def test_dois_dias_depois_nao_e_rolagem(self):
        movs = rent.casar_rolagens([mov("2026-09-01", "SELL", 10000.0),
                                    mov("2026-09-03", "BUY", 10000.0)])
        self.assertFalse(any(m.get("rolagem") for m in movs))

    def test_uma_compra_nao_casa_com_duas_vendas(self):
        movs = rent.casar_rolagens([mov("2026-09-01", "SELL", 100.0),
                                    mov("2026-09-01", "SELL", 100.0),
                                    mov("2026-09-01", "BUY", 100.0)])
        self.assertEqual(sum(1 for m in movs if m.get("rolagem")), 2)
        self.assertEqual(rent.fluxos_por_dia(movs), {"2026-09-01": -100.0})

    def test_rolagem_nao_altera_a_rentabilidade_da_carteira(self):
        """O ponto todo: o mesmo dinheiro continuando aplicado não é fluxo."""
        series = {"a": [("2026-09-01T18:00", 10000.0), ("2026-09-02T18:00", 10100.0)]}
        movs = {"a": [mov("2026-09-02", "SELL", 10000.0), mov("2026-09-02", "BUY", 10000.0)]}
        com_rolagem = rent.rentabilidade_carteira(series, movs)
        sem_movimento = rent.rentabilidade_carteira(series, {})
        self.assertAlmostEqual(com_rolagem["valor"], sem_movimento["valor"], places=6)
        self.assertAlmostEqual(com_rolagem["valor"], 1.0, places=4)


class FluxosTests(unittest.TestCase):
    def test_aporte_soma_e_resgate_subtrai_no_mesmo_dia(self):
        movs = [mov("2026-09-01", "BUY", 300.0), mov("2026-09-01", "SELL", 100.0)]
        self.assertEqual(rent.fluxos_por_dia(movs), {"2026-09-01": 200.0})

    def test_carimbo_com_hora_vira_dia(self):
        self.assertEqual(rent.fluxos_por_dia([mov("2026-09-01T13:00:00Z", "BUY", 50.0)]),
                         {"2026-09-01": 50.0})


class CarteiraTests(unittest.TestCase):
    def test_serie_curta_nao_inventa_rentabilidade(self):
        resultado = rent.rentabilidade_carteira({"a": [("2026-09-14T18:00", 1000.0)]}, {})
        self.assertIsNone(resultado["valor"])
        self.assertEqual(resultado["motivo"], "serie_curta")

    def test_diz_que_a_serie_nao_cobre_desde_a_aplicacao(self):
        resultado = rent.rentabilidade_carteira(
            {"a": [("2026-08-16T22:30", 100.0), ("2026-09-14T18:40", 102.0)]}, {})
        self.assertFalse(resultado["serieDesdeAplicacao"])
        self.assertEqual(resultado["janela"]["de"], "2026-08-16")
        self.assertAlmostEqual(resultado["valor"], 2.0, places=4)

    def test_conexao_nova_no_meio_nao_vira_rendimento(self):
        """O erro que apareceu na carteira real: 43% em 29 dias.

        Uma conexão nova faz o saldo medido pular sem que nada tenha rendido.
        Só quem existia nas duas pontas entra no cálculo do trecho.
        """
        series = {
            "antiga": [("2026-09-01", 1000.0), ("2026-09-02", 1010.0), ("2026-09-03", 1020.1)],
            # aparece no dia 2 trazendo 9.000 de dinheiro que já existia
            "nova": [("2026-09-02", 9000.0), ("2026-09-03", 9090.0)],
        }
        resultado = rent.rentabilidade_carteira(series, {})
        # 1% no primeiro trecho e 1% no segundo: ~2,01%, e não os ~900% que a
        # soma ingênua produziria.
        self.assertAlmostEqual(resultado["valor"], 2.01, places=2)
        # A que entrou no meio só passa a votar quando tem dois pontos seus.
        self.assertEqual(resultado["posicoes"], 2)
        self.assertEqual(resultado["subperiodos"], 2)

    def test_posicao_que_some_nao_vira_prejuizo(self):
        series = {
            "fica": [("2026-09-01", 1000.0), ("2026-09-02", 1010.0)],
            "some": [("2026-09-01", 5000.0)],
        }
        resultado = rent.rentabilidade_carteira(series, {})
        self.assertAlmostEqual(resultado["valor"], 1.0, places=4)

    def test_aporte_continua_sendo_descontado(self):
        series = {"a": [("2026-09-01", 100.0), ("2026-09-02", 210.0)]}
        fluxos = {"a": [mov("2026-09-02", "BUY", 100.0)]}
        resultado = rent.rentabilidade_carteira(series, fluxos)
        self.assertAlmostEqual(resultado["valor"], 5.0, places=4)

    def test_rolagem_entre_posicoes_nao_mexe_no_total(self):
        """CDB que vence numa posição e é recomprado em outra, no mesmo dia.

        O dinheiro sai mesmo de um papel e entra em outro: o fluxo de cada
        posição conta inteiro, e o resultado da carteira não se mexe.
        """
        series = {"velha": [("2026-09-01", 10000.0), ("2026-09-02", 0.0)],
                  "nova": [("2026-09-01", 0.0), ("2026-09-02", 10000.0)]}
        movs = {"velha": [mov("2026-09-02", "SELL", 10000.0)],
                "nova": [mov("2026-09-02", "BUY", 10000.0)]}
        resultado = rent.rentabilidade_carteira(series, movs)
        self.assertAlmostEqual(resultado["valor"], 0.0, places=4)

    def test_queda_sem_movimento_nao_vira_prejuizo(self):
        """Resgate que a Pluggy não entregou aparecia como -75% num dia."""
        series = {"cdb": [("2026-09-01", 10000.0), ("2026-09-02", 2500.0),
                          ("2026-09-03", 2501.0)]}
        resultado = rent.rentabilidade_carteira(series, {})
        self.assertEqual(resultado["trechosSemMovimento"], 1)
        self.assertFalse(resultado["confiavel"])
        self.assertEqual(resultado["motivo"], "queda_sem_movimento")
        # sobra só o trecho explicável: ~0,04%
        self.assertAlmostEqual(resultado["valor"], 0.04, places=2)

    def test_queda_explicada_por_resgate_continua_valendo(self):
        series = {"cdb": [("2026-09-01", 10000.0), ("2026-09-02", 2500.0)]}
        movs = {"cdb": [mov("2026-09-02", "SELL", 7500.0)]}
        resultado = rent.rentabilidade_carteira(series, movs)
        self.assertEqual(resultado["trechosSemMovimento"], 0)
        self.assertAlmostEqual(resultado["valor"], 0.0, places=4)

    def test_queda_pequena_sem_movimento_continua_sendo_perda(self):
        """Fundo que cai 3% no dia é perda de verdade, não resgate escondido."""
        series = {"fundo": [("2026-09-01", 1000.0), ("2026-09-02", 970.0)]}
        resultado = rent.rentabilidade_carteira(series, {})
        self.assertAlmostEqual(resultado["valor"], -3.0, places=4)
        self.assertEqual(resultado["trechosSemMovimento"], 0)

    def test_coleta_parcial_nao_contamina_quem_nao_foi_medido(self):
        """O caso real: uma rodada cobriu 2 posições de 71.

        Quem não foi observado naquele dia não vota; quem foi, vota com o peso
        do próprio saldo. Antes, o trecho seguinte à coleta parcial sozinho
        valia +45%.
        """
        series = {
            "grande": [("2026-09-01", 100000.0), ("2026-09-03", 100040.0)],
            "pequena": [("2026-09-01", 1000.0), ("2026-09-02", 1000.4),
                        ("2026-09-03", 1000.8)],
        }
        resultado = rent.rentabilidade_carteira(series, {})
        # ~0,04% ao dia nas duas: o total fica na mesma ordem de grandeza.
        self.assertLess(abs(resultado["valor"]), 0.2)


class XirrHistoricoIncompletoTests(unittest.TestCase):
    def test_resgate_antes_de_qualquer_aporte_nao_vira_rentabilidade(self):
        """Registro que começa no meio da vida do papel não tem capital base."""
        movs = [{"data": "2025-08-18", "tipo": "SELL", "valor": 453.04},
                {"data": "2025-08-30", "tipo": "BUY", "valor": 120.0}]
        saida = rent.rentabilidade_xirr(movs, 100.0, "2026-09-14")
        self.assertIsNone(saida["valor"])
        self.assertFalse(saida["confiavel"])
        self.assertEqual(saida["motivo"], "historico_de_compras_incompleto")

    def test_raiz_absurda_nao_e_publicada_como_retorno(self):
        with patch.object(rent, "xirr", return_value=34877144.04):
            saida = rent.rentabilidade_xirr(
                [{"data": "2025-01-02", "tipo": "BUY", "valor": 1000.0}], 1100.0, "2026-01-02")
        self.assertIsNone(saida["valor"])
        self.assertEqual(saida["motivo"], "resultado_implausivel")

    def test_historico_completo_continua_respondendo(self):
        saida = rent.rentabilidade_xirr(
            [{"data": "2025-09-14", "tipo": "BUY", "valor": 1000.0}], 1100.0, "2026-09-14")
        self.assertAlmostEqual(saida["valor"], 10.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
