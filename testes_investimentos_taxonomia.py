"""Regressões da taxonomia de classes de investimento.

Execute: python testes_investimentos_taxonomia.py

O teste que importa é o do tipo desconhecido: ele fixa a promessa de que uma
classe nova entregue pela instituição aparece sozinha, sem ninguém escrever
código para ela.
"""

import sqlite3
import unittest

import investimentos_taxonomia as tax


class ClassificarTests(unittest.TestCase):
    def test_tipos_conhecidos_caem_na_classe_certa(self):
        casos = {
            ("FIXED_INCOME", "CDB"): ("renda_fixa", "Renda fixa", "CDB"),
            ("MUTUAL_FUND", "STOCK_FUND"): ("fundos", "Fundos", "Fundo de ações"),
            ("EQUITY", "STOCK"): ("acoes", "Ações", "Ação"),
            ("COE", ""): ("estruturados", "Estruturados", ""),
            ("PENSION", ""): ("previdencia", "Previdência", ""),
        }
        for (tipo, subtipo), (classe, rotulo, rotulo_sub) in casos.items():
            info = tax.classificar(tipo, subtipo)
            self.assertEqual(info["classe"], classe, tipo)
            self.assertEqual(info["rotulo"], rotulo, tipo)
            self.assertEqual(info["rotuloSubtipo"], rotulo_sub, tipo)
            self.assertTrue(info["conhecida"], tipo)

    def test_subtipo_pode_mudar_a_classe(self):
        """FII é fundo no papel, mas se lê junto da renda variável."""
        self.assertEqual(tax.classificar("MUTUAL_FUND", "REAL_ESTATE_FUND")["classe"], "acoes")
        self.assertEqual(tax.classificar("MUTUAL_FUND", "MULTIMARKET_FUND")["classe"], "fundos")

    def test_subtipo_desconhecido_nao_muda_a_classe(self):
        info = tax.classificar("FIXED_INCOME", "ALGO_NOVO")
        self.assertEqual(info["classe"], "renda_fixa")
        self.assertTrue(info["conhecida"])
        self.assertEqual(info["rotuloSubtipo"], "Algo Novo")

    def test_tipo_desconhecido_vira_classe_propria(self):
        """A promessa do módulo: dinheiro novo não some dentro de "Outros"."""
        info = tax.classificar("CRYPTO_STAKING", "")
        self.assertEqual(info["classe"], "outro:CRYPTO_STAKING")
        self.assertEqual(info["rotulo"], "Crypto Staking")
        self.assertEqual(info["perfil"], "generico")
        self.assertFalse(info["conhecida"])
        self.assertEqual(info["ordem"], tax.ORDEM_DESCONHECIDA)

    def test_sem_tipo_cai_em_outros(self):
        info = tax.classificar("", "")
        self.assertEqual(info["classe"], "outros")
        self.assertEqual(info["rotulo"], "Outros")

    def test_classe_manual_vence_a_deducao_por_tipo(self):
        info = tax.classificar("FIXED_INCOME", "CDB", classe_manual="imoveis")
        self.assertEqual(info["classe"], "imoveis")
        self.assertEqual(info["rotulo"], "Imóveis")

    def test_classe_manual_inexistente_nao_sequestra_a_classificacao(self):
        info = tax.classificar("FIXED_INCOME", "CDB", classe_manual="inventada")
        self.assertEqual(info["classe"], "renda_fixa")

    def test_humanizar_nao_inventa_traducao(self):
        self.assertEqual(tax.humanizar("REAL_ESTATE"), "Real Estate")
        self.assertEqual(tax.humanizar("crypto"), "Crypto")
        self.assertEqual(tax.humanizar(""), "Outros")


