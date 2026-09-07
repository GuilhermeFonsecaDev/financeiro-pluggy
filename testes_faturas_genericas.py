"""Regressões de consolidação e filtros, sem rede nem banco do usuário."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import banco
import cartoes
import extrato_camada
import pluggy_extrato as px
import visao_geral
from importar_pluggy import SCHEMA_PLUGGY


class DataTeste(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 4, tzinfo=tz)


class FaturasGenericasTests(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="faturas_genericas_")
        self.addCleanup(pasta.cleanup)
        for modulo, nome, valor in (
            (banco, "DATABASE_PATH", Path(pasta.name) / "teste.db"),
            (px, "_tabelas_prontas", False),
            (extrato_camada, "_camada_pronta", False),
            (px, "datetime", DataTeste),
        ):
            substituto = patch.object(modulo, nome, valor)
            substituto.start()
            self.addCleanup(substituto.stop)
        original, conexoes = banco.connect, []

        def conectar():
            conn = original()
            conexoes.append(conn)
            return conn

        substituto = patch.object(banco, "connect", side_effect=conectar)
        substituto.start()
        self.addCleanup(substituto.stop)
        self.addCleanup(lambda: [c.close() for c in conexoes])
        banco.ensure_database()
        with banco.connect() as conn:
            conn.executescript(SCHEMA_PLUGGY)
            for item, instituicao in (("a", "Banco Alpha"), ("b", "Banco Beta")):
                conn.execute("INSERT INTO pluggy_itens VALUES (?,?,?)", (item, instituicao, "2026-09-01"))
                conn.execute(
                    "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,titular,"
                    "limite_credito,limite_disponivel,vencimento,raw_json,importado_em) "
                    "VALUES (?,?,'CREDIT','CREDIT_CARD','VISA','9999','Teste',1000,900,?,?,'2026-09-01')",
                    (f"cartao-{item}", item, f"2026-10-{5 if item == 'a' else 10:02d}",
                     json.dumps({"creditData": {"brand": "VISA"}})),
                )
                conn.execute(
                    "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,titular,importado_em) "
                    "VALUES (?,?,'BANK','CHECKING_ACCOUNT','Conta','123','Teste','2026-09-01')",
                    (f"conta-{item}", item),
                )
            conn.execute(
                "INSERT INTO pluggy_faturas (fatura_id,conta_id,vencimento,fechamento,competencia,valor_total,importado_em) "
                "VALUES ('fatura-a','cartao-a','2026-10-05','2026-09-25','2026-10',100,'2026-09-01')"
            )
            transacoes = (
                ("compra-a", "cartao-a", 80, "fatura-a", "POSTED", None, None, {}),
                ("compra-b", "cartao-b", 40, "", "PENDING", None, None, {}),
                ("parcela-b", "cartao-b", 10, "", "PENDING", 1, 3,
                 {"creditCardMetadata": {"purchaseDate": "2026-09-10", "cardNumber": "9999"}}),
            )
            for tx, fonte, valor, fatura, status, parcela, total, raw in transacoes:
                conn.execute(
                    "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,valor,"
                    "tipo,status,fatura_id,parcela_numero,parcela_total,raw_json,importado_em) "
                    "VALUES (?,?,'2026-09-10','2026-09',2026,9,?,?,'DEBIT',?,?,?,?,?,'2026-09-01')",
                    (tx, fonte, tx, valor, status, fatura, parcela, total, json.dumps(raw)),
                )

    def agrupar(self):
        with banco.connect() as conn:
            ids = [i["cartaoId"] for i in cartoes.identidades(conn).values()]
        cartoes.salvar({"cartoes": [{"cartaoId": cid, "tag": "Viagens"} for cid in ids]})
        dados = px.cartoes_payload(2026)
        return dados, dados["cartoes"][0]

    def test_oficial_e_aberta_preservam_valores_e_parcelas(self):
        individual = px.cartoes_payload(2026)
        dados, grupo = self.agrupar()
        self.assertEqual(len(individual["cartoes"]), 2)
        self.assertEqual(len(dados["cartoes"]), 1)
        self.assertEqual(dados["valores"][grupo["id"]][9], 150)
        self.assertEqual(dados["origens"][grupo["id"]][9], "mista")
        self.assertEqual(dados["totalMes"], individual["totalMes"])
        self.assertEqual(dados["valores"][grupo["id"]][10:12], [10, 10])
        self.assertEqual(len(dados["itens"][grupo["id"]]), 2)
        self.assertTrue(all(i["contaId"] == "cartao-b" for i in dados["itens"][grupo["id"]]))
        self.assertIsNone(grupo["limiteCredito"])
        self.assertEqual(grupo["vencimentos"], ["2026-10-05", "2026-10-10"])
        resumo = visao_geral._cartoes_do_mes("2026-10")[0]
        self.assertEqual(resumo["valor"], 150)
        self.assertEqual(resumo["cor"], grupo["cor"])
        self.assertIsNone(resumo["limite"])

    def test_tag_filtra_reais_e_projetadas_sem_trocar_fonte(self):
        _, grupo = self.agrupar()
        reais = px.extrato_payload({"mesDe": "2026-10", "mesAte": "2026-10", "cartao": grupo["id"]})
        self.assertEqual({t["contaId"] for t in reais["transacoes"]}, {"cartao-a", "cartao-b"})
        self.assertTrue(all(t["contaNome"] == "Viagens" for t in reais["transacoes"]))
        futuras = px.extrato_payload({"mesDe": "2026-11", "mesAte": "2026-11", "cartao": grupo["id"]})
        self.assertEqual(len(futuras["transacoes"]), 1)
        parcela = futuras["transacoes"][0]
        self.assertEqual(parcela["contaId"], "cartao-b")
        self.assertEqual(parcela["contaNome"], "Viagens")
        self.assertEqual(parcela["grupoId"], grupo["id"])
        self.assertTrue(parcela["projetada"])
        self.assertEqual(futuras["resumo"]["saidas"], 10)
        individual = px.extrato_payload({"mesDe": "2026-11", "mesAte": "2026-11", "cartao": "cartao-a"})
        self.assertEqual(individual["transacoes"], [])
        fora = px.extrato_payload({"mesDe": "2026-11", "mesAte": "2026-11", "cartao": "nenhum"})
        self.assertEqual(fora["transacoes"], [])

    def preparar_mes_misto(self):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,valor,"
                "tipo,status,fatura_id,raw_json,importado_em) "
                "VALUES ('nova','cartao-b','2026-10-10','2026-10',2026,10,'nova',40,"
                "'DEBIT','PENDING','','{}','2026-10-10')"
            )
        return {"mesDe": "2026-11", "mesAte": "2026-11"}

    def test_mes_misto_resumo_e_evolucao_incluem_parcelas(self):
        filtros = self.preparar_mes_misto()
        dados = px.extrato_payload(filtros)
        self.assertEqual(dados["resumo"]["saidas"], 50)
        novembro = next(m for m in dados["evolucaoMensal"] if m["mes"] == "2026-11")
        self.assertEqual(novembro["saidas"], 50)
        self.assertTrue(novembro["saidasEstimativa"])
        # A série continua incluindo meses fora do período selecionado.
        dezembro = next(m for m in dados["evolucaoMensal"] if m["mes"] == "2026-12")
        self.assertEqual(dezembro["saidas"], 10)

    def test_filtros_preservam_projecoes_correspondentes(self):
        filtros = self.preparar_mes_misto()
        dados = px.extrato_payload({**filtros, "tipo": "DEBIT"})
        self.assertEqual(dados["resumo"]["saidas"], 50)
        parcela = next(t for t in dados["transacoes"] if t.get("projetada"))
        categoria = parcela["categoria"]["id"]
        self.assertIsNotNone(categoria)
        filtrado = px.extrato_payload({**filtros, "categoria": categoria})
        self.assertIn(parcela["id"], [t["id"] for t in filtrado["transacoes"]])
        busca = px.extrato_payload({**filtros, "busca": "parcela-b"})
        self.assertEqual(busca["resumo"]["saidas"], 10)
        self.assertEqual(next(m for m in busca["evolucaoMensal"] if m["mes"] == "2026-11")["saidas"], 10)
        for extra in ({"tipo": "CREDIT"}, {"status": "PENDING"},
                      {"status": "POSTED"}, {"modo": "mes"}, {"cartao": "nenhum"},
                      {"categoria": "inexistente"}):
            d = px.extrato_payload({**filtros, **extra})
            self.assertFalse(any(t.get("projetada") for t in d["transacoes"]))

    def test_paginacao_preserva_totais_sem_repetir_linhas(self):
        filtros = self.preparar_mes_misto()
        completo = px.extrato_payload(filtros)
        ids = []
        for offset in range(completo["totalFiltrado"]):
            pagina = px.extrato_payload({**filtros, "limite": 1, "offset": offset})
            self.assertEqual(pagina["resumo"], completo["resumo"])
            self.assertEqual(pagina["porCategoria"], completo["porCategoria"])
            self.assertEqual(pagina["evolucaoMensal"], completo["evolucaoMensal"])
            self.assertEqual(pagina["totalFiltrado"], completo["totalFiltrado"])
            self.assertEqual(len(pagina["transacoes"]), 1)
            self.assertEqual(pagina["temMais"], offset + 1 < completo["totalFiltrado"])
            ids.extend(t["id"] for t in pagina["transacoes"])
        self.assertEqual(ids, [t["id"] for t in completo["transacoes"]])
        vazio = px.extrato_payload({**filtros, "limite": 1, "offset": len(ids)})
        self.assertEqual(vazio["transacoes"], [])
        self.assertEqual(vazio["resumo"], completo["resumo"])

    def test_contas_e_cartoes_mesmo_numero_em_bancos_distintos(self):
        filtros = px.filtros_payload()
        self.assertEqual({c["id"] for c in filtros["contas"]}, {"conta-a", "conta-b"})
        self.assertEqual(len(filtros["cartoes"]), 2)
        self.assertEqual(len(filtros["contasEdicao"]), 4)

    def test_fatura_sem_transacoes_acrescenta_ano_disponivel(self):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_faturas (fatura_id,conta_id,vencimento,fechamento,competencia,valor_total,importado_em) "
                "VALUES ('historica','cartao-a','2024-03-05','2024-02-25','2024-03',123,'2026-09-01')"
            )
        dados = px.cartoes_payload(2024)
        self.assertIn(2024, dados["anosDisponiveis"])
        self.assertEqual(dados["totalMes"][2], 123)


if __name__ == "__main__":
    unittest.main()
