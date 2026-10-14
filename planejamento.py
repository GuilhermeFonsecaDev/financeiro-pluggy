"""Planejamento e metas, a partir do ritmo real.

A tela não pede cadastro para ter valor: ela mede o que já aconteceu e
projeta a partir disso.

- Entradas: a série mensal da tela Entradas.
- Gastos (custo de vida): o total de Contas Fixas 2.0 de cada mês (faturas
  mais contas fora do cartão). Os gastos variáveis do cartão estão dentro
  das faturas.
- Aporte: o líquido mensal das movimentações de investimento (aplicações
  menos resgates), o mesmo de Investimentos.
- Patrimônio e liquidez: Investimentos e a Carteira.

O ritmo usa a MEDIANA dos meses fechados: um resgate grande num mês (troca de
corretora, compra) não deve mudar a leitura do hábito.

Metas são opcionais e ficam guardadas aqui.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from statistics import median
from typing import Any
from uuid import uuid4

import banco as fin

TIPOS = ("manual", "carteira", "aba", "cdb", "liquidez")
HORIZONTE = 36          # meses da projeção
JANELA = 6              # meses fechados para o ritmo
MESES_RESERVA = 6
CDI_PADRAO = 0.9        # % ao mês, quando não há série local

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_metas (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  alvo REAL NOT NULL,
  prazo TEXT NOT NULL DEFAULT '',
  tipo TEXT NOT NULL DEFAULT 'manual',
  referencia TEXT NOT NULL DEFAULT '',
  valor_manual REAL NOT NULL DEFAULT 0,
  inicial REAL NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL,
  arquivada INTEGER NOT NULL DEFAULT 0,
  ordem INTEGER NOT NULL DEFAULT 0
);
"""


def garantir_tabelas(conn) -> None:
    conn.executescript(SCHEMA)
    if "emoji" not in {c[1] for c in conn.execute("PRAGMA table_info(plan_metas)")}:
        conn.execute("ALTER TABLE plan_metas ADD COLUMN emoji TEXT NOT NULL DEFAULT ''")


def _hoje() -> date:
    return date.today()


# ---------------------------------------------------------------- meses

def mes_mais(mes: str, n: int) -> str:
    a, m = map(int, mes.split("-"))
    t = a * 12 + (m - 1) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def meses_entre(de: str, ate: str) -> int:
    """Quantos meses de `de` até `ate` ('YYYY-MM'); negativo se `ate` vem antes."""
    a1, m1 = map(int, de.split("-"))
    a2, m2 = map(int, ate.split("-"))
    return (a2 - a1) * 12 + (m2 - m1)


def janela(mes: str, n: int = JANELA) -> list[str]:
    """Os `n` meses FECHADOS antes de `mes`, do mais antigo ao mais recente."""
    return [mes_mais(mes, -i) for i in range(n, 0, -1)]


# ---------------------------------------------------------------- contas puras

def ritmo(serie: dict[str, float], meses: list[str], usar_mediana: bool = False) -> float | None:
    """Média (ou mediana) dos meses da janela que têm dado."""
    valores = [serie[m] for m in meses if m in serie]
    if not valores:
        return None
    return round(median(valores) if usar_mediana else sum(valores) / len(valores), 2)


def variacao(atual: float, base: float | None) -> float | None:
    if not base:
        return None
    return round((atual / base - 1) * 100, 1)


def projetar(saldo: float, aporte: float, taxa_mes: float, inicio: str,
             meses: int = HORIZONTE) -> list[dict[str, Any]]:
    """Saldo no fim de cada mês: rende sobre o saldo e recebe o aporte."""
    pontos, s = [], float(saldo)
    for i in range(1, meses + 1):
        s = s * (1 + taxa_mes / 100) + aporte
        pontos.append({"mes": mes_mais(inicio, i), "saldo": round(s, 2)})
    return pontos


def quando_atinge(pontos: list[dict[str, Any]], alvo: float) -> str | None:
    return next((p["mes"] for p in pontos if p["saldo"] >= alvo), None)


def marcos(saldo: float, n: int = 3) -> list[float]:
    """Os próximos números redondos acima do patrimônio de hoje."""
    degraus = [25_000, 50_000, 100_000, 150_000, 200_000, 250_000, 300_000, 400_000, 500_000,
               750_000, 1_000_000, 1_500_000, 2_000_000, 3_000_000, 5_000_000]
    return [d for d in degraus if d > saldo][:n]


