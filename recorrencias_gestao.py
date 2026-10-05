"""Cadastro e calendário das recorrências aceitas, independente do detector."""
from __future__ import annotations

import json
import collections
import math
import re
import statistics
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta

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
# Quantos ciclos entram na média. Três pega o hábito recente; doze atravessa
# o ano inteiro e dilui sazonalidade (escola em fevereiro, viagem em julho).
JANELAS_MEDIA = (3, 6, 12)
JANELA_MEDIA_PADRAO = 3

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


def janela_de(s):
    try:
        janela = int(s.get("janelaMedia") or JANELA_MEDIA_PADRAO)
    except (TypeError, ValueError):
        return JANELA_MEDIA_PADRAO
    return janela if janela in JANELAS_MEDIA else JANELA_MEDIA_PADRAO


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


_MEMO: dict = {}


def _memo(conn, nome, calcular):
    """Mesmo valor durante uma requisição, sem refazer a consulta.

    Ler um cadastro resolve a tag, e a tela lê dezenas de cadastros; sem isso
    identidades() e contas_ativas() rodavam centenas de vezes por abertura.
    A chave é a conexão (uma por requisição) e o valor vive 3 segundos.
    """
    import time
    chave = (id(conn), nome)
    guardado = _MEMO.get(chave)
    agora = time.monotonic()
    if guardado and agora - guardado[0] < 3:
        return guardado[1]
    if len(_MEMO) > 64:
        _MEMO.clear()
    valor = calcular()
    _MEMO[chave] = (agora, valor)
    return valor


def _identidades(conn):
    import cartoes
    return _memo(conn, "identidades", lambda: cartoes.identidades(conn))


def _ativas(conn):
    return _memo(conn, "ativas", lambda: px.contas_ativas(conn))


def _resolver_contas(conn, referencia):
    """Contas de uma referência: tag/cartão, ou banco ("contas:<tag>")."""
    referencia = str(referencia or "")
    if referencia.startswith("contas:"):
        bancos = _memo(conn, "bancos", lambda: {b["id"]: set(b["contas"]) for b in px.bancos_das_contas(conn, None)})
        return set(bancos.get(referencia, set()))
    return {fonte for fonte, ident in _identidades(conn).items()
            if referencia in {fonte, ident["cartaoId"], ident["tagId"]}}


def _referencia_do_cartao(identidade: dict) -> str:
    """A referência estável de um cartão: a tag, senão a identidade do cartão.

    O conta_id da Pluggy muda quando o banco é reconectado; a tag (e o
    cartaoId, que vem do final do número) sobrevive. Hábito que guardava só
    o conta_id ficava "pagamento anterior indisponível" depois de reconectar.
    """
    return identidade.get("tagId") or identidade.get("cartaoId") or ""


def _conta_mais_recente(conn, contas):
    """Entre várias contas ativas da mesma tag, a que tem movimento mais novo."""
    contas = sorted(contas)
    if len(contas) <= 1:
        return contas[0] if contas else ""
    marcadores = ",".join("?" for _ in contas)
    linha = conn.execute(
        f"SELECT conta_id FROM pluggy_transacoes WHERE conta_id IN ({marcadores}) "
        "ORDER BY data DESC LIMIT 1", contas).fetchone()
    return linha["conta_id"] if linha else contas[0]


def _resolver_pagamento(conn, referencia):
    """(conta_id ativo, referência estável) para o que a tela mandou.

    A tela manda a tag do cartão (ou o cartaoId); cadastros antigos mandam
    o conta_id. Os dois caminhos chegam no mesmo par.
    """
    import cartoes
    referencia = str(referencia or "")
    ativas = _ativas(conn)
    identidades = _identidades(conn)
    if referencia in ativas:
        return referencia, _referencia_do_cartao(identidades.get(referencia, {}))
    contas = _resolver_contas(conn, referencia) & set(ativas)
    return _conta_mais_recente(conn, contas), (referencia if contas else "")


def _contas_da_tag(conn, s):
    """Todas as contas (ativas ou não) do pagamento do cadastro.

    Com tag: todos os cartões dela, inclusive os de conexões antigas, que é
    onde está o histórico de antes da reconexão. Sem tag: a conta do cadastro.
    """
    import cartoes
    ref = s.get("cartaoRef")
    contas = _resolver_contas(conn, ref) if ref else set()
    return contas | {s["contaId"]} if s.get("contaId") else contas


