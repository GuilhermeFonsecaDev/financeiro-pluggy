"""O que existe em cada banco: contas, cartões, investimentos e as conexões.

A tela de Conexões listava conexões, e um banco pode ter várias -- o BTG
chegou a ter quatro depois de reconexões, e aparecia como quatro linhas "BTG"
sem dizer o que cada uma trazia. Aqui a unidade é o banco (ver
`px.bancos_por_item`), e as conexões viram um detalhe dele.

Nada é recalculado. Contas visíveis vêm de `px.contas_ativas`, cartões de
`cartoes.identidades`/`fontes_ativas`, a fatura do mês da mesma coluna que a
tela de Cartões mostra, e o estado de cada conexão do que a sincronização já
gravou. Este módulo só agrupa e diz o que está parado.

"Congelado" é a palavra-chave da tela: um instrumento cuja conexão falha ou
foi arquivada continua aparecendo -- o dado existe --, mas não vai mais
atualizar. Foi assim que o cartão e os investimentos do BTG ficaram depois da
reconexão, e nada dizia isso.
"""
from __future__ import annotations

import calendar
import json
import math
import re
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import atualizar_pluggy as sync
import banco as fin
import cartoes
import pluggy_extrato as px


def _dia(iso) -> int | None:
    try:
        return date.fromisoformat(str(iso or "")[:10]).day
    except ValueError:
        return None


def proximo_vencimento(iso, hoje: date) -> str | None:
    """A próxima data de vencimento a partir de uma data de ciclo.

    A Pluggy manda a data do ciclo atual, que pode já ter passado quando a
    conexão está parada. O dia do mês é o que se mantém: avança mês a mês até
    não estar no passado, e dia 31 num mês de 30 cai no último dia.
    """
    try:
        base = date.fromisoformat(str(iso or "")[:10])
    except ValueError:
        return None
    ano, mes, dia = base.year, base.month, base.day
    while True:
        alvo = date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))
        if alvo >= hoje:
            return alvo.isoformat()
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)


# Silêncio "fora do padrão": nunca antes de 3 dias (o limite da tela antiga),
# e só quando a chance de ser acaso, dado o ritmo do banco, cai abaixo de 5%.
DIAS_MINIMOS_SEM_NOVIDADE = 3
CHANCE_DE_ACASO = 0.05
JANELA_DO_RITMO = 90


def silencio_fora_do_padrao(dias_com_movimento: int, dias_sem_novidade: int | None,
                            janela: int = JANELA_DO_RITMO) -> dict[str, Any]:
    """Se o banco está mudo há mais tempo do que o uso dele explica.

    Três dias sem lançamento acendiam o laranja em qualquer banco -- inclusive
    num que você usa duas vezes por mês. O que importa não é o silêncio, é o
    silêncio comparado ao ritmo: com `r` dias de movimento por dia, a chance de
    passar `s` dias seguidos sem nada, por acaso, é e^(-r·s). O limite em dias
    sai dessa conta: um banco diário acende em ~4 dias, um quinzenal só depois
    de meses.

    Não distingue "não usei" de "o banco não mandou": as duas coisas deixam o
    mesmo buraco. Por isso é um laranja, não um aviso.
    """
    ritmo = dias_com_movimento / janela if janela else 0.0
    if ritmo <= 0:
        esperado = None                   # sem histórico, não há padrão para fugir
    else:
        esperado = max(DIAS_MINIMOS_SEM_NOVIDADE, math.ceil(-math.log(CHANCE_DE_ACASO) / ritmo))
    fora = (dias_sem_novidade is not None and esperado is not None
            and dias_sem_novidade > esperado)
    return {"diasSemNovidade": dias_sem_novidade, "esperadoAte": esperado, "fora": fora}