def meses_para(falta: float, por_mes: float | None) -> int | None:
    if falta <= 0:
        return 0
    if not por_mes or por_mes <= 0:
        return None
    return math.ceil(falta / por_mes)


def progresso_meta(meta: dict[str, Any], atual: float, por_mes: float | None,
                   mes: str, chega: str | None = None) -> dict[str, Any]:
    """Quanto falta, em que mês chega no ritmo de hoje e o que falta para o prazo.

    `por_mes` é quanto a meta anda por mês no ritmo real. `chega` pode vir
    pronto (metas ligadas à carteira usam a projeção, que inclui rendimento).
    """
    alvo = float(meta["alvo"])
    falta = max(0.0, alvo - atual)
    r: dict[str, Any] = {
        "atual": round(atual, 2), "falta": round(falta, 2),
        "pct": round(min(100.0, atual / alvo * 100), 1) if alvo > 0 else 0.0,
        "porMes": round(por_mes, 2) if por_mes else None,
        "chegaEm": None, "precisaPorMes": None, "extraPorMes": None, "situacao": "sem_ritmo",
    }
    if falta <= 0:
        r["situacao"] = "concluida"
        return r
    if chega is None:
        n = meses_para(falta, por_mes)
        chega = mes_mais(mes, n) if n is not None else None
    r["chegaEm"] = chega
    prazo = meta.get("prazo") or ""
    if prazo:
        restantes = meses_entre(mes, prazo) + 1
        if restantes <= 0:
            r["situacao"] = "vencida"
            return r
        r["precisaPorMes"] = round(falta / restantes, 2)
        if chega and chega <= prazo:
            r["situacao"] = "no_prazo"
        else:
            r["situacao"] = "atrasada"
            r["extraPorMes"] = round(r["precisaPorMes"] - (por_mes or 0), 2)
    elif chega:
        r["situacao"] = "no_ritmo"
    return r


# ---------------------------------------------------------------- fontes

def _liquidez_30(lotes: list[dict], prazos: dict[str, int], hoje: date) -> float:
    """Mesma régua da barra de liquidez de Investimentos: resgate em até 30 dias."""
    total = 0.0
    for l in lotes:
        cnpj = "".join(ch for ch in str(l.get("codigo") or "") if ch.isdigit())
        dias = prazos.get(cnpj, l.get("diasResgate"))
        if dias is None and l.get("vencimento"):
            dias = max(0, (date.fromisoformat(l["vencimento"][:10]) - hoje).days)
        if dias is not None and dias <= 30:
            total += float(l.get("liquido") or 0)
    return total


def _emissor(o: dict) -> str:
    nome = o.get("emissor") or re.sub(r"^CDB - ", "", o.get("nome") or "")
    return re.sub(r"\s+S\.?A\.?$", "", nome, flags=re.I).strip()


def _fontes(hoje: date) -> dict[str, Any]:
    """Patrimônio, liquidez, abas e CDBs. Falha de uma fonte não derruba a tela."""
    import carteira
    import investimentos
    f: dict[str, Any] = {"carteira": 0.0, "aba": {}, "cdb": {}, "liquidez": 0.0,
                         "abas": [], "investimentos": None}
    try:
        inv = investimentos.payload()
        f["investimentos"] = inv
        f["carteira"] = float(inv.get("consolidado", {}).get("liquido") or 0)
    except Exception:
        inv = None
    try:
        c = carteira.payload(0)
        f["abas"] = [{"id": a["id"], "nome": a["nome"]} for a in c.get("abas", [])]
        f["aba"] = {k: float(v.get("totalAtual") or 0) for k, v in (c.get("perfis") or {}).items()}
        for o in c.get("outros") or []:
            if not o.get("ignorado"):
                e = _emissor(o)
                f["cdb"][e] = f["cdb"].get(e, 0.0) + float(o.get("posicao") or 0)
        prazos = {i["cnpj"]: int(i["diasResgate"]) for i in c.get("itens", [])
                  if i.get("diasResgate") is not None}
    except Exception:
        prazos = {}
    if inv:
        f["liquidez"] = _liquidez_30(inv.get("lotes") or [], prazos, hoje)
    return f


def _valor_ligado(meta: dict, fontes: dict) -> float:
    tipo, ref = meta["tipo"], meta.get("referencia") or ""
    if tipo == "manual":
        return float(meta.get("valorManual") or 0)
    if tipo in ("carteira", "liquidez"):
        return float(fontes.get(tipo) or 0)
    return float(fontes.get(tipo, {}).get(ref) or 0)


