"""Regressões das séries do Banco Central (CDI, IPCA).

Execute: python testes_indices.py

Tudo offline: a API do BCB é simulada. O que os testes fixam é o
comportamento quando ela falha ou vem incompleta -- porque é aí que uma tela
de patrimônio começa a mentir.
"""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import banco
import indices


def resposta(pontos):
    """Formato do SGS: valor em texto, data em dd/MM/yyyy."""
    return json.dumps([{"data": d, "valor": v} for d, v in pontos]).encode("utf-8")


class ParsingTests(unittest.TestCase):
    def test_le_valor_em_texto_e_data_brasileira(self):
        self.assertEqual(indices._ponto({"data": "03/08/2026", "valor": "0.052531"}),
                         ("2026-08-03", 0.052531))

    def test_aceita_virgula_decimal(self):
        self.assertEqual(indices._ponto({"data": "03/08/2026", "valor": "0,052531"})[1],
                         0.052531)

    def test_ponto_malformado_e_descartado_sem_quebrar(self):
        for bruto in ({"data": "x", "valor": "1"}, {"data": "03/08/2026", "valor": "n/d"},
                      {"valor": "1"}, {"data": "03/08/2026"}, {}):
            self.assertIsNone(indices._ponto(bruto), bruto)

    def test_dia_util_anterior_pula_o_fim_de_semana(self):
        # 14/09/2026 é segunda: o dia útil anterior é a sexta, 11/09.
        self.assertEqual(indices.dia_util_anterior(date(2026, 9, 14)), date(2026, 9, 11))
        self.assertEqual(indices.dia_util_anterior(date(2026, 9, 10)), date(2026, 9, 9))


class BancoTests(unittest.TestCase):
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
        self.addCleanup(lambda: [c.close() for c in conexoes])
        banco.ensure_database()
        indices.garantir_tabelas()

    def gravar(self, serie, pontos):
        with banco.connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO indices_series VALUES (?,?,?)",
                             [(serie, d, v) for d, v in pontos])
            conn.commit()


class SincronizarTests(BancoTests):
    def test_grava_os_pontos_baixados(self):
        with patch.object(indices, "_baixar",
                          return_value=resposta([("03/08/2026", "0.05"), ("04/08/2026", "0.05")])):
            saida = indices.sincronizar(["12"])
        self.assertTrue(saida["ok"])
        with banco.connect() as conn:
            self.assertEqual(indices.ultima_data(conn, "12"), "2026-08-04")

    def test_coleta_incremental_pede_so_o_que_falta(self):
        self.gravar("12", [("2026-08-03", 0.05)])
        chamadas = []

        def espiao(url):
            chamadas.append(url)
            return resposta([("04/08/2026", "0.05")])

        with patch.object(indices, "_baixar", side_effect=espiao):
            indices.sincronizar(["12"])
        self.assertIn("dataInicial=04%2F08%2F2026".replace("%2F", "/"), chamadas[0])

    def test_serie_desconhecida_e_recusada_sem_rede(self):
        with patch.object(indices, "_baixar", side_effect=AssertionError("não deveria chamar")):
            saida = indices.sincronizar(["999"])
        self.assertIn("erro", saida["resultado"]["999"])

    def test_falha_de_rede_vira_motivo_e_nao_excecao(self):
        with patch.object(indices, "_baixar", side_effect=OSError("sem rede")):
            saida = indices.sincronizar(["12"])
        self.assertFalse(saida["ok"])
        self.assertIn("sem rede", saida["resultado"]["12"]["erro"])
        # a tentativa fica registrada, para não insistir em rajada
        with banco.connect() as conn:
            self.assertTrue(indices._meta(conn, "indices_12_tentativa_em"))

    def test_corpo_vazio_nao_quebra(self):
        with patch.object(indices, "_baixar", return_value=b""):
            self.assertTrue(indices.sincronizar(["12"])["ok"])

    def test_nao_rebaixa_dado_ja_gravado(self):
        self.gravar("12", [("2026-08-03", 0.05)])
        with patch.object(indices, "_baixar", return_value=resposta([])):
            indices.sincronizar(["12"])
        with banco.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM indices_series").fetchone()[0], 1)


class PrecisaAtualizarTests(BancoTests):
    def test_serie_em_dia_nao_bate_na_api(self):
        self.gravar("12", [(indices.dia_util_anterior().isoformat(), 0.05)])
        with banco.connect() as conn:
            self.assertFalse(indices.precisa_atualizar(conn, "12"))

    def test_serie_vazia_precisa(self):
        with banco.connect() as conn:
            self.assertTrue(indices.precisa_atualizar(conn, "12"))

    def test_tentativa_recente_segura_a_proxima(self):
        """API fora do ar não pode virar rajada a cada carga de tela."""
        self.gravar("12", [("2020-01-02", 0.05)])
        with banco.connect() as conn:
            indices._gravar_meta(conn, "indices_12_tentativa_em",
                                 datetime.now().isoformat(timespec="seconds"))
            conn.commit()
        with banco.connect() as conn:
            self.assertFalse(indices.precisa_atualizar(conn, "12"))

    def test_tentativa_antiga_libera(self):
        self.gravar("12", [("2020-01-02", 0.05)])
        antiga = (datetime.now() - timedelta(hours=indices.HORAS_ENTRE_TENTATIVAS + 1))
        with banco.connect() as conn:
            indices._gravar_meta(conn, "indices_12_tentativa_em", antiga.isoformat(timespec="seconds"))
            conn.commit()
        with banco.connect() as conn:
            self.assertTrue(indices.precisa_atualizar(conn, "12"))