# Código COMPE -> arquivo em assets/bancos. Banco sem arquivo fica com as
# iniciais na mesma caixa. Para um banco novo: o SVG na pasta e uma linha aqui;
# a cor (colorida ou branca) é decidida sozinha por `logo_do_banco`.
LOGO_POR_CODIGO = {
    "001": "bb", "033": "santander", "077": "inter", "102": "xp", "104": "caixa",
    "208": "btg", "212": "original", "237": "bradesco", "260": "nubank",
    "290": "pagbank", "336": "c6", "341": "itau", "380": "picpay",
    "748": "sicredi", "756": "sicoob",
}
# Ajuste ótico por logo. Uma marca compacta ("nu") ocupa a caixa inteira,
# enquanto um logotipo largo ("btg pactual") fica fino no meio dela: no mesmo
# tamanho de caixa, a compacta parece maior. Fração da caixa; ausente = 1.
ESCALA_DA_LOGO = {"nubank": 0.72}
PASTA_LOGOS = Path(__file__).resolve().parent / "assets" / "bancos"
# Contraste mínimo para gráficos (WCAG 1.4.11) contra a superfície do tema.
CONTRASTE_MINIMO = 3.0
SUPERFICIE_DO_TEMA = "#1b1e24"


def _luminancia(hexa: str) -> float:
    h = hexa.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    canal = lambda c: c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    r, g, b = (canal(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return .2126 * r + .7152 * g + .0722 * b


def _contraste(a: str, b: str) -> float:
    la, lb = _luminancia(a), _luminancia(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


@lru_cache(maxsize=64)
def _cor_principal(caminho: str, _mtime: float) -> str:
    """A cor que mais aparece no SVG, ignorando o branco.

    SVG sem nenhum fill explícito é pintado de preto pelo navegador -- é o caso
    da logo do C6 -- e conta como preto.
    """
    texto = Path(caminho).read_text(encoding="utf-8", errors="replace")
    cores = re.findall(r"(?:fill|stroke|stop-color)\s*[:=]\s*[\"']?\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b", texto)
    contagem = Counter(c.lower() for c in cores if c.lower() not in ("#fff", "#ffffff"))
    return contagem.most_common(1)[0][0] if contagem else "#000000"


def logo_do_banco(codigo: str) -> dict[str, Any] | None:
    """Onde está a logo e se ela precisa ficar branca no tema escuro.

    A regra é uma só, para ficar padronizado: se a cor principal da marca não
    tem contraste suficiente com o fundo, a logo inteira vira branca; se tem,
    fica com as cores dela. Nada escolhido à mão -- a logo azul-marinho do BTG
    (contraste 1,1) e a preta do C6 caem na regra do mesmo jeito que a laranja
    do Itaú (5,6).
    """
    slug = LOGO_POR_CODIGO.get(codigo or "")
    arquivo = PASTA_LOGOS / f"{slug}.svg" if slug else None
    if not arquivo or not arquivo.exists():
        return None
    cor = _cor_principal(str(arquivo), arquivo.stat().st_mtime)
    return {"url": f"assets/bancos/{slug}.svg",
            "branca": _contraste(cor, SUPERFICIE_DO_TEMA) < CONTRASTE_MINIMO,
            "escala": ESCALA_DA_LOGO.get(slug, 1)}


def mascarar(numero) -> str:
    """Só os quatro últimos dígitos saem daqui; o número inteiro, nunca."""
    digitos = re.sub(r"\D", "", str(numero or ""))
    return f"···· {digitos[-4:]}" if digitos else ""


def _agencia(raw_json) -> str:
    try:
        transferencia = str((json.loads(raw_json or "{}").get("bankData") or {})
                            .get("transferNumber") or "")
    except (ValueError, TypeError, AttributeError):
        return ""
    partes = re.split(r"[/\-. ]", transferencia)
    return partes[1] if len(partes) > 2 and partes[1].isdigit() else ""


def _tipo(linha) -> str:
    if "invest" in str(linha["nome"] or "").lower():
        return "Investimentos"
    return {"SAVINGS_ACCOUNT": "Poupança"}.get(linha["subtipo"], "Conta corrente")


def _iniciais(nome: str) -> str:
    partes = [p for p in re.split(r"\s+", nome.strip()) if p]
    return (partes[0][0] + partes[1][0] if len(partes) > 1 else (partes[0] if partes else "?")[:2]).upper()


def _estado_das_conexoes() -> tuple[set[str], dict[str, dict], dict[str, dict]]:
    meta = sync._ler_meta()
    arquivadas = set(sync._json_meta(meta, sync.CHAVE_ARQUIVADAS, []))
    return (arquivadas, sync._json_meta(meta, sync.CHAVE_CONEXOES, {}),
            sync._json_meta(meta, sync.CHAVE_PRODUTOS, {}))


@fin.escopo_leitura
def payload(hoje: date | None = None) -> dict[str, Any]:
    """Um card por banco, na ordem em que a tela os empilha."""
    hoje = hoje or date.today()
    fin.ensure_database()
    arquivadas, estados, produtos = _estado_das_conexoes()

    def viva(item_id: str) -> bool:
        # Nunca sincronizada ainda conta como viva: acabou de ser conectada.
        return item_id not in arquivadas and estados.get(item_id, {}).get("resultado") != "erro"

    with fin.connect() as conn:
        identidades = cartoes.identidades(conn)
        ativas = px.contas_ativas(conn)
        fontes = cartoes.fontes_ativas(conn)
        bancos_do_item = px.bancos_por_item(conn, identidades)
        apelidos = px.mapa_apelidos(conn, ativas)
        contas = [dict(l) for l in conn.execute(
            "SELECT * FROM pluggy_contas WHERE subtipo <> 'CREDIT_CARD' ORDER BY nome")]
        investimentos = [dict(l) for l in conn.execute(
            "SELECT item_id, status, saldo_liquido, disponivel_resgate, importado_em "
            "FROM pluggy_investimentos")] if _tabela(conn, "pluggy_investimentos") else []
        entrega: dict[str, dict[str, int]] = {}
        for l in conn.execute("SELECT item_id, subtipo FROM pluggy_contas"):
            chave = "cartoes" if l["subtipo"] == "CREDIT_CARD" else "contas"
            entrega.setdefault(l["item_id"], {"contas": 0, "cartoes": 0, "investimentos": 0})[chave] += 1
        # Só posição ativa: resgatada é histórico, e contar as duas punha "66
        # investimentos" no rodapé do mesmo card que dizia "4 posições".
        for l in investimentos:
            if l["status"] == "ACTIVE":
                entrega.setdefault(l["item_id"], {"contas": 0, "cartoes": 0, "investimentos": 0})["investimentos"] += 1
        ultimo_dado = {l["item_id"]: l["ultimo"] for l in conn.execute(
            "SELECT a.item_id, MAX(SUBSTR(t.data,1,10)) ultimo FROM pluggy_transacoes t "
            "JOIN pluggy_contas a USING(conta_id) WHERE SUBSTR(t.data,1,10) <= ? "
            "GROUP BY a.item_id", (hoje.isoformat(),))}
        itens = [dict(l) for l in conn.execute(
            "SELECT item_id, importado_em FROM pluggy_itens ORDER BY importado_em")]
        # A transação mais recente de cada conta, pela DATA DA COMPRA. Não pelo
        # `createdAt` da Pluggy: ele marca quando aquela conexão viu a
        # transação, e zera a cada reconexão -- o cartão parado do BTG aparecia
        # como "visto hoje". Parcela futura fica de fora (data depois de hoje).
        hoje_iso = hoje.isoformat()
        ultima_vista = {l["conta_id"]: l["vista"] for l in conn.execute(
            "SELECT conta_id, MAX(SUBSTR(data,1,10)) vista FROM pluggy_transacoes "
            "WHERE SUBSTR(data,1,10) <= ? GROUP BY conta_id", (hoje_iso,))}
        # Ritmo de cada conexão: dias distintos com movimento na janela. É por
        # conexão porque o banco é a soma delas, inclusive das antigas -- o
        # histórico de uso é real mesmo que tenha chegado por uma conexão morta.
        inicio_ritmo = date.fromordinal(hoje.toordinal() - JANELA_DO_RITMO).isoformat()
        dias_por_item = {}
        for l in conn.execute(
                "SELECT a.item_id, SUBSTR(t.data,1,10) dia FROM pluggy_transacoes t "
                "JOIN pluggy_contas a USING(conta_id) WHERE SUBSTR(t.data,1,10) BETWEEN ? AND ?",
                (inicio_ritmo, hoje_iso)):
            dias_por_item.setdefault(l["item_id"], set()).add(l["dia"])
        # A Pluggy não manda a data de fechamento no cartão, e às vezes nem a
        # de vencimento. A fatura fechada manda as duas, do jeito que o banco
        # informou. Procura em todas as versões do cartão: a fatura pode ter
        # chegado por uma conexão anterior.
        versoes: dict[str, list[str]] = {}
        for fonte, ident in identidades.items():
            versoes.setdefault(ident["cartaoId"], []).append(fonte)
        ultima_fatura: dict[str, dict] = {}
        if _tabela(conn, "pluggy_faturas"):
            for cartao_id, fontes_do_cartao in versoes.items():
                marcas = ",".join("?" * len(fontes_do_cartao))
                linha = conn.execute(
                    f"SELECT fechamento, vencimento FROM pluggy_faturas WHERE conta_id IN ({marcas}) "
                    "AND fechamento <> '' ORDER BY competencia DESC LIMIT 1", fontes_do_cartao).fetchone()
                if linha:
                    ultima_fatura[cartao_id] = dict(linha)

    # A fatura do mês vem da mesma coluna que a tela de Cartões desenha: aqui
    # não se refaz conta de fatura, só se lê.
    faturas = px.cartoes_payload(hoje.year, "fatura")
    mes = hoje.month - 1

    grupos: dict[str, dict[str, Any]] = {}

    def banco(item_id: str) -> dict[str, Any]:
        info = bancos_do_item.get(item_id) or {"chave": "vazio", "nome": "Sem dados", "codigo": ""}
        g = grupos.get(info["chave"])
        if g is None:
            g = grupos[info["chave"]] = {
                "chave": info["chave"], "nome": info["nome"], "codigo": info["codigo"],
                "iniciais": _iniciais(info["nome"]), "logo": logo_do_banco(info["codigo"]),
                "contas": [], "cartoes": [],
                "faturas": [], "investimentos": None, "conexoes": [], "avisos": [],
            }
        return g

    for item in itens:
        item_id = item["item_id"]
        g = banco(item_id)
        estado = estados.get(item_id, {})
        g["conexoes"].append({
            "id": item_id, "idCurto": item_id[:8], "arquivada": item_id in arquivadas,
            "viva": viva(item_id), "importadoEm": item["importado_em"],
            "dadoMaisRecente": ultimo_dado.get(item_id),
            "resultado": estado.get("resultado") or "", "tentativaEm": estado.get("tentativaEm"),
            "entrega": entrega.get(item_id, {"contas": 0, "cartoes": 0, "investimentos": 0}),
            "_dias": dias_por_item.get(item_id, set()),
            "autorizados": (produtos.get(item_id) or {}).get("autorizados"),
            "avisosPluggy": (produtos.get(item_id) or {}).get("avisos") or [],
        })

    for linha in contas:
        if linha["conta_id"] not in ativas:
            continue
        # Só conta corrente. "BTG Investimentos" (que o banco manda como conta
        # corrente, mas é a conta da corretora) e poupança ficam de fora -- da
        # lista E do "Em conta" da faixa, para os dois números baterem.
        if _tipo(linha) != "Conta corrente":
            continue
        g = banco(linha["item_id"])
        g["contas"].append({
            "contaId": linha["conta_id"], "nome": apelidos.get(linha["conta_id"]) or px.nome_da_conta(linha),
            "tipo": _tipo(linha), "agencia": _agencia(linha["raw_json"]),
            "final": mascarar(linha["numero"]), "saldo": round(float(linha["saldo"] or 0), 2),
            "atualizadoEm": linha["atualizado_em"] or linha["importado_em"],
            "ultimaVista": ultima_vista.get(linha["conta_id"]),
            "congelada": not viva(linha["item_id"]),
        })

    for conta_id, ident in identidades.items():
        if conta_id not in fontes:
            continue
        g = banco(ident["itemId"])
        fatura = ultima_fatura.get(ident["cartaoId"], {})
        # Da Pluggy é o ciclo atual; da fatura, o último fechado. O atual vale mais.
        vencimento = ident.get("vencimento") or fatura.get("vencimento")
        limite = ident.get("limiteCredito")
        disponivel = ident.get("limiteDisponivel")
        usado = (max(0.0, float(limite) - float(disponivel or 0))
                 if limite not in (None, 0) else None)
        g["cartoes"].append({
            "contaId": conta_id, "cartaoId": ident["cartaoId"],
            "final": ident.get("numero") or "", "marca": (ident.get("marca") or "").title(),
            "nome": ident.get("nomeExibicao") or "",
            "limite": float(limite) if limite not in (None, "") else None,
            "disponivel": float(disponivel) if disponivel not in (None, "") else None,
            "usadoPct": round(usado / float(limite) * 100, 1) if usado is not None else None,
            "diaFechamento": _dia(ident.get("fechamento") or fatura.get("fechamento")),
            "diaVencimento": _dia(vencimento),
            "proximoVencimento": proximo_vencimento(vencimento, hoje),
            # Sem nenhuma das duas fontes o cartão é novo: não teve fatura
            # fechada ainda, e dizer isso é melhor que um dia inventado.
            "semFatura": not fatura,
            "ultimaVista": ultima_vista.get(conta_id),
            "congelado": not viva(ident["itemId"]),
        })

    # A coluna de fatura é por tag: dois cartões do Itaú na mesma tag dividem
    # uma coluna, então a fatura pertence ao banco, não a cada cartão.
    for coluna in faturas.get("cartoes") or []:
        membros = set(coluna.get("contas") or []) | {coluna["id"]}
        for g in grupos.values():
            if membros & {c["contaId"] for c in g["cartoes"]}:
                valores = (faturas.get("valores") or {}).get(coluna["id"]) or []
                origens = (faturas.get("origens") or {}).get(coluna["id"]) or []
                g["faturas"].append({
                    "nome": coluna.get("nome") or "", "mes": f"{hoje.year:04d}-{hoje.month:02d}",
                    "valor": round(float(valores[mes]), 2) if mes < len(valores) else 0.0,
                    "origem": origens[mes] if mes < len(origens) else "vazio",
                })
                break

    # Investimentos: como a tela de Investimentos, conexão arquivada fica de
    # fora -- é o que impede a carteira de duplicar numa reconexão.
    for linha in investimentos:
        if linha["item_id"] in arquivadas or linha["status"] != "ACTIVE":
            continue
        g = banco(linha["item_id"])
        inv = g["investimentos"] or {"liquido": 0.0, "disponivelResgate": 0.0, "posicoes": 0,
                                    "coletadoEm": "", "congelado": True}
        inv["liquido"] += float(linha["saldo_liquido"] or 0)
        inv["disponivelResgate"] += float(linha["disponivel_resgate"] or 0)
        inv["posicoes"] += 1
        inv["coletadoEm"] = max(inv["coletadoEm"], str(linha["importado_em"] or ""))
        # Congelado só se TODA posição vier de conexão parada.
        inv["congelado"] = inv["congelado"] and not viva(linha["item_id"])
        g["investimentos"] = inv

    for g in grupos.values():
        _resumir(g, hoje)
        for c in g["conexoes"]:
            c.pop("_dias", None)      # conjunto interno; não vai no JSON
    ordem = sorted(grupos.values(), key=lambda g: (g["chave"] == "vazio", g["nome"].lower()))
    return {"bancos": ordem, "geradoEm": hoje.isoformat()}


def _resumir(g: dict[str, Any], hoje: date) -> None:
    """Os três números da faixa, e os avisos do que o banco não entrega."""
    for inv in [g["investimentos"]] if g["investimentos"] else []:
        inv["liquido"] = round(inv["liquido"], 2)
        inv["disponivelResgate"] = round(inv["disponivelResgate"], 2)

    com_limite = [c for c in g["cartoes"] if c["limite"]]
    total = sum(c["limite"] for c in com_limite)
    usado = sum(c["limite"] - (c["disponivel"] or 0) for c in com_limite)
    vencimentos = sorted(c["proximoVencimento"] for c in g["cartoes"] if c["proximoVencimento"])
    g["resumo"] = {
        "emConta": round(sum(c["saldo"] for c in g["contas"]), 2) if g["contas"] else None,
        "limiteUsadoPct": round(max(0.0, usado) / total * 100, 1) if total else None,
        "proximoVencimento": vencimentos[0] if vencimentos else None,
        # O último lançamento que o banco entregou, como a coluna "Dado mais
        # recente" da tela de Conexões. Arquivada fica de fora: o que ela trouxe
        # não diz nada sobre se o banco ainda está mandando dados.
        "dadoMaisRecente": max((c["dadoMaisRecente"] for c in g["conexoes"]
                                if not c["arquivada"] and c["dadoMaisRecente"]), default=None),
    }
    recente = g["resumo"]["dadoMaisRecente"]
    dias_com_movimento = len(set().union(*(c.get("_dias", set()) for c in g["conexoes"])))
    g["resumo"]["silencio"] = silencio_fora_do_padrao(
        dias_com_movimento,
        (hoje - date.fromisoformat(recente)).days if recente else None)

    vivas = [c for c in g["conexoes"] if c["viva"]]
    ativas = [c for c in g["conexoes"] if not c["arquivada"]]
    if ativas and not vivas:
        g["avisos"].append({"codigo": "conexao_falhando",
                            "texto": f"Nenhuma conexão do {g['nome']} está atualizando. "
                                     "Os dados abaixo estão parados na última importação."})
    # Um aviso por CAUSA, não por produto: cartão e investimentos parados pela
    # mesma razão eram dois parágrafos quase iguais, um embaixo do outro.
    por_causa: dict[str, list[str]] = {}
    for produto, rotulo, chave, mostrado in (
            ("CREDIT_CARDS", "cartão", "cartoes", bool(g["cartoes"])),
            ("INVESTMENTS", "investimentos", "investimentos", bool(g["investimentos"]))):
        causa = _causa_de_produto(vivas, produto, chave, mostrado)
        if causa:
            por_causa.setdefault(causa, []).append(rotulo)
    for causa, rotulos in por_causa.items():
        g["avisos"].append(_aviso(causa, rotulos))
    # O que a Pluggy disse, com o texto dela. Hoje vem vazio pelo MeuPluggy;
    # por um conector de Open Finance direto, é aqui que o limite aparece.
    vistos = set()
    for c in vivas:
        for a in c.get("avisosPluggy") or []:
            if a["texto"] not in vistos:
                vistos.add(a["texto"])
                g["avisos"].append({"codigo": "pluggy", "texto": f"{a['produto'].capitalize()}: {a['texto']}"})
    g["estado"] = ("vazio" if g["chave"] == "vazio" else
                   "erro" if (ativas and not vivas) else
                   "aviso" if g["avisos"] else "ok")


def _causa_de_produto(vivas, produto, chave, mostrado) -> str | None:
    """Por que um produto que vinha parou de vir pela conexão viva.

    Só produto que JÁ VEIO antes: banco sem investimentos não recebe aviso de
    investimentos. E o consentimento que a API mostra é o do app com o Meu
    Pluggy -- a mesma lista completa para todo banco --, então "autorizado"
    não prova que o banco compartilhou. Ele só é evidência quando o produto
    FALTA nele. A certeza, quando existir, vem do `statusDetail` da Pluggy.
    """
    if not mostrado or not vivas or any(c["entrega"].get(chave) for c in vivas):
        return None
    conhecidos = [c["autorizados"] for c in vivas if c.get("autorizados") is not None]
    if conhecidos and not any(produto in a for a in conhecidos):
        return "fora_do_consentimento"
    return "parou"


def _aviso(causa: str, rotulos: list[str]) -> dict[str, str]:
    """O texto curto. O porquê longo fica na ajuda da tela, não no card."""
    lista = " e ".join(rotulos)
    plural = len(rotulos) > 1 or rotulos[0].endswith("s")
    sujeito = lista[0].upper() + lista[1:]
    if causa == "fora_do_consentimento":
        return {"codigo": "fora_do_consentimento",
                "texto": f"{sujeito} {'ficaram' if plural else 'ficou'} fora do consentimento. "
                         f"Reconecte marcando {lista}."}
    return {"codigo": "parou",
            "texto": f"{sujeito} {'pararam' if plural else 'parou'} de atualizar — provável limite "
                     "mensal do Open Finance. Evite reconectar; renova todo mês."}


def _tabela(conn, nome: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (nome,)).fetchone())