def _taxa_mes(conn, hoje: date) -> tuple[float, str]:
    """Rendimento mensal da projeção: o CDI dos últimos 12 meses."""
    try:
        import indices
        de = hoje.replace(year=hoje.year - 1).isoformat()
        r = indices.fator(conn, indices.PADRAO, de, hoje.isoformat())
        if r.get("fator") and r.get("pontos", 0) > 150:
            return round((r["fator"] ** (1 / 12) - 1) * 100, 4), "CDI dos últimos 12 meses"
    except Exception:
        pass
    return CDI_PADRAO, "CDI estimado"


# Meses fechados não mudam: o total de Contas Fixas de cada um fica guardado
# enquanto o backend roda. O mês corrente é sempre recalculado.
_CACHE_GASTOS: dict[str, float] = {}


def _gastos(meses: list[str], corrente: str) -> dict[str, float]:
    import fixas2
    saida = {}
    for m in meses:
        if m != corrente and m in _CACHE_GASTOS:
            valor = _CACHE_GASTOS[m]
        else:
            try:
                valor = float(fixas2.payload(m)["resumo"]["total"])
            except Exception:
                continue
            if m != corrente:
                _CACHE_GASTOS[m] = valor
        if valor > 0:
            saida[m] = valor
    return saida


# ---------------------------------------------------------------- tendência

HORIZONTE_MAX = 24                       # o seletor da tela vai até 24 meses
PONTOS_FUTURO = tuple(range(1, HORIZONTE_MAX + 1))   # cada um custa uma projeção de fatura
MESES_FLUXO = HORIZONTE_MAX
REF_PARCELAS = 30.0      # % da renda líquida: referência de mercado para parcelas
REGRA_4 = 25             # número da liberdade: 25 anos de custo (regra dos 4%)
_CACHE_FUTURO: dict[tuple, dict[str, float]] = {}


def _assinatura_fixas(conn) -> tuple:
    """Muda sempre que uma conta fixa da 2.0 é criada, editada ou removida."""
    try:
        return (tuple(conn.execute("SELECT COUNT(*), TOTAL(valor_previsto), GROUP_CONCAT(desde || ate || ativo) "
                                   "FROM f2_fixas_contas").fetchone())
                + tuple(conn.execute("SELECT COUNT(*), TOTAL(COALESCE(valor, 0)), TOTAL(removida) "
                                     "FROM f2_fixas_mes").fetchone()))
    except Exception:
        return ()


def _futuro(meses: list[str], hoje: date, assinatura: tuple) -> dict[str, dict[str, float]]:
    """Parcelas de cartão e contas fixas (fora das faturas) de cada mês futuro.

    Vem de Contas Fixas 2.0: o mesmo mês que a tela mostra. Fica guardado até
    o dia virar ou alguma conta fixa mudar.
    """
    import fixas2
    saida = {}
    for m in meses:
        chave = (m, hoje.isoformat(), assinatura)
        if chave not in _CACHE_FUTURO:
            try:
                r = fixas2.payload(m)["resumo"]
            except Exception:
                continue
            _CACHE_FUTURO[chave] = {"parcelas": float(r["faturasParcelas"]),
                                    "cobrado": float(r["faturasCobrado"]),
                                    "fixas": float(r["previstos"]) + float(r["foraCartao"]) - float(r["reembolsos"])}
        saida[m] = _CACHE_FUTURO[chave]
    return saida


def contas_que_acabam(linhas: list[dict], desde_mes: str) -> list[dict]:
    """Contas fixas com fim marcado.

    Na 2.0 as contas recorrentes viram uma cópia por mês (início = fim). Um
    parcelamento é um registro com intervalo (início antes do fim). Vale o
    registro mais recente de cada conta: o cadastro antigo "jan a out" de uma
    conta que seguiu em cópias mensais não está acabando.
    """
    ultimo: dict[str, dict] = {}
    for l in linhas:
        atual = ultimo.get(l["nome"])
        if atual is None or (l["ate"] or "9999-99") > (atual["ate"] or "9999-99"):
            ultimo[l["nome"]] = l
    return sorted(({"nome": n, "ate": l["ate"], "valor": round(float(l["valor"]), 2)}
                   for n, l in ultimo.items()
                   if l["ate"] and l["desde"] < l["ate"] and l["ate"] >= desde_mes),
                  key=lambda c: c["ate"])


