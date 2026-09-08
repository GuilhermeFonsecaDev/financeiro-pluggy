"""Regressões da identificação de fundos; SQLite temporário e downloads simulados.

Execute: python -m unittest testes_fundos -v

Os catálogos reais (BTG e CVM) só são exercitados em testes_fundos_rede.py,
que depende de internet. Aqui os arquivos são fabricados, com um caso central:
o CNPJ da classe não é o que o BTG lista, e mesmo assim o link tem que sair.
"""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import banco
import fundos

CATALOGO = [
    {"CNPJ": "36181846000112", "Name": "A1 Hedge FICFIM RL", "NameOriginal": "A1 HEDGE",
     "Detail": {"categoryBTG": "Multimercado", "subcategoryBTG": None, "riskName": "MODERADO",
                "investmentType": "Investidores em geral", "minimumInitialInvestment": 5000,
                "numeroDiaFinanceiroResgate": 31}},
    {"CNPJ": "63446494000152", "Name": "Zeno Global USD F FIA IE RL", "NameOriginal": "ZENO",
     "Detail": {"categoryBTG": "Renda Variável", "riskName": "SOFISTICADO",
                "investmentType": "Investidor qualificado", "minimumInitialInvestment": 1000,
                "numeroDiaFinanceiroResgate": 4}},
]

CAD_FI = (
    "TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;SIT;CLASSE;CLASSE_ANBIMA;GESTOR;ADMIN;CD_CVM\n"
    "FI;36.181.846/0001-12;A1 HEDGE FUNDO DE INVESTIMENTO MULTIMERCADO;EM FUNCIONAMENTO NORMAL;"
    "Fundo Multimercado;Multimercados Livre;A1 GESTORA;BTG ADMINISTRADORA;123\n"
    "FI;11.111.111/0001-11;FUNDO FORA DO BTG MULTIMERCADO;EM FUNCIONAMENTO NORMAL;"
    "Fundo Multimercado;Multimercados Livre;OUTRA GESTORA;OUTRO ADMIN;456\n"
)

REGISTRO_FUNDO = (
    "ID_Registro_Fundo;CNPJ_Fundo;Codigo_CVM;Tipo_Fundo;Denominacao_Social;Situacao;"
    "Administrador;Gestor\n"
    "900;22.222.222/0001-22;789;FIF;ZENO GLOBAL FUNDO DE INVESTIMENTO FINANCEIRO;"
    "EM FUNCIONAMENTO NORMAL;BTG ADMINISTRADORA;ZENO GESTORA\n"
)

REGISTRO_CLASSE = (
    "ID_Registro_Fundo;ID_Registro_Classe;CNPJ_Classe;Codigo_CVM;Denominacao_Social;Situacao;"
    "Classificacao;Classificacao_Anbima\n"
    "900;901;63.446.494/0001-52;790;ZENO GLOBAL USD CLASSE DE COTAS;EM FUNCIONAMENTO NORMAL;"
    "Ações;Ações Investimento no Exterior\n"
    "900;902;44.444.444/0001-44;791;ZENO GLOBAL BRL CLASSE DE COTAS;EM FUNCIONAMENTO NORMAL;"
    "Ações;Ações Investimento no Exterior\n"
)


def _zip_cvm() -> bytes:
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w") as pacote:
        pacote.writestr("registro_fundo.csv", REGISTRO_FUNDO.encode("latin-1"))
        pacote.writestr("registro_classe.csv", REGISTRO_CLASSE.encode("latin-1"))
        pacote.writestr("registro_subclasse.csv", b"ID_Registro_Classe\n")
    return memoria.getvalue()


class SlugTests(unittest.TestCase):
    """Pares nome -> slug lidos dos links da listagem real do BTG.

    A regra do slug é o que sustenta o link direto; se ela mudar aqui dentro,
    todo link passa a apontar para uma página inexistente sem erro visível.
    """

    PARES = {
        "A1 D30 FICFIRF LP CrPr RL": "a1-d30-ficfirf-lp-crpr-rl",
        "A1 FICFIRF LP RL": "a1-ficfirf-lp-rl",
        "A1 Hedge FICFIM RL": "a1-hedge-ficfim-rl",
        "A1 Incentivado Infra FICFIRF CrPr RL": "a1-incentivado-infra-ficfirf-crpr-rl",
        "Absolute Alpha Global FICFIM RL": "absolute-alpha-global-ficfim-rl",
        "Absolute Alpha Marb FICFIM RL": "absolute-alpha-marb-ficfim-rl",
        "Absolute Atenas P FICFIRF CrPr RL": "absolute-atenas-p-ficfirf-crpr-rl",
        "XP Deb. Inc. CDI Infra FICFIRF Access CrPr": "xp-deb-inc-cdi-infra-ficfirf-access-crpr",
        "Zeno Global USD F FIA IE RL": "zeno-global-usd-f-fia-ie-rl",
    }

    def test_slug_reproduz_os_links_do_catalogo(self):
        for nome, slug in self.PARES.items():
            self.assertEqual(fundos.slugificar(nome), slug, nome)

    def test_acento_e_pontuacao_nao_vazam_para_a_url(self):
        self.assertEqual(fundos.slugificar("Ações & Renda Variável (BRL)"), "acoes-renda-variavel-brl")


