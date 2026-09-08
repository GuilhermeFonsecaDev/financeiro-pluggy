"""Contrato com as fontes reais: catálogo do BTG e cadastro da CVM.

Execute: python testes_fundos_rede.py

Depende de internet e fica fora da suíte rápida, mas é o único teste que pega
o que mais pode quebrar sem aviso: o BTG mudar o formato da API ou o limite de
página, e a CVM mudar as colunas. Se algo aqui falhar, `fundos.py` para de
alimentar o catálogo local -- e, sem catálogo, não há link para abrir.

Nada daqui grava no banco: só leitura das fontes públicas.
"""

import json
import unittest
import urllib.error
import urllib.request

import fundos


def _abrir(url: str):
    pedido = urllib.request.Request(url, headers={"User-Agent": "FinanceiroPluggy/1.0"})
    return urllib.request.urlopen(pedido, timeout=fundos.TIMEOUT_SEGUNDOS)


class ContratoBtgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            bruto = fundos._baixar(fundos.CATALOGO_BTG.format(pagina=1))
        except (urllib.error.URLError, OSError) as erro:
            raise unittest.SkipTest(f"sem acesso ao catálogo do BTG: {erro}")
        cls.dados = json.loads(bruto.decode("utf-8"))

    def test_resposta_tem_os_campos_que_usamos(self):
        self.assertGreater(self.dados["total"], 100)
        item = self.dados["items"][0]
        self.assertRegex(item["CNPJ"], r"^\d{14}$")
        self.assertTrue(item["Name"].strip())
        self.assertIn("Detail", item)

    def test_limite_de_pagina_continua_em_cem(self):
        """Se o teto subir, `_itens_btg` só fica mais lento; se cair, ele quebra."""
        with self.assertRaises(urllib.error.HTTPError) as caso:
            _abrir(fundos.CATALOGO_BTG.format(pagina=1).replace("size=100", "size=101"))
        self.assertEqual(caso.exception.code, 422)

    def test_acentos_vem_em_utf8(self):
        texto = json.dumps(self.dados, ensure_ascii=False)
        self.assertNotIn("�", texto)
        self.assertNotIn("Ã§", texto)

    def test_nome_do_catalogo_produz_slug_utilizavel(self):
        """A página só se resolve por JavaScript: o HTML servido é o mesmo
        para slug válido e inválido, e nem `<title>` vem preenchido. Dá para
        conferir por HTTP apenas que o nome vira um slug bem formado; que ele
        abre o detalhe certo foi verificado em navegador e está fixado nos
        pares reais de testes_fundos.SlugTests."""
        for item in self.dados["items"][:20]:
            slug = fundos.slugificar(item["Name"])
            self.assertRegex(slug, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertNotIn("--", slug)


class ContratoCvmTests(unittest.TestCase):
    def test_cad_fi_tem_as_colunas_usadas(self):
        try:
            with _abrir(fundos.CAD_FI_CVM) as resposta:
                cabecalho = resposta.readline().decode("latin-1")
        except (urllib.error.URLError, OSError) as erro:
            raise unittest.SkipTest(f"sem acesso ao cadastro da CVM: {erro}")
        colunas = set(cabecalho.strip().split(";"))
        for coluna in ("CNPJ_FUNDO", "DENOM_SOCIAL", "SIT", "CLASSE",
                       "CLASSE_ANBIMA", "GESTOR", "ADMIN", "CD_CVM"):
            self.assertIn(coluna, colunas)

    def test_registro_rcvm175_tem_a_ponte_fundo_classe(self):
        import io
        import zipfile
        try:
            pacote = zipfile.ZipFile(io.BytesIO(fundos._baixar(fundos.REGISTRO_CVM)))
        except (urllib.error.URLError, OSError) as erro:
            raise unittest.SkipTest(f"sem acesso ao registro da CVM: {erro}")
        with pacote:
            nomes = pacote.namelist()
            self.assertIn("registro_fundo.csv", nomes)
            self.assertIn("registro_classe.csv", nomes)
            with pacote.open("registro_classe.csv") as arquivo:
                colunas = set(arquivo.readline().decode("latin-1").strip().split(";"))
        # É esta coluna que liga a classe ao fundo, e a ponte depende dela.
        for coluna in ("ID_Registro_Fundo", "CNPJ_Classe", "Denominacao_Social", "Situacao"):
            self.assertIn(coluna, colunas)


if __name__ == "__main__":
    unittest.main()
