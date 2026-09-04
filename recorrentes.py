"""Detecta gastos que se repetem todo mes e sugere cadastro em Contas Fixas.

Nao projeta nada por conta propria: so sugere. Quem decide se aquilo e uma
conta fixa e a pessoa, no painel de sugestoes.

Por que a classificacao importa mais que a deteccao
---------------------------------------------------
Achar repeticao e facil -- agrupar por descricao resolve. O problema e que
"repete todo mes" tem tres causas que pedem tratamento OPOSTO, e tratar as
tres como conta fixa infla o previsto sem ninguem perceber:

- PARCELAMENTO no cartao (parcela_total > 1): repete porque tem parcela, e
  acaba. A tira de evolucao JA projeta isso via cartoes_payload, entao
  cadastrar como conta fixa conta o mesmo gasto duas vezes.
- ENCERRADO: repetiu por meses e parou. Projetar e inventar gasto.
- RECORRENTE de verdade: repete, sem fim previsto, e aconteceu agora. Esse
  e o unico que vale sugerir.

Medido nos dados reais no momento em que isto foi escrito: das 16 repeticoes
estaveis, 5 eram parcelamento (R$ 648/mes) e 6 tinham parado (R$ 630/mes).
Sugerir as 16 teria criado R$ 1.278/mes de despesa fantasma.
"""

from __future__ import annotations

import collections
import re
import sqlite3
import statistics
from datetime import datetime
from typing import Any

import extrato_camada as cam
import pluggy_extrato as px

# Uma repeticao precisa aparecer em pelo menos isto para ser sugerida. Menos
# que isso pega coincidencia (duas compras no mesmo lugar em meses seguidos).
MESES_MINIMOS = 4
# Janela de analise. 12 meses da estabilidade sem carregar historico velho
# demais, que traria assinaturas ja canceladas.
JANELA_MESES = 12
# Acima disso o valor varia demais para virar "valor previsto" -- e gasto
# frequente no mesmo lugar (supermercado, restaurante), nao conta fixa.
CV_MAXIMO = 25.0
# Sem aparecer no mes corrente nem no anterior, trata como encerrado. Um mes
# de tolerancia basta (a fatura do mes corrente ainda entra) e evita
# ressuscitar assinatura cancelada -- na pratica foi isso que separou as duas
# descricoes da mesma Netflix, quando o lojista trocou de descritor.
MESES_TOLERANCIA = 1

# Regularidade = meses com lancamento / meses entre o primeiro e o ultimo.
# Assinatura da ~1.0 (todo mes); comer 3x no mesmo restaurante e voltar meio
# ano depois da ~0.4. E o que separa recorrencia de coincidencia -- contar
# quantos meses apareceu, sozinho, nao separa.
REGULARIDADE_ALTA = 0.8
REGULARIDADE_MEDIA = 0.6

SCHEMA = """
-- Sugestao recusada nao volta a aparecer. Sem isto o painel vira ruido: a
-- mesma coisa que a pessoa ja decidiu que nao e conta fixa reaparece todo
-- mes, e ela para de olhar o painel.
CREATE TABLE IF NOT EXISTS recorrentes_ignorados (
  chave TEXT PRIMARY KEY,
  descricao TEXT NOT NULL DEFAULT '',
  ignorado_em TEXT NOT NULL
);
"""

_schema_pronto = False


def _abrir() -> sqlite3.Connection:
    global _schema_pronto
    conn = cam.conectar()
    conn.row_factory = sqlite3.Row
    px.garantir_tabelas(conn)
    cam.garantir_camada(conn)
    px.garantir_extrato_materializado(conn)
    if not _schema_pronto:
        conn.executescript(SCHEMA)
        conn.commit()
        _schema_pronto = True
    return conn