class FatorTests(BancoTests):
    def test_fator_composto_bate_com_a_conta_manual(self):
        self.gravar("12", [("2026-08-03", 0.05), ("2026-08-04", 0.05), ("2026-08-05", 0.05)])
        with banco.connect() as conn:
            resultado = indices.fator(conn, "12", "2026-08-03", "2026-08-05")
        esperado = 1.0005 ** 3
        self.assertAlmostEqual(resultado["fator"], esperado, places=10)
        self.assertAlmostEqual(resultado["variacao"], (esperado - 1) * 100, places=4)
        self.assertEqual(resultado["status"], "ok")
        self.assertEqual(resultado["pontos"], 3)

    def test_serie_que_termina_antes_fica_parcial_com_a_janela_real(self):
        """Repetir o último valor para fechar o período inventaria rendimento."""
        self.gravar("12", [("2026-08-03", 0.05), ("2026-08-04", 0.05)])
        with banco.connect() as conn:
            resultado = indices.fator(conn, "12", "2026-08-03", "2026-08-10")
        self.assertEqual(resultado["status"], "parcial")
        self.assertEqual(resultado["janela"], {"de": "2026-08-03", "ate": "2026-08-04"})
        self.assertIn("2026-08-04", resultado["motivo"])

    def test_sem_serie_fica_indisponivel_em_vez_de_zero(self):
        with banco.connect() as conn:
            resultado = indices.fator(conn, "12", "2026-08-03", "2026-08-10")
        self.assertEqual(resultado["status"], "indisponivel")
        self.assertIsNone(resultado["fator"])

    def test_intervalo_recorta_a_serie(self):
        self.gravar("12", [("2026-08-01", 1.0), ("2026-08-05", 1.0), ("2026-08-09", 1.0)])
        with banco.connect() as conn:
            self.assertEqual(indices.fator(conn, "12", "2026-08-02", "2026-08-06")["pontos"], 1)


class CompararTests(unittest.TestCase):
    def indice(self, variacao, status="ok"):
        return {"variacao": variacao, "status": status, "nome": "CDI",
                "janela": {"de": "2026-08-16", "ate": "2026-09-14"}, "motivo": ""}

    def test_percentual_do_indice_e_excesso(self):
        saida = indices.comparar(1.82, self.indice(1.14))
        self.assertAlmostEqual(saida["percentualDoIndice"], 159.65, places=1)
        self.assertAlmostEqual(saida["excessoPP"], 0.68, places=4)

    def test_retorno_negativo_nao_vira_percentual_do_cdi(self):
        """Com perda, "% do CDI" perde sentido; o excesso continua dizendo tudo."""
        saida = indices.comparar(-0.5, self.indice(1.14))
        self.assertIsNone(saida["percentualDoIndice"])
        self.assertAlmostEqual(saida["excessoPP"], -1.64, places=4)

    def test_sem_indice_nao_inventa_comparacao(self):
        saida = indices.comparar(1.82, {"variacao": None, "status": "indisponivel",
                                        "janela": None, "motivo": "sem série"})
        self.assertIsNone(saida["percentualDoIndice"])
        self.assertIsNone(saida["excessoPP"])
        self.assertEqual(saida["status"], "indisponivel")

    def test_sem_retorno_da_carteira_nao_inventa_comparacao(self):
        saida = indices.comparar(None, self.indice(1.14))
        self.assertIsNone(saida["percentualDoIndice"])
        self.assertEqual(saida["noPeriodo"], 1.14)

    def test_janela_do_indice_acompanha_o_numero(self):
        saida = indices.comparar(1.0, self.indice(0.9, status="parcial"))
        self.assertEqual(saida["status"], "parcial")
        self.assertEqual(saida["janela"], {"de": "2026-08-16", "ate": "2026-09-14"})


class ManterAtualizadoTests(BancoTests):
    def test_serie_atrasada_dispara_coleta_em_segundo_plano(self):
        with patch.object(indices, "atualizar_em_background") as disparo:
            indices.manter_atualizado()
        disparo.assert_called_once_with(["12"])

    def test_serie_em_dia_nao_dispara_nada(self):
        self.gravar("12", [(indices.dia_util_anterior().isoformat(), 0.05)])
        with patch.object(indices, "atualizar_em_background") as disparo:
            self.assertIsNone(indices.manter_atualizado())
        disparo.assert_not_called()

    def test_falha_ao_decidir_nao_derruba_quem_chamou(self):
        """O comparativo é adorno; a tela que o chama não pode cair com ele."""
        with patch.object(indices, "precisa_atualizar", side_effect=RuntimeError("banco travado")):
            self.assertIsNone(indices.manter_atualizado())


class EstadoTests(BancoTests):
    def test_estado_lista_as_series_e_o_atraso(self):
        self.gravar("12", [("2020-01-02", 0.05)])
        estado = indices.estado()
        self.assertEqual(estado["12"]["pontos"], 1)
        self.assertTrue(estado["12"]["vencido"])
        self.assertEqual(estado["433"]["pontos"], 0)


if __name__ == "__main__":
    unittest.main()
