"""Cadastro e calendário das recorrências aceitas, independente do detector."""
from __future__ import annotations

import json
import collections
import math
import re
import statistics
import uuid
from calendar import monthrange
from datetime import datetime

import ciclos
import extrato_camada as cam
import pluggy_extrato as px
import recorrentes as rec


# Uma previsão recorrente tem dois comportamentos possíveis, e é só isso que
# separa "recorrência" de "hábito":
#   cobranca -> cobrança única; a transação real SUBSTITUI a previsão do mês.
#   reserva  -> orçamento do ciclo; as compras reais CONSOMEM o previsto.
# O campo `tipo: "habito"` é a grafia antiga do segundo caso. Ele continua
# sendo gravado junto, espelhado, para que qualquer leitura antiga continue
# valendo -- e para que a conversão não precise de migração em massa.
COMPORTAMENTOS = ("cobranca", "reserva")


# Um termo curto casa com qualquer coisa ("ar" dentro de "farmacia"). Três
# letras é o mínimo que ainda distingue algo.
TERMO_MINIMO = 3
TERMOS_MAXIMO = 12


def _descendentes(conn, categoria_id):
    """A categoria escolhida e todas as filhas dela.

    Quem escolhe "Automotivo" quer o automotivo inteiro, inclusive
    "Postos de combustível", que é filha dela.
    """
    if not categoria_id:
        return set()
    pais = {l["id"]: l["pai_id"] for l in conn.execute(
        "SELECT id, pai_id FROM extrato_categorias")}
    if categoria_id not in pais:
        return set()
    familia = {categoria_id}
    # A árvore tem dois níveis hoje; o laço não depende disso.
    for _ in range(len(pais)):
        novos = {i for i, pai in pais.items() if pai in familia} - familia
        if not novos:
            break
        familia |= novos
    return familia


def termos_de(s):
    """Termos de busca do cadastro: normalizados, sem repetição e sem os curtos."""
    saida = []
    for termo in s.get("termos") or []:
        limpo = cam.normalizar(str(termo or ""))
        if len(limpo) >= TERMO_MINIMO and limpo not in saida:
            saida.append(limpo)
    return saida


def comportamento_de(s):
    comportamento = s.get("comportamento")
    if comportamento in COMPORTAMENTOS:
        return comportamento
    return "reserva" if s.get("tipo") == "habito" else "cobranca"


def _aplicar_comportamento(s, comportamento):
    """Grava o comportamento e o espelho histórico, sempre juntos."""
    if comportamento not in COMPORTAMENTOS:
        raise ValueError("Escolha cobrança única ou reserva do mês.")
    s["comportamento"] = comportamento
    if comportamento == "reserva":
        s["tipo"] = "habito"
    else:
        s.pop("tipo", None)
    return s


def contas_pagamento(conn):
    ativas = px.contas_ativas(conn)
    nomes = px.mapa_apelidos(conn, ativas)
    import cartoes
    identidades = cartoes.identidades(conn)
    resultado = []
    for conta in conn.execute("SELECT conta_id,subtipo,numero FROM pluggy_contas"):
        cid = conta["conta_id"]
        if cid not in ativas:
            continue
        cartao = conta["subtipo"] == "CREDIT_CARD"
        nome = nomes.get(cid, "Conta")
        if cartao:
            identidade = identidades.get(cid, {})
            nome = identidade.get("nomeOriginal") or nome
            tag = identidade.get("tag")
            if tag and tag != nome:
                nome = f"{tag} · {nome}"
        resultado.append({"id": cid, "nome": nome, "noCartao": cartao})
    return sorted(resultado, key=lambda c: (not c["noCartao"], c["nome"].casefold(), c["id"]))


