"""Um cadastro, dois comportamentos: cobrança única e reserva do mês.

Cobrança única: a transação real SUBSTITUI a previsão da competência.
Reserva do mês: as compras reais CONSOMEM o previsto, e sobra o restante.

O que estes testes protegem é a unificação em si -- que os dois comportamentos
sejam o mesmo cadastro, conversível, e que a reserva não seja mais privilégio
de cartão.
"""
import json
import unittest
from unittest.mock import patch

import banco
import recorrencias_gestao as gestao
import recorrentes as rec
import testes_recorrentes as base


class ComportamentoTests(unittest.TestCase):
    inserir = base.RecorrentesTests.inserir

    def setUp(self):
        base.RecorrentesTests.setUp(self)
        data_teste = patch.object(gestao, "datetime", base.fixtures.DataTeste)
        data_teste.start()
        self.addCleanup(data_teste.stop)

    def gastar(self, tx, dia, descricao, valor, conta="conta-b"):
        """Compra JÁ ocorrida: lançamento futuro não consome reserva, e o dia
        padrão do `inserir` (15) cai depois do 'hoje' das fixtures (04/09)."""
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,"
                "descricao,valor,tipo,status,fatura_id,parcela_numero,parcela_total,raw_json,"
                "importado_em) VALUES (?,?,?,'2026-09',2026,9,?,?,'DEBIT','PENDING','',"
                "NULL,NULL,'{}','2026-09-04')",
                (tx, conta, f"2026-09-{dia:02d}", descricao, valor))

    def gastar_no_mes(self, tx, mes, descricao, valor, dia=2, conta="conta-b"):
        """Compra num mês passado, em dia que já aconteceu."""
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,"
                "descricao,valor,tipo,status,fatura_id,parcela_numero,parcela_total,raw_json,"
                "importado_em) VALUES (?,?,?,?,2026,?,?,?,'DEBIT','PENDING','',"
                "NULL,NULL,'{}','2026-09-04')",
                (tx, conta, f"2026-{mes:02d}-{dia:02d}", f"2026-{mes:02d}", mes, descricao, valor))

    def dados(self, chave):
        with banco.connect() as conn:
            linha = conn.execute(
                "SELECT dados FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        return json.loads(linha["dados"])

    def netflix(self):
        return next(s for s in rec.sugestoes_payload()["sugestoes"]
                    if s["lojista"] == "netflix")

    def previsao(self, chave):
        return next(p for p in rec.sugestoes_payload()["previsoes"] if p["chave"] == chave)

    def projetado(self, chave, ano=2026):
        with banco.connect() as conn:
            return [i for i in gestao.projetados(conn, ano)
                    if i["compraId"] == "recorrente:" + chave]

    # ------------------------------------------------------------ leitura
    def test_cadastro_antigo_sem_campo_e_lido_pelo_tipo(self):
        """Compatibilidade: `tipo: habito` continua valendo como reserva."""
        self.assertEqual(gestao.comportamento_de({"tipo": "habito"}), "reserva")
        self.assertEqual(gestao.comportamento_de({}), "cobranca")
        # O campo novo tem precedência sobre o espelho antigo.
        self.assertEqual(
            gestao.comportamento_de({"tipo": "habito", "comportamento": "cobranca"}),
            "cobranca")

    def test_gravar_reserva_espelha_o_tipo_antigo(self):
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "valorPrevisto": 400, "modoValor": "fixo",
            "diaTipico": 10, "comportamento": "reserva"})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["lojista"] == "mercado do bairro")
        gravado = self.dados(chave)
        self.assertEqual(gravado["comportamento"], "reserva")
        self.assertEqual(gravado["tipo"], "habito", "o espelho antigo tem de continuar")

    # --------------------------------------------------------- comportamento
    def test_cobranca_e_substituida_pelo_real_e_reserva_e_consumida(self):
        s = self.netflix()
        rec.salvar_previsao(s["chave"])
        # Cobrança já identificada em setembro: nada previsto para o mês.
        self.inserir("netflix-9", 9, "NETFLIX.COM SAO PAULO BRA", 20.9)
        setembro = [i for i in self.projetado(s["chave"]) if i["mes"] == 9]
        self.assertEqual(setembro, [], "cobrança única: o real substitui o previsto")

        rec.converter_previsao(s["chave"], "reserva")
        setembro = [i for i in self.projetado(s["chave"]) if i["mes"] == 9]
        self.assertEqual(setembro, [],
                         "reserva consumida por inteiro não sobra saldo a prever")
        self.assertEqual(self.previsao(s["chave"])["comportamento"], "reserva")

    def test_cobrada_aparece_no_ciclo_sem_somar_previsao(self):
        rec.criar_previsao({
            "descricao": "Visor", "lojista": "visor", "contaId": "conta-b",
            "valorPrevisto": 50, "modoValor": "fixo", "diaTipico": 2,
            "comportamento": "cobranca"})
        self.gastar("visor-real", 2, "VISOR", 49.9)
        rec.sugestoes_payload()  # Atualiza o extrato materializado.
        with banco.connect() as conn:
            cobradas = gestao.cobradas(conn, "2026-09", {"conta-b"})
            visor = next(i for i in cobradas if i["descricao"] == "Visor")
            self.assertEqual(visor["valorLancado"], 49.9)
            self.assertEqual(gestao.cobradas(conn, "2026-10", {"conta-b"}), [])
            self.assertEqual(gestao.cobradas(conn, "2026-09", {"outra-conta"}), [])
            self.assertFalse(any(i["mes"] == 9 and "visor" in i["descricao"].lower()
                                 for i in gestao.projetados(conn, 2026)))
            conn.execute("UPDATE recorrentes_previsoes SET ativo=0 WHERE chave=?",
                         (visor["chave"],))
            self.assertEqual(gestao.cobradas(conn, "2026-09", {"conta-b"}), [])

    def test_reserva_sobra_o_que_ainda_nao_foi_gasto(self):
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "valorPrevisto": 400, "modoValor": "fixo",
            "diaTipico": 10, "comportamento": "reserva"})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["lojista"] == "mercado do bairro")
        antes = [i for i in self.projetado(chave) if i["mes"] == 9]
        self.assertEqual(len(antes), 1)
        # Não são os R$ 400 cheios: "hoje" é 04/09 e restam 27 dos 30 dias.
        self.assertEqual(antes[0]["valor"], round(400 * 27 / 30, 2))

        self.gastar("mercado-set", 2, "MERCADO DO BAIRRO", 150)
        depois = [i for i in self.projetado(chave) if i["mes"] == 9]
        # Agora o saldo do orçamento (250) é menor que o teto do tempo (360),
        # e vale o menor dos dois.
        self.assertEqual(depois[0]["valor"], 250, "a compra real consome a reserva")
        self.assertEqual(depois[0]["valorLancado"], 150)

    def test_reserva_vale_em_conta_bancaria(self):
        """A reserva era exclusiva de cartão -- e não havia razão para isso."""
        rec.criar_previsao({
            "descricao": "Feira", "lojista": "feira livre",
            "contaId": "conta-b", "valorPrevisto": 120, "modoValor": "fixo",
            "diaTipico": 5, "comportamento": "reserva"})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["lojista"] == "feira livre")
        itens = self.projetado(chave)
        self.assertTrue(itens, "reserva em conta bancária tem de projetar")
        self.assertFalse(itens[0]["noCartao"])

    def test_dispensar_um_habito_tira_ele_da_lista(self):
        """Só as sugestões consultavam a lista de dispensados; hábito voltava."""
        for mes in (5, 6, 7, 8):
            for indice in range(2):
                self.inserir(f"posto-{mes}-{indice}", mes, "POSTO IPIRANGA", 100)
        posto = next(h for h in rec.sugestoes_payload()["habitos"] if "posto" in h["lojista"])
        rec.ignorar(posto["chave"], posto["descricao"])

        depois = rec.sugestoes_payload()
        self.assertFalse([h for h in depois["habitos"] if "posto" in h["lojista"]])
        self.assertFalse([d for d in depois["descobertas"] if "posto" in d["lojista"]])

        rec.reconsiderar(posto["chave"])
        self.assertTrue([h for h in rec.sugestoes_payload()["habitos"] if "posto" in h["lojista"]])

    # -------------------------------------------------- termos e categoria
    def categorizar(self, transacoes, categoria="combustivel", nome="Postos de combustível"):
        """Garante a categoria e prende as transações nela, via ajuste manual."""
        with banco.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO extrato_categorias "
                "(id,nome,cor,ativo,ordem,pai_id,emoji,personalizada) "
                "VALUES (?,?,'#888888',1,90,NULL,'',1)", (categoria, nome))
            for tx in transacoes:
                conn.execute(
                    "INSERT INTO extrato_ajustes (transacao_id,categoria_id_manual,atualizado_em) "
                    "VALUES (?,?,'2026-09-04') ON CONFLICT(transacao_id) DO UPDATE SET "
                    "categoria_id_manual=excluded.categoria_id_manual",
                    (tx, categoria))

    def test_varios_termos_casam_por_conter_a_palavra(self):
        """Um cadastro, vários postos: o termo pega todos sem cadastrar um a um."""
        for mes in (5, 6, 7, 8):
            self.inserir(f"shell-{mes}", mes, "POSTO SHELL AVENIDA", 60, conta="conta-b")
            self.inserir(f"ale-{mes}", mes, "Posto Ale Centro", 40, conta="conta-b")
        rec.criar_previsao({
            "descricao": "Gasolina", "lojista": "", "termos": ["posto"],
            "contaId": "conta-b", "valorPrevisto": 100, "modoValor": "media",
            "diaTipico": 5, "comportamento": "reserva"})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["descricao"] == "Gasolina")
        # Quatro meses de R$ 100 (60 + 40): a média dos ciclos é R$ 100.
        self.assertEqual(previsao["valorAtual"], 100)
        self.assertEqual(previsao["termos"], ["posto"])

    def test_termo_curto_demais_e_recusado(self):
        """Duas letras casariam com meio extrato ("ar" dentro de "farmacia")."""
        rec.criar_previsao({
            "descricao": "Gasolina", "lojista": "", "termos": ["po", "posto"],
            "contaId": "conta-b", "valorPrevisto": 100, "modoValor": "fixo",
            "diaTipico": 5, "comportamento": "reserva"})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["descricao"] == "Gasolina")
        self.assertEqual(previsao["termos"], ["posto"])

    def test_cadastro_sem_nenhum_crivo_e_recusado(self):
        with self.assertRaises(ValueError):
            rec.criar_previsao({
                "descricao": "Nada", "lojista": "", "termos": [], "categoriaId": "",
                "contaId": "conta-b", "valorPrevisto": 100, "modoValor": "fixo",
                "diaTipico": 5})

    def test_categoria_pega_qualquer_estabelecimento_dela(self):
        rec.sugestoes_payload()          # constrói a camada e as categorias
        for mes in (5, 6, 7, 8):
            self.inserir(f"shell-{mes}", mes, f"LOJA DIFERENTE {mes}", 50, conta="conta-b")
        self.categorizar([f"shell-{mes}" for mes in (5, 6, 7, 8)])
        rec.criar_previsao({
            "descricao": "Gasolina", "lojista": "", "categoriaId": "combustivel",
            "contaId": "conta-b", "valorPrevisto": 10, "modoValor": "media",
            "diaTipico": 5, "comportamento": "reserva"})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["descricao"] == "Gasolina")
        # Nenhuma das descrições se repete: só a categoria as une.
        self.assertEqual(previsao["valorAtual"], 50)

    def test_categoria_nao_conta_o_que_um_cadastro_mais_especifico_pega(self):
        """A reserva da categoria e a do posto da esquina não somam o mesmo litro."""
        rec.sugestoes_payload()
        for mes in (5, 6, 7, 8):
            self.inserir(f"central-{mes}", mes, "POSTO CENTRAL", 80, conta="conta-b")
            self.inserir(f"outro-{mes}", mes, f"POSTO NOVO {mes}", 20, conta="conta-b")
        self.categorizar([f"central-{mes}" for mes in (5, 6, 7, 8)]
                         + [f"outro-{mes}" for mes in (5, 6, 7, 8)])
        rec.criar_previsao({
            "descricao": "Central", "lojista": "posto central", "contaId": "conta-b",
            "valorPrevisto": 80, "modoValor": "media", "diaTipico": 5,
            "comportamento": "reserva"})
        rec.criar_previsao({
            "descricao": "Gasolina em geral", "lojista": "", "categoriaId": "combustivel",
            "contaId": "conta-b", "valorPrevisto": 10, "modoValor": "media",
            "diaTipico": 5, "comportamento": "reserva"})
        previsoes = {p["descricao"]: p for p in rec.sugestoes_payload()["previsoes"]}
        self.assertEqual(previsoes["Central"]["valorAtual"], 80)
        # Só os R$ 20 que ninguém mais reivindica.
        self.assertEqual(previsoes["Gasolina em geral"]["valorAtual"], 20)

    def test_categoria_soma_todos_os_pagamentos(self):
        """Quanto eu gasto de comida por mês é um fato sobre mim, não sobre um
        cartão. Já Netflix é uma cobrança num cartão específico."""
        rec.sugestoes_payload()
        # Duas contas bancárias de propósito: a soma é por COMPETÊNCIA, e num
        # cartão a compra cai na fatura do mês seguinte. Misturar as duas
        # réguas aqui só embaralharia a asserção -- a régua certa para prever
        # fatura é a competência, e é ela que vale nos dois casos.
        for mes in (6, 7, 8):
            self.gastar_no_mes(f"aqui-{mes}", mes, f"MERCADO {mes}", 60, conta="conta-b")
            self.gastar_no_mes(f"ali-{mes}", mes, f"PADARIA {mes}", 40, conta="conta-a")
        self.categorizar([f"aqui-{m}" for m in (6, 7, 8)]
                         + [f"ali-{m}" for m in (6, 7, 8)], "alimentacao", "Alimentação")
        rec.criar_previsao({
            "descricao": "Comida", "lojista": "", "categoriaId": "alimentacao",
            "contaId": "conta-b", "modoValor": "media", "janelaMedia": 3,
            "comportamento": "reserva"})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["descricao"] == "Comida")
        # 60 numa conta + 40 na outra, em cada um dos três meses.
        self.assertEqual(previsao["valorAtual"], 100)

    def test_lojista_continua_preso_ao_seu_pagamento(self):
        rec.sugestoes_payload()
        for mes in (6, 7, 8):
            self.gastar_no_mes(f"aqui-{mes}", mes, "ASSINATURA X", 30, conta="conta-b")
            self.gastar_no_mes(f"ali-{mes}", mes, "ASSINATURA X", 70, conta="cartao-b")
        rec.criar_previsao({
            "descricao": "Assinatura", "lojista": "assinatura x", "contaId": "conta-b",
            "modoValor": "media", "janelaMedia": 3, "comportamento": "reserva"})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["descricao"] == "Assinatura")
        self.assertEqual(previsao["valorAtual"], 30, "não pode somar o outro pagamento")

    def test_base_ignora_o_ciclo_ainda_aberto(self):
        """Usar o ciclo corrente como base é circular: o restante seria zero."""
        for mes, valor in ((6, 300), (7, 300), (8, 300)):
            self.gastar_no_mes(f"merc-{mes}", mes, "MERCADO DO BAIRRO", valor)
        self.gastar("merc-agora", 2, "MERCADO DO BAIRRO", 10)   # ciclo corrente
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "modoValor": "media", "janelaMedia": 3,
            "comportamento": "reserva", "rateioProporcional": False})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["descricao"] == "Mercado")
        setembro = next(i for i in self.projetado(chave) if i["mes"] == 9)
        self.assertEqual(setembro["valorBase"], 300, "os R$ 10 de hoje não são a base")
        self.assertEqual(setembro["valor"], 290)
        # Meses seguintes usam a MESMA base: outubro ainda está pela metade.
        outubro = next(i for i in self.projetado(chave) if i["mes"] == 10)
        self.assertEqual(outubro["valorBase"], 300)

    def test_testar_conta_os_lancamentos_antes_de_salvar(self):
        for mes in (6, 7, 8):
            self.inserir(f"shell-{mes}", mes, "POSTO SHELL", 70, conta="conta-b")
        resultado = rec.testar_previsao({"contaId": "conta-b", "termos": ["posto"]})
        self.assertEqual(resultado["lancamentos"], 3)
        self.assertEqual(resultado["porCriterio"], {"termo": 3})
        self.assertEqual(resultado["mediaCiclos"], 70)
        self.assertTrue(resultado["exemplos"])

    # ------------------------------------------- janela da média e rateio
    def test_janela_da_media_muda_a_base(self):
        """3, 6 ou 12 ciclos: a janela curta segue o hábito recente."""
        for mes, valor in ((3, 400), (4, 400), (5, 400), (6, 100), (7, 100), (8, 100)):
            self.gastar_no_mes(f"merc-{mes}", mes, "MERCADO DO BAIRRO", valor)
        for janela, esperado in ((3, 100), (6, 250)):
            rec.criar_previsao({
                "descricao": f"Mercado {janela}", "lojista": "mercado do bairro",
                "contaId": "conta-b", "valorPrevisto": 1, "modoValor": "media",
                "diaTipico": 10, "comportamento": "reserva", "janelaMedia": janela})
            previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                            if p["descricao"] == f"Mercado {janela}")
            self.assertEqual(previsao["janelaMedia"], janela)
            self.assertEqual(previsao["valorAtual"], esperado,
                             f"janela de {janela} ciclos")
            rec.excluir_previsao(previsao["chave"])

    def test_janela_invalida_cai_no_padrao(self):
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "valorPrevisto": 100, "modoValor": "media",
            "diaTipico": 10, "comportamento": "reserva", "janelaMedia": 7})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["descricao"] == "Mercado")
        self.assertEqual(previsao["janelaMedia"], 3)

    def test_media_vale_de_verdade_na_cobranca_unica(self):
        """Antes "media" era aceita na cobrança e caía calada no valor fixo."""
        for mes, valor in ((6, 100), (7, 200), (8, 300)):
            self.gastar_no_mes(f"luz-{mes}", mes, "COMPANHIA DE ENERGIA", valor)
        rec.criar_previsao({
            "descricao": "Energia", "lojista": "companhia de energia",
            "contaId": "conta-b", "valorPrevisto": 1, "modoValor": "media",
            "diaTipico": 12, "comportamento": "cobranca", "janelaMedia": 3})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["descricao"] == "Energia")
        projetados = self.projetado(chave)
        self.assertTrue(projetados, "a cobrança não projetou nada")
        self.assertEqual(projetados[0]["valor"], 200, "média de 100, 200 e 300")

    def test_reserva_e_proporcional_ao_que_falta_do_ciclo(self):
        """Faltando dois dias, não se prevê um mês inteiro de mercado."""
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "valorPrevisto": 300, "modoValor": "fixo",
            "diaTipico": 10, "comportamento": "reserva"})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["descricao"] == "Mercado")
        # Conta bancária: o ciclo é o mês corrido. "Hoje" das fixtures é 04/09,
        # então restam 27 dos 30 dias de setembro.
        setembro = next(i for i in self.projetado(chave) if i["mes"] == 9)
        self.assertAlmostEqual(setembro["fracaoRestante"], 27 / 30, places=2)
        self.assertAlmostEqual(setembro["valor"], round(300 * 27 / 30, 2), places=2)
        # Outubro ainda não começou: previsão cheia.
        outubro = next(i for i in self.projetado(chave) if i["mes"] == 10)
        self.assertEqual(outubro["fracaoRestante"], 1.0)
        self.assertEqual(outubro["valor"], 300)

    def test_rateio_pode_ser_desligado(self):
        """Gasolina é um abastecimento por mês: acontece inteiro ou não acontece."""
        for chave, rateia, esperado in (("Espalhado", True, round(300 * 27 / 30, 2)),
                                        ("Evento", False, 300)):
            rec.criar_previsao({
                "descricao": chave, "lojista": f"loja {chave.lower()}",
                "contaId": "conta-b", "valorPrevisto": 300, "modoValor": "fixo",
                "comportamento": "reserva", "rateioProporcional": rateia})
        projetados = {p["descricao"]: p for p in rec.sugestoes_payload()["previsoes"]}
        for nome, esperado in (("Espalhado", round(300 * 27 / 30, 2)), ("Evento", 300)):
            setembro = next(i for i in self.projetado(projetados[nome]["chave"])
                            if i["mes"] == 9)
            self.assertEqual(setembro["valor"], esperado, nome)

    def test_reserva_nao_pede_dia_da_cobranca(self):
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "valorPrevisto": 300, "modoValor": "fixo",
            "comportamento": "reserva"})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["descricao"] == "Mercado")
        self.assertEqual(self.dados(chave)["diaTipico"], 1)
        # Cobrança única continua exigindo.
        with self.assertRaises(ValueError):
            rec.criar_previsao({
                "descricao": "Assinatura", "lojista": "assinatura qualquer",
                "contaId": "conta-b", "valorPrevisto": 30, "modoValor": "fixo",
                "comportamento": "cobranca"})

    # ------------------------------------------------------------ conversão
    def test_conversao_preserva_a_chave_e_o_estado(self):
        s = self.netflix()
        rec.salvar_previsao(s["chave"])
        rec.salvar_previsao(s["chave"], False)          # pausa
        rec.converter_previsao(s["chave"], "reserva")
        previsao = self.previsao(s["chave"])
        self.assertEqual(previsao["chave"], s["chave"], "a chave é identidade, não rótulo")
        self.assertFalse(previsao["ativa"], "converter não retoma uma pausada")
        rec.converter_previsao(s["chave"], "cobranca")
        self.assertEqual(self.previsao(s["chave"])["comportamento"], "cobranca")
        self.assertNotIn("tipo", self.dados(s["chave"]))

    def test_previa_da_conversao_nao_grava(self):
        s = self.netflix()
        rec.salvar_previsao(s["chave"])
        antes = self.dados(s["chave"])
        previa = rec.previa_conversao(s["chave"], "reserva")
        self.assertEqual(self.dados(s["chave"]), antes, "prévia é leitura, não escrita")
        self.assertEqual(previa["mes"], "2026-09")
        self.assertIn("diferenca", previa)

    def test_comportamento_invalido_e_recusado(self):
        s = self.netflix()
        rec.salvar_previsao(s["chave"])
        with self.assertRaises(ValueError):
            rec.converter_previsao(s["chave"], "qualquer")

    # ------------------------------------------------------------ modoValor
    def test_media_vale_tambem_para_cobranca_unica(self):
        """Conta de luz varia demais para 'último valor' e é cobrança única."""
        rec.criar_previsao({
            "descricao": "Energia", "lojista": "companhia de energia",
            "contaId": "conta-b", "valorPrevisto": 180, "modoValor": "media",
            "diaTipico": 12})
        previsao = next(p for p in rec.sugestoes_payload()["previsoes"]
                        if p["lojista"] == "companhia de energia")
        self.assertEqual(previsao["modoValor"], "media")
        self.assertEqual(previsao["comportamento"], "cobranca")


if __name__ == "__main__":
    unittest.main(verbosity=2)
