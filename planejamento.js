/* Planejamento: só o que vem pela frente. Quanto dá para gastar no próximo
   mês, quanto da renda já está comprometido, quanto dá para aportar nos
   próximos meses, para onde o ritmo leva, as metas e as parcelas de
   cartão. O histórico fica na Visão Geral.

   Visual minimalista: cada card tem título, uma linha de legenda com os
   números, o gráfico sem eixo nem grade e uma frase de rodapé. O detalhe
   aparece no mouse.

   Os números vêm prontos de /api/planejamento; "e se" e "Posso comprar?" são
   refeitos aqui no navegador, sem gravar nada. */
(function () {
const $ = id => document.getElementById(id);
let P = null;
let META = null;
// Horizonte dos gráficos (meses à frente), lembrado neste navegador.
const PERIODOS = [3, 6, 12, 18, 24];
let N = (() => { try { const v = Number(localStorage.getItem("planejamento.periodo")); return PERIODOS.includes(v) ? v : 6; } catch { return 6; } })();
// Rótulos de mês sem amontoar: até 6 marcas espalhadas.
const marcasDe = n => { const passo = Math.max(1, Math.ceil((n - 1) / 6)); const r = []; for (let i = 0; i < n; i += passo) r.push(i); if (r.at(-1) !== n - 1) r.push(n - 1); return r; };

const curto = v => {
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e6) return `${s}R$ ${(a / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 2 })} mi`;
  if (a >= 1000) return `${s}R$ ${(a / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mil`;
  return `${s}R$ ${Math.round(a).toLocaleString("pt-BR")}`;
};
const pct = v => `${v.toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;
const mesCurto = m => {
  const [a, n] = m.split("-");
  return `${MESES[Number(n) - 1].slice(0, 3).toLowerCase()}/${a.slice(2)}`;
};
const nomeMes = m => MESES[Number(m.split("-")[1]) - 1].toLowerCase();
const mesesEntre = (a, b) => {
  const [ya, ma] = a.split("-").map(Number), [yb, mb] = b.split("-").map(Number);
  return (yb - ya) * 12 + (mb - ma);
};
const mesMais = (m, n) => {
  const [a, b] = m.split("-").map(Number), t = a * 12 + b - 1 + n;
  return `${Math.floor(t / 12)}-${String(t % 12 + 1).padStart(2, "0")}`;
};
const emMeses = n => n <= 0 ? "agora" : n < 12 ? `${n} ${n === 1 ? "mês" : "meses"}`
  : `${Math.floor(n / 12)} ano${n >= 24 ? "s" : ""}${n % 12 ? ` e ${n % 12} ${n % 12 === 1 ? "mês" : "meses"}` : ""}`;
const chip = (txt, tipo = "") => `<span class="pl-chip ${tipo}">${txt}</span>`;

async function carregar() {
  setEstado("estado", "Calculando…");
  try {
    P = await pedir("/planejamento");
    setEstado("estado", "");
    desenhar();
  } catch (e) { setEstado("estado", e.message, "erro"); }
}

function desenhar() {
  $("btnPeriodo").textContent = `Próximos ${N} meses ▾`;
  document.querySelectorAll("[data-periodo]").forEach(b => b.classList.toggle("marcado", Number(b.dataset.periodo) === N));
  $("avisos").hidden = !P.avisos.length;
  $("avisos").textContent = P.avisos.join(" · ");
  kpis(); fluxo(); projecao(); parcelas(); metas();
}

/* ------------------------------------------------------------ gráfico de linha */

// Curva suave (Catmull-Rom em Bézier) pelos pontos, sem passar do topo/fundo.
function curva(pts) {
  if (pts.length < 2) return "";
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

/* Linhas finas, ponto vazado no fim, rótulos de mês discretos e uma faixa
   invisível por ponto com a dica. `series`: [{valores, cor, tracejada}]. */
function linhas(svg, meses, series, { dica, rotulos = [], marcas = [] } = {}) {
  const W = svg.clientWidth || 700, H = Number(svg.getAttribute("height")), T = 10, B = 20, L = 6, R = 10;
  const todos = [...series.flatMap(s => s.valores), ...marcas.map(m => m.valor)];
  const min = Math.min(...todos), max = Math.max(...todos), folga = (max - min) * .15 || Math.abs(max) * .1 || 1;
  const x = i => L + i * (W - L - R) / Math.max(1, meses.length - 1);
  const y = v => T + (H - T - B) * (1 - (v - (min - folga)) / ((max + folga) - (min - folga)));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  let h = marcas.map(m => `<line class="marca" x1="${L}" x2="${W - R}" y1="${y(m.valor)}" y2="${y(m.valor)}"/>
    <text class="alvo" x="${L}" y="${y(m.valor) - 4}">${m.rotulo}</text>`).join("");
  for (const s of series) {
    const pts = s.valores.map((v, i) => [x(i), y(v)]);
    h += `<path class="linha ${s.tracejada ? "tracejada" : ""}" stroke="${s.cor}" d="${curva(pts)}"/>`;
    // Uma bolinha por mês, nas duas linhas; a do último é maior. O primeiro
    // ponto da tracejada (o "e se") é o de hoje, que a principal já marca.
    h += pts.map((p, i) => s.tracejada && i === 0 ? "" : `<circle class="fim" stroke="${s.cor}" cx="${p[0]}" cy="${p[1]}" r="${i === pts.length - 1 ? 4 : 2.5}"/>`).join("");
  }
  // Série com dica própria: cada bolinha mostra só o valor daquela linha
  // (área de toque maior que a bolinha). Sem isso, uma dica por mês.
  const porSerie = series.some(s => s.dica);
  if (porSerie) {
    for (const s of series) {
      if (!s.dica) continue;
      h += s.valores.map((v, i) => s.tracejada && i === 0 ? "" : `<circle class="hover" cx="${x(i)}" cy="${y(v)}" r="10"><title>${esc(s.dica(i))}</title></circle>`).join("");
    }
  }
  h += rotulos.map(i => `<text class="eixo-m" x="${x(i)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === meses.length - 1 ? "end" : "middle"}">${i === 0 && meses[0] === P.corrente ? "hoje" : mesCurto(meses[i])}</text>`).join("");
  const w = (W - L - R) / Math.max(1, meses.length - 1);
  if (!porSerie) h += meses.map((m, i) => `<rect class="hover" x="${x(i) - w / 2}" y="0" width="${w}" height="${H}"><title>${esc(dica(i))}</title></rect>`).join("");
  svg.innerHTML = h;
}

/* ------------------------------------------------------------ KPIs */

function kpis() {
  const t = P.teto, r = P.resumo, ind = P.indicadores;
  if (t) {
    $("kTetoRot").textContent = `Livre para gastar em ${nomeMes(t.mes)}`;
    $("kTeto").innerHTML = `<span class="${t.valor < 0 ? "ruim" : ""}">${curto(t.valor)}</span>`
      + (t.valor >= t.variavelMedio ? chip("seu variável cabe", "bom") : chip(`passa ${curto(t.variavelMedio - t.valor)}`, "ruim"));
    $("kTetoApoio").innerHTML = `<b>${curto(t.jaNaFatura)}</b> já na fatura · variável médio ${curto(t.variavelMedio)}`;
    $("cTeto").title = `${fmtBRL(t.entrada)} de entrada (${ind.entradaOrigem}) − ${fmtBRL(t.contratado)} já contratados (contas fixas e parcelas) − ${fmtBRL(t.aporte)} de aporte = ${fmtBRL(t.valor)} para o variável.`;
  }
  const vA = r.aporteRitmo ? (r.aporteMes / r.aporteRitmo - 1) * 100 : null;
  $("kAporte").innerHTML = (r.aporteRitmo != null ? `${curto(r.aporteRitmo)}<small style="font-size:14px;color:var(--text-3);font-weight:500">/mês</small>` : "—")
    + (vA != null ? chip(`${vA >= 0 ? "+" : "−"}${pct(Math.abs(vA))} em ${nomeMes(P.corrente).slice(0, 3)}`, vA >= 0 ? "bom" : "ruim") : "");
  $("kAporteApoio").innerHTML = r.taxaPoupanca != null ? `<b>${Math.round(r.taxaPoupanca)}%</b> do que entra vira investimento` : "";
  $("cAporte").title = "Mediana do líquido investido (aplicações menos resgates) nos últimos 6 meses fechados."
    + (ind.liberdadePct != null ? `\nLiberdade financeira: o rendimento da carteira já paga ${pct(ind.liberdadePct)} do seu custo de vida (${fmtBRL(ind.rendimentoMes)}/mês).` : "");
  if (ind.contratadoPct != null) {
    const p = ind.parcelasPct;
    $("kComp").innerHTML = pct(ind.contratadoPct) + chip(`parcelas ${pct(p)}`, p > ind.refParcelas ? "ruim" : "bom");
    $("kCompApoio").innerHTML = `contas fixas e parcelas · referência para parcelas até <b>${ind.refParcelas}%</b>`;
    $("cComp").title = `Contas fixas e parcelas de cartão do próximo mês sobre a entrada esperada. A régua de mercado para parcelas é até ${ind.refParcelas}% da renda líquida.`;
  }
}

/* ------------------------------------------------------------ aporte previsto */

function fluxo() {
  const F = P.fluxo.slice(0, N), svg = $("fluxo");
  if (!F.length) { $("fluxoLeg").textContent = ""; svg.innerHTML = ""; $("fluxoFrase").textContent = "Sem previsão: faltam dados de Contas Fixas 2.0."; return; }
  // Tudo o que sobra vira aporte: a sobra do mês É o aporte previsto.
  const ritmo = P.teto?.aporte || 0;
  const pior = F.reduce((a, m) => m.sobra < a.sobra ? m : a, F[0]);
  const total = F.reduce((a, m) => a + Math.max(0, m.sobra), 0), media = total / F.length;
  $("fluxoLeg").innerHTML = `<span><i style="background:var(--pl-verde)"></i>Próximos ${F.length} meses <b>${curto(total)}</b></span>`
    + `<span class="sep">·</span><span>Média <b>${curto(media)}/mês</b></span>`
    + `<span class="sep">·</span><span>Menor <b>${mesCurto(pior.mes)}</b></span>`
    + (ritmo ? `<span class="sep">·</span><span>Seu ritmo <b>${curto(ritmo)}</b></span>` : "");
  linhas(svg, F.map(m => m.mes), [{ valores: F.map(m => m.sobra), cor: "var(--pl-verde)" }], {
    rotulos: marcasDe(F.length),
    marcas: ritmo ? [{ valor: ritmo, rotulo: "seu ritmo" }] : [],
    dica: i => { const m = F[i]; return `${mesCurto(m.mes)}: aporte previsto ${fmtBRL(m.sobra)}\n`
      + `entrada ${fmtBRL(m.entrada)} − contas fixas ${fmtBRL(m.fixas)} − parcelas ${fmtBRL(m.parcelas)} − variável médio ${fmtBRL(m.variavel)}`
      + m.eventos.map(e => `\n${e.texto}: ${e.valor < 0 ? "−" : "+"}${fmtBRL(Math.abs(e.valor))}`).join(""); },
  });
  // O porquê, numa frase: o maior evento do mês mais apertado e do melhor.
  const negativos = F.filter(m => m.sobra < 0);
  const motivo = m => { const e = [...m.eventos].sort((a, b) => Math.abs(b.valor) - Math.abs(a.valor))[0]; return e ? `<b>${esc(e.texto)}</b>` : ""; };
  const melhor = F.reduce((a, m) => m.sobra > a.sobra ? m : a, F[0]);
  $("fluxoFrase").innerHTML = negativos.length
    ? `<span class="ruim">${negativos.map(m => mesCurto(m.mes)).join(", ")} fecha${negativos.length > 1 ? "m" : ""} no negativo</span>: faltam ${curto(-pior.sobra)} em ${mesCurto(pior.mes)}.`
    : `${mesCurto(pior.mes)} é o mês mais apertado${motivo(pior) ? `, com ${motivo(pior)}` : ""}`
      + (melhor !== pior && melhor.mes > pior.mes ? `; a partir de ${mesCurto(melhor.mes)} sobra mais${motivo(melhor) ? ` (${motivo(melhor)})` : ""}.` : ".");
}

/* ------------------------------------------------------------ projeção */

function serie(saldo, aporte, taxa, n) {
  const out = [];
  let s = saldo;
  for (let i = 0; i < n; i++) { s = s * (1 + taxa / 100) + aporte; out.push(s); }
  return out;
}

function projecao() {
  const pr = P.projecao, extra = lerValor($("extra").value);
  const pontos = pr.pontos.slice(0, N);
  const meses = [pr.inicio, ...pontos.map(p => p.mes)];
  const base = [pr.saldo, ...pontos.map(p => p.saldo)];
  // "E se": o valor digitado é o aporte mensal inteiro (substitui o ritmo).
  const ese = extra ? [pr.saldo, ...serie(pr.saldo, extra, pr.taxaMes, pontos.length)] : null;
  const quando = (s, alvo) => { const i = s.findIndex(v => v >= alvo); return i < 0 ? null : i; };
  // Linhas de números só com a bolinha; o que cada cor é fica na legenda à
  // direita do título.
  $("marcos").innerHTML = `<span><i style="background:var(--pl-azul)"></i><b>${curto(pr.saldo)}</b></span>`
    + `<span class="sep">·</span><span>Em ${mesCurto(meses.at(-1))} <b>${curto(base.at(-1))}</b></span>`
    + pr.marcos.map(m => { const i = quando(base, m.valor); return i == null ? "" : `<span class="sep">·</span><span>${curto(m.valor).replace("R$ ", "")} <b>${mesCurto(meses[i])}</b></span>`; }).join("");
  $("projLeg").innerHTML = `<span><i style="background:var(--pl-azul)"></i>Seu ritmo · ${curto(pr.aporte)}/mês</span>`
    + (ese ? `<span><i style="background:var(--pl-verde)"></i>Aportando ${curto(extra)}/mês</span>` : "");
  // Segunda linha de legenda, em verde, para o "e se" (só com valor digitado).
  $("marcosEse").hidden = !ese;
  if (ese) {
    $("marcosEse").innerHTML = `<span><i style="background:var(--pl-verde)"></i><b>${curto(pr.saldo)}</b></span>`
      + `<span class="sep">·</span><span>Em ${mesCurto(meses.at(-1))} <b>${curto(ese.at(-1))}</b></span>`
      + pr.marcos.map(m => { const i = quando(ese, m.valor); return i == null ? "" : `<span class="sep">·</span><span>${curto(m.valor).replace("R$ ", "")} <b>${mesCurto(meses[i])}</b></span>`; }).join("");
  }
  const series = [{ valores: base, cor: "var(--pl-azul)", dica: i => `${mesCurto(meses[i])} · no seu ritmo (${curto(pr.aporte)}/mês): ${fmtBRL(base[i])}` }];
  if (ese) series.push({ valores: ese, cor: "var(--pl-verde)", tracejada: true,
    // Cada linha mostra só o próprio valor: a comparação fica na frase do rodapé.
    dica: i => `${mesCurto(meses[i])} · aportando ${curto(extra)}/mês: ${fmtBRL(ese[i])}` });
  linhas($("grafico"), meses, series, {
    rotulos: marcasDe(meses.length),
    dica: i => `${mesCurto(meses[i])}: ${fmtBRL(base[i])}${ese ? ` · aportando ${fmtBRL(extra)}/mês ${fmtBRL(ese[i])}` : ""}`,
  });
  // "E se": o primeiro marco chega antes ou depois do que no seu ritmo.
  const m = pr.marcos.find(x => quando(base, x.valor) != null);
  if (ese && m) {
    const a = quando(base, m.valor), b = quando(ese, m.valor);
    $("eseTxt").innerHTML = b == null ? `<b>${curto(m.valor)}</b> não chega no período.`
      : b < a ? `<b>${curto(m.valor)}</b> chega <b>${emMeses(a - b)}</b> antes.`
      : b > a ? `<b>${curto(m.valor)}</b> chega <b>${emMeses(b - a)}</b> depois.`
      : `<b>${curto(m.valor)}</b> chega no mesmo mês.`;
  } else $("eseTxt").innerHTML = "";
  $("projTit").title = `Aporte de ${fmtBRL(pr.aporte)}/mês (mediana de 6 meses) e ${pr.taxaMes.toLocaleString("pt-BR", { maximumFractionDigits: 2 })}% a.m. (${pr.origemTaxa}).`;
}

/* ------------------------------------------------------------ parcelas de cartão */

let PARC_MES = null;              // mês escolhido no gráfico
let PARC_CARTAO = null;           // cartão escolhido em "Por cartão" (filtra as parcelas)
const CORES_RESERVA = ["#4a95ea", "#e8954a", "#9085e9", "#3fbf7f", "#c9a227", "#e5787a"];

function corCartao(id) {
  const c = P.parcelamentos.cartoes, i = c.findIndex(x => x.id === id);
  return c[i]?.cor || CORES_RESERVA[Math.max(0, i) % CORES_RESERVA.length];
}

// Um quadradinho por parcela: pagas em cinza, a do mês na cor do cartão,
// as que faltam vazias. Compra longa (mais de 24) fica só com o número.
function progresso(atual, total, cor) {
  if (total > 24) return "";
  return Array.from({ length: total }, (_, k) => k + 1 < atual ? '<i class="pago"></i>'
    : k + 1 === atual ? `<i style="background:${cor}"></i>` : "<i></i>").join("");
}

function parcelas() {
  const A = P.parcelamentos;
  $("secParcelas").hidden = !A || !A.compras.length;
  if (!A || !A.compras.length) return;
  const meses = A.meses.slice(0, N);
  const noPeriodo = c => c.parcelas.filter(x => x.mes <= meses.at(-1)).reduce((a, x) => a + x.valor, 0);
  const saldoPeriodo = A.compras.reduce((a, c) => a + noPeriodo(c), 0);
  if (!PARC_MES || !meses.some(m => m.mes === PARC_MES)) PARC_MES = meses[0].mes;
  const prox = meses[0], em6 = meses[Math.min(5, meses.length - 1)];
  const ultima = A.compras.map(c => c.terminaEm).filter(Boolean).sort().at(-1);

  $("parcTotal").innerHTML = curto(saldoPeriodo)
    + (em6.total < prox.total ? chip(`−${curto(prox.total - em6.total)}/mês até ${mesCurto(em6.mes)}`, "bom") : "");
  $("parcTit").title = `${fmtBRL(saldoPeriodo)} em parcelas nos próximos ${N} meses (${fmtBRL(A.saldo)} em ${A.meses.length} meses).`;

  // Barras arredondadas: o mês escolhido em cor cheia, os outros suaves.
  const svg = $("parcGraf"), W = svg.clientWidth || 700, H = Number(svg.getAttribute("height")), B = 20, T = 18;
  const max = Math.max(...meses.map(m => m.total), 1), w = W / meses.length, bw = Math.min(30, w * .5);
  const rotulo = new Set(marcasDe(meses.length).concat(meses.length <= 12 ? meses.map((_, i) => i) : []));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = meses.map((m, i) => {
    const h = Math.max(m.total ? 4 : 0, m.total / max * (H - T - B)), sel = m.mes === PARC_MES;
    return `<g class="col" data-parc-mes="${m.mes}"><title>${mesCurto(m.mes)}: ${fmtBRL(m.total)} em ${m.compras} parcela${m.compras === 1 ? "" : "s"}${m.terminam ? ` · ${m.terminam} termina${m.terminam > 1 ? "m" : ""}` : ""}</title>
      <rect class="hover" x="${i * w}" y="0" width="${w}" height="${H}"/>
      ${m.total ? `<rect class="pbar" x="${i * w + (w - bw) / 2}" y="${H - B - h}" width="${bw}" height="${h}" rx="7" fill="var(--pl-verde)" opacity="${sel ? 1 : .35}"/>` : ""}
      ${sel && m.total ? `<text class="alvo" x="${i * w + w / 2}" y="${H - B - h - 6}" text-anchor="middle" style="fill:var(--text);font-weight:650">${curto(m.total).replace("R$ ", "")}</text>` : ""}
      ${rotulo.has(i) || sel ? `<text class="eixo-m" x="${i * w + w / 2}" y="${H - 4}" text-anchor="middle" style="${sel ? "fill:var(--text)" : ""}">${mesCurto(m.mes)}</text>` : ""}</g>`;
  }).join("");

  // Por cartão, no mês escolhido.
  const mes = meses.find(m => m.mes === PARC_MES);
  const porCartao = Object.entries(mes.porCartao).sort((a, b) => b[1] - a[1]);
  $("parcCartTit").textContent = `Por cartão · ${nomeMes(PARC_MES)}`;
  // Cartão escolhido que não tem parcela neste mês: volta para todos.
  if (PARC_CARTAO && !mes.porCartao[PARC_CARTAO]) PARC_CARTAO = null;
  $("parcCartoes").innerHTML = porCartao.map(([id, v]) => {
    const nome = A.cartoes.find(c => c.id === id)?.nome || "Cartão", fr = mes.total ? v / mes.total : 0;
    const apagado = PARC_CARTAO && PARC_CARTAO !== id;
    return `<div class="bl clicavel" data-parc-cartao="${esc(id)}" role="button" tabindex="0" aria-pressed="${PARC_CARTAO === id}"
      title="${PARC_CARTAO === id ? "Mostrar todos os cartões" : `Ver só as parcelas de ${esc(nome)}`}" style="${apagado ? "opacity:.45" : ""}${PARC_CARTAO === id ? ";box-shadow:inset 0 0 0 1px " + corCartao(id) : ""}"><span class="f" style="width:${fr * 100}%;background:${corCartao(id)}"></span>
      <span class="nome">${esc(nome)}</span><span class="val">${fmtBRL(v)}${chip(pct(fr * 100))}</span></div>`;
  }).join("") || `<p class="rodape">Sem parcelas neste mês.</p>`;

  // Cada parcela do mês, com a fração já paga da compra na barra de fundo.
  const doMes = A.compras.filter(c => !PARC_CARTAO || c.cartao === PARC_CARTAO)
    .map(c => ({ c, p: c.parcelas.find(x => x.mes === PARC_MES) })).filter(x => x.p)
    // Quem está acabando vem primeiro: menos parcelas restantes depois desta;
    // empate, a de maior valor.
    .sort((a, b) => (a.c.total - a.p.atual) - (b.c.total - b.p.atual) || b.p.valor - a.p.valor);
  const nomeFiltro = PARC_CARTAO ? A.cartoes.find(c => c.id === PARC_CARTAO)?.nome : "";
  $("parcMesTit").textContent = `Parcelas de ${nomeMes(PARC_MES)}${nomeFiltro ? ` · ${nomeFiltro}` : ""} · ${doMes.length}`;
  $("parcLista").innerHTML = doMes.map(({ c, p }) => {
    const nome = A.cartoes.find(x => x.id === c.cartao)?.nome || "";
    const fim = p.atual >= c.total;
    return `<div class="bl" title="${esc(nome)} · ${esc(c.descricao)} · ${fim ? "última parcela" : c.terminaEm ? `até ${mesCurto(c.terminaEm)}` : "mais de 12 meses"} · restam ${fmtBRL(c.saldo)}">
      <span class="nome"><i class="pl-ponto" style="background:${corCartao(c.cartao)}"></i>${esc(c.descricao)}
        <span class="prog">${progresso(p.atual, c.total, corCartao(c.cartao))}<small>${p.atual}/${c.total}</small></span></span>
      <span class="val">${fmtBRL(p.valor)}${fim ? chip("última", "bom") : ""}</span></div>`;
  }).join("") || `<p class="rodape">Sem parcelas neste mês.</p>`;

  $("parcFrase").innerHTML = `${mesCurto(prox.mes)} tem <b>${curto(prox.total)}</b> em parcelas`
    + (ultima ? `; a última termina em <b>${mesCurto(ultima)}</b>.` : ".")
    + ` Clique num mês para ver as parcelas dele.`;
}

/* ------------------------------------------------------------ metas */

function linhaMeta(m) {
  const cor = m.situacao === "concluida" ? "var(--verde)" : m.situacao === "atrasada" || m.situacao === "vencida" ? "var(--laranja)" : "var(--pl-azul)";
  const quando = m.situacao === "concluida" ? chip("concluída", "bom")
    : m.situacao === "vencida" ? chip("prazo passou", "ruim")
    : m.chegaEm ? chip(mesCurto(m.chegaEm), m.situacao === "atrasada" ? "ruim" : "") : "";
  const dica = [
    `${fmtBRL(m.atual)} de ${fmtBRL(m.alvo)} (${pct(m.pct)})`,
    m.chegaEm ? `No seu ritmo, chega em ${mesCurto(m.chegaEm)} (${emMeses(mesesEntre(P.corrente, m.chegaEm))})` : "",
    m.precisaPorMes ? `Para o prazo de ${mesCurto(m.prazo)}: ${fmtBRL(m.precisaPorMes)}/mês` : "",
    m.situacao === "atrasada" ? `Faltam ${fmtBRL(m.extraPorMes)}/mês além do ritmo` : "",
  ].filter(Boolean).join("\n");
  // Card quadrado que se enche de baixo para cima conforme a meta avança.
  return `<div class="meta-q" title="${esc(dica)}">
    <span class="meta-enche" style="height:${Math.max(0, Math.min(100, m.pct))}%;background:${cor}"></span>
    <span class="meta-pct">${Math.round(m.pct)}%</span>
    <span class="meta-quando">${quando}</span>
    <button class="icone meta-menu" data-meta="${m.id}" data-abre-pop aria-label="Ações da meta ${esc(m.nome)}">⋯</button>
    <span class="meta-emoji">${esc(m.emoji || "🎯")}</span>
    <span class="meta-nome">${esc(m.nome)}</span>
    <span class="meta-valor">${curto(m.atual)}</span>
    <span class="meta-alvo">de ${curto(m.alvo)}</span>
  </div>`;
}

function plano(ativas) {
  // Quanto as metas com prazo pedem por mês, contra o aporte do ritmo.
  const comPrazo = ativas.filter(m => m.precisaPorMes && m.situacao !== "concluida");
  if (!comPrazo.length) return "";
  const pede = comPrazo.reduce((a, m) => a + m.precisaPorMes, 0), aporte = P.teto?.aporte || 0;
  return `<span>As metas com prazo pedem <b>${curto(pede)}/mês</b></span><span class="sep">·</span>`
    + (pede <= aporte ? `<span>seu aporte de <b>${curto(aporte)}</b> cobre todas</span>`
      : `<span class="ruim">faltam ${curto(pede - aporte)}/mês sobre o seu ritmo</span>`);
}

function metas() {
  const ativas = P.metas.filter(m => !m.arquivada), arq = P.metas.filter(m => m.arquivada);
  $("plano").innerHTML = plano(ativas);
  $("plano").hidden = !$("plano").innerHTML;
  $("metas").innerHTML = ativas.map(linhaMeta).join("")
    + `<button type="button" class="meta-q meta-nova" id="btnMeta">+ Nova meta${ativas.length ? "" : "<small>Com prazo, a tela diz quanto guardar por mês</small>"}</button>`
    + (arq.length ? `<details class="rodape meta-arq"><summary>Arquivadas (${arq.length})</summary><div class="metas-grade" style="opacity:.7">${arq.map(linhaMeta).join("")}</div></details>` : "");
}

function abrirMenuMeta(botao) {
  META = P.metas.find(m => m.id === botao.dataset.meta);
  if (!META) return;
  $("plMenuLista").innerHTML = `
    <button class="pop-item" data-acao="editar">Editar</button>
    <button class="pop-item" data-acao="arquivar">${META.arquivada ? "Reativar" : "Arquivar"}</button>
    <button class="pop-item perigo" data-acao="excluir">Excluir</button>`;
  abrirPop($("plMenu"), botao, { alinhar: "direita" });
}

$("plMenu").addEventListener("click", async e => {
  const b = e.target.closest("[data-acao]");
  if (!b || !META) return;
  fecharPop();
  try {
    if (b.dataset.acao === "editar") return abrirMeta(META);
    if (b.dataset.acao === "arquivar") P = await pedir("/planejamento/meta/arquivar", "POST", { id: META.id, arquivada: !META.arquivada });
    if (b.dataset.acao === "excluir") {
      if (!confirm(`Excluir a meta "${META.nome}"?`)) return;
      P = await pedir("/planejamento/meta/excluir", "POST", { id: META.id });
    }
    desenhar();
  } catch (err) { setEstado("estado", err.message, "erro"); }
});

function opcoesRef() {
  const tipo = $("metaTipo").value;
  $("campoRef").hidden = !["aba", "cdb"].includes(tipo);
  $("campoManual").hidden = tipo !== "manual";
  $("metaRefRotulo").textContent = tipo === "cdb" ? "CDBs de" : "Aba";
  const lista = tipo === "aba" ? P.referencias.abas.map(a => [a.id, a.nome])
    : tipo === "cdb" ? P.referencias.cdbs.map(c => [c, c]) : [];
  $("metaRef").innerHTML = lista.map(([v, n]) => `<option value="${esc(v)}">${esc(n)}</option>`).join("");
  if (META && META.tipo === tipo && META.referencia) $("metaRef").value = META.referencia;
}

function abrirMeta(m = null) {
  META = m;
  $("metaTitulo").textContent = m ? "Editar meta" : "Nova meta";
  $("metaEmoji").value = m?.emoji || "";
  $("metaNome").value = m?.nome || "";
  $("metaAlvo").value = m ? fmtValor(m.alvo) : "";
  $("metaPrazo").value = m?.prazo || "";
  $("metaPrazo").dispatchEvent(new Event("change"));
  $("metaTipo").value = m?.tipo || "carteira";
  $("metaManual").value = m?.tipo === "manual" ? fmtValor(m.valorManual) : "";
  $("metaErro").textContent = "";
  opcoesRef();
  $("modalMeta").hidden = false;
  $("metaNome").focus();
}

$("metaTipo").addEventListener("change", opcoesRef);
// Atalhos de emoji; qualquer outro pode ser digitado ou colado (Win + .).
$("emojis").innerHTML = ["💰", "🏦", "🛟", "✈️", "🏖️", "🚗", "🏠", "📱", "💻", "🎓", "💍", "👶", "🎯", "📈"]
  .map(e => `<button type="button" data-emoji="${e}" aria-label="Usar ${e}">${e}</button>`).join("");
$("metaSalvar").addEventListener("click", async () => {
  try {
    P = await pedir("/planejamento/meta", "POST", {
      id: META?.id, emoji: $("metaEmoji").value, nome: $("metaNome").value, alvo: lerValor($("metaAlvo").value),
      prazo: $("metaPrazo").value, tipo: $("metaTipo").value, referencia: $("metaRef").value,
      valorManual: lerValor($("metaManual").value),
    });
    $("modalMeta").hidden = true;
    desenhar();
  } catch (e) { $("metaErro").textContent = e.message; }
});

/* ------------------------------------------------------------ eventos */

$("extra").addEventListener("input", () => P && projecao());
document.addEventListener("click", e => {
  if (e.target.closest("#btnMeta")) return abrirMeta();
  const em = e.target.closest("[data-emoji]");
  if (em) { $("metaEmoji").value = em.dataset.emoji; return; }
  const m = e.target.closest("[data-meta]");
  if (m) return abrirMenuMeta(m);
  if (e.target.closest("#btnPeriodo")) return abrirPop($("popPeriodo"), $("btnPeriodo"), { alinhar: "direita" });
  const per = e.target.closest("[data-periodo]");
  if (per) {
    N = Number(per.dataset.periodo);
    try { localStorage.setItem("planejamento.periodo", N); } catch { /* opcional */ }
    fecharPop();
    return desenhar();
  }
  const pc = e.target.closest("[data-parc-cartao]");
  if (pc) { PARC_CARTAO = PARC_CARTAO === pc.dataset.parcCartao ? null : pc.dataset.parcCartao; return parcelas(); }
  const pm = e.target.closest("[data-parc-mes]");
  if (pm) { PARC_MES = pm.dataset.parcMes; return parcelas(); }
  if (e.target.closest("[data-fecha]") || e.target.classList.contains("modal-fundo"))
    e.target.closest(".modal-fundo").hidden = true;
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") document.querySelectorAll(".modal-fundo").forEach(x => { x.hidden = true; });
  // Linha de cartão escolhida pelo teclado.
  const pc = (e.key === "Enter" || e.key === " ") && e.target.closest?.("[data-parc-cartao]");
  if (pc) { e.preventDefault(); pc.click(); }
});
let redim;
window.addEventListener("resize", () => { clearTimeout(redim); redim = setTimeout(() => P && (fluxo(), projecao(), parcelas()), 120); });

montarNav(montarAlternador("planejamento", "planejamento.html"));
campoMesTodos();
carregar();
})();
