"""Recorrências: detecção, aceite, substituição pelo real e pausa, em banco temporário."""
import json
import unittest
from unittest.mock import patch
import banco
import recorrentes as rec
import pluggy_extrato as px
import testes_faturas_genericas as fixtures


class RecorrentesTests(unittest.TestCase):
    def setUp(self):
        fixtures.FaturasGenericasTests.setUp(self)
        p = patch.object(rec, "_mes_atual", return_value="2026-09")
        p.start()
        self.addCleanup(p.stop)
        for mes, descricao in ((6, "NETFLIX ENTRETENIMENTO BARUERI BRA"),
                               (7, "netflix.com sao paulo bra"),
                               (8, "NETFLIX.COM SAO PAULO BRA")):
            self.inserir(f"netflix-{mes}", mes, descricao, 20.9)

    def inserir(self, tx, mes, descricao, valor, parcela=None, conta="cartao-b"):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,valor,"
                "tipo,status,fatura_id,parcela_numero,parcela_total,raw_json,importado_em) "
                "VALUES (?,?,?, ?,2026,?, ?,?,'DEBIT','PENDING','',?,?,?,'2026-09-04')",
                (tx, conta, f"2026-{mes:02d}-15", f"2026-{mes:02d}", mes, descricao, valor,
                 1 if parcela else None, parcela, json.dumps({})),
            )

    def aceitar(self):
        s = next(s for s in rec.sugestoes_payload()["sugestoes"] if s["lojista"] == "netflix")
        rec.salvar_previsao(s["chave"])
        return s

    def test_descritores_e_tres_meses(self):
        sugestoes = rec.sugestoes_payload()["sugestoes"]
        netflix = [s for s in sugestoes if s["lojista"] == "netflix"]
        self.assertEqual(len(netflix), 1)
        self.assertEqual(netflix[0]["meses"], 3)
        self.assertEqual(netflix[0]["valorUltimo"], 20.9)
        with banco.connect() as conn:
            self.assertEqual(rec.projetados(conn, 2026), [])

    def test_exclui_parcelas_e_gastos_frequentes(self):
        for mes in (6, 7, 8):
            self.inserir(f"parcela-{mes}", mes, "Loja parcelada", 50, 12)
            self.inserir(f"emprestimo-{mes}", mes, "Parcela Paga | Emprestimo", 90)
            self.inserir(f"mercado-{mes}", mes, "Mercado", 30)
            self.inserir(f"mercado-extra-{mes}", mes, "Mercado", 30)
        self.assertEqual([s["lojista"] for s in rec.sugestoes_payload()["sugestoes"]], ["netflix"])

    def test_cobranca_extra_nao_esconde_assinatura(self):
        for mes in (6, 7, 8):
            self.inserir(f"apple-{mes}", mes, "Apple.Com/Bill", 19.9)
        self.inserir("apple-avulsa", 7, "Apple.Com/Bill", 8.9)
        dados = rec.sugestoes_payload()
        apple = next(s for s in dados["sugestoes"] if s["lojista"] == "apple.com/bill")
        self.assertEqual(apple["valorUltimo"], 19.9)
        self.assertTrue(apple["faixaValor"])
        rec.salvar_previsao(apple["chave"])
        self.inserir("apple-avulsa-set", 9, "Apple.Com/Bill", 8.9)
        d = px.extrato_payload({"mesDe":"2026-10", "mesAte":"2026-10"})
        self.assertEqual(next(t for t in d["transacoes"] if t.get("recorrente"))["valor"], 19.9)
        self.inserir("apple-real-set", 9, "Apple.Com/Bill", 19.9)
        self.assertFalse(any(t.get("recorrente") for t in px.extrato_payload({"mesDe":"2026-10", "mesAte":"2026-10"})["transacoes"]))

    def test_duplicidade_e_historico_curto_nao_sao_sugeridos(self):
        self.inserir("netflix-copia", 8, "Netflix.com", 20.9)
        for mes in (7, 8):
            self.inserir(f"assinatura-{mes}", mes, "Servico novo", 40)
        dados = rec.sugestoes_payload()
        self.assertEqual(dados["possiveis"], [])
        self.assertFalse(any(s["lojista"] in ("netflix", "servico novo") for s in dados["sugestoes"]))
        self.assertNotIn("descartadosDetalhes", dados)
        with self.assertRaises(ValueError):
            rec.salvar_previsao("cartao-b|servico novo")
        self.assertEqual(rec.sugestoes_payload()["previsoes"], [])

    def test_duas_assinaturas_no_mes_tem_previsoes_distintas(self):
        for mes in (6, 7, 8):
            self.inserir(f"servico-a-{mes}", mes, "App Store", 20)
            self.inserir(f"servico-b-{mes}", mes, "App Store", 50)
        servicos = [s for s in rec.sugestoes_payload()["sugestoes"] if s["lojista"] == "app store"]
        self.assertEqual(len(servicos), 2)
        for s in servicos:
            rec.salvar_previsao(s["chave"])
        self.inserir("servico-a-set", 9, "App Store", 20)
        dados = px.extrato_payload({"mesDe":"2026-10", "mesAte":"2026-10"})
        self.assertEqual([t["valor"] for t in dados["transacoes"] if t.get("recorrente")], [50])

    def test_datas_na_virada_nao_sao_gasto_frequente(self):
        for mes in (6,7,8):
            self.inserir(f"virada-{mes}", mes, "Servico virada", 30)
        with banco.connect() as conn:
            for tx, data in (("virada-6","2026-07-01"),("virada-7","2026-07-31"),("virada-8","2026-08-30")):
                conn.execute("UPDATE pluggy_transacoes SET data=? WHERE transacao_id=?", (data,tx))
        d = rec.sugestoes_payload()
        self.assertTrue(any(s["lojista"] == "servico virada" for s in d["possiveis"] + d["sugestoes"]))

    def test_aceite_nao_cria_fixa_e_pausa_remove_projecao(self):
        rec.sugestoes_payload()
        with banco.connect() as conn:
            antes = conn.execute("SELECT COUNT(*) FROM fixas_contas").fetchone()[0]
        s = self.aceitar()
        with banco.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fixas_contas").fetchone()[0], antes)
        f = {"mesDe": "2026-10", "mesAte": "2026-10"}
        d = px.extrato_payload(f)
        extras = [t for t in d["transacoes"] if t.get("recorrente")]
        self.assertEqual(len(extras), 1)
        self.assertEqual(extras[0]["valor"], 20.9)
        self.assertIsNone(extras[0]["parcelaTotal"])
        self.assertEqual(next(m for m in d["evolucaoMensal"] if m["mes"] == "2026-10")["saidas"], d["resumo"]["saidas"])
        cartoes = px.cartoes_payload(2026)
        self.assertEqual(cartoes["valores"]["cartao-b"][9], 70.9)
        rec.salvar_previsao(s["chave"], False)
        self.assertFalse(any(t.get("recorrente") for t in px.extrato_payload(f)["transacoes"]))
        rec.salvar_previsao(s["chave"], True)
        self.assertTrue(any(t.get("recorrente") for t in px.extrato_payload(f)["transacoes"]))

    def test_real_substitui_previsto_e_atualiza_valor(self):
        self.aceitar()
        self.inserir("netflix-setembro", 9, "NETFLIX ENTRETENIMENTO", 25.9)
        outubro = px.extrato_payload({"mesDe": "2026-10", "mesAte": "2026-10"})
        self.assertFalse(any(t.get("recorrente") for t in outubro["transacoes"]))
        novembro = px.extrato_payload({"mesDe": "2026-11", "mesAte": "2026-11"})
        self.assertEqual(next(t for t in novembro["transacoes"] if t.get("recorrente"))["valor"], 25.9)

    def test_fixa_posterior_impede_duplicacao(self):
        self.aceitar()
        with banco.connect() as conn:
            conn.execute("INSERT INTO fixas_contas (id,nome,termo,valor_previsto,criado_em) VALUES ('netflix-fixa','Netflix','NETFLIX',20.9,'2026-09-04')")
            self.assertEqual(rec.projetados(conn, 2026), [])

    def test_fatura_fechada_e_horizonte(self):
        self.aceitar()
        with banco.connect() as conn:
            conn.execute("INSERT INTO pluggy_faturas (fatura_id,conta_id,competencia,valor_total,importado_em) VALUES ('fechada','cartao-b','2026-10',60,'2026-09-04')")
            self.assertFalse(any(t["mes"] == 10 for t in rec.projetados(conn, 2026)))
            self.assertFalse(any(t["mes"] > 8 for t in rec.projetados(conn, 2027)))

    def test_conta_bancaria_e_filtros(self):
        for mes in (6, 7, 8):
            self.inserir(f"livelo-{mes}", mes, "CLUBE LIVELO", 44.9, conta="conta-b")
        s = next(s for s in rec.sugestoes_payload()["sugestoes"] if s["lojista"] == "livelo")
        rec.salvar_previsao(s["chave"])
        f = {"mesDe": "2026-10", "mesAte": "2026-10", "cartao": "nenhum"}
        d = px.extrato_payload(f)
        self.assertEqual(d["resumo"]["saidas"], 44.9)
        self.assertEqual(d["transacoes"][0]["contaId"], "conta-b")
        self.assertEqual(px.extrato_payload({**f, "conta": "conta-b"})["resumo"]["saidas"], 44.9)
        self.assertEqual(px.extrato_payload({**f, "conta": "conta-a"})["resumo"]["saidas"], 0)
        self.assertFalse(any(t.get("recorrente") for t in px.extrato_payload({**f, "cartao": "todos"})["transacoes"]))


if __name__ == "__main__":
    unittest.main()
