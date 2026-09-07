"""Regras conservadoras de recorrência, sem rede nem banco do usuário."""
import json
import unittest
from unittest.mock import patch

import banco
import recorrentes as rec
import testes_faturas_genericas as fixtures


def linhas(datas, valores=20):
    if isinstance(valores, (int, float)):
        valores = [valores] * len(datas)
    return [{"data": data, "valor": valor, "transacao_id": f"tx-{i}"}
            for i, (data, valor) in enumerate(zip(datas, valores))]


class CadenciaTests(unittest.TestCase):
    def test_tres_cobrancas_e_virada_de_mes(self):
        series, _ = rec._analisar_series(linhas(["2026-07-01", "2026-07-31", "2026-08-30"]))
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["evidencia"]["ciclosMensais"], 3)

    def test_duas_cobrancas_nao_bastam(self):
        self.assertFalse(rec._analisar_series(linhas(["2026-07-15", "2026-08-15"]))[0])

    def test_lacuna_antiga_exige_historico_extenso_e_ciclos_recentes(self):
        datas = ["2026-01-15", "2026-02-15", "2026-04-15", "2026-05-15",
                 "2026-06-15", "2026-07-15", "2026-08-15"]
        self.assertEqual(len(rec._analisar_series(linhas(datas))[0]), 1)
        self.assertFalse(rec._analisar_series(linhas(datas[:-1] + ["2026-09-15"]))[0])

    def test_compras_semanais_quinzenais_e_a_cada_24_dias_nao_sao_mensais(self):
        for datas in (["2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22"],
                      ["2026-06-01", "2026-06-16", "2026-07-01", "2026-07-16"],
                      ["2026-06-01", "2026-06-25", "2026-07-19", "2026-08-12"]):
            with self.subTest(datas=datas):
                self.assertFalse(rec._analisar_series(linhas(datas))[0])

    def test_duplicidade_ambigua_nao_gera_sugestao(self):
        self.assertFalse(rec._analisar_series(linhas([
            "2026-06-15", "2026-07-15", "2026-08-15", "2026-08-15"]))[0])

    def test_reajuste_sequencial_preserva_serie(self):
        series, _ = rec._analisar_series(linhas(
            ["2026-06-15", "2026-07-15", "2026-08-15"], [20, 20, 30]))
        self.assertEqual(len(series), 1)
        self.assertIsNone(series[0]["faixaValor"])
        self.assertTrue(series[0]["evidencia"]["valorReajustado"])

    def test_valores_alternados_nao_sao_reajuste(self):
        self.assertFalse(rec._analisar_series(linhas(
            ["2026-05-15", "2026-06-15", "2026-07-15", "2026-08-15"], [20, 30, 20, 30]))[0])

    def test_dois_planos_tem_faixas_sem_sobreposicao(self):
        datas = ["2026-06-15", "2026-06-15", "2026-07-15", "2026-07-15", "2026-08-15", "2026-08-15"]
        series, _ = rec._analisar_series(linhas(datas, [20, 50, 20, 50, 20, 50]))
        self.assertEqual(len(series), 2)
        self.assertLess(series[0]["faixaValor"]["max"], series[1]["faixaValor"]["min"])

    def test_compra_extra_nao_esconde_assinatura(self):
        series, _ = rec._analisar_series(linhas(
            ["2026-06-15", "2026-07-15", "2026-07-21", "2026-08-15"], [19.9, 19.9, 8.9, 19.9]))
        self.assertEqual(len(series), 1)
        self.assertTrue(all(i["valor"] == 19.9 for i in series[0]["itens"]))

    def test_aliases_explicitos_e_numeros_de_lojista(self):
        self.assertEqual(rec._chave("NETFLIX ENTRETENIMENTO BARUERI BRA"), "netflix")
        self.assertEqual(rec._chave("Netflix.com Sao Paulo Bra"), "netflix")
        self.assertEqual(rec._chave("CLUBE LIVELO CLUBE LIV SANTANA DE PA BRA"), "livelo")
        self.assertNotEqual(rec._chave("Loja Netflix Gift Card"), "netflix")
        self.assertNotEqual(rec._chave("Pontos Livelo Compra Avulsa"), "livelo")
        self.assertNotEqual(rec._chave("Loja 373"), rec._chave("Loja 374"))