def _ler(conn, dados):
    """Cadastro salvo, com o pagamento apontando para a conta ATIVA da tag.

    Reconectar o banco cria um conta_id novo para o mesmo cartão. O cadastro
    guarda a tag (cartaoRef); aqui ela é resolvida de novo, então todo o
    resto do código -- que compara conta_id -- segue funcionando.
    """
    s = json.loads(dados)
    if s.get("contaId") in _ativas(conn):
        return s
    # Cadastro de antes da tag (só o conta_id antigo): a tag vem da própria
    # conta antiga, que continua nas identidades mesmo arquivada.
    ref = s.get("cartaoRef")
    if not ref:
        import cartoes
        ref = _referencia_do_cartao(_identidades(conn).get(s.get("contaId"), {}))
    if not ref:
        # Conta bancária: o banco a que a conta antiga pertencia.
        bancos = _memo(conn, "bancos", lambda: {b["id"]: set(b["contas"]) for b in px.bancos_das_contas(conn, None)})
        ref = next((chave for chave, contas in bancos.items() if s.get("contaId") in contas), "")
    if ref:
        conta, _ = _resolver_pagamento(conn, ref)
        if conta:
            s["contaId"], s["cartaoRef"] = conta, ref
    return s


def contas_pagamento(conn):
    """Opções de pagamento: um item por TAG de cartão e um por conta.

    Cartão sai pela tag (a mesma usada em Cartões e Contas Fixas), e não pelo
    conta_id da Pluggy, que muda a cada reconexão.
    """
    ativas = px.contas_ativas(conn)
    nomes = px.mapa_apelidos(conn, ativas)
    import cartoes
    resultado, vistos = [], set()
    for coluna in cartoes.colunas(conn):
        membros = [m for m in coluna["membros"] if m["contaId"] in ativas]
        if not membros:
            continue
        ref = coluna.get("tagId") or (membros[0]["cartaoId"] if len(membros) == 1 else coluna["id"])
        # Com tag, o nome é só a tag ("BTG"): consolida todos os cartões
        # dela, inclusive o de antes de reconectar. Sem tag, o cartão.
        nome = coluna.get("tag") or coluna.get("nomeExibicao") or coluna.get("nome") or "Cartão"
        if not coluna.get("tag") and len(membros) == 1 and membros[0].get("nomeOriginal") and membros[0]["nomeOriginal"] != nome:
            nome = f"{nome} · {membros[0]['nomeOriginal']}"
        resultado.append({"id": ref, "nome": nome, "noCartao": True,
                          "contas": [m["contaId"] for m in coluna["membros"]]})
        vistos.update(m["contaId"] for m in coluna["membros"])
    # Contas: uma opção por banco, como no filtro de Transações -- Inter
    # Corrente e Inter Poupança são o mesmo lugar.
    for banco in px.bancos_das_contas(conn, set(ativas)):
        contas = [cid for cid in banco["contas"] if cid not in vistos]
        if contas:
            resultado.append({"id": banco["id"], "nome": banco["nome"], "noCartao": False, "contas": contas})
    return sorted(resultado, key=lambda c: (not c["noCartao"], c["nome"].casefold(), c["id"]))