def projetar_custo(base: float, corrente: str, futuro: dict[str, dict[str, float]],
                   gerado_ate: str, acabam: list[dict]) -> tuple[list[dict], list[dict]]:
    """Custo de vida nos próximos meses: o de hoje mais o que muda de fato.

    A referência é o próximo mês. Dali em diante:
    - parcelas de cartão: a diferença para a referência (acaba, cai; nova, sobe);
    - contas fixas: nos meses que a 2.0 já gerou, o total real do mês (com
      remoções e edições); depois disso, o último mês gerado menos as contas
      com fim marcado.
    Os gastos variáveis ficam iguais à média: não dá para prever.
    """
    m1 = mes_mais(corrente, 1)
    if m1 not in futuro:
        return [], []
    ref_p, ref_f = futuro[m1]["parcelas"], futuro[m1]["fixas"]
    ultimo_fixas = ref_f
    pontos, eventos = [], []
    for n in PONTOS_FUTURO:
        m = mes_mais(corrente, n)
        if m not in futuro:
            continue
        if m <= gerado_ate:
            fixas = futuro[m]["fixas"]
            ultimo_fixas = fixas
        else:
            fixas = ultimo_fixas - sum(c["valor"] for c in acabam if gerado_ate <= c["ate"] < m)
        pontos.append({"mes": m, "valor": round(base + (futuro[m]["parcelas"] - ref_p) + (fixas - ref_f), 2),
                       "parcelas": round(futuro[m]["parcelas"], 2), "fixas": round(fixas, 2)})
    anterior = pontos[0] if pontos else None
    for p in pontos[1:]:
        for campo, texto in (("fixas", "contas fixas"), ("parcelas", "parcelas de cartão")):
            dif = p[campo] - anterior[campo]
            if abs(dif) >= 50:
                eventos.append({"mes": p["mes"], "texto": f"{texto} {'diminuem' if dif < 0 else 'aumentam'}",
                                "valor": round(dif, 2)})
        anterior = p
    return pontos, eventos


def _tendencia(base: float | None, corrente: str, hoje: date) -> dict[str, Any] | None:
    if not base:
        return None
    with fin.connect() as conn:
        try:
            linhas = [dict(l) for l in conn.execute(
                "SELECT nome, desde, ate, valor_previsto AS valor FROM f2_fixas_contas "
                "WHERE ativo = 1 AND incluir_calculos = 1")]
        except Exception:
            linhas = []
        assinatura = _assinatura_fixas(conn)
    # Último mês que a 2.0 já gerou (cópias mensais têm início = fim).
    gerado_ate = max((l["ate"] for l in linhas if l["ate"] and l["desde"] == l["ate"]), default=corrente)
    futuro = _futuro([mes_mais(corrente, n) for n in PONTOS_FUTURO], hoje, assinatura)
    acabam = contas_que_acabam(linhas, corrente)
    pontos, eventos = projetar_custo(base, corrente, futuro, gerado_ate, acabam)
    for c in acabam:
        eventos.append({"mes": mes_mais(c["ate"], 1), "texto": f"{c['nome']} termina", "valor": -c["valor"]})
    return {"base": base, "futuro": pontos, "eventos": sorted(eventos, key=lambda e: e["mes"]),
            "geradoAte": gerado_ate, "bruto": futuro}


def montar_fluxo(entrada: float, base: float, pontos: list[dict], eventos: list[dict],
                 aporte_meta: float, n: int = MESES_FLUXO) -> list[dict[str, Any]]:
    """Os próximos meses: o que entra, o que já está contratado, o variável e a sobra.

    O variável é o custo de vida médio menos o contratado do primeiro mês, e
    fica constante: é o hábito, não uma previsão de compra.
    """
    if not pontos:
        return []
    variavel = max(0.0, base - pontos[0]["parcelas"] - pontos[0]["fixas"])
    saida = []
    for p in pontos[:n]:
        custo = p["fixas"] + p["parcelas"] + variavel
        sobra = entrada - custo
        saida.append({"mes": p["mes"], "entrada": round(entrada, 2), "fixas": round(p["fixas"], 2),
                      "parcelas": round(p["parcelas"], 2), "variavel": round(variavel, 2),
                      "custo": round(custo, 2), "sobra": round(sobra, 2),
                      "folga": round(sobra - aporte_meta, 2),
                      "eventos": [e for e in eventos if e["mes"] == p["mes"]]})
    return saida


