/* Gráficos minimalistas compartilhados (estilo do Planejamento).

   vLinhas(svg, rotulos, series, opcoes)   linha curva, bolinha em cada ponto
   vBarras(svg, itens, opcoes)             barras arredondadas, um em destaque
   vListaBarras(lista)                     HTML da lista com barra de fundo

   O <svg> leva class="v-graf" e um atributo height; a largura é a do
   container. Depende de esc() do app.js. Estilos em visual.css. */

// Curva suave (Catmull-Rom em Bézier) pelos pontos.
function vCurva(pts) {
  if (pts.length < 2) return pts.length ? `M${pts[0][0]},${pts[0][1]}` : "";
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

// Até `max` marcas espalhadas, sempre com a primeira e a última.
function vMarcas(n, max = 6) {
  const passo = Math.max(1, Math.ceil((n - 1) / max)), r = [];
  for (let i = 0; i < n; i += passo) r.push(i);
  if (n && r.at(-1) !== n - 1) r.push(n - 1);
  return r;
}

/* rotulos: texto do eixo por ponto (ex.: "out/26").
   series: [{ valores:[number|null], cor, tracejada }]; null quebra a linha.
   opcoes: { dica(i) -> texto, marcas:[{valor, rotulo}], eixo:[índices] } */
function vLinhas(svg, rotulos, series, { dica = () => "", marcas = [], eixo } = {}) {
  const W = svg.clientWidth || 700, H = Number(svg.getAttribute("height")) || 140, T = 10, B = 20, L = 6, R = 10;
  const n = rotulos.length;
  const todos = [...series.flatMap(s => s.valores.filter(v => v != null)), ...marcas.map(m => m.valor)];
  if (!todos.length || !n) { svg.innerHTML = ""; return; }
  const min = Math.min(...todos), max = Math.max(...todos), folga = (max - min) * .15 || Math.abs(max) * .1 || 1;
  const x = i => L + i * (W - L - R) / Math.max(1, n - 1);
  const y = v => T + (H - T - B) * (1 - (v - (min - folga)) / ((max + folga) - (min - folga)));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  let h = marcas.map(m => `<line class="v-marca" x1="${L}" x2="${W - R}" y1="${y(m.valor)}" y2="${y(m.valor)}"/>
    <text class="v-alvo" x="${L}" y="${y(m.valor) - 4}">${esc(m.rotulo)}</text>`).join("");
  for (const s of series) {
    // Trechos contínuos: um null no meio não liga os pontos vizinhos.
    const trechos = [[]];
    s.valores.forEach((v, i) => v == null ? trechos.push([]) : trechos.at(-1).push([x(i), y(v)]));
    for (const pts of trechos.filter(t => t.length)) {
      h += `<path class="v-linha ${s.tracejada ? "tracejada" : ""}" stroke="${s.cor}" d="${vCurva(pts)}"/>`;
    }
    if (!s.tracejada) {
      const ult = s.valores.reduce((a, v, i) => v != null ? i : a, -1);
      h += s.valores.map((v, i) => v == null ? "" : `<circle class="v-ponto" stroke="${s.cor}" cx="${x(i)}" cy="${y(v)}" r="${i === ult ? 4 : 2.5}"/>`).join("");
    }
  }
  h += (eixo || vMarcas(n)).map(i => `<text class="v-mes" x="${x(i)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(rotulos[i])}</text>`).join("");
  const w = (W - L - R) / Math.max(1, n - 1);
  h += rotulos.map((_, i) => `<rect class="v-hover" x="${x(i) - w / 2}" y="0" width="${w}" height="${H}"><title>${esc(dica(i))}</title></rect>`).join("");
  svg.innerHTML = h;
}

/* itens: [{ rotulo, valor, dica, partes:[{valor, cor, nome}] }] (partes empilha).
   opcoes: { destaque: índice, cor, aoClicar: true (marca data-v-i), valorNoTopo: fmt } */
function vBarras(svg, itens, { destaque = itens.length - 1, cor = "var(--v-verde)", clicavel = false, valorNoTopo = null, eixo } = {}) {
  const W = svg.clientWidth || 700, H = Number(svg.getAttribute("height")) || 130, B = 20, T = 18;
  const n = itens.length;
  if (!n) { svg.innerHTML = ""; return; }
  const max = Math.max(...itens.map(i => Math.abs(i.valor || 0)), 1), w = W / n, bw = Math.min(30, w * .55);
  const mostra = new Set(eixo || (n <= 12 ? itens.map((_, i) => i) : vMarcas(n, 8)));
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  // Id único por gráfico para os recortes (pode haver vários na página).
  const base = svg.id || (svg.dataset.vId ||= `g${Math.random().toString(36).slice(2, 8)}`);
  svg.innerHTML = itens.map((it, i) => {
    const sel = i === destaque, total = Math.abs(it.valor || 0);
    const altura = total ? Math.max(4, total / max * (H - T - B)) : 0;
    const partes = it.partes?.length ? it.partes : [{ valor: total, cor: it.cor || cor }];
    const x0 = i * w + (w - bw) / 2;
    let topo = H - B, blocos = "";
    // Empilhado: as partes são retângulos retos, e a barra inteira é recortada
    // com cantos redondos. Só as pontas arredondam; a troca de cor fica reta.
    const soma = partes.reduce((a, p) => a + Math.abs(p.valor || 0), 0) || 1;
    partes.forEach(p => {
      const h = altura * Math.abs(p.valor || 0) / soma;
      if (!h) return;
      topo -= h;
      blocos += `<rect class="v-bar" x="${x0}" y="${topo}" width="${bw}" height="${h}" fill="${p.cor}" opacity="${sel ? 1 : .4}"><title>${esc(p.nome ? `${p.nome}: ` : "")}${esc(it.dica || "")}</title></rect>`;
    });
    const clip = `${base}-c${i}`;
    const recorte = altura ? `<clipPath id="${clip}"><rect x="${x0}" y="${H - B - altura}" width="${bw}" height="${altura}" rx="${Math.min(7, bw / 2, altura / 2)}"/></clipPath>` : "";
    return `<g class="v-col ${clicavel ? "clicavel" : ""}" data-v-i="${i}"><title>${esc(it.dica || "")}</title>
      <rect class="v-hover" x="${i * w}" y="0" width="${w}" height="${H}"/>${recorte}<g clip-path="url(#${clip})">${blocos}</g>
      ${sel && valorNoTopo && total ? `<text class="v-valor" x="${i * w + w / 2}" y="${topo - 6}" text-anchor="middle">${esc(valorNoTopo(it.valor))}</text>` : ""}
      ${mostra.has(i) || sel ? `<text class="v-mes ${sel ? "sel" : ""}" x="${i * w + w / 2}" y="${H - 4}" text-anchor="middle">${esc(it.rotulo)}</text>` : ""}</g>`;
  }).join("");
}

/* lista: [{ nome, sub, valor, chip, chipTipo, fracao (0..1), cor, dica, attrs }] */
function vListaBarras(lista) {
  return lista.map(l => `<div class="v-bl ${l.attrs ? "clicavel" : ""}" ${l.attrs || ""} title="${esc(l.dica || "")}">
    <span class="v-f" style="width:${Math.max(0, Math.min(1, l.fracao || 0)) * 100}%;background:${l.cor || "var(--v-azul)"}"></span>
    <span class="v-nome">${l.ponto ? `<i class="v-ponto-cor" style="background:${l.ponto}"></i>` : ""}${esc(l.nome)}${l.sub ? `<small>${esc(l.sub)}</small>` : ""}</span>
    <span class="v-val">${l.valor}${l.chip ? `<span class="v-chip ${l.chipTipo || ""}">${esc(l.chip)}</span>` : ""}</span></div>`).join("");
}
