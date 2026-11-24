"""Conciliação automática sem valores ou contas fixos."""
import sqlite3
import unittest

from pluggy_extrato import _conciliar_pagamentos_adicionais


class PagamentosFatura(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE pluggy_transacoes (conta_id,transacao_id,data,status,fatura_id,valor,tipo,descricao);
            CREATE TABLE pluggy_ciclos (conta_id,inicio,fim,competencia);
            CREATE TABLE pluggy_faturas (conta_id,fatura_id,competencia,fechamento,valor_total);
            INSERT INTO pluggy_ciclos VALUES ('a','2026-08-30','2026-09-30','2026-10');
            INSERT INTO pluggy_faturas VALUES ('a','anterior','2026-09','2026-08-30',3709.99);
        ''')

    def pagamento(self, id, valor, dia, status='POSTED', conta='a', fatura='anterior'):
        self.db.execute('INSERT INTO pluggy_transacoes VALUES (?,?,?,?,?,?,?,?)',
                        (conta,id,'2026-09-'+dia,status,fatura,-valor,'CREDIT','PAGAMENTO RECEBIDO'))

    def calcular(self, cobrancas=1453.68, origem='aberta_projecao'):
        valores = {'a': [0] * 9 + [cobrancas,0,0]}
        auditoria = _conciliar_pagamentos_adicionais(self.db,2026,{'a'},valores,{'a':[origem]*12})
        return valores['a'][9], auditoria

    def completo(self):
        self.pagamento('quitacao',3709.99,'01','PENDING',fatura=None)
        self.pagamento('extra',65,'05')
        self.pagamento('extra-pendente',65,'06','PENDING',fatura=None)

    def test_inter_deduplica_e_recalcula_com_novas_compras(self):
        self.completo()
        saldo, audit = self.calcular()
        self.assertEqual(saldo,1388.68)
        self.assertEqual(audit[0]['pagamentosIds'],['extra'])
        self.assertEqual(self.calcular(1553.75)[0],1488.75)

    def test_parcial_nao_quita_anterior(self):
        self.pagamento('extra',65,'05')
        self.assertEqual(self.calcular()[0],1453.68)

    def test_quitacao_duplicada(self):
        self.completo()
        self.pagamento('quitacao-postada',3709.99,'02')
        self.assertEqual(self.calcular()[0],1388.68)

    def test_pagamentos_reais_iguais_preservados(self):
        self.pagamento('quitacao',3709.99,'01')
        self.pagamento('extra1',65,'05')
        self.pagamento('extra2',65,'09')
        self.assertEqual(self.calcular()[0],1323.68)

    def test_extra_pendente_sem_par_ja_abate(self):
        # Adiantamento ainda pendente no banco (sem cópia confirmada) já
        # desconta da fatura aberta: é o que resta a pagar no vencimento.
        self.pagamento('quitacao',3709.99,'01')
        self.pagamento('extra',65,'05','PENDING')
        saldo, audit = self.calcular()
        self.assertEqual(saldo,1388.68)
        self.assertEqual(audit[0]['pagamentos'],[{'data':'2026-09-05','valor':65,'pendente':True}])

    def test_oficial_tem_prioridade(self):
        self.completo()
        self.db.execute("INSERT INTO pluggy_faturas VALUES ('a','atual','2026-10','2026-09-30',1388.75)")
        self.assertEqual(self.calcular()[0],1453.68)
        self.assertEqual(self.calcular(1388.75,'oficial')[0],1388.75)

    def test_outra_conta_ou_fatura_nao_entra(self):
        self.pagamento('quitacao',3709.99,'01')
        self.pagamento('extra',65,'05',conta='b')
        self.pagamento('extra2',65,'05',fatura='outra')
        self.assertEqual(self.calcular()[0],1453.68)

    def test_fim_exclusivo(self):
        self.pagamento('quitacao',3709.99,'01')
        self.pagamento('extra',65,'30')
        self.assertEqual(self.calcular()[0],1453.68)

    def test_pareamento_ambiguo_nao_abate(self):
        self.completo()
        self.pagamento('outro',65,'06')
        self.assertEqual(self.calcular()[0],1453.68)


if __name__ == '__main__':
    unittest.main()


class PagamentoVistoNaConta(PagamentosFatura):
    """O débito na conta corrente chega antes do crédito no cartão."""
    def setUp(self):
        super().setUp()
        self.db.executescript('''
            CREATE TABLE pluggy_contas (conta_id,item_id,tipo);
            INSERT INTO pluggy_contas VALUES ('a','item1','CREDIT'),('cc','item1','BANK');
        ''')

    def debito_conta(self, id, valor, dia):
        self.db.execute('INSERT INTO pluggy_transacoes VALUES (?,?,?,?,?,?,?,?)',
                        ('cc',id,'2026-09-'+dia,'POSTED','',-valor,'DEBIT','Pagamento efetuado - Pagamento Fatura'))

    def test_adiantamento_so_na_conta_abate(self):
        self.pagamento('quitacao',3709.99,'01')
        self.debito_conta('cc-quit',3709.99,'01')   # mesmo pagamento: nao conta duas vezes
        self.debito_conta('cc-adiant',523,'23')     # ainda nao chegou ao cartao
        saldo, audit = self.calcular()
        self.assertEqual(saldo, round(1453.68 - 523, 2))
        self.assertEqual(audit[0]['pagamentos'], [{'data':'2026-09-23','valor':523,'pendente':False,'pelaConta':True}])

    def test_quando_chega_ao_cartao_nao_duplica(self):
        self.pagamento('quitacao',3709.99,'01')
        self.debito_conta('cc-adiant',523,'23')
        self.pagamento('adiant-cartao',523,'24')
        self.assertEqual(self.calcular()[0], round(1453.68 - 523, 2))

    def test_conexao_com_dois_cartoes_nao_adivinha(self):
        self.db.execute("INSERT INTO pluggy_contas VALUES ('b','item1','CREDIT')")
        self.pagamento('quitacao',3709.99,'01')
        self.debito_conta('cc-adiant',523,'23')
        self.assertEqual(self.calcular()[0], 1453.68)
        self.assertEqual(self.calcular()[1], [])
