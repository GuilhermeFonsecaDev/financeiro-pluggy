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

    def test_reserva_sobra_o_que_ainda_nao_foi_gasto(self):
        rec.criar_previsao({
            "descricao": "Mercado", "lojista": "mercado do bairro",
            "contaId": "conta-b", "valorPrevisto": 400, "modoValor": "fixo",
            "diaTipico": 10, "comportamento": "reserva"})
        chave = next(p["chave"] for p in rec.sugestoes_payload()["previsoes"]
                     if p["lojista"] == "mercado do bairro")
        antes = [i for i in self.projetado(chave) if i["mes"] == 9]
        self.assertEqual(len(antes), 1)
        self.assertEqual(antes[0]["valor"], 400)

        self.gastar("mercado-set", 2, "MERCADO DO BAIRRO", 150)
        depois = [i for i in self.projetado(chave) if i["mes"] == 9]
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

    def test_testar_conta_os_lancamentos_antes_de_salvar(self):
        for mes in (6, 7, 8):
            self.inserir(f"shell-{mes}", mes, "POSTO SHELL", 70, conta="conta-b")
        resultado = rec.testar_previsao({"contaId": "conta-b", "termos": ["posto"]})
        self.assertEqual(resultado["lancamentos"], 3)
        self.assertEqual(resultado["porCriterio"], {"termo": 3})
        self.assertEqual(resultado["mediaCiclos"], 70)
        self.assertTrue(resultado["exemplos"])

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