def _normalizar(conn, dados, anterior=None):
    anterior = anterior or {}
    s = {**anterior, **{k: dados[k] for k in (
        "descricao", "lojista", "contaId", "valorPrevisto", "modoValor", "diaTipico",
        "comportamento", "termos", "categoriaId",
    ) if k in dados}}
    _aplicar_comportamento(s, comportamento_de(s))
    s["descricao"] = str(s.get("descricao") or "").strip()
    s["lojista"] = rec._chave(str(s.get("lojista") or ""))
    if not 2 <= len(s["descricao"]) <= 160:
        raise ValueError("Informe um nome de 2 a 160 caracteres.")
    s["termos"] = termos_de(s)[:TERMOS_MAXIMO]
    categoria = str(s.get("categoriaId") or "").strip()
    if categoria and not conn.execute(
            "SELECT 1 FROM extrato_categorias WHERE id=?", (categoria,)).fetchone():
        raise ValueError("Escolha uma categoria existente.")
    s["categoriaId"] = categoria or None

    # Um cadastro precisa de pelo menos um jeito de reconhecer a cobrança. A
    # identificação do estabelecimento deixou de ser o único: termo e
    # categoria também servem, e uma reserva de categoria pode não ter
    # estabelecimento nenhum.
    if not s["lojista"] and not s["termos"] and not s["categoriaId"]:
        raise ValueError(
            "Informe a identificação no extrato, um termo ou uma categoria.")
    if s["lojista"] and not 3 <= len(s["lojista"]) <= 240:
        raise ValueError("Informe a identificação do estabelecimento no extrato (3 a 240 caracteres).")
    cid = str(s.get("contaId") or "")
    conta = conn.execute("SELECT subtipo FROM pluggy_contas WHERE conta_id=?", (cid,)).fetchone()
    if not conta or cid not in px.contas_ativas(conn):
        raise ValueError("Escolha uma conta ou cartão ativo para o pagamento.")
    s["contaId"], s["noCartao"] = cid, conta["subtipo"] == "CREDIT_CARD"
    try:
        valor = float(s.get("valorPrevisto", s.get("valorUltimo", 0)))
    except (ValueError, TypeError):
        raise ValueError("Informe um valor previsto maior que zero.") from None
    if not math.isfinite(valor) or round(valor, 2) <= 0:
        raise ValueError("Informe um valor previsto maior que zero.")
    s["valorPrevisto"] = round(valor, 2)
    modo = s.get("modoValor", "ultimo")
    if modo not in ("ultimo", "fixo", "media"):
        raise ValueError("Escolha último valor cobrado, valor definido ou média dos ciclos.")
    # "media" já foi exclusiva de hábito. Ela é uma forma de estimar o valor,
    # não um comportamento: conta de luz é cobrança única e varia todo mês.
    s["modoValor"] = modo
    try:
        dia = float(s.get("diaTipico") or 0)
        if not math.isfinite(dia) or not dia.is_integer() or not 1 <= dia <= 31:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Informe um dia da cobrança entre 1 e 31.") from None
    s["diaTipico"] = int(dia)
    if anterior and s["lojista"] and s["lojista"] != anterior.get("lojista"):
        # Faixa inferida pertence ao estabelecimento anterior.
        s["faixaValor"] = None
        s["transacaoBaseId"] = ""
    s.setdefault("valorUltimo", s["valorPrevisto"])
    s.setdefault("transacaoBaseId", "")
    s.setdefault("faixaValor", None)
    return s


def _verificar_duplicata(conn, s):
    for linha in conn.execute("SELECT chave,dados FROM recorrentes_previsoes"):
        outro = json.loads(linha["dados"])
        if linha["chave"] == s.get("chave"):
            continue
        if outro.get("contaId") != s["contaId"]:
            continue
        mesma_categoria = bool(s.get("categoriaId")) and outro.get("categoriaId") == s.get("categoriaId")
        if not mesma_categoria and (not s["lojista"] or outro.get("lojista") != s["lojista"]):
            continue
        if mesma_categoria:
            raise ValueError("Já existe uma previsão dessa categoria neste pagamento. Edite o cadastro existente.")
        a, b = s.get("faixaValor"), outro.get("faixaValor")
        if a and b and (a["max"] < b["min"] or b["max"] < a["min"]):
            continue
        raise ValueError("Já existe uma recorrência desse estabelecimento neste pagamento. Edite ou retome o cadastro existente.")


