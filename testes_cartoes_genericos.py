"""Regressões de identidade/tag. SQLite temporário, sem rede nem dados pessoais."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import banco
import cartoes
import importar_pluggy
import pluggy_extrato


class CartoesGenericosTests(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix='teste_cartoes_')
        self.addCleanup(pasta.cleanup)
        p = patch.object(banco, 'DATABASE_PATH', Path(pasta.name) / 'teste.db')
        p.start()
        self.addCleanup(p.stop)
        original = banco.connect
        conexoes = []

        def conectar():
            conn = original()
            conexoes.append(conn)
            return conn

        p = patch.object(banco, 'connect', side_effect=conectar)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: [c.close() for c in conexoes])
        banco.ensure_database()
        with banco.connect() as conn:
            conn.executescript(importar_pluggy.SCHEMA_PLUGGY)

    def adicionar(self, fonte, item='conexao', nome='Cartão do fornecedor', numero='8113', marca='MASTERCARD', tipo='CREDIT_CARD'):
        with banco.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO pluggy_itens VALUES (?, 'Fornecedor', '2026-09-01')", (item,))
            conn.execute(
                "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,raw_json,importado_em,limite_credito) "
                "VALUES (?,?,?,?,?,?,?,'2026-09-01',1000)",
                (fonte, item, 'CREDIT' if tipo == 'CREDIT_CARD' else 'BANK', tipo, nome, numero,
                 json.dumps({'creditData': {'brand': marca}}) if tipo == 'CREDIT_CARD' else '{}'))
        return self.identidade(fonte) if tipo == 'CREDIT_CARD' else None

    def identidade(self, fonte):
        with banco.connect() as conn:
            return cartoes.identidades(conn)[fonte]

    def taguear(self, fonte, tag):
        return cartoes.salvar({'cartoes': [{'cartaoId': self.identidade(fonte)['cartaoId'], 'tag': tag}]})

    def colunas(self):
        with banco.connect() as conn:
            return cartoes.colunas(conn)

    def transacao(self, fonte, dia):
        with banco.connect() as conn:
            conn.execute(
                "INSERT INTO pluggy_transacoes (transacao_id,conta_id,data,mes_ref,ano,mes,descricao,valor,tipo,importado_em) "
                "VALUES (?,?,?,'2026-09',2026,9,'Compra',123,'DEBIT','2026-09-01')",
                (fonte + dia, fonte, '2026-09-' + dia))

    def test_cartoes_sem_tag_da_mesma_conexao_sao_individuais(self):
        a = self.adicionar('a', nome='PLATINUM PRIME DUO')
        b = self.adicionar('b', nome='gold', numero='7412')
        self.assertEqual(a['nomeExibicao'], 'MASTERCARD 8113')
        self.assertEqual(b['nomeExibicao'], 'MASTERCARD 7412')
        self.assertEqual(len(self.colunas()), 2)
        self.assertNotEqual(a['grupo'], b['grupo'])

    def test_tag_consolida_e_remocao_restaura_identidade(self):
        self.adicionar('a')
        self.adicionar('b', item='outro-banco', numero='9773', marca='VISA')
        self.taguear('a', 'Itaú')
        self.taguear('b', '  ITAU  ')
        grupo = self.colunas()[0]
        self.assertEqual(len(self.colunas()), 1)
        self.assertEqual(grupo['nome'], 'Itaú')
        self.assertEqual(set(grupo['contas']), {'a', 'b'})
        self.assertIsNone(grupo['limiteCredito'])
        self.assertEqual(len(grupo['membros']), 2)
        self.taguear('a', '')
        self.assertEqual(self.identidade('a')['nomeExibicao'], 'MASTERCARD 8113')
        self.assertEqual(len(self.colunas()), 2)

    def test_cor_da_tag_e_aplicada_ao_grupo_inteiro(self):
        a = self.adicionar('a')
        b = self.adicionar('b', item='outro-banco', numero='9773', marca='VISA')
        cartoes.salvar({'cartoes': [
            {'cartaoId': a['cartaoId'], 'tag': 'Viagens'},
            {'cartaoId': b['cartaoId'], 'tag': 'Viagens'},
        ]})
        cartoes.salvar({'cartoes': [
            {'cartaoId': a['cartaoId'], 'tag': 'Viagens', 'cor': '#123456'},
            {'cartaoId': b['cartaoId'], 'tag': 'Viagens', 'cor': '#123456'},
        ]})
        self.assertEqual({self.identidade('a')['cor'], self.identidade('b')['cor']}, {'#123456'})
        self.assertEqual(self.colunas()[0]['cor'], '#123456')
        self.assertEqual(cartoes.payload()['tags'][0]['cor'], '#123456')

    def test_tag_nao_captura_conta_bancaria(self):
        self.adicionar('banco', tipo='CHECKING_ACCOUNT', nome='INTER')
        self.adicionar('cartao')
        self.taguear('cartao', 'INTER')
        with banco.connect() as conn:
            mapa = cartoes.identidades(conn)
            self.assertNotIn('banco', mapa)
            self.assertEqual(cartoes.resolver_contas(conn, mapa['cartao']['tagId']), {'cartao'})

    def test_novo_cartao_sem_regra_de_banco(self):
        self.adicionar('primeiro')
        antes = self.colunas()[0]['id']
        novo = self.adicionar('novo', item='corretora-nova', marca='ELO', numero='5432')
        self.assertEqual(novo['nomeExibicao'], 'ELO 5432')
        self.assertFalse(novo['tag'])
        self.assertIn(antes, {c['id'] for c in self.colunas()})

    def test_mesmo_final_nao_oculta_cartoes_distintos(self):
        self.adicionar('a', item='banco-a')
        self.adicionar('b', item='banco-b')
        self.assertEqual(len(self.colunas()), 2)

    def test_reconexao_com_historico_inequivoco_preserva_tag(self):
        original = self.adicionar('antigo', item='antiga')
        self.taguear('antigo', 'Viagens')
        for dia in ('01', '02', '03'):
            self.transacao('antigo', dia)
        # A descoberta ocorre depois da primeira importação das transações.
        with banco.connect() as conn:
            conn.execute("INSERT INTO pluggy_itens VALUES ('nova','Fornecedor','2026-09-04')")
            conn.execute("INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,raw_json,importado_em) "
                         "SELECT 'novo','nova',tipo,subtipo,nome,numero,raw_json,'2026-09-04' FROM pluggy_contas WHERE conta_id='antigo'")
        for dia in ('01', '02', '03'):
            self.transacao('novo', dia)
        nova = self.identidade('novo')
        self.assertEqual(nova['cartaoId'], original['cartaoId'])
        self.assertEqual(nova['tag'], 'Viagens')
        self.assertEqual(self.colunas()[0]['contas'], ['novo'])

    def test_renomear_tag_mantem_id_e_uniao_resolve_referencias(self):
        self.adicionar('a')
        self.adicionar('b', numero='1234')
        self.taguear('a', 'Pessoal')
        tag = self.identidade('a')['tagId']
        cartoes.renomear_tag(tag, {'nome': 'Viagens'})
        self.assertEqual(self.identidade('a')['tagId'], tag)
        self.assertEqual(self.identidade('a')['tag'], 'Viagens')
        self.taguear('b', 'Empresa')
        alvo = self.identidade('b')['tagId']
        with banco.connect() as conn:
            conn.execute('CREATE TABLE fixas_contas (forma_pagamento TEXT)')
            conn.execute('INSERT INTO fixas_contas VALUES (?)', (tag,))
        cartoes.renomear_tag(tag, {'nome': 'Empresa'})
        self.assertEqual(self.identidade('a')['tagId'], alvo)
        with banco.connect() as conn:
            self.assertEqual(conn.execute('SELECT forma_pagamento FROM fixas_contas').fetchone()[0], alvo)

    def test_salvar_em_lote_e_atomico(self):
        a = self.adicionar('a')
        with self.assertRaises(ValueError):
            cartoes.salvar({'cartoes': [{'cartaoId': a['cartaoId'], 'tag': 'Nova'}, {'cartaoId': 'inexistente', 'tag': 'Falha'}]})
        self.assertEqual(self.identidade('a')['tag'], '')
        self.assertEqual(cartoes.payload()['tags'], [])

    def test_nome_tag_nao_e_id_nem_html(self):
        self.adicionar('a')
        self.taguear('a', '<Viagens & família>')
        i = self.identidade('a')
        self.assertTrue(i['grupo'].startswith('tag:'))
        self.assertEqual(i['nomeExibicao'], '<Viagens & família>')

    def test_catalogo_vazio(self):
        self.assertEqual(cartoes.payload()['cartoes'], [])
        self.assertEqual(self.colunas(), [])

    def test_rotulo_automatico_legado_nao_vira_tag(self):
        with banco.connect() as conn:
            conn.execute("INSERT INTO pluggy_itens VALUES ('conexao','Fornecedor','2026-09-01')")
            conn.execute(
                "INSERT INTO pluggy_contas (conta_id,item_id,tipo,subtipo,nome,numero,raw_json,importado_em) "
                "VALUES ('a','conexao','CREDIT','CREDIT_CARD','PLATINUM PRIME DUO','8113',?,'2026-09-01')",
                (json.dumps({'creditData': {'brand': 'MASTERCARD'}}),),
            )
            conn.execute("CREATE TABLE cartoes_identidade (conta_id TEXT PRIMARY KEY, apelido TEXT, grupo TEXT, cor TEXT, ordem INTEGER, atualizado_em TEXT)")
            conn.execute("INSERT INTO cartoes_identidade VALUES ('a','INTER','INTER','#4a95ea',0,'2026-01-01')")
        identidade = self.identidade('a')
        self.assertEqual(identidade['nomeExibicao'], 'MASTERCARD 8113')
        self.assertEqual(identidade['tag'], '')

    def test_contas_bancarias_de_mesmo_numero_mantem_produtos_distintos(self):
        base = {'item_id': 'item', 'subtipo': 'CHECKING_ACCOUNT',
                'titular': 'Pessoa', 'numero': '123-4'}
        banking = pluggy_extrato._chave_conta({**base, 'conta_id': 'a', 'nome': 'BTG Banking'}, 'MeuPluggy')
        investimentos = pluggy_extrato._chave_conta({**base, 'conta_id': 'b', 'nome': 'BTG Investimentos'}, 'MeuPluggy')
        self.assertNotEqual(banking, investimentos)


if __name__ == '__main__':
    unittest.main()
