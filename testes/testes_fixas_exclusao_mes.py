"""Excluir uma conta fixa de UM mês não pode derrubá-la dos outros.

Banco temporário de verdade (não em memória): `remover_do_mes` e `mes_payload`
abrem conexões próprias, e o objetivo aqui é justamente provar que o que uma
grava a outra enxerga.
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

import banco
import fixas
import extrato_camada as cam
import importar_pluggy


class ExclusaoPorMes(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.original = banco.DATABASE_PATH
        banco.DATABASE_PATH = pathlib.Path(self.pasta.name) / "teste.db"
        # Os dois modulos lembram que ja prepararam o banco; trocar o arquivo
        # no meio do processo torna essa memoria mentira.
        fixas._pronto = False
        cam._camada_pronta = False
        banco.ensure_database()
        # A camada do extrato so abre com o esquema do Pluggy no lugar; nada
        # dele entra nos casos, que so falam de contas fixas.
        with banco.connect() as base:
            base.executescript(importar_pluggy.SCHEMA_PLUGGY)
            base.commit()
        with fixas._abrir() as conn:
            conn.execute(
                "INSERT INTO fixas_contas (id, nome, valor_previsto, criado_em) "
                "VALUES ('fx1', 'Aluguel', 1000, '2026-01-01')")
            conn.execute(
                "INSERT INTO fixas_descontos (id, fixa_id, mes_ref, descricao, valor) "
                "VALUES ('dc1', 'fx1', '2026-09', 'Metade', 500)")
            conn.execute(
                "INSERT INTO fixas_descontos (id, fixa_id, mes_ref, descricao, valor) "
                "VALUES ('dc2', 'fx1', '2026-10', 'Metade', 500)")
            conn.commit()

    def tearDown(self):
        banco.DATABASE_PATH = self.original
        # Deixa a memoria de preparacao suja para quem vier depois: o banco de
        # verdade ainda nao foi visto por estes modulos nesta execucao.
        fixas._pronto = False
        cam._camada_pronta = False
        self.pasta.cleanup()

    def ids(self, mes):
        return [i["id"] for i in fixas.mes_payload(mes)["itens"]]

    def test_some_do_mes_e_fica_nos_outros(self):
        fixas.remover_do_mes("fx1", "2026-09")
        self.assertEqual(self.ids("2026-09"), [])
        self.assertEqual(self.ids("2026-08"), ["fx1"])
        self.assertEqual(self.ids("2026-10"), ["fx1"])

    def test_cadastro_e_subdescontos_dos_outros_meses_sobrevivem(self):
        fixas.remover_do_mes("fx1", "2026-09")
        with fixas._abrir() as conn:
            self.assertTrue(conn.execute(
                "SELECT 1 FROM fixas_contas WHERE id = 'fx1'").fetchone())
            restantes = [l[0] for l in conn.execute(
                "SELECT id FROM fixas_descontos WHERE fixa_id = 'fx1'")]
        self.assertEqual(restantes, ["dc2"], "o desconto do mês excluído vai junto; o do outro fica")

    def test_remover_cadastro_continua_tirando_de_todos(self):
        fixas.remover("fx1")
        self.assertEqual(self.ids("2026-09"), [])
        self.assertEqual(self.ids("2026-10"), [])

    def test_mes_invalido_e_conta_inexistente_falham(self):
        with self.assertRaises(ValueError):
            fixas.remover_do_mes("fx1", "setembro")
        with self.assertRaises(ValueError):
            fixas.remover_do_mes("nao-existe", "2026-09")


if __name__ == "__main__":
    unittest.main()