def _normalizar(conn, dados, anterior=None):
    anterior = anterior or {}
    s = {**anterior, **{k: dados[k] for k in (
        "descricao", "lojista", "contaId", "valorPrevisto", "modoValor", "diaTipico",
        "comportamento", "termos", "categoriaId", "janelaMedia", "rateioProporcional",
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
    # "contaId" vindo da tela pode ser a tag do cartão; guarda as duas coisas:
    # o conta_id ativo (o que o resto do código compara) e a referência
    # estável, que sobrevive a reconectar o banco.
    pedido = str(dados.get("contaId") or s.get("cartaoRef") or s.get("contaId") or "")
    cid, ref = _resolver_pagamento(conn, pedido)
    conta = conn.execute("SELECT subtipo FROM pluggy_contas WHERE conta_id=?", (cid,)).fetchone()
    if not conta:
        raise ValueError("Escolha uma conta ou cartão ativo para o pagamento.")
    s["contaId"], s["noCartao"] = cid, conta["subtipo"] == "CREDIT_CARD"
    # Vale para cartão (tag) e para conta (banco): os dois sobrevivem a
    # reconectar e a ter mais de uma conta no mesmo lugar.
    if ref:
        s["cartaoRef"] = ref
    else:
        s.pop("cartaoRef", None)
    modo = s.get("modoValor", "ultimo")
    if modo not in ("ultimo", "fixo", "media"):
        raise ValueError("Escolha último valor cobrado, valor definido ou média dos ciclos.")
    s["modoValor"] = modo
    s["janelaMedia"] = janela_de(s)
    # Valor informado é validado como sempre. Valor OMITIDO é derivado do
    # histórico que os crivos casam -- a tela deixou de pedir um "valor
    # inicial" a quem escolheu "último valor cobrado" ou "média", porque era
    # pedir justamente o número que o app existe para descobrir. Quem precisa
    # fixar um número usa "valor definido", e a API continua aceitando um.
    if "valorPrevisto" in dados or modo == "fixo":
        try:
            valor = float(dados.get("valorPrevisto", s.get("valorPrevisto", 0)))
        except (ValueError, TypeError):
            raise ValueError("Informe um valor previsto maior que zero.") from None
        if not math.isfinite(valor) or round(valor, 2) <= 0:
            raise ValueError("Informe um valor previsto maior que zero.")
    else:
        # Derivar só pode MELHORAR a estimativa. Sem base no histórico, o que
        # já se sabia continua valendo -- trocar o pagamento de um cadastro
        # não é motivo para esquecer o preço dele.
        valor = (_valor_do_historico(conn, s, modo)
                 or float(anterior.get("valorPrevisto") or 0)
                 or float(s.get("valorUltimo") or 0))
    s["valorPrevisto"] = round(valor, 2)
    # Só faz sentido ratear gasto ESPALHADO pelo ciclo. Um abastecimento por
    # mês é um evento: ou acontece inteiro, ou não acontece.
    s["rateioProporcional"] = bool(s.get("rateioProporcional", True))
    if comportamento_de(s) == "reserva":
        # Reserva não tem dia: ela é o orçamento do ciclo inteiro, consumido
        # aos poucos. Pedir um dia seria pedir um dado que não existe.
        s["diaTipico"] = 1
    else:
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


def _competencia_corrente(conn, s):
    """A competência do ciclo que está aberto agora."""
    hoje = datetime.now().date().isoformat()
    if not s.get("noCartao"):
        return hoje[:7]
    # Cartão sem ciclo conhecido cai no mês seguinte, como faz `_calendario` e
    # o último degrau de `ciclos.expressao_competencia`: a compra de hoje entra
    # na fatura que vence no mês que vem, não na do mês corrente.
    return ciclos.competencia_de(conn, s["contaId"], hoje) or rec._recuar(hoje[:7], -1)


def _valor_do_historico(conn, s, modo):
    """Último valor cobrado, ou a média da janela, conforme o modo.

    Roda no salvamento para o cadastro nascer com um número em vez de um
    palpite digitado. Sem histórico devolve 0: honesto, e a projeção
    simplesmente não emite nada até a primeira cobrança aparecer.
    """
    try:
        reais = _reais(conn, s)
    except Exception:
        return 0.0
    hoje = datetime.now().date().isoformat()
    passados = [r for r in reais if str(r["data"])[:10] <= hoje]
    if not passados:
        return 0.0
    if modo == "media":
        # O ciclo AINDA ABERTO fica fora: ele está incompleto, e usá-lo como
        # base é circular -- a base viraria "o que já gastei neste ciclo", e o
        # restante a prever seria sempre zero.
        return _media_dos_ciclos(
            s, _totais_por_competencia(conn, s, passados),
            _competencia_corrente(conn, s)) or 0.0
    return abs(float(passados[0]["valor"] or 0))


def _verificar_duplicata(conn, s):
    for linha in conn.execute("SELECT chave,dados FROM recorrentes_previsoes"):
        outro = _ler(conn, linha["dados"])
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
                s = _ler(conn, linha["dados"])
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
                # Gasto espalhado (mais de uma compra por mês) rateia; gasto
                # de evento, como um abastecimento mensal, não.
                "rateioProporcional": (habito.get("quantidadeCompras") or 0)
                                      >= 2 * max(1, habito.get("mesesAtivos") or 1),
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
        s = _ler(conn, linha["dados"])
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

        original = _ler(conn, linha["dados"])
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
            # A tela manda a tag do cartão; a prévia procura na conta ativa dela.
            "contaId": _resolver_pagamento(conn, dados.get("contaId"))[0],
            "lojista": rec._chave(str(dados.get("lojista") or "")),
            "termos": dados.get("termos") or [],
            "categoriaId": str(dados.get("categoriaId") or "") or None,
            "janelaMedia": dados.get("janelaMedia"),
            "modoValor": "media",
        }
        if not s["contaId"]:
            raise ValueError("Escolha a conta ou o cartão do pagamento.")
        reais = _reais(conn, s, criterios=True)
        hoje = datetime.now().date().isoformat()
        passados = [r for r in reais if str(r["data"])[:10] <= hoje]
        totais = _totais_por_competencia(conn, {**s, "noCartao": bool(
            conn.execute("SELECT 1 FROM pluggy_contas WHERE conta_id=? AND subtipo='CREDIT_CARD'",
                         (s["contaId"],)).fetchone())}, passados)
        com_gasto = [v for v in totais.values() if v > 0]
        por_criterio = collections.Counter(r.get("criterio") for r in passados)
        return {
            "lancamentos": len(passados),
            "meses": len([v for v in totais.values() if v > 0]),
            "porCriterio": dict(por_criterio),
            # Mesma conta que o cadastro vai fazer, com a mesma janela.
            "mediaCiclos": _media_dos_ciclos(s, totais) or 0.0,
            "janelaMedia": janela_de(s),
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
        anterior = _ler(conn, linha["dados"])
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
        s = _ler(conn, linha["dados"])
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
    amplo = por_categoria(s)
    outros = []
    for linha in conn.execute(
            "SELECT chave, dados FROM recorrentes_previsoes WHERE ativo=1"):
        if linha["chave"] == s.get("chave"):
            continue
        outro = _ler(conn, linha["dados"])
        # Escopo amplo enxerga todos os pagamentos, então um cadastro
        # específico de qualquer cartão pode reivindicar a linha; escopo de um
        # cartão só disputa com os cadastros daquele cartão.
        if not amplo and not (_contas_da_tag(conn, outro) & _contas_da_tag(conn, s)):
            continue
        outro["_contas"] = _contas_da_tag(conn, outro)
        termos = termos_de(outro)
        if outro.get("lojista") or termos:
            outros.append((outro, termos))
    if not outros:
        return set()

    contas = sorted(px.contas_ativas(conn)) if amplo else sorted(_contas_da_tag(conn, s))
    marcadores = ", ".join("?" for _ in contas) or "NULL"
    reservados = set()
    for linha in conn.execute(
        "SELECT transacao_id,conta_id,descricao,categoria_id FROM extrato_efetivo_cache "
        f"WHERE conta_id IN ({marcadores}) AND tipo='DEBIT' AND incluida=1", contas
    ):
        for outro, termos in outros:
            if linha["conta_id"] not in outro["_contas"]:
                continue
            if _casa(outro, linha, termos, set()):
                reservados.add(linha["transacao_id"])
                break
    return reservados


def por_categoria(s):
    """O cadastro é de uma categoria inteira, sem texto nenhum a casar?

    Só nesse caso o escopo é a pessoa, não o cartão: "quanto eu gasto de
    comida por mês" é um fato sobre mim. Netflix, ao contrário, é uma cobrança
    num cartão específico.
    """
    return bool(s.get("categoriaId")) and not s.get("lojista") and not termos_de(s)


def _reais(conn, s, criterios=False):
    faixa = s.get("faixaValor")
    termos = termos_de(s)
    familia = _descendentes(conn, s.get("categoriaId"))
    reservados = _reservados_por_outros(conn, s) if familia else set()

    # Cadastro de categoria olha TODOS os pagamentos: a média de comida do mês
    # é a soma dos cartões, e o consumo tem de vir da mesma fonte -- se a base
    # somasse tudo mas o consumo só um cartão, gastar no outro não abateria
    # nada e a reserva nunca fecharia.
    if por_categoria(s):
        onde, parametros = "conta_id IN ({})".format(
            ", ".join("?" for _ in px.contas_ativas(conn)) or "NULL"
        ), tuple(sorted(px.contas_ativas(conn)))
    else:
        # Todos os cartões da tag, inclusive o de antes de reconectar o banco:
        # o histórico do hábito é do cartão "BTG", não de um conta_id.
        contas = sorted(_contas_da_tag(conn, s))
        onde, parametros = "conta_id IN ({})".format(", ".join("?" for _ in contas) or "NULL"), tuple(contas)

    saida = []
    for linha in conn.execute(
        "SELECT transacao_id,conta_id,descricao,valor,data,competencia_fatura,categoria_id "
        f"FROM extrato_efetivo_cache "
        f"WHERE {onde} AND tipo='DEBIT' AND incluida=1 AND COALESCE(parcela_total,1)<=1 "
        "ORDER BY data DESC, transacao_id", parametros
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
    """Em que competência este lançamento caiu.

    Num cadastro de categoria as linhas vêm de vários pagamentos, então a
    pergunta é feita sobre a conta DA LINHA, não a do cadastro: uma compra no
    cartão A cai no ciclo do cartão A.
    """
    competencia = str(real.get("competencia_fatura") or "")[:7]
    if len(competencia) == 7:
        return competencia
    conta = real["conta_id"] if por_categoria(s) and "conta_id" in real.keys()         else s["contaId"]
    if _e_cartao(conn, conta):
        return ciclos.competencia_de(conn, conta, real["data"]) or str(real["data"])[:7]
    return str(real["data"])[:7]


def _e_cartao(conn, conta_id):
    linha = conn.execute(
        "SELECT subtipo FROM pluggy_contas WHERE conta_id=?", (conta_id,)).fetchone()
    return bool(linha) and linha["subtipo"] == "CREDIT_CARD"


def _totais_por_competencia(conn, s, reais):
    """Valor já lançado por ciclo; somente lançamentos até hoje contam."""
    hoje = datetime.now().date().isoformat()
    totais = collections.defaultdict(float)
    for real in reais:
        if str(real["data"])[:10] > hoje:
            continue
        totais[_competencia_real(conn, s, real)] += abs(float(real["valor"] or 0))
    return {competencia: round(valor, 2) for competencia, valor in totais.items()}


def _media_dos_ciclos(s, totais, competencia=None):
    """Média dos últimos N ciclos COMPLETOS com gasto.

    `competencia` é o limite: entram só os ciclos ANTERIORES a ela. Quem chama
    passa sempre o ciclo aberto, nunca o mês que está sendo projetado -- senão
    a previsão de novembro usaria outubro, que ainda está pela metade, e sairia
    baixa por um motivo que não é o comportamento da pessoa.

    Ciclo sem gasto nenhum também fica fora: costuma ser mês sem importação ou
    anterior ao cadastro.
    """
    anteriores = [valor for mes, valor in sorted(totais.items(), reverse=True)
                  if (competencia is None or mes < competencia) and valor > 0]
    anteriores = anteriores[:janela_de(s)]
    return round(statistics.mean(anteriores), 2) if anteriores else None


def _base_habito(s, totais, competencia):
    media = _media_dos_ciclos(s, totais, competencia)
    if s.get("modoValor") == "media" and media is not None:
        return media
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


def _valor(s, reais, limite=None, totais=None, competencia=None):
    """Quanto prever para uma COBRANÇA ÚNICA, conforme o modo de valor.

    "media" existia só para reserva e era aceita aqui sem fazer nada -- caía
    silenciosamente no valor fixo. Ela é útil na cobrança também: conta de luz
    e água variam demais para "último valor" e continuam sendo uma cobrança
    por ciclo.
    """
    base = next((r for r in reais if limite is None or r["data"][:10] <= limite), None)
    modo = s.get("modoValor", "ultimo")
    valor = s.get("valorPrevisto", s.get("valorUltimo", 0))
    if modo == "ultimo" and base:
        valor = abs(float(base["valor"]))
    elif modo == "media" and totais is not None:
        media = _media_dos_ciclos(s, totais, competencia)
        if media is not None:
            valor = media
    return round(float(valor), 2), base


def _fracao_restante(conn, s, competencia, hoje):
    """Quanto do ciclo ainda falta, de 0 a 1.

    Uma reserva é o gasto de um ciclo INTEIRO. Faltando dois dias para
    fechar, prever o mês inteiro de mercado é prever uma compra que não vai
    caber no tempo que sobra. Ciclo que ainda nem começou devolve 1.
    """
    janela = competencia
    if s.get("noCartao"):
        linha = conn.execute(
            "SELECT inicio, fim FROM pluggy_ciclos WHERE conta_id=? AND competencia=?",
            (s["contaId"], competencia)).fetchone()
        if linha:
            inicio, fim = str(linha["inicio"])[:10], str(linha["fim"])[:10]
            janela = ""
        else:
            # Cartão sem ciclo conhecido (recém-conectado, nenhuma fatura
            # fechada). A competência já assume, nesse caso, que a compra cai
            # na fatura do mês SEGUINTE -- então o ciclo é o mês anterior à
            # competência. Não é premissa nova: é a mesma que já está em uso
            # em `_calendario` e no último degrau de `ciclos`.
            janela = rec._recuar(competencia, 1)
    if janela:
        ano, mes = int(janela[:4]), int(janela[5:7])
        inicio = f"{janela}-01"
        fim = (date(ano, mes, monthrange(ano, mes)[1]) + timedelta(days=1)).isoformat()
    try:
        inicio, fim, agora = (date.fromisoformat(x) for x in (inicio, fim, hoje))
    except ValueError:
        return 1.0
    dias = (fim - inicio).days
    if dias <= 0:
        return 1.0
    restantes = (fim - max(agora, inicio)).days
    return max(0.0, min(1.0, restantes / dias))


def _projetados_habito(conn, s, ano, fechadas):
    reais = _reais(conn, s)
    totais = _totais_por_competencia(conn, s, reais)
    hoje = datetime.now().date().isoformat()
    corrente = _competencia_corrente(conn, s)
    base_real = next((real for real in reais if str(real["data"])[:10] <= hoje), None)
    resultado = []
    for competencia, data in _calendario(conn, s).items():
        if int(competencia[:4]) != ano:
            continue
        # Fatura fechada é soberana -- mas só existe fatura em cartão.
        if s["noCartao"] and (s["contaId"], competencia) in fechadas:
            continue
        if _fixas_cobrem(conn, s, competencia):
            continue
        # A base sai sempre dos ciclos já fechados, não dos anteriores ao mês
        # projetado: o ciclo corrente está incompleto.
        valor_base = _base_habito(s, totais, corrente)
        valor_lancado = totais.get(competencia, 0)
        fracao = (_fracao_restante(conn, s, competencia, hoje)
                  if s.get("rateioProporcional", True) else 1.0)
        # Dois limites honestos, e vale o menor: o que falta do orçamento, e o
        # que ainda cabe no tempo que sobra do ciclo. No começo do ciclo a
        # fração é ~1 e sobra o saldo inteiro, como antes; no fim, quase nada.
        saldo = max(0.0, valor_base - valor_lancado)
        restante = round(min(saldo, valor_base * fracao), 2)
        if restante <= 0:
            continue
        resultado.append({
            "mes": int(competencia[5:]), "data": data, "contaId": s["contaId"],
            "transacaoBaseId": base_real["transacao_id"] if base_real else "",
            "compraId": "recorrente:" + s["chave"],
            "descricao": s["descricao"], "valor": restante,
            "valorBase": valor_base, "valorLancado": valor_lancado,
            "modoValor": s.get("modoValor", "ultimo"),
            "fracaoRestante": round(fracao, 3),
            "tipoPrevisao": "habito", "parcelaAtual": None, "parcelaTotal": None,
            "recorrente": True, "noCartao": s["noCartao"],
        })
    return resultado


def cobradas(conn, competencia, contas):
    """Cobranças ativas já lançadas no ciclo, apenas para exibição."""
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='recorrentes_previsoes'").fetchone():
        return []
    saida = []
    for linha in conn.execute(
            "SELECT chave,dados FROM recorrentes_previsoes WHERE ativo=1 ORDER BY chave"):
        s = _ler(conn, linha["dados"])
        s["chave"] = linha["chave"]
        if s["contaId"] not in contas or comportamento_de(s) != "cobranca":
            continue
        s["noCartao"] = _e_cartao(conn, s["contaId"])
        lancado = _totais_por_competencia(conn, s, _reais(conn, s)).get(competencia, 0)
        if lancado > 0:
            saida.append({"chave": s["chave"], "descricao": s["descricao"],
                          "valorLancado": round(lancado, 2)})
    return saida


def consumidas(conn, competencia, contas, encerrado=False):
    """Reservas ATIVAS que já foram gastas por inteiro nesta competência.

    Elas não entram na projeção -- não há nada a prever -- mas desaparecer da
    tela faz parecer que o cadastro foi perdido. Aqui é informação de exibição
    e soma zero: o gasto delas já está em "já lançado".
    """
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='recorrentes_previsoes'").fetchone():
        return []
    subtipos = {r[0]: r[1] for r in conn.execute("SELECT conta_id,subtipo FROM pluggy_contas")}
    saida = []
    for linha in conn.execute(
            "SELECT chave,dados FROM recorrentes_previsoes WHERE ativo=1 ORDER BY chave"):
        s = _ler(conn, linha["dados"])
        s["chave"] = linha["chave"]
        if s["contaId"] not in contas or comportamento_de(s) != "reserva":
            continue
        s["noCartao"] = subtipos.get(s["contaId"]) == "CREDIT_CARD"
        reais = _reais(conn, s)
        totais = _totais_por_competencia(conn, s, reais)
        corrente = _competencia_corrente(conn, s)
        base = _base_habito(s, totais, corrente)
        lancado = totais.get(competencia, 0)
        # O ciclo do cartão já virou (a competência corrente é posterior) vale
        # como encerrado, mesmo antes de a fatura chegar como fechada.
        fechou = encerrado or competencia < corrente
        # Ciclo encerrado entra com qualquer gasto (ou meta): é o fechamento
        # da reserva. Ciclo aberto só lista a que já estourou a meta.
        if fechou:
            if base <= 0 and lancado <= 0:
                continue
        elif base <= 0 or lancado < base:
            continue
        saida.append({"chave": s["chave"], "descricao": s["descricao"],
                      "valorBase": base, "valorLancado": round(lancado, 2),
                      "encerrado": bool(fechou)})
    return saida


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
        s = _ler(conn, linha["dados"])
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
    totais = _totais_por_competencia(conn, s, reais) if s.get("modoValor") == "media" else None
    resultado = []
    for competencia, data in _calendario(conn, s).items():
        if int(competencia[:4]) != ano or competencia in meses_reais:
            continue
        if s["noCartao"] and (s["contaId"], competencia) in fechadas:
            continue
        if _fixas_cobrem(conn, s, competencia):
            continue
        valor, base = _valor(s, reais, data, totais, _competencia_corrente(conn, s))
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
        # A opção de cartão é a TAG, mas o cadastro aponta para um conta_id:
        # indexa cada opção também pelas contas que ela reúne.
        contas = {}
        for c in contas_pagamento(conn):
            contas[c["id"]] = c
            for cid in c.get("contas", []):
                contas.setdefault(cid, c)
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
            s = _ler(conn, linha["dados"])
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
            s["janelaMedia"] = janela_de(s)
            s["rateioProporcional"] = bool(s.get("rateioProporcional", True))
            s["categoriaNome"] = (categorias_nomes.get(s.get("categoriaId")) or "") if s.get("categoriaId") else ""
            if s["comportamento"] == "reserva":
                totais = _totais_por_competencia(conn, s, reais)
                proxima = futuras.get(s["chave"])
                valor = ((proxima or {}).get("valorBase")
                         or _base_habito(s, totais, _competencia_corrente(conn, s)))
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
