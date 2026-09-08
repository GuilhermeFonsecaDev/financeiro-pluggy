"""Regressões da carteira-alvo e da distribuição de aportes.

Execute: python -m unittest testes_carteira -v

O cuidado central é a conta do aporte: a soma das fatias tem que bater com o
valor informado até o centavo, inclusive quando os percentuais não fecham em
100% ou geram divisão inexata.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import banco
import carteira
import fundos

# Dois fundos do catálogo do BTG e uma classe que responde pelo CNPJ do fundo,
# para conferir que a carteira herda a mesma ponte da tela de identificação.
CATALOGO = [
    {"CNPJ": "36181846000112", "Name": "A1 Hedge FICFIM RL", "NameOriginal": "A1 HEDGE",
     "Detail": {"categoryBTG": "Multimercado", "riskName": "MODERADO",
                "investmentType": "Investidores em geral (Não qualificados)",
                "minimumInitialInvestment": 5000, "numeroDiaFinanceiroResgate": 31}},
    {"CNPJ": "63446494000152", "Name": "Zeno Global USD F FIA IE RL", "NameOriginal": "ZENO",
     "Detail": {"categoryBTG": "Renda Variável", "riskName": "SOFISTICADO",
                "investmentType": "Investidor qualificado",
                "minimumInitialInvestment": 100, "numeroDiaFinanceiroResgate": 4}},
]

CAD_FI = (
    "TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;SIT;CLASSE;CLASSE_ANBIMA;GESTOR;ADMIN;CD_CVM\n"
    "FI;36.181.846/0001-12;A1 HEDGE FUNDO DE INVESTIMENTO MULTIMERCADO;EM FUNCIONAMENTO NORMAL;"
    "Fundo Multimercado;Multimercados Livre;A1 GESTORA;BTG ADMIN;123\n"
    "FI;11.111.111/0001-11;FIDC FORA DO BTG;EM FUNCIONAMENTO NORMAL;"
    "FIDC;FIDC Outros;OUTRA GESTORA;OUTRO ADMIN;456\n"
)

REGISTRO_FUNDO = (
    "ID_Registro_Fundo;CNPJ_Fundo;Codigo_CVM;Tipo_Fundo;Denominacao_Social;Situacao;"
    "Administrador;Gestor\n"
    "900;22.222.222/0001-22;789;FIF;ZENO GLOBAL FUNDO DE INVESTIMENTO FINANCEIRO;"
    "EM FUNCIONAMENTO NORMAL;BTG ADMIN;ZENO GESTORA\n"
)

REGISTRO_CLASSE = (
    "ID_Registro_Fundo;ID_Registro_Classe;CNPJ_Classe;Codigo_CVM;Denominacao_Social;Situacao;"
    "Classificacao;Classificacao_Anbima\n"
    "900;901;63.446.494/0001-52;790;ZENO GLOBAL USD CLASSE DE COTAS;EM FUNCIONAMENTO NORMAL;"
    "Ações;Ações Investimento no Exterior\n"
)


def _zip_cvm() -> bytes:
    import io
    import zipfile
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as pacote:
        pacote.writestr("registro_fundo.csv", REGISTRO_FUNDO.encode("latin-1"))
        pacote.writestr("registro_classe.csv", REGISTRO_CLASSE.encode("latin-1"))
    return memoria.getvalue()


class DistribuicaoTests(unittest.TestCase):
    """A conta pura, sem banco: é onde um centavo se perde sem ninguém ver."""

    def distribuir(self, percentuais, aporte, iq=()):
        itens = [{"percentual": p, "ordem": i,
                  "qualificado": "sim" if i in iq else "nao"}
                 for i, p in enumerate(percentuais)]
        carteira._distribuir(itens, aporte)
        return [item["aporte"] for item in itens]

    def test_soma_bate_com_o_aporte(self):
        fatias = self.distribuir([10, 20, 20, 20, 30], 1000)
        self.assertEqual(fatias, [100, 200, 200, 200, 300])
        self.assertEqual(round(sum(fatias), 2), 1000)

    def test_divisao_inexata_nao_perde_nem_cria_centavo(self):
        for aporte in (100, 333.33, 1000.01, 7, 0.03):
            for percentuais in ([1, 1, 1], [33.33, 33.33, 33.34], [10, 20, 20, 20, 30]):
                fatias = self.distribuir(percentuais, aporte)
                self.assertEqual(round(sum(fatias), 2), round(aporte, 2),
                                 f"{aporte} em {percentuais}")
                self.assertTrue(all(round(f, 2) == f for f in fatias))

    def test_sobra_de_centavos_vai_para_as_maiores_fatias(self):
        # 0,01 em três partes iguais: um centavo para a primeira, na ordem.
        self.assertEqual(self.distribuir([1, 1, 1], 0.01), [0.01, 0, 0])
        # 10/20/70 com resto: o centavo sobra para a maior alocação.
        fatias = self.distribuir([10, 20, 70], 0.01)
        self.assertEqual(fatias, [0, 0, 0.01])

    def test_percentuais_que_nao_fecham_ainda_distribuem_o_aporte_inteiro(self):
        # 90% no total: o aporte informado é repartido na proporção informada,
        # e a tela avisa que a soma não fecha.
        fatias = self.distribuir([30, 30, 30], 900)
        self.assertEqual(fatias, [300, 300, 300])
        fatias = self.distribuir([50, 60], 1100)
        self.assertEqual(round(sum(fatias), 2), 1100)

    def test_aporte_zero_ou_carteira_vazia(self):
        self.assertEqual(self.distribuir([50, 50], 0), [0, 0])
        self.assertEqual(self.distribuir([], 1000), [])
        self.assertEqual(self.distribuir([0, 0], 1000), [0, 0])

    def test_aporte_negativo_nao_vira_valor_negativo(self):
        self.assertEqual(self.distribuir([50, 50], -100), [0, 0])

    def test_fundo_iq_sai_da_conta_e_a_fatia_dele_se_dilui(self):
        # 20% do fundo IQ repartidos entre 10/20/50, na proporção deles.
        fatias = self.distribuir([10, 20, 20, 50], 1000, iq={2})
        self.assertEqual(fatias[2], 0)
        self.assertEqual(round(sum(fatias), 2), 1000)
        self.assertEqual(fatias, [125, 250, 0, 625])

    def test_redistribuicao_de_iq_nao_perde_centavo(self):
        for aporte in (100, 333.33, 1000.01, 0.03, 7):
            fatias = self.distribuir([10, 20, 20, 20, 30], aporte, iq={0, 3})
            self.assertEqual(round(sum(fatias), 2), round(aporte, 2), aporte)
            self.assertEqual([fatias[0], fatias[3]], [0, 0], aporte)

    def test_centavo_de_resto_nao_cai_em_fundo_iq(self):
        fatias = self.distribuir([50, 50], 0.01, iq={0})
        self.assertEqual(fatias, [0, 0.01])

    def test_carteira_toda_iq_nao_distribui_nada(self):
        self.assertEqual(self.distribuir([50, 50], 1000, iq={0, 1}), [0, 0])


class DataResgateTests(unittest.TestCase):
    """D+N vira a data em que o dinheiro cai, resgatando hoje."""

    def data(self, dias, hoje=date(2026, 9, 8)):          # 08/09/2026 é terça
        with patch.object(carteira, "_hoje", return_value=hoje):
            return carteira._data_resgate(dias)

    def test_conta_dias_corridos_a_partir_de_hoje(self):
        self.assertEqual(self.data(0), "2026-09-08")
        self.assertEqual(self.data(1), "2026-09-09")
        self.assertEqual(self.data(31), "2026-10-09")
        self.assertEqual(self.data(91), "2026-12-08")

    def test_fim_de_semana_rola_para_a_segunda(self):
        # D+4 cai no sábado 12/09 e D+5 no domingo: os dois liquidam na segunda.
        self.assertEqual(self.data(4), "2026-09-14")
        self.assertEqual(self.data(5), "2026-09-14")

    def test_sem_liquidez_informada_nao_inventa_data(self):
        for valor in (None, "", "  ", "x", -3):
            self.assertEqual(self.data(valor), "", repr(valor))

    def test_data_acompanha_a_liquidez_de_cada_fundo(self):
        self.assertEqual(self.data(10), "2026-09-18")
        self.assertEqual(self.data(60), "2026-11-09")


class CarteiraTests(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        troca = patch.object(banco, "DATABASE_PATH", Path(pasta.name) / "teste.db")
        troca.start()
        self.addCleanup(troca.stop)
        conexoes = []
        conectar = banco.connect

        def conectar_teste():
            conn = conectar()
            conexoes.append(conn)
            return conn

        troca_conexao = patch.object(banco, "connect", side_effect=conectar_teste)
        troca_conexao.start()
        self.addCleanup(troca_conexao.stop)
        self.addCleanup(lambda: [conn.close() for conn in conexoes])
        banco.ensure_database()
        fundos.garantir_tabelas()
        carteira.garantir_tabelas()
        fundos._sincronizando.clear()
        self.addCleanup(fundos._sincronizando.clear)
        troca_rede = patch.object(fundos, "_baixar", side_effect=self.baixar)
        troca_rede.start()
        self.addCleanup(troca_rede.stop)
        fundos.sincronizar_btg()
        fundos.sincronizar_cvm()

    def baixar(self, url: str) -> bytes:
        import json
        if url.startswith(fundos.CAD_FI_CVM):
            return CAD_FI.encode("latin-1")
        if url.startswith(fundos.REGISTRO_CVM):
            return _zip_cvm()
        pagina = int(url.split("page=")[1].split("&")[0])
        itens = CATALOGO if pagina == 1 else []
        return json.dumps({"items": itens, "total": len(CATALOGO), "total_pages": 1}).encode()

    def liberar_iq(self, aporte=0):
        """Marca todos como não-IQ: sem isso, o fundo restrito sai da conta.

        Serve aos testes que falam de mínimo e de soma dos percentuais, para
        não medirem a redistribuição por IQ de carona.
        """
        itens = carteira.payload()["itens"]
        for item in itens:
            item["qualificado"] = "nao"
        return carteira.salvar(itens, aporte=aporte)

    # ------------------------------------------------------------- cadastro

    def test_cadastro_pede_cnpj_e_percentual_e_preenche_o_resto(self):
        dados = carteira.adicionar("36.181.846/0001-12", 40)
        item = dados["itens"][0]
        self.assertEqual(item["nome"], "A1 Hedge FICFIM RL")
        self.assertEqual(item["anbima"], "Multimercados Livre")
        self.assertEqual(item["aporteMinimo"], 5000)
        self.assertEqual(item["diasResgate"], 31)
        self.assertEqual(item["qualificado"], "nao")
        self.assertTrue(item["url"].endswith("/a1-hedge-ficfim-rl"))
        self.assertEqual(item["percentual"], 40)

    def test_cnpj_do_fundo_traz_o_link_da_classe_que_o_btg_lista(self):
        item = carteira.adicionar("22.222.222/0001-22", 50)["itens"][0]
        self.assertTrue(item["url"].endswith("/zeno-global-usd-f-fia-ie-rl"))
        self.assertEqual(item["urlComo"], "classe")

    def test_fundo_fora_do_catalogo_entra_com_o_nome_da_cvm_e_sem_link(self):
        item = carteira.adicionar("11.111.111/0001-11", 20)["itens"][0]
        self.assertEqual(item["nome"], "FIDC FORA DO BTG")
        self.assertEqual(item["anbima"], "FIDC Outros")
        self.assertEqual(item["url"], "")
        self.assertFalse(item["noCatalogo"])

    def test_cnpj_invalido_ou_repetido_e_recusado(self):
        carteira.adicionar("36.181.846/0001-12", 10)
        with self.assertRaises(ValueError):
            carteira.adicionar("36.181.846/0001-12", 10)     # repetido
        with self.assertRaises(ValueError):
            carteira.adicionar("36.181.846/0001-13", 10)     # dígito errado e desconhecido
        with self.assertRaises(ValueError):
            carteira.adicionar("123", 10)                    # incompleto

    def test_cnpj_do_catalogo_entra_mesmo_com_digito_estranho(self):
        """Dígito verificador é indício, não veredito: catálogo manda."""
        self.assertFalse(fundos.cnpj_valido("11111111000111"))
        item = carteira.adicionar("11.111.111/0001-11", 20)["itens"][0]
        self.assertEqual(item["nome"], "FIDC FORA DO BTG")

    def test_qualificado_vem_do_catalogo_sem_confundir_nao_qualificado(self):
        aberto = carteira.adicionar("36.181.846/0001-12", 50)["itens"][0]
        restrito = carteira.adicionar("63.446.494/0001-52", 50)["itens"][1]
        self.assertEqual(aberto["qualificado"], "nao")
        self.assertEqual(restrito["qualificado"], "sim")

    # -------------------------------------------------------------- edição

    def test_edicao_manual_vence_o_catalogo_e_persiste(self):
        carteira.adicionar("36.181.846/0001-12", 100)
        itens = carteira.payload()["itens"]
        itens[0].update({"aporteMinimo": 100, "aporteMinimoProprio": True,
                         "anbima": "Renda Fixa Duração Livre", "diasResgate": "31"})
        salvo = carteira.salvar(itens)["itens"][0]
        self.assertEqual(salvo["aporteMinimo"], 100)
        self.assertTrue(salvo["aporteMinimoProprio"])
        self.assertEqual(salvo["anbima"], "Renda Fixa Duração Livre")
        self.assertEqual(salvo["diasResgate"], 31)
        # Uma nova leitura mantém o valor editado, não o do catálogo (5000).
        self.assertEqual(carteira.payload()["itens"][0]["aporteMinimo"], 100)

    def test_colunas_de_texto_livre_saem_de_um_banco_antigo(self):
        """A tela deixou de mostrar volatilidade, taxas, corretoras e XP."""
        with banco.connect() as conn:
            for coluna in carteira.REMOVIDAS:
                conn.execute(f"ALTER TABLE carteira_alvo ADD COLUMN {coluna} TEXT "
                             "NOT NULL DEFAULT ''")
            conn.commit()
        carteira.garantir_tabelas()
        with banco.connect() as conn:
            colunas = {linha[1] for linha in conn.execute("PRAGMA table_info(carteira_alvo)")}
        self.assertFalse(colunas & set(carteira.REMOVIDAS))
        # E o cadastro continua funcionando depois da limpeza.
        self.assertEqual(carteira.adicionar("36.181.846/0001-12", 100)["somaPercentual"], 100)

    def test_apagar_o_minimo_editado_devolve_o_valor_do_catalogo(self):
        carteira.adicionar("36.181.846/0001-12", 100)
        itens = carteira.payload()["itens"]
        itens[0].update({"aporteMinimo": 100, "aporteMinimoProprio": True})
        carteira.salvar(itens)
        itens = carteira.payload()["itens"]
        itens[0].update({"aporteMinimo": None, "aporteMinimoProprio": False})
        self.assertEqual(carteira.salvar(itens)["itens"][0]["aporteMinimo"], 5000)

    def test_salvar_recusa_percentual_fora_da_faixa_e_cnpj_repetido(self):
        carteira.adicionar("36.181.846/0001-12", 50)
        itens = carteira.payload()["itens"]
        itens[0]["percentual"] = 140
        with self.assertRaises(ValueError):
            carteira.salvar(itens)
        itens[0]["percentual"] = 50
        with self.assertRaises(ValueError):
            carteira.salvar(itens + itens)
        # A recusa não deixou a tabela pela metade.
        self.assertEqual(len(carteira.payload()["itens"]), 1)

    def test_salvar_remove_quem_saiu_da_lista(self):
        carteira.adicionar("36.181.846/0001-12", 50)
        carteira.adicionar("63.446.494/0001-52", 50)
        itens = carteira.payload()["itens"]
        dados = carteira.salvar([itens[1]])
        self.assertEqual([i["cnpj"] for i in dados["itens"]], ["63446494000152"])

    def test_excluir_tira_da_carteira(self):
        carteira.adicionar("36.181.846/0001-12", 100)
        self.assertEqual(carteira.excluir("36.181.846/0001-12")["itens"], [])

    # --------------------------------------------------------------- aporte

    def test_aporte_distribuido_e_minimo_sinalizado(self):
        carteira.adicionar("36.181.846/0001-12", 50)   # mínimo 5.000
        carteira.adicionar("63.446.494/0001-52", 50)   # mínimo 100
        dados = self.liberar_iq(aporte=2000)
        a1, zeno = dados["itens"]
        self.assertEqual([a1["aporte"], zeno["aporte"]], [1000, 1000])
        self.assertTrue(a1["abaixoDoMinimo"])
        self.assertEqual(a1["falta"], 4000)
        self.assertFalse(zeno["abaixoDoMinimo"])
        self.assertEqual(dados["abaixoDoMinimo"], 1)
        self.assertEqual(dados["totalDistribuido"], 2000)

    def test_soma_dos_percentuais_e_reportada(self):
        carteira.adicionar("36.181.846/0001-12", 30)
        carteira.adicionar("63.446.494/0001-52", 30)
        dados = self.liberar_iq(aporte=1000)
        self.assertEqual(dados["somaPercentual"], 60)
        self.assertFalse(dados["somaFecha"])
        # Mesmo sem fechar 100%, o aporte informado sai inteiro.
        self.assertEqual(dados["totalDistribuido"], 1000)

    def test_carteira_de_cinco_fundos_fecha_em_cem(self):
        for cnpj, p in (("36.181.846/0001-12", 10), ("63.446.494/0001-52", 20),
                        ("11.111.111/0001-11", 20), ("22.222.222/0001-22", 20)):
            carteira.adicionar(cnpj, p)
        itens = carteira.payload()["itens"]
        itens[-1]["percentual"] = 50
        for item in itens:
            item["qualificado"] = "nao"
        dados = carteira.salvar(itens, aporte=3000)
        self.assertEqual(dados["somaPercentual"], 100)
        self.assertTrue(dados["somaFecha"])
        self.assertEqual([i["aporte"] for i in dados["itens"]], [300, 600, 600, 1500])

    def test_marcar_iq_redistribui_e_a_tela_sabe_explicar(self):
        carteira.adicionar("36.181.846/0001-12", 50)
        carteira.adicionar("11.111.111/0001-11", 50)
        itens = carteira.payload()["itens"]
        itens[1]["qualificado"] = "sim"
        dados = carteira.salvar(itens, aporte=1000)
        elegivel, restrito = dados["itens"]
        self.assertEqual(elegivel["aporte"], 1000)      # recebe a fatia do outro
        self.assertEqual(restrito["aporte"], 0)
        self.assertTrue(restrito["redistribuido"])
        self.assertFalse(restrito["elegivel"])
        self.assertEqual(dados["redistribuidos"], 1)
        self.assertEqual(dados["percentualRedistribuido"], 50)
        # A soma cadastrada continua 100%: o que muda é a base do cálculo.
        self.assertEqual(dados["somaPercentual"], 100)
        self.assertTrue(dados["somaFecha"])
        self.assertFalse(dados["semElegivel"])

    def test_iq_que_vem_do_catalogo_tambem_fica_de_fora(self):
        """Zeno é restrito no catálogo do BTG, sem ninguém marcar nada."""
        carteira.adicionar("36.181.846/0001-12", 50)
        carteira.adicionar("63.446.494/0001-52", 50)
        dados = carteira.payload(1000)
        self.assertEqual([i["aporte"] for i in dados["itens"]], [1000, 0])
        self.assertEqual(dados["redistribuidos"], 1)

    def test_carteira_toda_iq_avisa_em_vez_de_inventar_destino(self):
        carteira.adicionar("63.446.494/0001-52", 100)
        dados = carteira.payload(1000)
        self.assertTrue(dados["semElegivel"])
        self.assertEqual(dados["totalDistribuido"], 0)

    def test_fundo_iq_nao_e_acusado_de_estar_abaixo_do_minimo(self):
        carteira.adicionar("36.181.846/0001-12", 50)    # mínimo 5.000
        carteira.adicionar("63.446.494/0001-52", 50)    # IQ, mínimo 100
        dados = carteira.payload(1000)
        self.assertFalse(dados["itens"][1]["abaixoDoMinimo"])
        self.assertEqual(dados["abaixoDoMinimo"], 1)    # só o que recebeu

    def test_payload_traz_a_data_de_resgate_de_cada_fundo(self):
        carteira.adicionar("36.181.846/0001-12", 100)      # D+31 no catálogo
        with patch.object(carteira, "_hoje", return_value=date(2026, 9, 8)):
            item = carteira.payload()["itens"][0]
        self.assertEqual(item["diasResgate"], 31)
        self.assertEqual(item["dataResgate"], "2026-10-09")

    def test_liquidez_editada_muda_a_data(self):
        carteira.adicionar("36.181.846/0001-12", 100)
        itens = carteira.payload()["itens"]
        itens[0]["diasResgate"] = "1"
        with patch.object(carteira, "_hoje", return_value=date(2026, 9, 8)):
            item = carteira.salvar(itens)["itens"][0]
        self.assertEqual(item["dataResgate"], "2026-09-09")

    def test_aporte_nao_e_guardado_entre_consultas(self):
        carteira.adicionar("36.181.846/0001-12", 100)
        carteira.payload(5000)
        self.assertEqual(carteira.payload()["aporte"], 0)
        self.assertEqual(carteira.payload()["itens"][0]["aporte"], 0)

    def test_aporte_em_texto_do_teclado_e_aceito(self):
        carteira.adicionar("36.181.846/0001-12", 100)
        self.assertEqual(carteira.payload("1500.50")["totalDistribuido"], 1500.5)


if __name__ == "__main__":
    unittest.main()
