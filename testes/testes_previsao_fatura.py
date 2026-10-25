"""Previa da fatura: a decomposicao tem de fechar com o total, sempre.

O valor destes testes esta num numero so: a soma dos baldes e o total do mes.
Se alguem passar a CALCULAR a previa em vez de DECOMPOR o payload de cartoes,
e aqui que isso aparece -- o total continuaria certo e a soma, nao.
"""
import unittest
from datetime import date
from unittest.mock import patch

import banco
import previsao_fatura as pf
import pluggy_extrato as px
import recorrentes as rec
import testes_recorrentes as base


class DataFixa(date):
    """Mesmo "hoje" das fixtures, para o calendario de previsao nao andar."""
    @classmethod
    def today(cls):
        return cls(2026, 9, 4)


class PrevisaoFaturaTests(unittest.TestCase):
    inserir = base.RecorrentesTests.inserir

    def setUp(self):
        base.RecorrentesTests.setUp(self)
        hoje = patch.object(pf, "date", DataFixa)
        hoje.start()
        self.addCleanup(hoje.stop)

    def previsao(self, meses=3):
        return pf.previsao_payload(meses)

    def test_soma_dos_componentes_e_o_total_do_mes(self):
        rec.salvar_previsao(next(
            s for s in rec.sugestoes_payload()["sugestoes"] if s["lojista"] == "netflix")["chave"])
        dados = self.previsao()
        decomponiveis = 0
        for cartao in dados["cartoes"]:
            for mes in cartao["meses"]:
                if mes["definitivo"]:
                    self.assertEqual(mes["somaComponentes"], 0)
                    self.assertEqual(mes["previstos"], [])
                    continue
                decomponiveis += 1
                self.assertAlmostEqual(
                    mes["total"], mes["somaComponentes"], places=2,
                    msg=f"{cartao['nome']} {mes['competencia']} nao fecha")
        self.assertGreater(decomponiveis, 0, "nenhum mes decomponivel no cenario")

    def test_parcela_nao_entra_no_que_a_tela_preve(self):
        """Parcela é fato conhecido e assunto da tela de Cartões, não daqui."""
        dados = self.previsao()
        for cartao in dados["cartoes"]:
            for mes in cartao["meses"]:
                for item in mes["previstos"]:
                    self.assertIn(item["tipo"], ("recorrencia", "reserva"))
                # A parcela continua no total da fatura, só não é o assunto.
                if mes["parcelas"]:
                    self.assertGreater(mes["total"], mes["previsto"])

    def test_reserva_de_habito_nao_conta_o_que_ja_foi_gasto(self):
        """valorLancado ja esta dentro de 'aberta'; so o restante e componente."""
        for mes in (5, 6, 7, 8):
            for indice in range(2):
                self.inserir(f"posto-{mes}-{indice}", mes, "POSTO IPIRANGA", 100)
        habitos = rec.sugestoes_payload()["habitos"]
        posto = next(h for h in habitos if "posto" in h["lojista"])
        rec.projetar_habito(posto["chave"])

        dados = self.previsao()
        achou = False
        for cartao in dados["cartoes"]:
            for mes in cartao["meses"]:
                if mes["definitivo"]:
                    continue
                componentes = mes["componentes"]
                if componentes["reservaRestante"] or componentes["reservaConsumida"]:
                    achou = True
                self.assertAlmostEqual(mes["total"], mes["somaComponentes"], places=2)
        self.assertTrue(achou, "a reserva do habito nao apareceu na previa")

    def test_mes_com_fatura_oficial_nao_se_decompoe(self):
        with banco.connect() as conn:
            competencia = conn.execute(
                "SELECT competencia FROM pluggy_faturas WHERE valor_total <> 0"
            ).fetchone()
        if not competencia:
            self.skipTest("cenario sem fatura fechada")
        ano = int(competencia[0][:4])
        payload = px.cartoes_payload(ano, "fatura")
        indice = int(competencia[0][5:7]) - 1
        for conta, origens in payload["origens"].items():
            if origens[indice] != "oficial":
                continue
            baldes = payload["componentes"][conta][indice]
            self.assertEqual(
                [v for v in baldes.values() if v], [],
                "fatura oficial e autoridade final: nao sobra componente")


    # ------------------------------------------------------------ acurácia
    def test_previsao_e_congelada_uma_vez_por_ciclo(self):
        """Olhar de novo não pode reescrever o palpite já feito."""
        self.previsao()
        with banco.connect() as conn:
            linhas = [dict(r) for r in conn.execute(
                "SELECT * FROM previsao_fatura_historico ORDER BY conta_id, competencia")]
        self.assertTrue(linhas, "nada foi congelado")
        self.assertTrue(all(l["previsto"] for l in linhas))

        # Um gasto novo muda a previsão, mas não o que já foi registrado.
        self.inserir("compra-nova", 9, "COMPRA NOVA", 500, conta="cartao-b")
        self.previsao()
        with banco.connect() as conn:
            depois = [dict(r) for r in conn.execute(
                "SELECT * FROM previsao_fatura_historico ORDER BY conta_id, competencia")]
        anteriores = {(l["conta_id"], l["competencia"]): l["previsto"] for l in linhas}
        for linha in depois:
            chave = (linha["conta_id"], linha["competencia"])
            if chave in anteriores:
                self.assertEqual(linha["previsto"], anteriores[chave],
                                 "a previsão congelada foi reescrita")

    def test_mes_definitivo_nao_e_congelado(self):
        self.previsao()
        with banco.connect() as conn:
            congeladas = {(r[0], r[1]) for r in conn.execute(
                "SELECT conta_id,competencia FROM previsao_fatura_historico")}
            oficiais = {(r[0], r[1]) for r in conn.execute(
                "SELECT conta_id,competencia FROM pluggy_faturas WHERE valor_total <> 0")}
        self.assertFalse(congeladas & oficiais,
                         "mês com fatura emitida não é previsão, é fato")

    def test_acuracia_vazia_nao_inventa_numero(self):
        dados = self.previsao()
        acuracia = dados["acuracia"]
        if not acuracia["meses"]:
            self.assertIsNone(acuracia["erroMedio"])
            self.assertIsNone(acuracia["viesMedio"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