# ---------------------------------------------------------------- parcelamentos

MESES_PARCELAS = HORIZONTE_MAX


def agenda_parcelas(reais: list[dict], projetadas: list[dict], meses: list[str]) -> dict[str, Any]:
    """Junta parcelas lançadas e projetadas numa agenda por compra e por mês.

    `reais` e `projetadas` têm: compra, mes, valor, atual, total, descricao,
    cartao. A mesma parcela não aparece nas duas (a projeção não entra em mês
    que já tem a parcela real); se aparecer, vale a real.
    """
    janela = set(meses)
    vistas: set[tuple[str, str]] = set()
    por_compra: dict[str, dict] = {}
    for p in [*reais, *projetadas]:
        if p["mes"] not in janela or (p["compra"], p["mes"]) in vistas:
            continue
        vistas.add((p["compra"], p["mes"]))
        c = por_compra.setdefault(p["compra"], {
            "id": p["compra"], "descricao": p["descricao"], "cartao": p["cartao"],
            "total": p["total"], "parcelas": []})
        c["parcelas"].append({"mes": p["mes"], "valor": round(p["valor"], 2), "atual": p["atual"]})
    compras = []
    for c in por_compra.values():
        c["parcelas"].sort(key=lambda x: x["mes"])
        primeira, ultima = c["parcelas"][0], c["parcelas"][-1]
        compras.append({**c, "valorParcela": primeira["valor"], "proxima": primeira["atual"],
                        "restantes": len(c["parcelas"]), "saldo": round(sum(x["valor"] for x in c["parcelas"]), 2),
                        "terminaEm": ultima["mes"] if ultima["atual"] >= c["total"] else None,
                        "ultimaParcela": ultima["atual"]})
    compras.sort(key=lambda c: (c["terminaEm"] or "9999", -c["valorParcela"]))
    por_mes = []
    for m in meses:
        itens = [(c, x) for c in compras for x in c["parcelas"] if x["mes"] == m]
        cart: dict[str, float] = {}
        for c, x in itens:
            cart[c["cartao"]] = round(cart.get(c["cartao"], 0) + x["valor"], 2)
        por_mes.append({"mes": m, "total": round(sum(x["valor"] for _, x in itens), 2),
                        "compras": len(itens), "porCartao": cart,
                        "terminam": sum(1 for c, x in itens if x["atual"] >= c["total"])})
    return {"meses": por_mes, "compras": compras,
            "saldo": round(sum(c["saldo"] for c in compras), 2)}


def _parcelamentos(corrente: str) -> dict[str, Any]:
    """Parcelas de cartão dos próximos 12 meses, compra a compra.

    Lançadas: as transações parceladas cuja fatura ainda vence. Projetadas: as
    mesmas da tela de Cartões (respeita projeção excluída e não repete mês que
    já tem a parcela real).
    """
    import ciclos
    import pluggy_extrato as px
    meses = [mes_mais(corrente, n) for n in range(1, MESES_PARCELAS + 1)]
    anos = sorted({int(m[:4]) for m in meses})
    cartoes, conta_cartao, projetadas = {}, {}, []
    for ano in anos:
        d = px.cartoes_payload(ano, "fatura")
        for c in d["cartoes"]:
            cartoes[c["id"]] = {"id": c["id"], "nome": c["nome"], "cor": c.get("cor") or ""}
            for conta in c.get("contas") or []:
                conta_cartao[conta] = c["id"]
        for conta, itens in d["itens"].items():
            for i in itens:
                if not i.get("parcelaTotal") or i.get("recorrente"):
                    continue
                projetadas.append({"compra": i["compraId"], "mes": f"{ano:04d}-{i['mes']:02d}",
                                   "valor": float(i["valor"]), "atual": int(i["parcelaAtual"]),
                                   "total": int(i["parcelaTotal"]), "descricao": i["descricao"],
                                   "cartao": i.get("cartaoId") or conta_cartao.get(conta, conta)})
    competencia = ciclos.expressao_competencia("t", None)
    reais = []
    with fin.connect() as conn:
        for l in conn.execute(f"""
            SELECT t.conta_id, t.descricao, t.valor, t.parcela_numero, t.parcela_total,
                   SUBSTR(JSON_EXTRACT(t.raw_json, '$.creditCardMetadata.purchaseDate'), 1, 10) AS compra,
                   COALESCE(JSON_EXTRACT(t.raw_json, '$.creditCardMetadata.cardNumber'), '') AS numero_cartao,
                   COALESCE((SELECT f.competencia FROM pluggy_faturas f
                              WHERE f.conta_id = t.conta_id AND f.fatura_id = t.fatura_id), {competencia}) AS mes
            FROM pluggy_transacoes t
            JOIN pluggy_contas c ON c.conta_id = t.conta_id AND c.subtipo = 'CREDIT_CARD'
            WHERE t.tipo = 'DEBIT' AND t.parcela_total > 1 AND t.parcela_numero IS NOT NULL"""):
            if not l["mes"] or l["mes"] < meses[0] or l["conta_id"] not in conta_cartao:
                continue
            reais.append({"compra": "|".join(str(p) for p in px.chave_compra(l)), "mes": l["mes"],
                          "valor": abs(float(l["valor"] or 0)), "atual": int(l["parcela_numero"]),
                          "total": int(l["parcela_total"]),
                          "descricao": re.sub(r"\s+", " ", re.sub(r"\d+\s*/\s*\d+", "", l["descricao"] or "")).strip(),
                          "cartao": conta_cartao[l["conta_id"]]})
    agenda = agenda_parcelas(reais, projetadas, meses)
    usados = {c["cartao"] for c in agenda["compras"]}
    return {**agenda, "cartoes": [c for c in cartoes.values() if c["id"] in usados]}


