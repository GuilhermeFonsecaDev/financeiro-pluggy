"""Detalhe de uma transação: o que a Pluggy manda além da linha do extrato.

Alimenta o painel lateral de Transações. Nada aqui grava no banco.

O que existe (medido nos dados reais): descrição original do banco (de onde sai
cidade e país das compras no cartão), código MCC do tipo de loja, data e hora,
parcelas, final do cartão, fatura; e, em PIX/TED/boleto, nome, banco, documento
e conta de quem pagou ou recebeu. Campo que não vier simplesmente não aparece.

Documento e conta saem sempre MASCARADOS: são dados pessoais de terceiros.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import extrato_camada as cam

# MCC (Merchant Category Code) -> tipo de loja. Só os comuns; o resto mostra o
# número cru.
MCC = {
    "4111": "Transporte urbano", "5021": "Móveis", "5094": "Joias", "5944": "Joalheria", "5310": "Loja de descontos", "5300": "Atacado", "4121": "Táxi e aplicativo", "4131": "Ônibus",
    "4511": "Companhia aérea", "4722": "Agência de viagem", "4784": "Pedágio",
    "4812": "Telefonia", "4814": "Telecomunicações", "4899": "TV e streaming",
    "4900": "Água, luz e gás", "5045": "Informática", "5200": "Material de construção",
    "5211": "Material de construção", "5251": "Ferragens", "5311": "Loja de departamento",
    "5331": "Variedades", "5399": "Varejo em geral", "5411": "Supermercado",
    "5422": "Açougue", "5441": "Doces e confeitaria", "5462": "Padaria",
    "5499": "Alimentação em geral", "5541": "Posto de combustível",
    "5542": "Posto de combustível", "5611": "Roupa masculina", "5621": "Roupa feminina",
    "5651": "Roupas", "5661": "Calçados", "5691": "Roupas", "5699": "Acessórios",
    "5712": "Móveis", "5719": "Casa e decoração", "5722": "Eletrodomésticos",
    "5732": "Eletrônicos", "5734": "Software", "5735": "Música", "5812": "Restaurante",
    "5813": "Bar", "5814": "Lanchonete e fast food", "5912": "Farmácia",
    "5921": "Bebidas", "5941": "Artigos esportivos", "5942": "Livraria",
    "5945": "Brinquedos e jogos", "5947": "Presentes", "5949": "Armarinho e tecidos",
    "5964": "Compra por catálogo", "5965": "Compra por catálogo", "5969": "Venda direta",
    "5977": "Cosméticos", "5992": "Floricultura", "5995": "Pet shop",
    "5999": "Varejo especializado", "6300": "Seguros", "7011": "Hotel",
    "7230": "Salão de beleza", "7298": "Estética", "7372": "Serviços de TI",
    "7399": "Serviços empresariais", "7512": "Aluguel de carro", "7523": "Estacionamento",
    "7538": "Oficina mecânica", "7542": "Lava-rápido", "7832": "Cinema",
    "7841": "Locadora e streaming", "7922": "Shows e ingressos", "7994": "Jogos eletrônicos",
    "7997": "Clube e academia", "8011": "Médico", "8021": "Dentista",
    "8062": "Hospital", "8071": "Laboratório", "8099": "Saúde",
    "8211": "Escola", "8220": "Faculdade", "8299": "Cursos", "8398": "Doação",
    "8999": "Serviços profissionais", "9399": "Governo", "9311": "Impostos",
}

# ISPB -> banco. Os mais comuns em PIX.
BANCOS = {
    "00000000": "Banco do Brasil", "00360305": "Caixa", "60701190": "Itaú",
    "60746948": "Bradesco", "90400888": "Santander", "18236120": "Nubank",
    "00416968": "Inter", "30306294": "BTG Pactual", "31872495": "C6 Bank",
    "22896431": "PicPay", "10573521": "Mercado Pago", "08561701": "PagBank",
    "02038232": "Sicoob", "01181521": "Sicredi", "13140088": "Banco Pan",
    "92894922": "Banrisul", "07679404": "Banco Topázio", "33264668": "XP",
    "59588111": "Banco Votorantim", "17184037": "Mercantil", "62232889": "Banco Safra",
    "04902979": "Banco da Amazônia", "13370835": "Banco Original", "18189547": "Neon",
    "12896957": "Bmg", "00315557": "Unicred", "36113876": "Banco Will",
    "16501555": "Stone", "13935893": "Ailos", "09089356": "Agibank",
}

PAISES = {"BRA": "Brasil", "USA": "Estados Unidos", "IRL": "Irlanda", "GBR": "Reino Unido",
          "NLD": "Holanda", "LUX": "Luxemburgo", "SGP": "Singapura", "CHN": "China"}


def _mascarar_documento(valor: str) -> str:
    dig = re.sub(r"\D", "", valor or "")
    if len(dig) == 11:   # CPF: só os 6 do meio, como o próprio banco mostra
        return f"•••.{dig[3:6]}.{dig[6:9]}-••"
    if len(dig) == 14:   # CNPJ é público, mas segue o mesmo cuidado
        return f"{dig[:2]}.{dig[2:5]}.•••/••••-{dig[12:]}"
    return "•••" if dig else ""


def _mascarar_conta(valor: str) -> str:
    dig = re.sub(r"\D", "", str(valor or ""))
    return f"••••-{dig[-1]}" if dig else ""


def _local(raw: str) -> dict[str, str] | None:
    """Cidade e país da descrição original de compra no cartão.

    Formato de bandeira: loja em ~22 colunas, cidade em ~13 e o país em 3
    letras no fim ("POSTO ITAGAS           ITAGUAI       BRA").
    """
    texto = (raw or "").rstrip().upper()   # há banco que manda tudo minúsculo
    # Colunas fixas: loja nas 22 primeiras, país nas 3 últimas, cidade no meio.
    # (Separar por espaços duplos falhava quando a loja ocupa as 22 colunas.)
    m = re.match(r"^.{30,}\s([A-Z]{3})$", texto)
    if not m:
        return None
    cidade = re.sub(r"\s+", " ", texto[22:-4]).strip().title()
    pais = PAISES.get(m.group(1), m.group(1))
    if not cidade or len(cidade) < 3:
        return None
    return {"cidade": cidade, "pais": pais}


def _chave_lojista(descricao: str) -> str:
    """Nome da loja sem contador de parcela, números e cidade."""
    d = (descricao or "").upper()
    d = re.sub(r"\b\d{1,2}\s*/\s*\d{1,2}\b", " ", d)
    d = re.split(r"\s{2,}", d.strip())[0]
    d = re.sub(r"[^A-Z ]", " ", d)
    return re.sub(r"\s+", " ", d).strip()[:18]


def _somar_meses(competencia: str, n: int) -> str:
    a, m = int(competencia[:4]), int(competencia[5:7]) - 1 + n
    return f"{a + m // 12}-{m % 12 + 1:02d}"


def payload(transacao_id: str) -> dict[str, Any]:
    with cam._abrir() as conn:
        cam.garantir_extrato_materializado(conn)
        linha = conn.execute(
            """SELECT e.transacao_id, e.conta_id, e.data, e.descricao, e.valor, e.tipo,
                      e.parcela_numero, e.parcela_total, e.competencia_fatura,
                      t.raw_json, c.tipo AS conta_tipo, c.nome AS conta_nome
               FROM extrato_efetivo_cache e
               JOIN pluggy_transacoes t ON t.transacao_id = e.transacao_id
               LEFT JOIN pluggy_contas c ON c.conta_id = e.conta_id
               WHERE e.transacao_id = ?""", (transacao_id,)).fetchone()
        if not linha:
            raise ValueError("Transação não encontrada.")
        raw = json.loads(linha["raw_json"] or "{}")
        cc = raw.get("creditCardMetadata") or {}
        pg = raw.get("paymentData") or {}
        saida: dict[str, Any] = {"id": transacao_id, "operacao": raw.get("operationType") or ""}

        descricao_raw = raw.get("descriptionRaw") or ""
        cartao = linha["conta_tipo"] == "CREDIT"
        if cartao:
            local = _local(descricao_raw)
            if local:
                saida["local"] = local
        mcc = str(cc.get("payeeMCC") or "")
        if mcc:
            saida["mcc"] = {"codigo": mcc, "nome": MCC.get(mcc, "")}
        hora = cc.get("transactionDateTime") or ""
        if hora and "T" in hora and not hora.endswith("T00:00:00.000Z"):
            saida["dataHora"] = hora
        if cc.get("cardNumber"):
            saida["finalCartao"] = str(cc["cardNumber"])[-4:]
        if cartao and linha["competencia_fatura"]:
            saida["fatura"] = linha["competencia_fatura"]

        # Parcelas: o mês de cada uma, a partir da fatura desta.
        n, total = linha["parcela_numero"], linha["parcela_total"]
        if total and total > 1 and n and linha["competencia_fatura"]:
            base = linha["competencia_fatura"]
            meses = [_somar_meses(base, i - n) for i in range(1, total + 1)]
            valor = abs(float(linha["valor"] or 0))
            saida["parcelas"] = {"numero": n, "total": total, "meses": meses,
                                 "valorParcela": valor, "totalCompra": round(valor * total, 2),
                                 "restantes": total - n, "aPagar": round(valor * (total - n), 2),
                                 "compra": (cc.get("purchaseDate") or "")[:10]}

        # PIX / TED / boleto: a outra ponta.
        debito = linha["tipo"] == "DEBIT"
        outra = pg.get("receiver") if debito else pg.get("payer")
        if outra:
            doc = (outra.get("documentNumber") or {}).get("value") or ""
            ispb = str(outra.get("routingNumberISPB") or outra.get("routingNumber") or "")
            parte = {
                "papel": "Quem recebeu" if debito else "Quem pagou",
                "nome": outra.get("name") or "",
                "banco": BANCOS.get(ispb.zfill(8), "") if ispb else "",
                "documento": _mascarar_documento(doc),
                "tipoDocumento": (outra.get("documentNumber") or {}).get("type") or "",
                "agencia": str(outra.get("branchNumber") or ""),
                "conta": _mascarar_conta(outra.get("accountNumber")),
            }
            if any(parte[k] for k in ("nome", "banco", "documento")):
                saida["contraparte"] = parte
            chave_doc = doc.strip()
        else:
            chave_doc = ""

        # Histórico dos últimos 12 meses com a mesma loja / pessoa.
        hoje = date.today()
        inicio = f"{hoje.year - 1}-{hoje.month:02d}-01"
        meses12 = [_somar_meses(f"{hoje.year}-{hoje.month:02d}", -i) for i in range(11, -1, -1)]
        por_mes = {m: 0.0 for m in meses12}
        qtd, enviado, recebido = 0, 0.0, 0.0
        if chave_doc:
            linhas = conn.execute(
                """SELECT substr(e.data,1,7) mes, e.valor, e.tipo FROM extrato_efetivo_cache e
                   JOIN pluggy_transacoes t ON t.transacao_id = e.transacao_id
                   WHERE e.data >= ? AND e.incluida = 1
                     -- só o lado da outra pessoa: o seu próprio documento
                     -- aparece como pagador em todo PIX que você manda
                     AND ((e.tipo = 'DEBIT' AND JSON_EXTRACT(t.raw_json, '$.paymentData.receiver.documentNumber.value') = ?)
                       OR (e.tipo <> 'DEBIT' AND JSON_EXTRACT(t.raw_json, '$.paymentData.payer.documentNumber.value') = ?))""",
                (inicio, chave_doc, chave_doc)).fetchall()
            for r in linhas:
                v = abs(float(r["valor"] or 0))
                if r["tipo"] == "DEBIT":
                    qtd += 1; enviado += v
                    if r["mes"] in por_mes: por_mes[r["mes"]] += v
                else:
                    recebido += v
            rotulo = "esta pessoa" if len(re.sub(r"\D", "", chave_doc)) == 11 else "esta empresa"
        else:
            chave = _chave_lojista(linha["descricao"])
            if len(chave) >= 3:
                for r in conn.execute(
                        """SELECT substr(data,1,7) mes, valor, tipo, descricao, parcela_numero FROM extrato_efetivo_cache
                           WHERE data >= ? AND conta_id IN (SELECT conta_id FROM pluggy_contas WHERE tipo = ?)
                             AND incluida = 1""",
                        (inicio, linha["conta_tipo"] or "")):
                    if _chave_lojista(r["descricao"]) != chave:
                        continue
                    if r["tipo"] != "DEBIT":
                        continue
                    v = abs(float(r["valor"] or 0))
                    if (r["parcela_numero"] or 1) == 1:
                        qtd += 1
                    enviado += v
                    if r["mes"] in por_mes: por_mes[r["mes"]] += v
            rotulo = "esta loja"
        if qtd:
            saida["historico"] = {
                "rotulo": rotulo, "quantidade": qtd, "total": round(enviado, 2),
                "media": round(enviado / qtd, 2), "recebido": round(recebido, 2),
                "meses": [{"mes": m, "valor": round(por_mes[m], 2)} for m in meses12],
                "porDocumento": bool(chave_doc),
            }
        return saida
