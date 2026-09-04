"""Regressões da carteira multibanco; usa SQLite temporário e API simulada.

Execute: python -m unittest testes_investimentos -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import atualizar_pluggy
import banco
import importar_pluggy
import investimentos


class CarteiraTests(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        troca = patch.object(banco, "DATABASE_PATH", Path(pasta.name) / "teste.db")
        troca.start()
        self.addCleanup(troca.stop)
        # sqlite3.Connection.__exit__ confirma a transação, mas não fecha o
        # arquivo. Fechamos também as conexões dos módulos antes de apagar o
        # banco temporário no Windows.
        conexoes = []
        conectar = banco.connect

        def conectar_teste():
            conn = conectar()
            conexoes.append(conn)
            return conn

        troca_conexao = patch.object(banco, 'connect', side_effect=conectar_teste)
        troca_conexao.start()
        self.addCleanup(troca_conexao.stop)
        self.addCleanup(lambda: [conn.close() for conn in conexoes])
        banco.ensure_database()
        with banco.connect() as conn:
            conn.executescript(importar_pluggy.SCHEMA_PLUGGY)
            conn.executescript(investimentos.SCHEMA)

    def item(self, item, nome):
        with banco.connect() as conn:
            conn.execute("INSERT INTO pluggy_itens VALUES (?, ?, '2026-09-01')", (item, nome))
            conn.execute("INSERT INTO pluggy_contas (conta_id,item_id,nome,tipo,importado_em) VALUES (?,?,?,'BANK','2026-09-01')",
                         (item + '-conta', item, nome))

    def posicao(self, item, inv, saldo, nome="Fundo teste", status="ACTIVE", coleta="2026-09-01"):
        chave = item + ':' + inv
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_investimentos (investimento_chave,investimento_id,item_id,nome,"
                "saldo_liquido,valor_bruto,valor_original,status,importado_em) VALUES (?,?,?,?,?,?,?,?,?)",
                (chave, inv, item, nome, saldo, saldo, saldo, status, coleta))
        return chave

    def movimento(self, chave, mov, valor, tipo="BUY", data="2026-09-01"):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_investimento_movimentos (movimento_chave,movimento_id,investimento_chave,"
                "data,tipo,valor_bruto,importado_em) VALUES (?,?,?,?,?,?,'2026-09-01')",
                (chave + ':' + mov, mov, chave, data, tipo, valor))

    def extrato(self, item, mov, valor):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,valor,importado_em) "
                "VALUES (?,?,'2026-09-01','2026-09',2026,9,'APLICACAO COFRINHOS',?,'2026-09-01')",
                (mov, item + '-conta', valor))

    def snapshot(self, chave, data, saldo, status="ACTIVE"):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_investimento_snapshots (investimento_chave,coletado_em,saldo_liquido,status) "
                "VALUES (?,?,?,?)", (chave, data, saldo, status))

    def extrato_cdb(self, item, mov, valor, descricao, status='POSTED'):
        self.extrato(item, mov, valor)
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_transacoes SET descricao=?,status=? WHERE transacao_id=?',
                         (descricao, status, mov))

    def test_cdb_sem_posicao_identifica_aplicacao_sem_inventar_saldo(self):
        self.item('btg', 'BTG')
        self.extrato_cdb('btg', 'cdb', -10000, 'EMISSAO - CDB BANCO BTG PACTUAL S.A. - Venc: 2028-09-04 XX1008445008')
        self.extrato_cdb('btg', 'tef', 10000, 'RECEBIMENTO TRANSFERÊNCIA')
        self.extrato_cdb('btg', 'pix', 9999, 'Pix')
        p = investimentos.payload()
        self.assertEqual(p['resumo']['aplicacoesSemPosicao'], 10000)
        self.assertEqual(p['resumo']['liquido'], 0)
        self.assertEqual(p['resumo']['rendimentoBruto'], 0)
        self.assertEqual(p['lotes'], [])
        self.assertEqual(p['meses'][0]['aplicacoes'], 10000)
        self.assertEqual(p['mesesPosicoes'], [])
        self.assertEqual([m['id'] for m in p['movimentosSemPosicao']], ['cdb'])
        self.assertTrue(p['instituicoes'][0]['posicaoPendente'])

    def test_posicao_api_substitui_fallback_sem_duplicar(self):
        self.item('btg', 'BTG')
        self.extrato_cdb('btg', 'cdb', -10000, 'EMISSAO - CDB BANCO BTG')
        chave = self.posicao('btg', 'api-cdb', 10001, 'CDB BANCO BTG')
        self.movimento(chave, 'api-compra', 10000)
        p = investimentos.payload()
        self.assertEqual(p['resumo']['aplicacoesSemPosicao'], 0)
        self.assertEqual(p['movimentosSemPosicao'], [])
        self.assertEqual(p['resumo']['liquido'], 10001)
        self.assertEqual(p['meses'][0]['aplicacoes'], 10000)

    def test_extrato_generico_resgates_estornos_e_pendentes(self):
        self.item('nova', 'Corretora Nova')
        self.extrato_cdb('nova', 'compra', -500, 'APLICAÇÃO LCI BANCO TESTE')
        self.extrato_cdb('nova', 'venda', 200, 'RESGATE LCI BANCO TESTE')
        self.extrato_cdb('nova', 'falso', -999, 'PIX CDB BANCO TESTE')
        self.extrato_cdb('nova', 'estorno', -999, 'EMISSAO CDB CANCELADA')
        self.extrato_cdb('nova', 'pendente', -999, 'COMPRA CDB', status='PENDING')
        self.extrato_cdb('nova', 'sinal', 999, 'EMISSAO CDB')
        p = investimentos.payload()
        self.assertEqual(p['resumo']['aplicacoesSemPosicao'], 500)
        self.assertEqual(p['resumo']['resgatesSemPosicao'], 200)
        self.assertEqual(p['meses'][0]['liquido'], 300)
        self.assertEqual({m['id'] for m in p['movimentos']}, {'compra', 'venda'})

    def test_consolida_instituicoes_e_conexao_nova_sem_lista_de_bancos(self):
        for item, nome, saldo in [('a', 'Itaú', 100), ('b', 'Nubank', 200)]:
            self.item(item, nome)
            chave = self.posicao(item, item, saldo)
            self.movimento(chave, item, saldo)
        self.assertEqual(investimentos.payload()['resumo']['liquido'], 300)
        self.item('nova', 'Corretora Nova')
        chave = self.posicao('nova', 'novo', 400)
        self.movimento(chave, 'novo', 400)
        self.item('vazia', 'Banco sem posições')
        p = investimentos.payload()
        self.assertEqual(p['resumo']['liquido'], 700)
        self.assertEqual(p['resumo']['lotesAtivos'], 3)
        self.assertEqual(p['meses'][0]['aplicacoes'], 700)
        self.assertEqual(sum(i['liquido'] for i in p['instituicoes']), 700)
        self.assertEqual({l['instituicao'] for l in p['lotes']}, {'Itaú', 'Nubank', 'Corretora Nova'})
        self.assertIn('Banco sem posições', {i['instituicao'] for i in p['instituicoes']})
        self.assertTrue(all(not m['confirmadoExtrato'] for m in p['movimentos']))

    def test_fallback_nao_substitui_outra_conexao_do_mesmo_banco(self):
        for item in ('a', 'b'):
            self.item(item, 'Itaú')
            chave = self.posicao(item, item, 100, 'CDB - ITAU UNIBANCO S.A.')
            self.movimento(chave, item, 100)
        self.extrato('a', 'extrato-a', -150)
        p = investimentos.payload()
        self.assertEqual(p['meses'][0]['aplicacoes'], 250)
        self.assertEqual({m['id'] for m in p['movimentos']}, {'extrato-a', 'b'})
        self.assertEqual(p['confirmacaoExtrato']['divergenciasEndpointInvestimentos'][0]['itemId'], 'a')

    def test_carteira_com_outros_produtos_preserva_api(self):
        self.item('a', 'Itaú')
        cdb = self.posicao('a', 'cdb', 100, 'CDB - ITAU UNIBANCO S.A.')
        fundo = self.posicao('a', 'fundo', 200)
        self.movimento(cdb, 'cdb', 100)
        self.movimento(fundo, 'fundo', 200)
        self.extrato('a', 'cofrinho', -100)
        p = investimentos.payload()
        self.assertEqual(p['meses'][0]['aplicacoes'], 300)
        self.assertEqual({m['id'] for m in p['movimentos']}, {'cdb', 'fundo'})

    def test_confirmacao_extrato_so_quando_totais_conferem(self):
        self.item('a', 'Itaú')
        chave = self.posicao('a', 'cdb', 100, 'CDB - ITAU UNIBANCO S.A.')
        self.movimento(chave, 'aporte', 100)
        self.movimento(chave, 'resgate', 30, tipo='SELL')
        self.extrato('a', 'aporte-extrato', -100)
        p = investimentos.payload()
        movs = {m['id']: m for m in p['movimentos']}
        self.assertTrue(movs['aporte']['confirmadoExtrato'])
        self.assertFalse(movs['resgate']['confirmadoExtrato'])
        self.assertEqual(p['meses'][0]['liquido'], 70)

    def test_reconexao_usa_posicao_mais_recente_sem_duplicar(self):
        self.item('antiga', 'Banco Teste')
        self.item('nova', 'Banco Teste')
        antiga = self.posicao('antiga', 'mesmo-id', 100, coleta='2026-08-01')
        nova = self.posicao('nova', 'mesmo-id', 120, coleta='2026-09-01')
        self.movimento(antiga, 'mov', 100)
        self.movimento(nova, 'mov', 100)
        self.snapshot(antiga, '2026-09-01', 100)
        self.snapshot(nova, '2026-09-01', 120)
        p = investimentos.payload()
        self.assertEqual(p['resumo']['liquido'], 120)
        self.assertEqual(p['resumo']['movimentos'], 1)
        self.assertEqual(p['resumo']['lotesAtivos'], 1)
        self.assertEqual(p['snapshots'][0]['liquido'], 120)

    def test_snapshot_parcial_preserva_outras_conexoes_e_encerra_lote(self):
        self.item('a', 'Banco A')
        self.item('b', 'Banco B')
        a = self.posicao('a', 'a', 120)
        b = self.posicao('b', 'b', 0, status='TOTAL_WITHDRAWAL')
        self.snapshot(a, '2026-09-01', 100)
        self.snapshot(b, '2026-09-01', 200)
        self.snapshot(a, '2026-09-02', 120)
        self.snapshot(b, '2026-09-03', 0, status='TOTAL_WITHDRAWAL')
        self.assertEqual([s['liquido'] for s in investimentos.payload()['snapshots']], [300, 320, 120])

    def test_sem_conexoes(self):
        p = investimentos.payload()
        self.assertEqual(p['resumo']['liquido'], 0)
        self.assertEqual(p['instituicoes'], [])
        self.assertEqual(p['movimentos'], [])

    def test_sync_descobre_novas_conexoes_e_isola_falhas(self):
        self.item('a', 'Banco A')

        def listar(api_key, caminho, params):
            if caminho == '/investments':
                item = params['itemId']
                if item == 'quebrada':
                    raise RuntimeError('Indisponível')
                return [{'id': item + '-inv', 'name': 'Fundo', 'status': 'ACTIVE', 'balance': 100}]
            return []

        with patch.object(investimentos, 'get_api_key', return_value='teste'), patch.object(investimentos, '_listar', side_effect=listar):
            self.assertTrue(investimentos.sincronizar()['ok'])
            self.item('nova', 'Corretora Nova')
            self.item('quebrada', 'Banco indisponível')
            resultado = investimentos.sincronizar()
        self.assertEqual(resultado['investimentos'], 2)
        self.assertEqual(len(resultado['falhas']), 1)
        self.assertEqual(investimentos.payload()['resumo']['liquido'], 200)

    def test_sync_investimentos_independe_do_extrato(self):
        self.item('corretora', 'Corretora')
        with patch.object(atualizar_pluggy, '_rodar_sync', return_value=(False, 'Sem extrato')), patch.object(
            investimentos, 'sincronizar', return_value={'ok': True, 'falhas': []}
        ) as sync:
            atualizar_pluggy.atualizar(forcar=True, verboso=False)
        sync.assert_called_once_with(['corretora'])


if __name__ == '__main__':
    unittest.main()
