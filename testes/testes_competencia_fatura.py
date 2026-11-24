"""Competencia de fatura quando o banco muda o significado da previsao.

Cenario: cartao que fecha dia 30 e vence dia 7. Ate entao o conector mandava em
`billForecastDate` o mes do FECHAMENTO; num certo dia passou a mandar o mes do
VENCIMENTO. Com a regra antiga todas as compras do ciclo aberto pulavam uma
fatura. Os casos abaixo valem para qualquer cartao -- nada aqui e de um banco.
"""
import json
import sqlite3
import unittest

import testes  # noqa: F401  (backend no sys.path)
import ciclos

CONTA = "cartao-x"


def _banco():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE pluggy_transacoes (transacao_id TEXT, conta_id TEXT, data TEXT,
            descricao TEXT, raw_json TEXT, fatura_id TEXT DEFAULT '');
        CREATE TABLE pluggy_faturas (conta_id TEXT, fatura_id TEXT, competencia TEXT);
        CREATE TABLE pluggy_ciclos (conta_id TEXT, inicio TEXT, fim TEXT,
            competencia TEXT, fatura_id TEXT, origem TEXT);
        CREATE TABLE pluggy_cartao_previsao (conta_id TEXT, acertos INTEGER, amostras INTEGER);
    """)
    # Ciclos: fecha dia 30; a competencia e o mes do vencimento (seguinte).
    ciclos_ = [("2026-09-30", "2026-10-30", "2026-11", "fatura"),
               ("2026-10-30", "2026-11-30", "2026-12", "projetado"),
               ("2026-11-30", "2026-12-30", "2027-01", "projetado"),
               ("2026-12-30", "2027-01-30", "2027-02", "projetado"),
               ("2027-01-30", "2027-02-28", "2027-03", "projetado")]
    conn.executemany("INSERT INTO pluggy_ciclos VALUES (?,?,?,?,'',?)",
                     [(CONTA, *c) for c in ciclos_])
    conn.execute("INSERT INTO pluggy_cartao_previsao VALUES (?, 100, 100)", (CONTA,))
    return conn


def _tx(conn, tid, data, desc, previsao, parcela=None, total=None):
    meta = {"billForecastDate": previsao}
    if parcela:
        meta.update(installmentNumber=parcela, totalInstallments=total)
    conn.execute("INSERT INTO pluggy_transacoes VALUES (?,?,?,?,?, '')",
                 (tid, CONTA, data, desc, json.dumps({"creditCardMetadata": meta})))


def _comp(conn, tid):
    expr = ciclos.expressao_competencia("t", None)
    return conn.execute(f"SELECT {expr} FROM pluggy_transacoes t WHERE transacao_id = ?",
                        (tid,)).fetchone()[0]


class CompetenciaFaturaTests(unittest.TestCase):
    def setUp(self):
        self.conn = _banco()

    def test_a_vista_longe_do_fechamento_segue_a_janela(self):
        # Previsao no padrao novo (mes do vencimento) nao empurra para jan.
        _tx(self.conn, "a", "2026-11-15", "MERCADO", "2026-12")
        self.assertEqual(_comp(self.conn, "a"), "2026-12")

    def test_a_vista_no_padrao_antigo_continua_igual(self):
        _tx(self.conn, "b", "2026-11-05", "POSTO", "2026-11")
        self.assertEqual(_comp(self.conn, "b"), "2026-12")

    def test_compra_logo_apos_o_fechamento_usa_a_previsao(self):
        # Dia 31, depois do fechamento de 30/10: proxima fatura.
        _tx(self.conn, "c", "2026-10-31", "LOJA", "2026-10")
        self.assertEqual(_comp(self.conn, "c"), "2026-12")

    def test_parcelas_lancadas_juntas_andam_a_partir_da_primeira(self):
        for n in range(1, 5):
            _tx(self.conn, f"p{n}", "2026-11-07", "LOJA PARCELADA",
                f"2026-{11 + n:02d}" if n == 1 else f"2027-{n - 1:02d}", n, 4)
        self.assertEqual([_comp(self.conn, f"p{n}") for n in range(1, 5)],
                         ["2026-12", "2027-01", "2027-02", "2027-03"])

    def test_parcela_lancada_sozinha_segue_a_previsao(self):
        # Parcela 2/2 sem a 1 no mesmo dia: a data e a da compra original.
        _tx(self.conn, "s", "2026-09-20", "LOJA", "2026-11", 2, 2)
        self.assertEqual(_comp(self.conn, "s"), "2026-12")


if __name__ == "__main__":
    unittest.main()