class ColunasTests(unittest.TestCase):
    def test_todo_perfil_tem_coluna_principal_e_saldo(self):
        for perfil in tax.PERFIS:
            colunas = tax.colunas(perfil)
            self.assertTrue(any(c.get("principal") for c in colunas), perfil)
            self.assertIn("liquido", [c["id"] for c in colunas], perfil)

    def test_vocabulario_de_formato_e_fechado(self):
        """Teto de cinco verbos: precisão e alinhamento são atributos."""
        permitidos = {"moeda", "percentual", "data", "numero", "texto"}
        for perfil in tax.PERFIS:
            for coluna in tax.colunas(perfil) + tax.destaques(perfil):
                self.assertIn(coluna["formato"], permitidos, f"{perfil}/{coluna['id']}")

    def test_numero_alinha_a_direita(self):
        for coluna in tax.colunas("renda_fixa"):
            if coluna["formato"] in ("moeda", "percentual", "numero"):
                self.assertEqual(coluna.get("alinhamento"), "direita", coluna["id"])

    def test_perfil_desconhecido_cai_no_generico(self):
        self.assertEqual(tax.colunas("nao_existe"), tax.colunas("generico"))

    def test_colunas_sao_copias(self):
        """Quem recebe a lista não pode alterar a definição global."""
        primeira = tax.colunas("fundos")
        primeira[0]["rotulo"] = "alterado"
        self.assertNotEqual(tax.colunas("fundos")[0]["rotulo"], "alterado")


class ClassesPresentesTests(unittest.TestCase):
    def itens(self, *pares):
        return [{"tipo": t, "subtipo": s} for t, s in pares]

    def test_so_entra_classe_que_tem_posicao(self):
        classes = tax.classes_presentes(self.itens(("FIXED_INCOME", "CDB")))
        self.assertEqual([c["id"] for c in classes], ["renda_fixa"])
        self.assertEqual(classes[0]["posicoes"], 1)

    def test_ordem_canonica_e_contagem(self):
        classes = tax.classes_presentes(self.itens(
            ("MUTUAL_FUND", "STOCK_FUND"), ("FIXED_INCOME", "CDB"),
            ("FIXED_INCOME", "LCI"), ("EQUITY", "STOCK")))
        self.assertEqual([c["id"] for c in classes], ["renda_fixa", "fundos", "acoes"])
        self.assertEqual([c["posicoes"] for c in classes], [2, 1, 1])

    def test_classes_desconhecidas_ficam_no_fim_em_ordem_estavel(self):
        itens = self.itens(("ZZZ_NOVO", ""), ("AAA_NOVO", ""), ("FIXED_INCOME", "CDB"))
        ids = [c["id"] for c in tax.classes_presentes(itens)]
        self.assertEqual(ids[0], "renda_fixa")
        self.assertEqual(ids[1:], ["outro:AAA_NOVO", "outro:ZZZ_NOVO"])
        # a ordem não pode depender da ordem de chegada
        self.assertEqual(ids, [c["id"] for c in tax.classes_presentes(list(reversed(itens)))])

    def test_lancamento_manual_usa_a_classe_escolhida(self):
        itens = [{"tipo": "", "subtipo": "", "classe": "imoveis", "origem": "manual"}]
        classes = tax.classes_presentes(itens)
        self.assertEqual([c["id"] for c in classes], ["imoveis"])

    def test_renome_do_usuario_troca_so_o_rotulo(self):
        classes = tax.classes_presentes(self.itens(("CRYPTO_STAKING", "")),
                                        rotulos={"outro:CRYPTO_STAKING": "Staking"})
        self.assertEqual(classes[0]["id"], "outro:CRYPTO_STAKING")
        self.assertEqual(classes[0]["rotulo"], "Staking")
        self.assertFalse(classes[0]["conhecida"])

    def test_sem_itens_nao_ha_aba(self):
        self.assertEqual(tax.classes_presentes([]), [])


class RotulosPersonalizadosTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE app_meta (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
        self.addCleanup(self.conn.close)

    def gravar(self, valor):
        self.conn.execute("INSERT OR REPLACE INTO app_meta VALUES (?,?)", (tax.CHAVE_ROTULOS, valor))

    def test_sem_registro_nao_quebra(self):
        self.assertEqual(tax.rotulos_personalizados(self.conn), {})

    def test_le_o_que_o_usuario_gravou(self):
        self.gravar('{"outro:CRYPTO": "Cripto"}')
        self.assertEqual(tax.rotulos_personalizados(self.conn), {"outro:CRYPTO": "Cripto"})

    def test_banco_sem_app_meta_nao_derruba_a_tela(self):
        vazio = sqlite3.connect(":memory:")
        self.addCleanup(vazio.close)
        self.assertEqual(tax.rotulos_personalizados(vazio), {})

    def test_json_invalido_nao_derruba_a_tela(self):
        self.gravar("{quebrado")
        self.assertEqual(tax.rotulos_personalizados(self.conn), {})
        self.gravar('["lista"]')
        self.assertEqual(tax.rotulos_personalizados(self.conn), {})


if __name__ == "__main__":
    unittest.main()