def _entrada_esperada(conn) -> float | None:
    try:
        from extrato_camada import valor_esperado_entradas
        return valor_esperado_entradas(conn)
    except Exception:
        return None


def _entradas(mes: str, n: int) -> dict[str, float]:
    from extrato_camada import serie_entradas_por_regra
    s = serie_entradas_por_regra(n + 1, mes)
    return {m: float(v) for m, v in (s.get("total") or {}).get("valores", {}).items() if v}


def _aportes(inv: dict | None) -> dict[str, float]:
    return {m["mes"]: float(m["liquido"]) for m in (inv or {}).get("meses") or []}


def _metas(conn) -> list[dict[str, Any]]:
    return [{"id": l["id"], "nome": l["nome"], "emoji": l["emoji"], "alvo": l["alvo"], "prazo": l["prazo"],
             "tipo": l["tipo"], "referencia": l["referencia"], "valorManual": l["valor_manual"],
             "inicial": l["inicial"], "criadoEm": l["criado_em"], "arquivada": bool(l["arquivada"])}
            for l in conn.execute("SELECT * FROM plan_metas ORDER BY arquivada, ordem, criado_em")]


# ---------------------------------------------------------------- payload

def payload(mes: str = "") -> dict[str, Any]:
    hoje = _hoje()
    corrente = hoje.strftime("%Y-%m")
    mes = mes or corrente
    base = janela(mes)                    # meses fechados que medem o ritmo
    ano = janela(mes, 12)
    avisos: list[str] = []

    with fin.connect() as conn:
        garantir_tabelas(conn)
        conn.commit()
        metas_salvas = _metas(conn)
        fontes = _fontes(hoje)
        taxa, origem_taxa = _taxa_mes(conn, hoje)
        esperada = _entrada_esperada(conn)
    inv = fontes.pop("investimentos")
    if inv is None:
        avisos.append("Investimentos indisponível: patrimônio e aportes ficaram de fora.")

    try:
        entradas = _entradas(mes, 12)
    except Exception as e:
        entradas = {}
        avisos.append(f"Entradas indisponível: {e}")
    gastos = _gastos(ano + [mes], corrente)
    aportes = _aportes(inv)

    r_entrada = ritmo(entradas, base)
    r_gasto = ritmo(gastos, base)
    r_aporte = ritmo(aportes, base, usar_mediana=True)
    meses_base = sum(1 for m in base if m in gastos)
    if meses_base < JANELA:
        avisos.append(f"Ritmo medido em {meses_base} de {JANELA} meses: ainda há pouco histórico.")

    patrimonio = fontes["carteira"]

    resumo = {
        "patrimonio": round(patrimonio, 2),
        # Razão dos ritmos, não das somas: um resgate grande num mês zeraria
        # a taxa do ano sem dizer nada sobre o hábito.
        "taxaPoupanca": round(r_aporte / r_entrada * 100, 1) if r_aporte and r_entrada else None,
        "taxaPoupancaMes": (round(aportes.get(mes, 0) / entradas[mes] * 100, 1)
                            if entradas.get(mes) else None),
        "aporteRitmo": r_aporte,
        "aporteMes": round(aportes.get(mes, 0), 2),
        "custoVida": r_gasto,
        "entradaRitmo": r_entrada,
        "coberturaMeses": round(patrimonio / r_gasto, 1) if r_gasto else None,
        "liquidez30": round(fontes["liquidez"], 2),
        "mesesBase": meses_base,
    }

    # Projeção: ritmo de hoje e, para comparar, só o rendimento.
    aporte_proj = max(0.0, r_aporte or 0.0)
    pontos = projetar(patrimonio, aporte_proj, taxa, corrente)
    so_rende = projetar(patrimonio, 0, taxa, corrente)
    projecao = {
        "saldo": round(patrimonio, 2), "aporte": aporte_proj, "taxaMes": taxa,
        "origemTaxa": origem_taxa, "inicio": corrente, "pontos": pontos,
        "soRendendo": so_rende,
        "marcos": [{"valor": v, "mes": quando_atinge(pontos, v),
                    "semAporte": quando_atinge(so_rende, v)} for v in marcos(patrimonio)],
    }

    # Metas cadastradas, cada uma com o ritmo e o mês em que chega.
    pontos_carteira = pontos

    def estimado(atual: float) -> float | None:
        """Ritmo de uma parte da carteira: a fatia do aporte mais o rendimento."""
        if patrimonio <= 0 or atual <= 0:
            return None
        return aporte_proj * atual / patrimonio + atual * taxa / 100

    def ritmo_da_meta(m: dict, atual: float) -> tuple[float | None, str | None]:
        if m["tipo"] == "carteira":
            return aporte_proj, quando_atinge(pontos_carteira, float(m["alvo"]))
        decorridos = meses_entre(str(m.get("criadoEm") or corrente)[:7], corrente)
        if decorridos >= 3:
            medido = (atual - float(m.get("inicial") or 0)) / decorridos
            if medido > 0:
                return medido, None
        return (estimado(atual) if m["tipo"] != "manual" else None), None

    metas = []
    for m in metas_salvas:
        atual = _valor_ligado(m, fontes)
        por_mes, chega = ritmo_da_meta(m, atual)
        metas.append({**m, **progresso_meta(m, atual, por_mes, corrente, chega)})


    try:
        tendencia = _tendencia(r_gasto, corrente, hoje)
    except Exception as e:
        tendencia = None
        avisos.append(f"Previsão dos próximos meses indisponível: {e}")

    try:
        parcelamentos = _parcelamentos(corrente)
    except Exception as e:
        parcelamentos = None
        avisos.append(f"Parcelas de cartão indisponíveis: {e}")

    # Para frente: fluxo dos próximos meses, teto do variável e indicadores.
    entrada = esperada or r_entrada or 0.0
    fluxo = montar_fluxo(entrada, r_gasto or 0.0, (tendencia or {}).get("futuro") or [],
                         (tendencia or {}).get("eventos") or [], aporte_proj)
    prox = fluxo[0] if fluxo else None
    bruto = (tendencia or {}).get("bruto", {}).get(prox["mes"], {}) if prox else {}
    teto = None
    if prox:
        teto = {"mes": prox["mes"], "entrada": prox["entrada"], "contratado": round(prox["fixas"] + prox["parcelas"], 2),
                "aporte": round(aporte_proj, 2),
                "valor": round(prox["entrada"] - prox["fixas"] - prox["parcelas"] - aporte_proj, 2),
                "variavelMedio": prox["variavel"],
                "jaNaFatura": round(bruto.get("cobrado", 0.0), 2)}
    rend_mes = patrimonio * taxa / 100
    indicadores = {
        "parcelasPct": round(prox["parcelas"] / entrada * 100, 1) if prox and entrada else None,
        "contratadoPct": round((prox["fixas"] + prox["parcelas"]) / entrada * 100, 1) if prox and entrada else None,
        "refParcelas": REF_PARCELAS,
        "rendimentoMes": round(rend_mes, 2),
        "liberdadePct": round(rend_mes / r_gasto * 100, 1) if r_gasto else None,
        "numeroLiberdade": round(r_gasto * 12 * REGRA_4, -3) if r_gasto else None,
        "entradaOrigem": "valor esperado em Entradas" if esperada else "média de 6 meses",
    }

    return {
        "mes": mes, "corrente": corrente, "resumo": resumo, "projecao": projecao,
        "fluxo": fluxo, "teto": teto, "indicadores": indicadores,
        "eventos": [e for e in (tendencia or {}).get("eventos", []) if fluxo and e["mes"] <= fluxo[-1]["mes"]],
        "metas": metas,
        "parcelamentos": parcelamentos,
        "referencias": {"abas": fontes["abas"], "cdbs": sorted(fontes["cdb"])},
        "avisos": avisos,
    }