class FundosTests(unittest.TestCase):
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
        fundos._sincronizando.clear()
        self.addCleanup(fundos._sincronizando.clear)

        self.baixados = []
        troca_rede = patch.object(fundos, "_baixar", side_effect=self.baixar)
        troca_rede.start()
        self.addCleanup(troca_rede.stop)

    def baixar(self, url: str) -> bytes:
        self.baixados.append(url)
        if url.startswith(fundos.CAD_FI_CVM):
            return CAD_FI.encode("latin-1")
        if url.startswith(fundos.REGISTRO_CVM):
            return _zip_cvm()
        pagina = int(url.split("page=")[1].split("&")[0])
        if pagina > 1:
            return json.dumps({"items": [], "total": len(CATALOGO), "total_pages": 1}).encode()
        return json.dumps({"items": CATALOGO, "total": len(CATALOGO),
                           "total_pages": 1, "page": 1}).encode()

    # ------------------------------------------------------------- cadastro

    def test_sincronizacao_guarda_catalogo_e_cadastro(self):
        self.assertEqual(fundos.sincronizar_btg(), 2)
        self.assertEqual(fundos.sincronizar_cvm(), 5)   # 2 do cad_fi + 1 fundo + 2 classes
        with banco.connect() as conn:
            linha = conn.execute("SELECT * FROM fundos_btg WHERE cnpj='36181846000112'").fetchone()
            self.assertEqual(linha["slug"], "a1-hedge-ficfim-rl")
            self.assertEqual(linha["aplicacao_minima"], 5000)
            classe = conn.execute("SELECT * FROM fundos_cvm WHERE cnpj='63446494000152'").fetchone()
            self.assertEqual(classe["tipo"], "classe")
            # Gestor e administrador moram no fundo, e a classe herda os dois.
            self.assertEqual(classe["gestor"], "ZENO GESTORA")

    def test_acento_do_arquivo_da_cvm_sobrevive(self):
        fundos.sincronizar_cvm()
        with banco.connect() as conn:
            linha = conn.execute("SELECT * FROM fundos_cvm WHERE cnpj='63446494000152'").fetchone()
        self.assertEqual(linha["classificacao"], "Ações")

    def test_fundo_que_saiu_do_catalogo_nao_fica_para_tras(self):
        fundos.sincronizar_btg()
        with patch.object(fundos, "_itens_btg", return_value=CATALOGO[:1]):
            fundos.sincronizar_btg()
        with banco.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM fundos_btg WHERE cnpj='63446494000152'").fetchone())

    # -------------------------------------------------------------- consulta

    def test_cnpj_do_catalogo_vira_link_direto(self):
        dados = fundos.payload("36.181.846/0001-12")
        resultado = dados["resultados"][0]
        self.assertEqual(resultado["btg"]["como"], "cnpj")
        self.assertEqual(resultado["btg"]["url"],
                         "https://investimentos.btgpactual.com/fundos-de-investimento/a1-hedge-ficfim-rl")
        self.assertEqual(resultado["cvm"]["denominacao"],
                         "A1 HEDGE FUNDO DE INVESTIMENTO MULTIMERCADO")
        self.assertFalse(resultado["cnpjInvalido"])
        self.assertEqual(resultado["aviso"], "")

    def test_cnpj_do_fundo_acha_a_classe_que_o_btg_lista(self):
        """O caso que motivou a ferramenta: CNPJ do fundo, catálogo com a classe."""
        dados = fundos.payload("22.222.222/0001-22")
        resultado = dados["resultados"][0]
        self.assertEqual(resultado["cvm"]["tipo"], "fundo")
        self.assertEqual(resultado["btg"]["como"], "classe")
        self.assertEqual(resultado["btg"]["cnpj"], "63446494000152")
        self.assertIn("classe", resultado["aviso"])
        self.assertEqual(resultado["btg"]["parente"]["tipo"], "classe")

    def test_classe_irma_fora_do_btg_nao_inventa_link(self):
        dados = fundos.payload("44.444.444/0001-44")
        resultado = dados["resultados"][0]
        # A irmã (USD) está no BTG, mas com outro CNPJ: vale como alternativa
        # explícita, nunca como se fosse a classe pedida.
        self.assertEqual(resultado["btg"]["cnpj"], "63446494000152")
        self.assertEqual(resultado["btg"]["como"], "classe")
        self.assertNotEqual(resultado["cnpj"], resultado["btg"]["cnpj"])

    def test_fundo_fora_do_catalogo_explica_em_vez_de_falhar(self):
        dados = fundos.payload("11.111.111/0001-11")
        resultado = dados["resultados"][0]
        self.assertIsNone(resultado["btg"])
        self.assertEqual(resultado["cvm"]["gestor"], "OUTRA GESTORA")
        self.assertIn("catálogo público do BTG", resultado["aviso"])

    def test_cadastro_encerrado_na_cvm_e_sinalizado(self):
        # Sincroniza antes de mexer no cadastro: a consulta baixaria de novo
        # e desfaria a situação alterada aqui.
        fundos.payload("11.111.111/0001-11")
        with banco.connect() as conn:
            conn.execute("UPDATE fundos_cvm SET situacao='Cancelado' WHERE cnpj='11111111000111'")
            conn.commit()
        resultado = fundos.payload("11.111.111/0001-11")["resultados"][0]
        self.assertEqual(resultado["situacaoAtencao"], "Cancelado")
        em_ordem = fundos.payload("36.181.846/0001-12")["resultados"][0]
        self.assertNotIn("situacaoAtencao", em_ordem)

    def test_cnpj_com_digito_errado_e_apontado(self):
        dados = fundos.payload("36.181.846/0001-13")
        resultado = dados["resultados"][0]
        self.assertTrue(resultado["cnpjInvalido"])
        self.assertIsNone(resultado["btg"])

    def test_varios_cnpjs_no_texto_colado(self):
        dados = fundos.payload("""Indicações da semana:
        - A1 Hedge, 36.181.846/0001-12, aporte inicial
        - Zeno, 63446494000152
        - fora da plataforma: 11.111.111/0001-11""")
        self.assertEqual([r["cnpj"] for r in dados["resultados"]],
                         ["36181846000112", "63446494000152", "11111111000111"])
        self.assertEqual([bool(r["btg"]) for r in dados["resultados"]], [True, True, False])

    def test_busca_por_nome_quando_nao_veio_cnpj(self):
        dados = fundos.payload("zeno global")
        resultado = dados["resultados"][0]
        self.assertEqual(resultado["tipoEntrada"], "nome")
        self.assertEqual(resultado["btg"]["nome"], "Zeno Global USD F FIA IE RL")

    def test_nome_sem_acerto_no_btg_sugere_cadastro_da_cvm(self):
        dados = fundos.payload("fundo fora do btg")
        resultado = dados["resultados"][0]
        self.assertIsNone(resultado["btg"])
        self.assertEqual([p["cnpjFormatado"] for p in resultado["parecidosCvm"]],
                         ["11.111.111/0001-11"])

    # ------------------------------------------------------------- cache

    def test_consulta_sincroniza_uma_vez_e_reaproveita(self):
        fundos.payload("36.181.846/0001-12")
        baixados = len(self.baixados)
        self.assertGreaterEqual(baixados, 3)          # catálogo + cad_fi + zip
        fundos.payload("63446494000152")
        self.assertEqual(len(self.baixados), baixados)
        estado = fundos.payload("")["fontes"]
        self.assertFalse(estado["btg"]["vencido"])
        self.assertEqual(estado["btg"]["total"], 2)

    def test_cadastro_vencido_e_baixado_de_novo(self):
        fundos.payload("36.181.846/0001-12")
        baixados = len(self.baixados)
        with banco.connect() as conn:
            conn.execute("UPDATE app_meta SET valor='2020-01-01T00:00:00' "
                         "WHERE chave IN ('fundos_btg_em','fundos_cvm_em')")
            conn.commit()
        fundos.payload("36.181.846/0001-12")
        self.assertGreater(len(self.baixados), baixados)

    def test_catalogo_vazio_nao_apaga_a_copia_boa(self):
        fundos.sincronizar_btg()
        with patch.object(fundos, "_itens_btg", return_value=[]):
            resultado = fundos.sincronizar(["btg"])
        self.assertIn("erro", resultado["btg"])
        with banco.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM fundos_btg").fetchone()["n"], 2)

    def test_falha_de_rede_vira_aviso_e_nao_excecao(self):
        with patch.object(fundos, "_baixar", side_effect=OSError("sem rede")):
            resultado = fundos.atualizar_payload(["btg"])
        self.assertFalse(resultado["ok"])
        self.assertIn("sem rede", resultado["resultado"]["btg"]["erro"])

    def test_fonte_desconhecida_e_recusada(self):
        with self.assertRaises(ValueError):
            fundos.sincronizar(["itau"])


if __name__ == "__main__":
    unittest.main()