def _chave(descricao: str) -> str:
    """Descricao reduzida ao que identifica o lojista.

    Tira contador de parcela ("3/12") e numeros longos (id de pedido, doc),
    que mudam a cada cobranca e fariam a mesma assinatura virar N grupos --
    o mesmo problema que a projecao de parcelas do cartao ja teve.
    """
    texto = cam.normalizar(descricao)
    texto = re.sub(r"\d+\s*/\s*\d+", " ", texto)
    texto = re.sub(r"\b\d{2,}\b", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _termo_sugerido(chave: str) -> str:
    """Trecho para o vinculo CONTEM da conta fixa.

    Usa as primeiras palavras com 3+ letras: o comeco da descricao e a parte
    estavel (nome do lojista), o fim carrega cidade/pais que variam. Termo
    curto demais casaria com transacao alheia, longo demais para de casar na
    primeira variacao do lojista.
    """
    palavras = [p for p in chave.split() if len(p) >= 3][:3]
    return " ".join(palavras).upper()


def _mes_atual() -> str:
    return datetime.now().strftime("%Y-%m")


def _janela(meses: int) -> tuple[str, str]:
    """(primeiro, ultimo) mes da janela, terminando no mes corrente."""
    atual = _mes_atual()
    serial = int(atual[:4]) * 12 + int(atual[5:7]) - 1
    inicio = serial - (meses - 1)
    return f"{inicio // 12:04d}-{inicio % 12 + 1:02d}", atual


def sugestoes_payload(janela: int = JANELA_MESES) -> dict[str, Any]:
    """Recorrentes que valem cadastrar, mais o resumo do que foi descartado.

    O descartado vai no payload de proposito: sem ele o painel diria "achei 4"
    e a pessoa nao teria como saber que outros 11 foram vistos e recusados por
    um motivo -- que e justamente a parte em que se confia ou nao no detector.
    """
    mes_de, mes_ate = _janela(janela)
    with _abrir() as conn:
        ativas = px.contas_ativas(conn)
        marcadores = ", ".join("?" for _ in ativas) or "NULL"
        linhas = conn.execute(
            f"""
            SELECT e.descricao, e.valor, e.data, e.competencia_fatura AS mes,
                   e.parcela_total, e.categoria_id, e.conta_id,
                   c.subtipo AS conta_subtipo
            FROM extrato_efetivo_cache e
            JOIN pluggy_contas c ON c.conta_id = e.conta_id
            WHERE e.conta_id IN ({marcadores})
              AND e.tipo = 'DEBIT' AND e.incluida = 1
              AND e.competencia_fatura BETWEEN ? AND ?
            """,
            [*sorted(ativas), mes_de, mes_ate],
        ).fetchall()

        # Contas fixas ativas, para saber o que já está coberto. Precisa do
        # nome E do termo: uma conta pode estar cadastrada (nome igual ao do
        # gasto) com um termo que não pega a transação -- foi o que aconteceu
        # com duas contas cujo termo aponta para o nome do recebedor no
        # extrato, não para o nome da conta. Olhar só o termo fazia elas
        # voltarem como "nova", e
        # aceitar a sugestão criaria conta fixa duplicada.
        fixas_ativas = [
            {"id": l["id"], "nome": l["nome"],
             "nome_norm": cam.normalizar(l["nome"]),
             "termo": l["termo"], "termo_norm": cam.normalizar(l["termo"]),
             "valor": float(l["valor_previsto"] or 0)}
            for l in conn.execute(
                "SELECT id, nome, termo, valor_previsto FROM fixas_contas WHERE ativo = 1"
            )
        ]
        ignorados = {
            l["chave"] for l in conn.execute("SELECT chave FROM recorrentes_ignorados")
        }
        categorias = {
            l["id"]: {"id": l["id"], "nome": l["nome"], "cor": l["cor"],
                      "emoji": l["emoji"] or ""}
            for l in conn.execute("SELECT id, nome, cor, emoji FROM extrato_categorias")
        }

    grupos: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    for linha in linhas:
        grupos[_chave(linha["descricao"])].append(linha)

    limite_ativo = _recuar(_mes_atual(), MESES_TOLERANCIA)
    sugestoes: list[dict[str, Any]] = []
    vinculos_quebrados: list[dict[str, Any]] = []
    descartados = collections.Counter()

    def _cobertura(chave: str) -> tuple[dict | None, dict | None]:
        """(fixa cujo termo pega, fixa cujo nome bate mas o termo nao pega).

        Nome curto casaria com qualquer coisa ("Mae" dentro de "maetra..."),
        entao substring so vale de 6 letras pra cima; abaixo disso exige
        igualdade.
        """
        pelo_termo = next(
            (f for f in fixas_ativas if f["termo_norm"] and f["termo_norm"] in chave),
            None,
        )
        if pelo_termo:
            return pelo_termo, None
        pelo_nome = next(
            (f for f in fixas_ativas
             if f["nome_norm"] and (
                 f["nome_norm"] == chave
                 or (len(f["nome_norm"]) >= 6 and f["nome_norm"] in chave))),
            None,
        )
        return None, pelo_nome

    for chave, itens in grupos.items():
        meses = sorted({i["mes"] for i in itens})
        if len(meses) < MESES_MINIMOS:
            continue          # não é repetição, nem entra na contagem
        por_mes = collections.Counter(i["mes"] for i in itens)
        if max(por_mes.values()) > 1:
            descartados["frequente"] += 1
            continue

        valores = [abs(float(i["valor"] or 0)) for i in itens]
        media = statistics.mean(valores)
        cv = (statistics.pstdev(valores) / media * 100) if media else 0.0
        if cv >= CV_MAXIMO:
            descartados["valorInstavel"] += 1
            continue
        if meses[-1] < limite_ativo:
            descartados["encerrado"] += 1
            continue
        if any((i["parcela_total"] or 0) > 1 for i in itens):
            descartados["parcelamento"] += 1
            continue
        if chave in ignorados:
            descartados["ignorado"] += 1
            continue

        pelo_termo, pelo_nome = _cobertura(chave)
        if pelo_termo:
            descartados["jaCadastrado"] += 1
            continue

        # Categoria e dia mais frequentes viram o pré-preenchimento; a
        # descrição mais recente é a que o banco está mandando hoje.
        recente = max(itens, key=lambda i: i["data"])
        categoria_id = collections.Counter(
            i["categoria_id"] for i in itens if i["categoria_id"]
        ).most_common(1)
        dias = [int(str(i["data"])[8:10]) for i in itens if len(str(i["data"])) >= 10]
        # Regularidade em vez de corte seco: o irregular continua aparecendo,
        # só marcado. Esconder custaria caro num caso real -- a assinatura que
        # troca de descritor fica irregular nas duas metades, e sumir com ela
        # seria pior que mostrar com aviso.
        vao = _serial(meses[-1]) - _serial(meses[0]) + 1
        regularidade = len(meses) / vao if vao else 1.0
        if regularidade >= REGULARIDADE_ALTA:
            confianca = "alta"
        elif regularidade >= REGULARIDADE_MEDIA:
            confianca = "media"
        else:
            confianca = "baixa"
        sugestao = {
            "chave": chave,
            "descricao": recente["descricao"],
            "termoSugerido": _termo_sugerido(chave),
            "valorMedio": round(media, 2),
            "valorUltimo": round(abs(float(recente["valor"] or 0)), 2),
            "variacao": round(cv, 1),
            "meses": len(meses),
            "mesesJanela": janela,
            "primeiroMes": meses[0],
            "ultimoMes": meses[-1],
            "regularidade": round(regularidade, 2),
            "confianca": confianca,
            "diaTipico": statistics.mode(dias) if dias else None,
            "categoria": categorias.get(
                categoria_id[0][0] if categoria_id else None
            ),
            "noCartao": recente["conta_subtipo"] == "CREDIT_CARD",
        }
        # Já cadastrada pelo nome, mas o termo de vínculo não pega o gasto:
        # a conta existe e fica Pendente todo mês, porque nada casa com ela.
        # Aqui a ação certa é corrigir o termo, não cadastrar de novo.
        if pelo_nome:
            vinculos_quebrados.append({
                **sugestao,
                "fixaId": pelo_nome["id"],
                "fixaNome": pelo_nome["nome"],
                "termoAtual": pelo_nome["termo"],
                "valorCadastrado": pelo_nome["valor"],
            })
        else:
            sugestoes.append(sugestao)

    ordem_confianca = {"alta": 0, "media": 1, "baixa": 2}
    chave_ordem = lambda s: (ordem_confianca[s["confianca"]], -s["valorMedio"])
    sugestoes.sort(key=chave_ordem)
    vinculos_quebrados.sort(key=chave_ordem)
    return {
        "janela": {"de": mes_de, "ate": mes_ate, "meses": janela},
        "sugestoes": sugestoes,
        "vinculosQuebrados": vinculos_quebrados,
        "totalMensal": round(sum(s["valorMedio"] for s in sugestoes), 2),
        "descartados": dict(descartados),
    }


def corrigir_termo(fixa_id: str, termo: str) -> dict[str, Any]:
    """Troca só o termo de vínculo de uma conta fixa já cadastrada.

    Existe separado de fixas.atualizar porque aquele valida o cadastro
    inteiro -- para mudar um campo o cliente teria que reenviar todos, e um
    campo esquecido apagaria configuração.
    """
    termo = str(termo or "").strip()
    if not termo:
        raise ValueError("Informe o termo de vínculo.")
    with _abrir() as conn:
        cursor = conn.execute(
            "UPDATE fixas_contas SET termo = ? WHERE id = ?", (termo, fixa_id))
        if not cursor.rowcount:
            raise ValueError(f"Conta fixa {fixa_id} não encontrada.")
        # A lista de termos é o que o casamento lê hoje: gravar só a coluna
        # antiga deixaria a correção sem efeito nenhum. "Corrigir" substitui a
        # lista -- o termo estava errado, não faltando.
        conn.execute("DELETE FROM fixas_termos WHERE fixa_id = ?", (fixa_id,))
        conn.execute(
            "INSERT INTO fixas_termos (fixa_id, ordem, termo) VALUES (?, 0, ?)",
            (fixa_id, termo))
        conn.commit()
    return {"ok": True, "fixaId": fixa_id, "termo": termo, **sugestoes_payload()}


def _serial(mes_ref: str) -> int:
    return int(mes_ref[:4]) * 12 + int(mes_ref[5:7])


def _recuar(mes_ref: str, meses: int) -> str:
    serial = _serial(mes_ref) - 1 - meses
    return f"{serial // 12:04d}-{serial % 12 + 1:02d}"


def ignorar(chave: str, descricao: str = "") -> dict[str, Any]:
    """Marca uma sugestão como recusada para ela não voltar."""
    chave = str(chave or "").strip()
    if not chave:
        raise ValueError("Informe a chave da sugestão.")
    with _abrir() as conn:
        conn.execute(
            "INSERT INTO recorrentes_ignorados (chave, descricao, ignorado_em) "
            "VALUES (?, ?, ?) ON CONFLICT(chave) DO UPDATE SET "
            "ignorado_em = excluded.ignorado_em",
            (chave, str(descricao or ""), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    return {"ok": True, "chave": chave, **sugestoes_payload()}


def reconsiderar(chave: str) -> dict[str, Any]:
    """Desfaz um "ignorar" -- a sugestão volta a aparecer."""
    with _abrir() as conn:
        conn.execute("DELETE FROM recorrentes_ignorados WHERE chave = ?", (chave,))
        conn.commit()
    return {"ok": True, "chave": chave, **sugestoes_payload()}


def ignorados_payload() -> dict[str, Any]:
    with _abrir() as conn:
        return {
            "ignorados": [
                {"chave": l["chave"], "descricao": l["descricao"],
                 "ignoradoEm": l["ignorado_em"]}
                for l in conn.execute(
                    "SELECT * FROM recorrentes_ignorados ORDER BY ignorado_em DESC"
                )
            ]
        }