def _curto(v: float) -> str:
    if v >= 1_000_000:
        return f"R$ {v / 1_000_000:.1f} mi".replace(".0 ", " ").replace(".", ",")
    return f"R$ {v / 1000:.0f} mil"


# ---------------------------------------------------------------- escrita

def _numero(valor: Any, campo: str, minimo: float = 0) -> float:
    try:
        n = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{campo} inválido.")
    if not math.isfinite(n) or n < minimo:
        raise ValueError(f"{campo} inválido.")
    return round(n, 2)


def _validar_meta(dados: dict) -> dict[str, Any]:
    nome = str(dados.get("nome") or "").strip()[:60]
    if not nome:
        raise ValueError("Dê um nome para a meta.")
    alvo = _numero(dados.get("alvo"), "Valor da meta")
    if alvo <= 0:
        raise ValueError("Valor da meta inválido.")
    prazo = str(dados.get("prazo") or "").strip()[:7]
    if prazo:
        try:
            datetime.strptime(prazo, "%Y-%m")
        except ValueError:
            raise ValueError("Prazo deve ser um mês (AAAA-MM).")
    tipo = str(dados.get("tipo") or "manual")
    if tipo not in TIPOS:
        raise ValueError("Tipo de meta inválido.")
    referencia = str(dados.get("referencia") or "").strip()
    if tipo in ("aba", "cdb") and not referencia:
        raise ValueError("Escolha a aba ou o CDB da meta.")
    valor_manual = _numero(dados.get("valorManual") or 0, "Valor guardado") if tipo == "manual" else 0.0
    # Um emoji só (pode ter vários code points, como bandeiras e tons de pele).
    emoji = str(dados.get("emoji") or "").strip()[:8]
    return {"nome": nome, "emoji": emoji, "alvo": alvo, "prazo": prazo, "tipo": tipo,
            "referencia": referencia if tipo in ("aba", "cdb") else "", "valor_manual": valor_manual}