def _gravar(conn, s, ativa=True):
    _verificar_duplicata(conn, s)
    conn.execute(
        "INSERT INTO recorrentes_previsoes (chave,dados,ativo,atualizado_em) VALUES (?,?,?,?) "
        "ON CONFLICT(chave) DO UPDATE SET dados=excluded.dados,ativo=excluded.ativo,atualizado_em=excluded.atualizado_em",
        (s["chave"], json.dumps(s, ensure_ascii=False), int(ativa), datetime.now().isoformat()))


def salvar(chave, ativa=True):
    with rec._abrir() as conn:
        linha = conn.execute("SELECT * FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if linha:
            if ativa:
                s = json.loads(linha["dados"])
                _verificar_duplicata(conn, s)
            conn.execute("UPDATE recorrentes_previsoes SET ativo=?,atualizado_em=? WHERE chave=?",
                         (int(ativa), datetime.now().isoformat(), chave))
    if not linha:
        sugestao = next((s for s in rec.sugestoes_payload()["sugestoes"] if s["chave"] == chave), None)
        if not sugestao or not ativa:
            raise ValueError("Sugestão não encontrada. Atualize a lista.")
        with rec._abrir() as conn:
            _gravar(conn, _normalizar(conn, {}, sugestao))
    return {"ok": True, **rec.sugestoes_payload()}


def projetar_habito(chave):
    """Inclui um hábito de cartão na projeção, como reserva que se consome.

    Diferente de uma assinatura, cada compra não elimina a previsão: a soma
    das compras do ciclo reduz a reserva. Assim, R$ 230 de posto já lançados
    diminuem uma referência de R$ 250 para R$ 20, sem contar R$ 250 a mais.
    """
    dados = rec.sugestoes_payload()
    habito = next((h for h in dados.get("habitos", [])
                   + dados.get("categoriasHabito", []) if h["chave"] == chave), None)
    if not habito:
        # Uma sugestão de cobrança também pode ser ativada como reserva: quem
        # olha pode discordar da classificação, e a evidência é a mesma série.
        sugestao = next((s for s in dados.get("sugestoes", []) if s["chave"] == chave), None)
        if not sugestao:
            raise ValueError("Achado não encontrado. Atualize a lista.")
        habito = {**sugestao, "mediaMensal": sugestao["valorMedio"],
                  "ultimaCobranca": {"valor": sugestao["valorUltimo"]}}
    chave_previsao = "habito:" + chave
    with rec._abrir() as conn:
        existente = conn.execute("SELECT 1 FROM recorrentes_previsoes WHERE chave=?", (chave_previsao,)).fetchone()
        if existente:
            conn.execute("UPDATE recorrentes_previsoes SET ativo=1,atualizado_em=? WHERE chave=?",
                         (datetime.now().isoformat(), chave_previsao))
        else:
            s = {
                "chave": chave_previsao, "comportamento": "reserva", "tipo": "habito",
                "descricao": habito["descricao"], "lojista": habito["lojista"],
                "contaId": habito["contaId"], "noCartao": bool(habito.get("noCartao")),
                # Reserva de categoria não tem estabelecimento: o crivo é a
                # categoria, e é ela que pega qualquer posto novo.
                "categoriaId": habito.get("categoriaId") if not habito.get("lojista") else None,
                "termos": [],
                "valorPrevisto": habito["mediaMensal"],
                "valorUltimo": habito["ultimaCobranca"]["valor"],
                "modoValor": "media", "diaTipico": 1,
                "transacaoBaseId": "", "faixaValor": None,
            }
            _gravar(conn, s)
    return {"ok": True, "chave": chave_previsao, **rec.sugestoes_payload()}


def converter(chave, comportamento):
    """Troca o comportamento de um cadastro, preservando a identidade.

    A chave NUNCA muda -- nem o prefixo "habito:" de quem nasceu hábito. Ela é
    a PK da tabela, é o que `excluir` grava em recorrentes_ignorados e é o
    `compraId` que as projeções, as contas fixas e as exclusões manuais de
    parcela enxergam. Renomear quebraria os três de uma vez; o prefixo é
    histórico, não semântico.
    """
    with rec._abrir() as conn:
        linha = conn.execute("SELECT * FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if not linha:
            raise ValueError("Recorrência não encontrada. Atualize a lista.")
        s = json.loads(linha["dados"])
        s["chave"] = chave
        _aplicar_comportamento(s, comportamento)
        _gravar(conn, s, bool(linha["ativo"]))
    return {"ok": True, **rec.sugestoes_payload()}


def previa_conversao(chave, comportamento):
    """Quanto a previsão do mês corrente muda se o comportamento trocar.

    Converter no meio do ciclo mexe no número na hora: reserva -> cobrança
    apaga a previsão inteira da competência que já teve compra, e o valor
    previsto da fatura cai. Isto calcula a diferença sem gravar nada.
    """
    with rec._abrir() as conn:
        linha = conn.execute("SELECT dados,ativo FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if not linha:
            raise ValueError("Recorrência não encontrada. Atualize a lista.")
        atual = rec._mes_atual()
        ano = int(atual[:4])
        fechadas = {(r[0], r[1]) for r in conn.execute(
            "SELECT conta_id,competencia FROM pluggy_faturas")}
        subtipos = {r[0]: r[1] for r in conn.execute(
            "SELECT conta_id,subtipo FROM pluggy_contas")}

        def total(s):
            s = {**s, "chave": chave,
                 "noCartao": subtipos.get(s["contaId"]) == "CREDIT_CARD"}
            return round(sum(
                i["valor"] for i in _projetar_cadastro(conn, s, ano, fechadas)
                if f"{ano}-{i['mes']:02d}" == atual), 2)

        original = json.loads(linha["dados"])
        antes = total(original)
        depois = total(_aplicar_comportamento(dict(original), comportamento))
    return {"mes": atual, "antes": antes, "depois": depois,
            "diferenca": round(depois - antes, 2)}


def _exemplos(reais, quantos=6):
    vistos, saida = set(), []
    for r in reais:
        chave = rec._chave(r["descricao"])
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append({
            "data": str(r["data"])[:10], "descricao": r["descricao"],
            "valor": round(abs(float(r["valor"] or 0)), 2),
            "criterio": r.get("criterio"),
        })
        if len(saida) >= quantos:
            break
    return saida


def testar(dados):
    """Quantos lançamentos cada critério pega, antes de salvar.

    Um termo de três letras pode pegar meio extrato sem que a pessoa perceba.
    Mostrar a conta e alguns exemplos é mais honesto do que avisar depois.
    """
    with rec._abrir() as conn:
        s = {
            "chave": str(dados.get("chave") or ""),
            "contaId": str(dados.get("contaId") or ""),
            "lojista": rec._chave(str(dados.get("lojista") or "")),
            "termos": dados.get("termos") or [],
            "categoriaId": str(dados.get("categoriaId") or "") or None,
        }
        if not s["contaId"]:
            raise ValueError("Escolha a conta ou o cartão do pagamento.")
        reais = _reais(conn, s, criterios=True)
        hoje = datetime.now().date().isoformat()
        passados = [r for r in reais if str(r["data"])[:10] <= hoje]
        totais = _totais_por_competencia(conn, {**s, "noCartao": bool(
            conn.execute("SELECT 1 FROM pluggy_contas WHERE conta_id=? AND subtipo='CREDIT_CARD'",
                         (s["contaId"],)).fetchone())}, passados)
        com_gasto = sorted((v for v in totais.values() if v > 0), reverse=True)
        por_criterio = collections.Counter(r.get("criterio") for r in passados)
        return {
            "lancamentos": len(passados),
            "meses": len([v for v in totais.values() if v > 0]),
            "porCriterio": dict(por_criterio),
            # Mesma base que a reserva vai usar: média dos 3 ciclos com gasto.
            "mediaCiclos": round(sum(com_gasto[:3]) / len(com_gasto[:3]), 2) if com_gasto else 0.0,
            # Um exemplo por estabelecimento: repetir o mesmo nome três vezes
            # não mostra a abrangência do termo, que é a dúvida de quem lê.
            "exemplos": _exemplos(passados),
        }


def ativar(chave, comportamento=""):
    """Ativa um achado com o comportamento pedido, venha ele de que lista vier."""
    comportamento = comportamento or ""
    if chave.startswith("categoria:"):
        # Uma categoria inteira não é uma cobrança: são muitas compras, e a
        # única leitura que faz sentido é a reserva que elas consomem.
        return projetar_habito(chave)
    if comportamento == "reserva":
        return projetar_habito(chave)
    if comportamento == "cobranca":
        return salvar(chave, True)
    # Sem escolha explícita, vale a classificação do detector.
    dados = rec.sugestoes_payload()
    if any(h["chave"] == chave for h in dados.get("habitos", [])
           + dados.get("categoriasHabito", [])):
        return projetar_habito(chave)
    return salvar(chave, True)


def criar(dados):
    with rec._abrir() as conn:
        s = _normalizar(conn, dados)
        s["chave"] = "manual:" + uuid.uuid4().hex
        _gravar(conn, s)
    return {"ok": True, "chave": s["chave"], **rec.sugestoes_payload()}


def editar(chave, dados):
    with rec._abrir() as conn:
        linha = conn.execute("SELECT * FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if not linha:
            raise ValueError("Recorrência não encontrada. Atualize a lista.")
        anterior = json.loads(linha["dados"])
        s = _normalizar(conn, dados, anterior)
        if (s["contaId"] != anterior.get("contaId") and s["lojista"] == anterior.get("lojista")
                and s["modoValor"] == "ultimo"
                and s["valorPrevisto"] == anterior.get("valorPrevisto", anterior.get("valorUltimo"))):
            # O pagamento novo pode ainda não ter histórico. Levar o preço
            # atual evita voltar ao preço antigo no primeiro mês após a troca.
            limite = rec._mes_atual() + datetime.now().strftime("-%d")
            s["valorPrevisto"], _ = _valor(anterior, _reais(conn, anterior), limite)
            s["valorUltimo"] = s["valorPrevisto"]
        s["chave"] = chave
        _gravar(conn, s, bool(linha["ativo"]))
    return {"ok": True, **rec.sugestoes_payload()}


def excluir(chave):
    with rec._abrir() as conn:
        linha = conn.execute("SELECT dados FROM recorrentes_previsoes WHERE chave=?", (chave,)).fetchone()
        if not linha:
            raise ValueError("Recorrência não encontrada. Atualize a lista.")
        s = json.loads(linha["dados"])
        chaves = {chave}
        if not s.get("faixaValor"):
            chaves.add(s["contaId"] + "|" + s["lojista"])
        else:
            faixa = s["faixaValor"]
            chaves.add(f"{s['contaId']}|{s['lojista']}|faixa:{faixa['min']:.2f}:{faixa['max']:.2f}")
        conn.executemany(
            "INSERT OR REPLACE INTO recorrentes_ignorados (chave,descricao,ignorado_em) VALUES (?,?,?)",
            [(k, s["descricao"], datetime.now().isoformat()) for k in chaves])
        conn.execute("DELETE FROM recorrentes_previsoes WHERE chave=?", (chave,))
    return {"ok": True, **rec.sugestoes_payload()}


PARCELA_NO_TEXTO = r"\bparcela(?:s|mento)?\b|\b\d+\s*/\s*(?:[2-9]|[1-9]\d+)\b"


def _casa(s, linha, termos, familia):
    """Este lançamento pertence a este cadastro?

    Três crivos, em OU, do mais específico para o mais amplo:

    1. a identificação do estabelecimento, por igualdade sobre a forma
       normalizada -- é a regra original, e a única que nunca erra;
    2. qualquer um dos termos, por CONTÉM: "posto" pega "POSTO CENTRAL AVENIDA
       BRA" e também "posto shell";
    3. a categoria do lançamento, filhas incluídas -- é o que faz "qualquer
       posto que eu abastecer" funcionar sem cadastrar um posto por vez.
    """
    descricao = cam.normalizar(linha["descricao"])
    if s.get("lojista") and rec._chave(linha["descricao"]) == s["lojista"]:
        return "lojista"
    if any(termo in descricao for termo in termos):
        return "termo"
    if familia and linha["categoria_id"] in familia:
        return "categoria"
    return ""


def _reservados_por_outros(conn, s):
    """Lançamentos que outro cadastro ativo já reivindica por lojista ou termo.

    Sem isto, uma reserva de "Postos de combustível" e um cadastro do posto da
    esquina somariam o mesmo abastecimento nos dois, e a previsão da fatura
    ficaria com o dobro da gasolina. O mais específico ganha.
    """
    outros = []
    for linha in conn.execute(
            "SELECT chave, dados FROM recorrentes_previsoes WHERE ativo=1"):
        if linha["chave"] == s.get("chave"):
            continue
        outro = json.loads(linha["dados"])
        if outro.get("contaId") != s["contaId"]:
            continue
        termos = termos_de(outro)
        if outro.get("lojista") or termos:
            outros.append((outro, termos))
    if not outros:
        return set()

    reservados = set()
    for linha in conn.execute(
        "SELECT transacao_id,descricao,categoria_id FROM extrato_efetivo_cache "
        "WHERE conta_id=? AND tipo='DEBIT' AND incluida=1", (s["contaId"],)
    ):
        for outro, termos in outros:
            if _casa(outro, linha, termos, set()):
                reservados.add(linha["transacao_id"])
                break
    return reservados


def _reais(conn, s, criterios=False):
    faixa = s.get("faixaValor")
    termos = termos_de(s)
    familia = _descendentes(conn, s.get("categoriaId"))
    reservados = _reservados_por_outros(conn, s) if familia else set()

    saida = []
    for linha in conn.execute(
        "SELECT transacao_id,descricao,valor,data,competencia_fatura,categoria_id "
        "FROM extrato_efetivo_cache "
        "WHERE conta_id=? AND tipo='DEBIT' AND incluida=1 AND COALESCE(parcela_total,1)<=1 "
        "ORDER BY data DESC, transacao_id", (s["contaId"],)
    ):
        criterio = _casa(s, linha, termos, familia)
        if not criterio:
            continue
        # Só o casamento AMPLO cede: quem foi cadastrado pelo nome ou por um
        # termo continua contando, mesmo que outro cadastro também o pegue.
        if criterio == "categoria" and linha["transacao_id"] in reservados:
            continue
        if re.search(PARCELA_NO_TEXTO, cam.normalizar(linha["descricao"])):
            continue
        if faixa and not faixa["min"] <= abs(float(linha["valor"])) <= faixa["max"]:
            continue
        registro = dict(linha)
        if criterios:
            registro["criterio"] = criterio
        saida.append(registro)
    return saida



def _competencia_real(conn, s, real):
    competencia = str(real.get("competencia_fatura") or "")[:7]
    if s.get("noCartao") and len(competencia) == 7:
        return competencia
    if s.get("noCartao"):
        return ciclos.competencia_de(conn, s["contaId"], real["data"]) or str(real["data"])[:7]
    return str(real["data"])[:7]


def _totais_por_competencia(conn, s, reais):
    """Valor já lançado por ciclo; somente lançamentos até hoje contam."""
    hoje = datetime.now().date().isoformat()
    totais = collections.defaultdict(float)
    for real in reais:
        if str(real["data"])[:10] > hoje:
            continue
        totais[_competencia_real(conn, s, real)] += abs(float(real["valor"] or 0))
    return {competencia: round(valor, 2) for competencia, valor in totais.items()}


def _base_habito(s, totais, competencia):
    """Média dos três últimos ciclos com gasto anteriores à competência."""
    anteriores = [valor for mes, valor in sorted(totais.items(), reverse=True)
                  if mes < competencia and valor > 0][:3]
    if s.get("modoValor") == "media" and anteriores:
        return round(statistics.mean(anteriores), 2)
    return round(float(s.get("valorPrevisto") or 0), 2)


def _fixas_cobrem(conn, s, mes):
    import cartoes
    for f in conn.execute("SELECT * FROM fixas_contas WHERE ativo=1 AND incluir_calculos=1"):
        if (f["desde"] and mes < f["desde"]) or (f["ate"] and mes > f["ate"]):
            continue
        if f["conta_id"]:
            fontes = cartoes.resolver_contas(conn, f["conta_id"]) or {f["conta_id"]}
            if s["contaId"] not in fontes:
                continue
        termos = [r[0] for r in conn.execute("SELECT termo FROM fixas_termos WHERE fixa_id=?", (f["id"],))] or [f["termo"]]
        descricoes = [s["lojista"], cam.normalizar(s["descricao"])]
        if any(cam.normalizar(t) and any(cam.normalizar(t) in d for d in descricoes) for t in termos):
            return True
    return False


def _calendario(conn, s):
    atual = rec._mes_atual()
    fim = rec._recuar(atual, -11)
    dia = min(31, max(1, int(s.get("diaTipico") or 1)))
    datas = {}
    # Começar antes do horizonte cobre a fatura atual, cuja cobrança pode
    # ter ocorrido no mês anterior. O ciclo decide o destino, nunca o nome do banco.
    for deslocamento in range(-2, 12):
        mes = rec._recuar(atual, -deslocamento)
        ano, numero = map(int, mes.split("-"))
        data = f"{mes}-{min(dia, monthrange(ano, numero)[1]):02d}"
        competencia = mes
        if s["noCartao"]:
            competencia = ciclos.competencia_de(conn, s["contaId"], data) or rec._recuar(mes, -1)
        if atual <= competencia <= fim:
            datas[competencia] = data
    return datas


def _valor(s, reais, limite=None):
    base = next((r for r in reais if limite is None or r["data"][:10] <= limite), None)
    valor = s.get("valorPrevisto", s.get("valorUltimo", 0))
    if s.get("modoValor", "ultimo") == "ultimo" and base:
        valor = abs(float(base["valor"]))
    return round(float(valor), 2), base


def _projetados_habito(conn, s, ano, fechadas):
    reais = _reais(conn, s)
    totais = _totais_por_competencia(conn, s, reais)
    base_real = next((real for real in reais if str(real["data"])[:10] <= datetime.now().date().isoformat()), None)
    resultado = []
    for competencia, data in _calendario(conn, s).items():
        if int(competencia[:4]) != ano:
            continue
        # Fatura fechada é soberana -- mas só existe fatura em cartão.
        if s["noCartao"] and (s["contaId"], competencia) in fechadas:
            continue
        if _fixas_cobrem(conn, s, competencia):
            continue
        valor_base = _base_habito(s, totais, competencia)
        valor_lancado = totais.get(competencia, 0)
        restante = round(max(0, valor_base - valor_lancado), 2)
        if restante <= 0:
            continue
        resultado.append({
            "mes": int(competencia[5:]), "data": data, "contaId": s["contaId"],
            "transacaoBaseId": base_real["transacao_id"] if base_real else "",
            "compraId": "recorrente:" + s["chave"],
            "descricao": s["descricao"], "valor": restante,
            "valorBase": valor_base, "valorLancado": valor_lancado,
            "tipoPrevisao": "habito", "parcelaAtual": None, "parcelaTotal": None,
            "recorrente": True, "noCartao": s["noCartao"],
        })
    return resultado


def projetados(conn, ano):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='recorrentes_previsoes'").fetchone():
        return []
    linhas = conn.execute("SELECT chave,dados FROM recorrentes_previsoes WHERE ativo=1 ORDER BY chave").fetchall()
    if not linhas:
        return []
    conn.create_function("norm", 1, cam.normalizar, deterministic=True)
    cam.garantir_camada(conn)
    px.garantir_extrato_materializado(conn)
    ativas = px.contas_ativas(conn)
    subtipos = {r[0]: r[1] for r in conn.execute("SELECT conta_id,subtipo FROM pluggy_contas")}
    fechadas = {(r[0], r[1]) for r in conn.execute("SELECT conta_id,competencia FROM pluggy_faturas")}
    resultado = []
    for linha in linhas:
        s = json.loads(linha["dados"])
        s["chave"] = linha["chave"]
        if s["contaId"] not in ativas:
            continue
        s["noCartao"] = subtipos.get(s["contaId"]) == "CREDIT_CARD"
        resultado.extend(_projetar_cadastro(conn, s, ano, fechadas))
    return resultado


def _projetar_cadastro(conn, s, ano, fechadas):
    """Projeção de UM cadastro já normalizado.

    Separada de `projetados` para que dê para perguntar "e se este cadastro
    fosse do outro comportamento?" sem gravar nada no banco.
    """
    if comportamento_de(s) == "reserva":
        return _projetados_habito(conn, s, ano, fechadas)
    reais = _reais(conn, s)
    # Cobrança única: a competência que já teve a cobrança real não recebe
    # previsão nenhuma -- o real substitui o previsto, não o consome.
    meses_reais = {r["competencia_fatura"] if s["noCartao"] else r["data"][:7] for r in reais}
    resultado = []
    for competencia, data in _calendario(conn, s).items():
        if int(competencia[:4]) != ano or competencia in meses_reais:
            continue
        if s["noCartao"] and (s["contaId"], competencia) in fechadas:
            continue
        if _fixas_cobrem(conn, s, competencia):
            continue
        valor, base = _valor(s, reais, data)
        resultado.append({
            "mes": int(competencia[5:]), "data": data, "contaId": s["contaId"],
            "transacaoBaseId": base["transacao_id"] if base else s.get("transacaoBaseId", ""),
            "compraId": "recorrente:" + s["chave"], "descricao": s["descricao"],
            "valor": valor, "parcelaAtual": None, "parcelaTotal": None,
            "recorrente": True, "noCartao": s["noCartao"],
        })
    return resultado


def previsoes_payload(linhas):
    if not linhas:
        return []
    with rec._abrir() as conn:
        contas = {c["id"]: c for c in contas_pagamento(conn)}
        categorias_nomes = {l["id"]: l["nome"] for l in conn.execute(
            "SELECT id, nome FROM extrato_categorias")}
        atual = rec._mes_atual()
        ano = int(atual[:4])
        futuras = {}
        for a in (ano, ano + 1):
            for item in projetados(conn, a):
                chave = item["compraId"].removeprefix("recorrente:")
                futuras.setdefault(chave, {
                    "mes": f"{a}-{item['mes']:02d}", "valor": item["valor"],
                    "valorBase": item.get("valorBase"), "valorLancado": item.get("valorLancado"),
                    "tipo": item.get("tipoPrevisao"),
                })
        resultado = []
        for linha in linhas:
            s = json.loads(linha["dados"])
            s["chave"] = linha["chave"]
            s["ativa"] = bool(linha["ativo"])
            conta = contas.get(s["contaId"])
            s["contaNome"] = conta["nome"] if conta else "Pagamento indisponível · escolha outra conta"
            if conta:
                s["noCartao"] = conta["noCartao"]
            reais = _reais(conn, s)
            # Cobrança já informada para uma data futura não altera o valor
            # exibido hoje, nem o valor das competências anteriores.
            hoje = atual + datetime.now().strftime("-%d")
            s["comportamento"] = comportamento_de(s)
            s["termos"] = termos_de(s)
            s["categoriaNome"] = (categorias_nomes.get(s.get("categoriaId")) or "") if s.get("categoriaId") else ""
            if s["comportamento"] == "reserva":
                totais = _totais_por_competencia(conn, s, reais)
                proxima = futuras.get(s["chave"])
                valor = (proxima or {}).get("valorBase") or _base_habito(s, totais, atual)
                base = next((r for r in reais if str(r["data"])[:10] <= hoje), None)
            else:
                valor, base = _valor(s, reais, hoje)
            s.setdefault("modoValor", "ultimo")
            s.setdefault("valorPrevisto", s.get("valorUltimo", valor))
            s["valorUltimo"] = abs(float(base["valor"])) if base else s.get("valorUltimo", valor)
            s["valorAtual"] = valor
            s["proximaPrevisao"] = futuras.get(s["chave"])
            s["statusPrevisao"] = ("Pausada" if not s["ativa"] else "Escolha um pagamento ativo" if not conta
                else "Reserva do mês: cada compra reduz o saldo ainda previsto." if s["comportamento"] == "reserva"
                else "Coberta por conta fixa" if _fixas_cobrem(conn, s, atual)
                else "Previsão ativa" if s["proximaPrevisao"] else "Cobranças já identificadas nas faturas")
            resultado.append(s)
        return sorted(resultado, key=lambda s: (not s["ativa"], s["descricao"].casefold()))