class PayloadDeteccaoTests(unittest.TestCase):
    def setUp(self):
        fixtures.FaturasGenericasTests.setUp(self)
        for nome, valor in (("datetime", fixtures.DataTeste),
                            ("_mes_atual", lambda: "2026-09"),
                            ("_previsoes_payload", lambda dados: [{**json.loads(l["dados"]), "ativa": bool(l["ativo"])} for l in dados]),
                            ("_contas_pagamento_payload", lambda: [])):
            p = patch.object(rec, nome, valor, create=True)
            p.start()
            self.addCleanup(p.stop)

    def inserir(self, tx, data, descricao, valor=20, conta="cartao-b", parcela=None):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,valor,"
                "tipo,status,fatura_id,parcela_numero,parcela_total,raw_json,importado_em) "
                "VALUES (?,?,?,?,?,?,?,?,'DEBIT','POSTED','',?,?,?,'2026-09-04')",
                (tx, conta, data, data[:7], int(data[:4]), int(data[5:7]), descricao, valor,
                 1 if parcela else None, parcela, "{}"))

    def mensal(self, descricao="Servico mensal", prefixo="mensal", conta="cartao-b", meses=(6, 7, 8), parcela=None):
        for mes in meses:
            self.inserir(f"{prefixo}-{mes}", f"2026-{mes:02d}-15", descricao, conta=conta, parcela=parcela)

    def test_payload_sem_ruido_com_regras_e_evidencia(self):
        self.mensal()
        self.mensal("Duas cobranças", "curta", meses=(7, 8))
        dados = rec.sugestoes_payload()
        self.assertEqual(len(dados["sugestoes"]), 1)
        self.assertFalse(dados["possiveis"])
        self.assertNotIn("descartadosDetalhes", dados)
        self.assertTrue(dados["regrasSugestao"])
        self.assertEqual(dados["sugestoes"][0]["evidencia"]["cobrancas"], 3)
        self.assertEqual(dados["sugestoes"][0]["confianca"], "alta")

    def test_recencia_futuros_e_exclusao_manual(self):
        self.mensal("Cancelada", "cancelada", meses=(5, 6, 7))
        self.mensal("Futura", "futura", meses=(7, 8, 9))
        self.mensal("Excluida", "excluida")
        rec.sugestoes_payload()
        with banco.connect() as conn:
            conn.execute("INSERT INTO extrato_ajustes (transacao_id,calculo_override,atualizado_em) VALUES ('excluida-8',0,'2026-09-04')")
        self.assertFalse(rec.sugestoes_payload()["sugestoes"])

    def test_contas_distintas_nao_completam_historico(self):
        self.mensal(meses=(7, 8))
        self.inserir("outra-conta", "2026-06-15", "Servico mensal", conta="conta-b")
        self.assertFalse(rec.sugestoes_payload()["sugestoes"])

    def test_exclui_parcelas_transferencias_e_juros(self):
        self.mensal("Loja", "parcelada", parcela=12)
        self.mensal("Parcela Paga Emprestimo", "emprestimo")
        self.mensal("PIX JOAO", "transferencia")
        self.mensal("IOF DIARIO DB PF", "iof")
        self.mensal("JUROS PIX CREDITO", "juros")
        self.mensal("MENSALIDADE PACOTE DE SERVICOS", "mensalidade")
        self.assertEqual([s["lojista"] for s in rec.sugestoes_payload()["sugestoes"]], ["mensalidade pacote de servicos"])

    def test_fixa_usa_termo_alternativo_completo(self):
        self.mensal("NETFLIX ENTRETENIMENTO BARUERI BRA")
        rec.sugestoes_payload()
        with banco.connect() as conn:
            conn.execute("INSERT INTO fixas_contas (id,nome,termo,valor_previsto,criado_em) VALUES ('fixa','Streaming','ERRADO',20,'2026-09-04')")
            conn.execute("INSERT INTO fixas_termos VALUES ('fixa',0,'ERRADO')")
            conn.execute("INSERT INTO fixas_termos VALUES ('fixa',1,'NETFLIX ENTRETENIMENTO')")
        self.assertFalse(rec.sugestoes_payload()["sugestoes"])

    def test_fixa_de_outra_conta_nao_cobre_sugestao(self):
        self.mensal("Netflix")
        rec.sugestoes_payload()
        with banco.connect() as conn:
            conn.execute("INSERT INTO fixas_contas (id,nome,termo,valor_previsto,conta_id,criado_em) VALUES ('fixa','Netflix','NETFLIX',20,'conta-b','2026-09-04')")
        self.assertEqual(len(rec.sugestoes_payload()["sugestoes"]), 1)

    def test_fixa_encerrada_nao_cobre_sugestao(self):
        self.mensal("Netflix")
        rec.sugestoes_payload()
        with banco.connect() as conn:
            conn.execute("INSERT INTO fixas_contas (id,nome,termo,valor_previsto,ate,criado_em) VALUES ('fixa','Netflix','NETFLIX',20,'2026-08','2026-09-04')")
        self.assertEqual(len(rec.sugestoes_payload()["sugestoes"]), 1)

    def test_cadastro_pausado_e_ignorado_nao_reaparecem(self):
        self.mensal()
        sugestao = rec.sugestoes_payload()["sugestoes"][0]
        with banco.connect() as conn:
            conn.execute("INSERT INTO recorrentes_previsoes VALUES (?,?,0,'2026-09-04')", (sugestao["chave"], json.dumps(sugestao)))
        self.assertFalse(rec.sugestoes_payload()["sugestoes"])

    def test_faixa_ignorada_resiste_a_reajuste_sem_ocultar_outro_plano(self):
        for mes in (6, 7, 8):
            self.inserir(f"plano-a-{mes}", f"2026-{mes:02d}-15", "App Store", valor=20.9)
            self.inserir(f"plano-b-{mes}", f"2026-{mes:02d}-15", "App Store", valor=50)
        rec.sugestoes_payload()
        with banco.connect() as conn:
            conn.execute("INSERT INTO recorrentes_ignorados VALUES ('cartao-b|app store|faixa:17.51:22.29','App Store','2026-09-04')")
            conn.execute("INSERT INTO recorrentes_ignorados VALUES ('cartao-b|app store|faixa:invalida','App Store','2026-09-04')")
        sugestoes = rec.sugestoes_payload()["sugestoes"]
        self.assertEqual([s["valorUltimo"] for s in sugestoes], [50])
        with banco.connect() as conn:
            conn.execute("DELETE FROM recorrentes_previsoes")
            conn.execute("INSERT INTO recorrentes_ignorados VALUES (?,?,'2026-09-04')", (sugestoes[0]["chave"], sugestoes[0]["descricao"]))
        self.assertFalse(rec.sugestoes_payload()["sugestoes"])


if __name__ == "__main__":
    unittest.main()
