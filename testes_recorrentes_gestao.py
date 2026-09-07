"""Gestão de recorrências e substituição de previsões, só em banco temporário."""
import unittest
from unittest.mock import patch

import banco
import ciclos
import extrato_camada as cam
import pluggy_extrato as px
import recorrencias_gestao as gestao
import recorrentes as rec
import testes_recorrentes as base


class RecorrentesGestaoTests(unittest.TestCase):
    inserir = base.RecorrentesTests.inserir
    aceitar = base.RecorrentesTests.aceitar

    def setUp(self):
        base.RecorrentesTests.setUp(self)
        data_teste = patch.object(gestao, "datetime", base.fixtures.DataTeste)
        data_teste.start()
        self.addCleanup(data_teste.stop)

    def cadastro(self, **alteracoes):
        dados = {
            "descricao": "Assinatura manual", "lojista": "assinatura manual",
            "contaId": "conta-b", "valorPrevisto": 29.9,
            "modoValor": "ultimo", "diaTipico": 15,
        }
        dados.update(alteracoes)
        rec.criar_previsao(dados)
        return next(p for p in rec.sugestoes_payload()["previsoes"]
                    if p["lojista"] == dados["lojista"] and p["contaId"] == dados["contaId"])

    def previsao(self, chave):
        return next(p for p in rec.sugestoes_payload()["previsoes"] if p["chave"] == chave)

    def extrato(self, mes="2026-10", **filtros):
        return px.extrato_payload({"mesDe": mes, "mesAte": mes, **filtros})

    def recorrencias(self, mes="2026-10", **filtros):
        return [t for t in self.extrato(mes, **filtros)["transacoes"] if t.get("recorrente")]

    def conteudos_importados(self):
        with banco.connect() as conn:
            return [tuple(l) for l in conn.execute(
                "SELECT * FROM pluggy_transacoes ORDER BY transacao_id")]

    def test_manual_sem_transacao_base_entra_no_extrato_sem_criar_fixa(self):
        rec.sugestoes_payload()
        antes = self.conteudos_importados()
        with banco.connect() as conn:
            quantidade_fixas = conn.execute("SELECT COUNT(*) FROM fixas_contas").fetchone()[0]
        salva = self.cadastro()
        self.assertTrue(salva["ativa"])
        self.assertFalse(salva.get("transacaoBaseId"))
        linhas = self.recorrencias(conta="conta-b")
        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0]["descricao"], "Assinatura manual")
        self.assertEqual(linhas[0]["valor"], 29.9)
        self.assertEqual(linhas[0]["data"], "2026-10-15")
        self.assertEqual(linhas[0]["contaSubtipo"], "CHECKING_ACCOUNT")
        with banco.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fixas_contas").fetchone()[0], quantidade_fixas)
        self.assertEqual(self.conteudos_importados(), antes)

    def test_manualmente_cadastradas_tem_ids_distintos_e_estaveis(self):
        self.cadastro()
        self.cadastro(descricao="Outra assinatura", lojista="outra assinatura", valorPrevisto=11.5)
        primeiras = self.recorrencias()
        self.assertEqual(len(primeiras), 2)
        self.assertEqual(len({t["id"] for t in primeiras}), 2)
        self.assertEqual([t["id"] for t in primeiras], [t["id"] for t in self.recorrencias()])
        self.assertFalse({t["id"] for t in primeiras} & {t["id"] for t in self.recorrencias("2026-11")})
        completo = self.extrato(conta="conta-b")
        self.assertAlmostEqual(completo["resumo"]["saidas"], 41.4)
        outubro = next(m for m in completo["evolucaoMensal"] if m["mes"] == "2026-10")
        self.assertAlmostEqual(outubro["saidas"], 41.4)

    def test_manual_no_cartao_entra_na_fatura_e_real_a_substitui(self):
        antes = px.cartoes_payload(2026)["valores"]["cartao-b"][9]
        self.cadastro(contaId="cartao-b", noCartao=False)
        salva = rec.sugestoes_payload()["previsoes"][0]
        self.assertTrue(salva["noCartao"])
        self.assertAlmostEqual(px.cartoes_payload(2026)["valores"]["cartao-b"][9], antes + 29.9)
        self.assertEqual(len(self.recorrencias(cartao="cartao-b")), 1)
        self.inserir("manual-real-set", 9, "ASSINATURA MANUAL", 31.5)
        self.assertEqual(self.recorrencias(cartao="cartao-b"), [])
        self.assertAlmostEqual(px.cartoes_payload(2026)["valores"]["cartao-b"][9], antes + 31.5)
        self.assertEqual(self.recorrencias("2026-11", cartao="cartao-b")[0]["valor"], 31.5)

    def test_edicao_preserva_chave_e_aplica_nome_valor_dia_e_pagamento(self):
        salva = self.aceitar()
        antes = self.conteudos_importados()
        rec.editar_previsao(salva["chave"], {
            "descricao": "Netflix família", "contaId": "conta-b",
            "valorPrevisto": 39.9, "modoValor": "fixo", "diaTipico": 22,
        })
        atual = self.previsao(salva["chave"])
        self.assertEqual(atual["chave"], salva["chave"])
        self.assertEqual(atual["descricao"], "Netflix família")
        self.assertEqual(atual["lojista"], "netflix")
        self.assertEqual(atual["contaId"], "conta-b")
        self.assertFalse(atual["noCartao"])
        self.assertEqual(self.recorrencias(cartao="cartao-b"), [])
        linha = self.recorrencias(conta="conta-b")[0]
        self.assertEqual((linha["valor"], linha["data"]), (39.9, "2026-10-22"))
        self.assertEqual(self.conteudos_importados(), antes)

    def test_troca_conta_casamento_apenas_no_pagamento_escolhido(self):
        salva = self.aceitar()
        rec.editar_previsao(salva["chave"], {"contaId": "conta-b"})
        self.inserir("netflix-cartao-antigo", 10, "NETFLIX.COM", 99)
        self.assertEqual(self.recorrencias(conta="conta-b")[0]["valor"], 20.9)
        self.inserir("netflix-conta-nova", 10, "NETFLIX.COM", 25.9, conta="conta-b")
        self.assertEqual(self.recorrencias(conta="conta-b"), [])
        self.assertEqual(self.recorrencias("2026-11", conta="conta-b")[0]["valor"], 25.9)

    def test_nome_amigavel_nao_muda_identificador_de_casamento(self):
        salva = self.cadastro(descricao="Plano familiar", lojista="provedor video")
        rec.editar_previsao(salva["chave"], {"descricao": "Lazer mensal"})
        self.inserir("outro-provedor", 10, "provedor video adicional", 29.9, conta="conta-b")
        self.assertEqual(len(self.recorrencias(conta="conta-b")), 1)
        self.inserir("provedor-exato", 10, "PROVEDOR VIDEO", 29.9, conta="conta-b")
        self.assertEqual(self.recorrencias(conta="conta-b"), [])

    def test_pausa_e_retomada_preservam_edicao(self):
        salva = self.cadastro()
        rec.editar_previsao(salva["chave"], {"descricao": "Assinatura editada", "diaTipico": 27})
        rec.salvar_previsao(salva["chave"], False)
        self.assertFalse(self.previsao(salva["chave"])["ativa"])
        self.assertEqual(self.recorrencias(), [])
        rec.salvar_previsao(salva["chave"], True)
        linha = self.recorrencias()[0]
        self.assertEqual((linha["descricao"], linha["data"]), ("Assinatura editada", "2026-10-27"))

    def test_excluir_remove_cadastro_e_sugestao_preservando_transacoes(self):
        salva = self.aceitar()
        antes = self.conteudos_importados()
        rec.excluir_previsao(salva["chave"])
        dados = rec.sugestoes_payload()
        self.assertFalse(any(p["chave"] == salva["chave"] for p in dados["previsoes"]))
        self.assertFalse(any(p["lojista"] == "netflix" for p in dados["sugestoes"]))
        self.assertEqual(self.recorrencias(), [])
        self.assertEqual(self.conteudos_importados(), antes)
        with banco.connect() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM recorrentes_ignorados WHERE chave=?", (salva["chave"],)).fetchone())
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM recorrentes_previsoes WHERE chave=?", (salva["chave"],)).fetchone())

    def test_validacao_criacao_rejeita_valor_nao_finito_ou_nao_positivo(self):
        for valor in (float("nan"), float("inf"), float("-inf"), 0, -1, "abc"):
            with self.subTest(valor=valor), self.assertRaises(ValueError):
                self.cadastro(valorPrevisto=valor)
        self.assertEqual(rec.sugestoes_payload()["previsoes"], [])

    def test_edicao_invalida_nao_altera_o_cadastro(self):
        salva = self.cadastro()
        antes = self.previsao(salva["chave"])
        for campo, valor in (
            ("valorPrevisto", float("nan")), ("valorPrevisto", float("inf")),
            ("valorPrevisto", 0), ("valorPrevisto", -10),
            ("diaTipico", 0), ("diaTipico", 32), ("diaTipico", 2.5),
            ("contaId", "inexistente"), ("modoValor", "qualquer"),
            ("descricao", "  "), ("lojista", "  "),
        ):
            with self.subTest(campo=campo, valor=valor):
                with self.assertRaises(ValueError):
                    rec.editar_previsao(salva["chave"], {campo: valor})
                self.assertEqual(self.previsao(salva["chave"]), antes)

    def test_validacao_manual_rejeita_dia_e_conta_invalidos(self):
        for dados in ({"diaTipico": 0}, {"diaTipico": 32}, {"diaTipico": 1.5},
                      {"contaId": "inexistente"}, {"modoValor": "qualquer"}):
            with self.subTest(dados=dados), self.assertRaises(ValueError):
                self.cadastro(**dados)
        self.assertEqual(rec.sugestoes_payload()["previsoes"], [])

    def test_mesmo_lojista_na_mesma_conta_nao_pode_ser_duplicado(self):
        self.cadastro()
        with self.assertRaises(ValueError):
            self.cadastro(descricao="Nome alternativo", valorPrevisto=59.9)
        self.cadastro(contaId="conta-a")
        self.assertEqual(len(rec.sugestoes_payload()["previsoes"]), 2)

    def test_edicao_nao_pode_colidir_com_outro_cadastro(self):
        self.cadastro()
        outra = self.cadastro(contaId="conta-a")
        with self.assertRaises(ValueError):
            rec.editar_previsao(outra["chave"], {"contaId": "conta-b"})
        self.assertEqual(self.previsao(outra["chave"])["contaId"], "conta-a")

    def test_faixas_distintas_mesmo_lojista_podem_ser_aceitas(self):
        for mes in (6, 7, 8):
            self.inserir(f"app-menor-{mes}", mes, "App Store", 20)
            self.inserir(f"app-maior-{mes}", mes, "App Store", 50)
        sugestoes = [s for s in rec.sugestoes_payload()["sugestoes"] if s["lojista"] == "app store"]
        self.assertEqual(len(sugestoes), 2)
        for sugestao in sugestoes:
            rec.salvar_previsao(sugestao["chave"])
        self.assertEqual(sorted(t["valor"] for t in self.recorrencias()), [20, 50])

    def test_excluir_faixa_apos_trocar_pagamento_nao_sugere_de_novo(self):
        for conta in ("cartao-b", "conta-b"):
            for mes in (6, 7, 8):
                self.inserir(f"app-menor-{conta}-{mes}", mes, "App Store", 20, conta=conta)
                self.inserir(f"app-maior-{conta}-{mes}", mes, "App Store", 50, conta=conta)
        sugestao = next(s for s in rec.sugestoes_payload()["sugestoes"]
                        if s["lojista"] == "app store" and s["contaId"] == "cartao-b"
                        and s["valorUltimo"] == 20)
        rec.salvar_previsao(sugestao["chave"])
        rec.editar_previsao(sugestao["chave"], {"contaId": "conta-b"})
        rec.excluir_previsao(sugestao["chave"])
        novas = rec.sugestoes_payload()["sugestoes"]
        self.assertFalse(any(s["lojista"] == "app store" and s["contaId"] == "conta-b"
                             and s["valorUltimo"] == 20 for s in novas))
        self.assertTrue(any(s["lojista"] == "app store" and s["contaId"] == "conta-b"
                            and s["valorUltimo"] == 50 for s in novas))

    def test_modo_fixo_nao_e_substituido_pelo_ultimo_valor(self):
        salva = self.aceitar()
        rec.editar_previsao(salva["chave"], {"modoValor": "fixo", "valorPrevisto": 34.9})
        self.inserir("netflix-real-set", 9, "Netflix.com", 25.9)
        self.assertEqual(self.recorrencias(), [])
        self.assertEqual(self.recorrencias("2026-11")[0]["valor"], 34.9)
        self.assertEqual(self.previsao(salva["chave"])["valorPrevisto"], 34.9)

    def test_lista_ativa_mostra_valor_atual_e_nome_editado(self):
        salva = self.aceitar()
        rec.editar_previsao(salva["chave"], {"descricao": "Netflix da casa"})
        self.inserir("netflix-real-set", 9, "Netflix.com", 25.9)
        with banco.connect() as conn:
            conn.execute("UPDATE pluggy_transacoes SET data='2026-09-02' WHERE transacao_id='netflix-real-set'")
        atual = self.previsao(salva["chave"])
        self.assertEqual(atual["descricao"], "Netflix da casa")
        self.assertEqual(atual["valorUltimo"], 25.9)
        self.assertEqual(atual["valorAtual"], 25.9)
        self.assertEqual(atual["valorPrevisto"], 20.9)

    def test_transacao_excluida_nao_substitui_nem_reajusta_previsao(self):
        self.aceitar()
        self.inserir("netflix-excluida-set", 9, "Netflix.com", 80)
        cam.ajustar_transacao("netflix-excluida-set", {"incluidaNosCalculos": False})
        self.assertEqual(self.recorrencias()[0]["valor"], 20.9)
        self.assertEqual(self.recorrencias("2026-11")[0]["valor"], 20.9)

    def test_parcela_no_texto_nao_substitui_nem_reajusta_previsao(self):
        self.aceitar()
        self.inserir("netflix-parcelada-set", 9, "NETFLIX 2/12", 80)
        self.assertEqual(self.recorrencias()[0]["valor"], 20.9)
        self.assertEqual(self.recorrencias("2026-11")[0]["valor"], 20.9)

    def test_valor_futuro_nao_contamina_previsoes_anteriores(self):
        self.cadastro()
        self.inserir("manual-futura", 11, "ASSINATURA MANUAL", 99, conta="conta-b")
        self.assertEqual(self.recorrencias("2026-10")[0]["valor"], 29.9)
        self.assertEqual(self.recorrencias("2026-11"), [])
        self.assertEqual(self.recorrencias("2026-12")[0]["valor"], 99)

    def test_dia_31_encurta_em_fevereiro_sem_mudar_configuracao(self):
        salva = self.cadastro(diaTipico=31)
        linha = self.recorrencias("2027-02", conta="conta-b")[0]
        self.assertEqual(linha["data"], "2027-02-28")
        self.assertEqual(self.previsao(salva["chave"])["diaTipico"], 31)
        self.assertEqual(self.recorrencias("2027-03", conta="conta-b")[0]["data"], "2027-03-31")

    def test_dia_tipico_respeita_ciclo_de_fatura_antes_e_depois_do_fechamento(self):
        salva = self.cadastro(contaId="cartao-a", diaTipico=15)
        antes = self.recorrencias("2026-11", cartao="cartao-a")[0]
        self.assertEqual(antes["data"], "2026-10-15")
        rec.editar_previsao(salva["chave"], {"diaTipico": 28})
        depois = self.recorrencias("2026-11", cartao="cartao-a")[0]
        self.assertEqual(depois["data"], "2026-09-28")
        with banco.connect() as conn:
            self.assertEqual(ciclos.competencia_de(conn, "cartao-a", antes["data"]), "2026-11")
            self.assertEqual(ciclos.competencia_de(conn, "cartao-a", depois["data"]), "2026-11")


if __name__ == "__main__":
    unittest.main()
