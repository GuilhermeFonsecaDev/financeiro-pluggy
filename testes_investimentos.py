"""Regressões da carteira multibanco; usa SQLite temporário e API simulada.

Execute: python -m unittest testes_investimentos -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import atualizar_pluggy
import banco
import indices
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

    def test_fundo_sem_custo_nao_transforma_saldo_em_lucro(self):
        self.item('btg', 'BTG')
        fundo = self.posicao('btg', 'fundo', 9754, 'V8')
        cdb = self.posicao('btg', 'cdb', 1100, 'CDB')
        self.movimento(fundo, 'compra', 9754)
        self.snapshot(fundo, '2026-09-14', 9754)
        with banco.connect() as conn:
            conn.execute("UPDATE pluggy_investimentos SET tipo='MUTUAL_FUND',valor_original=NULL,"
                         "raw_json=? WHERE investimento_chave=?",
                         (json.dumps({'code': '42.774.627/0001-40', 'lastTwelveMonthsRate': 15.07}), fundo))
            conn.execute('UPDATE pluggy_investimentos SET valor_original=1000 WHERE investimento_chave=?', (cdb,))
        p = investimentos.payload()
        self.assertEqual(p['resumo']['liquido'], 10854)
        self.assertIsNone(p['resumo']['original'])
        self.assertIsNone(p['resumo']['rendimentoBruto'])
        self.assertIsNone(p['resumo']['rendimentoLiquido'])
        self.assertEqual(p['resumo']['rendimentoBrutoConhecido'], 100)
        self.assertEqual(p['resumo']['posicoesSemCapital'], 1)
        self.assertEqual(p['resumo']['saldoSemCapital'], 9754)
        lote = next(l for l in p['lotes'] if l['id'] == 'fundo')
        for campo in ['original', 'rendimentoBruto', 'rendimentoLiquido', 'rentabilidadeBruta']:
            self.assertIsNone(lote[campo])
        self.assertEqual(lote['rentabilidadeFundo12Meses'], 15.07)
        self.assertEqual(lote['codigo'], '42.774.627/0001-40')
        self.assertIsNone(p['instituicoes'][0]['original'])
        self.assertEqual(p['instituicoes'][0]['originalConhecido'], 1000)
        self.assertIsNone(p['snapshots'][0]['original'])
        self.assertEqual(p['meses'][0]['aplicacoes'], 9754)

    def test_carteira_inteira_sem_capital_nao_informa_ganho_zero(self):
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'fundo', 1000)
        self.movimento(chave, 'compra', 1500)
        self.movimento(chave, 'resgate', 500, tipo='SELL')
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=NULL')
        p = investimentos.payload()
        self.assertIsNone(p['resumo']['rendimentoBrutoConhecido'])
        self.assertIsNone(p['lotes'][0]['original'])
        self.assertEqual(p['resumo']['liquido'], 1000)

    def test_capital_zero_explicito_e_perda_continuam_validos(self):
        self.item('btg', 'BTG')
        zero = self.posicao('btg', 'zero', 50)
        perda = self.posicao('btg', 'perda', 900)
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=0 WHERE investimento_chave=?', (zero,))
            conn.execute('UPDATE pluggy_investimentos SET valor_original=1000 WHERE investimento_chave=?', (perda,))
        p = investimentos.payload()
        self.assertEqual(p['resumo']['original'], 1000)
        self.assertEqual(p['resumo']['rendimentoBruto'], -50)
        self.assertEqual(p['resumo']['rendimentoBrutoConhecido'], -50)
        self.assertEqual(p['resumo']['posicoesSemCapital'], 0)
        lote = next(l for l in p['lotes'] if l['id'] == 'zero')
        self.assertEqual(lote['original'], 0)
        self.assertEqual(lote['rendimentoBruto'], 50)
        self.assertIsNone(lote['rentabilidadeBruta'])

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

    def test_reconexao_arquivada_com_novos_ids_nao_duplica_carteira(self):
        self.item('antiga', 'BTG Investimentos,BTG Banking')
        self.item('nova', 'BTG Investimentos,BTG Banking,Cartão BTG BLACK')
        antiga = self.posicao('antiga', 'id-antigo', 100, coleta='2026-09-09')
        nova = self.posicao('nova', 'id-novo', 105, coleta='2026-09-08')
        self.movimento(antiga, 'compra-antiga', 100)
        self.movimento(nova, 'compra-nova', 100)
        self.snapshot(antiga, '2026-09-09', 100)
        self.snapshot(nova, '2026-09-08', 105)
        self.extrato_cdb('antiga', 'extrato-antigo', -100, 'APLICACAO CDB')
        with banco.connect() as conn:
            conn.execute('INSERT INTO app_meta(chave,valor) VALUES (?,?)',
                         ('pluggy_conexoes_arquivadas', json.dumps(['antiga'])))
        p = investimentos.payload()
        self.assertEqual(p['resumo']['liquido'], 105)
        self.assertEqual(p['resumo']['lotesAtivos'], 1)
        self.assertEqual([l['id'] for l in p['lotes']], ['id-novo'])
        self.assertEqual([m['id'] for m in p['movimentos']], ['compra-nova'])
        self.assertEqual(p['meses'][0]['aplicacoes'], 100)
        self.assertEqual([s['liquido'] for s in p['snapshots']], [105])
        self.assertEqual(p['movimentosSemPosicao'], [])
        self.assertEqual([(i['instituicao'], i['liquido']) for i in p['instituicoes']], [('BTG', 105)])
        with banco.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pluggy_investimentos').fetchone()[0], 2)

    def test_arquivada_sem_posicoes_nao_recria_aplicacao_pelo_extrato(self):
        self.item('antiga', 'BTG')
        self.extrato_cdb('antiga', 'extrato-antigo', -100, 'APLICACAO CDB')
        with banco.connect() as conn:
            conn.execute('INSERT INTO app_meta(chave,valor) VALUES (?,?)',
                         ('pluggy_conexoes_arquivadas', json.dumps(['antiga'])))
        self.assertEqual(investimentos.payload()['movimentosSemPosicao'], [])
        with patch.object(investimentos, 'get_api_key') as auth:
            self.assertEqual(investimentos.sincronizar(['antiga'])['resultado'], 'sem_itens')
        auth.assert_not_called()

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

    # ------------------------------------------------- rentabilidade e CDI

    def cdi(self, pontos):
        with banco.connect() as conn:
            indices.garantir_tabelas(conn)
            conn.executemany("INSERT OR REPLACE INTO indices_series VALUES ('12',?,?)", pontos)

    def carteira_com_aporte(self):
        """100 no dia 1, aporte de 100 no dia 2, 210 no fim: rendeu 5%, não 110%."""
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'cdb', 210, 'CDB BTG')
        with banco.connect() as conn:
            conn.execute("UPDATE pluggy_investimentos SET tipo='FIXED_INCOME' WHERE investimento_chave=?",
                         (chave,))
        self.snapshot(chave, '2026-09-01T18:00:00', 100)
        self.snapshot(chave, '2026-09-02T18:00:00', 210)
        self.movimento(chave, 'aporte', 100, data='2026-09-02')
        return chave

    def test_rentabilidade_desconta_o_aporte_do_meio(self):
        self.carteira_com_aporte()
        twr = investimentos.payload()['consolidado']['rentabilidade']['twr']
        self.assertAlmostEqual(twr['valor'], 5.0, places=6)
        self.assertEqual(twr['metodo'], 'twr')
        self.assertEqual(twr['janela'], {'de': '2026-09-01', 'ate': '2026-09-02'})

    def test_comparacao_com_o_cdi_comeca_no_dia_seguinte_ao_saldo_inicial(self):
        """O primeiro ponto é o saldo de partida; nele o CDI ainda não correu."""
        self.carteira_com_aporte()
        self.cdi([('2026-09-01', 1.0), ('2026-09-02', 2.0)])
        benchmark = investimentos.payload()['consolidado']['rentabilidade']['benchmark']
        self.assertEqual(benchmark['janela'], {'de': '2026-09-02', 'ate': '2026-09-02'})
        self.assertEqual(benchmark['noPeriodo'], 2.0)
        self.assertEqual(benchmark['percentualDoIndice'], 250.0)
        self.assertEqual(benchmark['excessoPP'], 3.0)

    def test_sem_serie_do_cdi_a_tela_nao_inventa_comparacao(self):
        self.carteira_com_aporte()
        benchmark = investimentos.payload()['consolidado']['rentabilidade']['benchmark']
        self.assertEqual(benchmark['status'], 'indisponivel')
        self.assertIsNone(benchmark['percentualDoIndice'])
        self.assertIsNone(benchmark['excessoPP'])

    def test_sem_snapshot_nenhum_a_rentabilidade_fica_ausente_e_nao_zero(self):
        self.item('btg', 'BTG')
        self.posicao('btg', 'cdb', 1000, 'CDB BTG')
        twr = investimentos.payload()['consolidado']['rentabilidade']['twr']
        self.assertIsNone(twr['valor'])
        self.assertFalse(twr['confiavel'])
        self.assertEqual(twr['motivo'], 'serie_curta')

    def test_cada_classe_e_cada_posicao_trazem_o_proprio_retorno(self):
        chave = self.carteira_com_aporte()
        self.cdi([('2026-09-02', 2.0)])
        dados = investimentos.payload()
        classe = next(c for c in dados['classes'] if c['id'] == 'renda_fixa')
        self.assertAlmostEqual(classe['rentabilidade']['twr']['valor'], 5.0, places=6)
        self.assertEqual(classe['rentabilidade']['benchmark']['noPeriodo'], 2.0)
        lote = next(l for l in dados['lotes'] if l['id'] == 'cdb')
        self.assertAlmostEqual(lote['rentabilidade']['valor'], 5.0, places=6)
        self.assertTrue(chave)

    def test_carteira_nova_entrando_no_meio_nao_vira_lucro(self):
        """Conexão que aparece depois soma saldo, não rendimento."""
        self.item('btg', 'BTG')
        antiga = self.posicao('btg', 'cdb', 100, 'CDB BTG')
        self.snapshot(antiga, '2026-09-01T18:00:00', 100)
        self.snapshot(antiga, '2026-09-02T18:00:00', 100)
        self.item('xp', 'XP')
        nova = self.posicao('xp', 'cdb2', 900, 'CDB XP')
        self.snapshot(nova, '2026-09-02T18:00:00', 900)
        twr = investimentos.payload()['consolidado']['rentabilidade']['twr']
        self.assertEqual(twr['valor'], 0.0)

    def test_xirr_usa_o_historico_de_movimentos(self):
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'cdb', 1100, 'CDB BTG')
        self.movimento(chave, 'compra', 1000, data='2025-09-01')
        self.snapshot(chave, '2026-09-01T18:00:00', 1100)
        xirr = investimentos.payload()['consolidado']['rentabilidade']['xirr']
        self.assertEqual(xirr['metodo'], 'xirr')
        self.assertAlmostEqual(xirr['valor'], 10.0, delta=0.5)


    # -------------------------------------------- aplicado pelos movimentos

    def test_sem_custo_na_api_o_aplicado_vem_dos_aportes(self):
        """Custo digitado à mão não sobrevive a aporte mensal; o histórico sim."""
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'fundo', 10500, 'Fundo V8')
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=NULL WHERE investimento_chave=?', (chave,))
        self.movimento(chave, 'a1', 5000, data='2026-07-01')
        self.movimento(chave, 'a2', 5000, data='2026-08-01')
        lote = investimentos.payload()['lotes'][0]
        self.assertEqual(lote['aplicado'], 10000)
        self.assertEqual(lote['origemAplicado'], 'movimentos')
        self.assertTrue(lote['aplicadoConfiavel'])
        self.assertEqual(lote['rendimentoBrutoEstimado'], 500)
        # a regra antiga continua valendo: custo não informado não vira custo
        self.assertIsNone(lote['original'])
        self.assertIsNone(lote['rendimentoBruto'])

    def test_resgate_reduz_o_aplicado(self):
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'fundo', 3000, 'Fundo V8')
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=NULL WHERE investimento_chave=?', (chave,))
        self.movimento(chave, 'a1', 5000, data='2026-07-01')
        self.movimento(chave, 'r1', 2000, tipo='SELL', data='2026-08-01')
        lote = investimentos.payload()['lotes'][0]
        self.assertEqual(lote['aplicado'], 3000)
        self.assertEqual(lote['resgates'], 2000)

    def test_sem_aporte_no_historico_o_aplicado_continua_ausente(self):
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'fundo', 9754, 'Fundo V8')
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=NULL WHERE investimento_chave=?', (chave,))
        lote = investimentos.payload()['lotes'][0]
        self.assertIsNone(lote['aplicado'])
        self.assertEqual(lote['motivoSemAplicado'], 'sem_aporte_no_historico')
        self.assertEqual(investimentos.payload()['consolidado']['posicoesSemAplicado'], 1)

    def test_exclusao_confirmada_nao_reimporta_duplicata_e_preserva_aporte_correto(self):
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'western', 1037, 'Western')
        self.movimento(chave, 'provisorio', 1037)
        self.movimento(chave, 'cotizado', 1037)
        with patch.object(banco, 'create_database_backup') as backup:
            investimentos.excluir_movimento(chave, 'provisorio', 'Duplicata confirmada pelo usuário')
        backup.assert_called_once()

        def listar(api_key, caminho, params):
            if caminho == '/investments':
                return [{'id': 'western', 'name': 'Western', 'type': 'MUTUAL_FUND',
                         'status': 'ACTIVE', 'balance': 1037, 'amount': 1037}]
            return [{'id': 'provisorio', 'amount': 1037, 'type': 'BUY', 'date': '2026-09-11'},
                    {'id': 'cotizado', 'amount': 1037, 'type': 'BUY', 'date': '2026-09-11'}]

        with patch.object(investimentos, 'get_api_key', return_value='teste'), \
                patch.object(investimentos, '_listar', side_effect=listar):
            self.assertEqual(investimentos.sincronizar(['btg'])['movimentos'], 1)
        dados = investimentos.payload()
        self.assertEqual([m['id'] for m in dados['movimentos']], ['cotizado'])
        self.assertEqual(dados['lotes'][0]['aplicado'], 1037)
        self.assertTrue(dados['lotes'][0]['aplicadoConfiavel'])
        self.assertEqual(dados['meses'][0]['aplicacoes'], 1037)
        with banco.connect() as conn:
            audit = conn.execute('SELECT registro_json FROM pluggy_investimento_movimentos_excluidos').fetchone()
            self.assertEqual(json.loads(audit[0])['movimento_id'], 'provisorio')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pluggy_investimento_movimentos').fetchone()[0], 1)

    def test_aporte_repetido_pela_api_nao_vira_prejuizo(self):
        """Dois BUY iguais no mesmo dia e saldo de um só: a conta não fecha."""
        self.item('btg', 'BTG')
        chave = self.posicao('btg', 'fundo', 1037, 'Fundo BDR')
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=NULL WHERE investimento_chave=?', (chave,))
        self.movimento(chave, 'a1', 1037, data='2026-09-11')
        self.movimento(chave, 'a2', 1037, data='2026-09-11')
        dados = investimentos.payload()
        lote = dados['lotes'][0]
        self.assertEqual(lote['aplicado'], 2074)         # o valor fica visível
        self.assertFalse(lote['aplicadoConfiavel'])      # mas não é custo apurado
        self.assertEqual(lote['motivoSemAplicado'], 'divergencia_com_saldo')
        self.assertIsNone(lote['rendimentoBrutoEstimado'])
        self.assertEqual(dados['consolidado']['posicoesComAplicadoDuvidoso'], 1)
        self.assertIsNone(dados['consolidado']['aplicado'])

    def test_carteira_com_as_duas_fontes_soma_as_duas(self):
        self.item('btg', 'BTG')
        cdb = self.posicao('btg', 'cdb', 1100, 'CDB BTG')     # custo da API: 1100
        fundo = self.posicao('btg', 'fundo', 5200, 'Fundo V8')
        with banco.connect() as conn:
            conn.execute('UPDATE pluggy_investimentos SET valor_original=1000 WHERE investimento_chave=?', (cdb,))
            conn.execute('UPDATE pluggy_investimentos SET valor_original=NULL WHERE investimento_chave=?', (fundo,))
        self.movimento(fundo, 'a1', 5000, data='2026-07-01')
        c = investimentos.payload()['consolidado']
        self.assertEqual(c['aplicado'], 6000)
        self.assertEqual(c['rendimentoBrutoEstimado'], 300)
        self.assertEqual(c['porFonte'], {'pluggy': 1, 'movimentos': 1})
        self.assertEqual(c['posicoesSemAplicado'], 0)


if __name__ == '__main__':
    unittest.main()