def salvar_meta(dados: dict) -> dict[str, Any]:
    m = _validar_meta(dados)
    meta_id = str(dados.get("id") or "")
    with fin.connect() as conn:
        garantir_tabelas(conn)
        if meta_id:
            if not conn.execute("SELECT 1 FROM plan_metas WHERE id = ?", (meta_id,)).fetchone():
                raise ValueError("Meta não encontrada.")
            conn.execute("UPDATE plan_metas SET nome=?, emoji=?, alvo=?, prazo=?, tipo=?, referencia=?, "
                         "valor_manual=? WHERE id=?", (*m.values(), meta_id))
        else:
            # O ritmo de uma meta ligada é medido a partir do valor de hoje.
            inicial = _valor_ligado({**m, "valorManual": m["valor_manual"]},
                                    _fontes(_hoje()) if m["tipo"] != "manual" else {})
            conn.execute("INSERT INTO plan_metas (id, nome, emoji, alvo, prazo, tipo, referencia, "
                         "valor_manual, inicial, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (uuid4().hex, *m.values(), inicial, _hoje().isoformat()))
        conn.commit()
    return payload()


def arquivar_meta(meta_id: str, arquivada: bool = True) -> dict[str, Any]:
    with fin.connect() as conn:
        garantir_tabelas(conn)
        if not conn.execute("UPDATE plan_metas SET arquivada=? WHERE id=?",
                            (int(bool(arquivada)), meta_id)).rowcount:
            raise ValueError("Meta não encontrada.")
        conn.commit()
    return payload()


def excluir_meta(meta_id: str) -> dict[str, Any]:
    with fin.connect() as conn:
        garantir_tabelas(conn)
        conn.execute("DELETE FROM plan_metas WHERE id=?", (meta_id,))
        conn.commit()
    return payload()
