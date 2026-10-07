/* Linhagem da carteira: o grafo das vagas e dos fundos que passaram por elas.

   Uma vaga é o lugar de um fundo na carteira recomendada. Quando o fundo
   fecha para novos aportes, outro ocupa a vaga (substituição) e a posição do
   fechado continua aplicada. Aqui cada vaga é uma raia no tempo: os fundos
   são barras do primeiro aporte até o fechamento (ou hoje), ligadas por
   setas na data da substituição. Clicar abre a calculadora na vaga.

   Vive numa IIFE, como carteira.js: a tela de Investimentos já usa `el`, `D`
   e `render` com esses nomes. */
(function () {
const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
const pctTxt = v => v == null ? "—" : `${Number(v).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;
const mesAno = iso => iso ? `${MESES[Number(iso.slice(5, 7)) - 1]}/${iso.slice(2, 4)}` : "";
const dia = iso => new Date(`${iso}T12:00:00`).getTime();
let DADOS = null;

function inicioDe(item, arestas) {
  // Primeiro aporte; sem ele, o dia em que entrou na vaga pela substituição.
  if (item.primeiroAporte) return item.primeiroAporte;
  const entrada = arestas.find(a => a.destino === item.cnpj);
  return entrada ? entrada.data : "";
}

function desenhar() {
  const alvo = $("linhagemGrafo");
  if (!alvo || !DADOS) return;
  const itens = Object.fromEntries((DADOS.itens || []).map(i => [i.cnpj, i]));
  const arestas = DADOS.substituicoes || [];
  const abas = DADOS.abas || [];
  const vagas = (DADOS.vagas || []).filter(v => v.cadeia.some(c => itens[c]));
  if (!vagas.length) {
    alvo.innerHTML = `<p class="linhagem-vazia">Cadastre os fundos na Calculadora de rebalanceamento para ver a linhagem da carteira.</p>`;
    return;
  }
  const hoje = new Date().toISOString().slice(0, 10);
  const inicios = vagas.flatMap(v => v.cadeia.map(c => itens[c] && inicioDe(itens[c], arestas))).filter(Boolean);
  // O eixo começa um pouco antes do primeiro aporte (mínimo de duas semanas de
  // régua). Antes era fixo em seis meses e tudo ficava espremido à direita.
  const t1 = dia(hoje);
  const primeiro = inicios.length ? Math.min(...inicios.map(dia)) : t1;
  const t0 = t1 - Math.max(t1 - primeiro, 14 * 864e5) * 1.06;

  const larg = Math.max(640, alvo.clientWidth || 900);
  const ESQ = 12, DIR = 170, RAIA = 46, TOPO = 26;
  const x = t => ESQ + (larg - ESQ - DIR) * (t1 > t0 ? (t - t0) / (t1 - t0) : 1);
  const maiorPos = Math.max(1, ...Object.values(itens).map(i => i.posicao || 0));
  const espessura = pos => 6 + 14 * Math.sqrt((pos || 0) / maiorPos);

  // Raias agrupadas por aba, na ordem das abas e, dentro, por alvo.
  const linhas = [];
  for (const aba of abas) {
    const daAba = vagas.filter(v => v.perfil === aba.id).sort((a, b) => (b.percentual || 0) - (a.percentual || 0));
    if (!daAba.length) continue;
    linhas.push({ aba });
    daAba.forEach(v => linhas.push({ vaga: v }));
  }
  const altura = TOPO + linhas.reduce((h, l) => h + (l.aba ? 26 : RAIA), 0) + 8;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${larg} ${altura}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(altura));
  svg.setAttribute("class", "linhagem-svg");
  const add = (tag, attrs, pai = svg) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    pai.appendChild(n);
    return n;
  };

  // Eixo do tempo: uma marca por mês (por trimestre em períodos longos), mais "hoje".
  const passo = t1 - t0 > 400 * 864e5 ? 3 : 1;
  const marcos = [];
  const d = new Date(t0); d.setDate(1); d.setMonth(d.getMonth() + 1);
  while (d.getTime() <= t1) {
    if (d.getMonth() % passo === 0) marcos.push(d.toISOString().slice(0, 10));
    d.setMonth(d.getMonth() + 1);
  }
  for (const m of marcos) {
    add("line", { x1: x(dia(m)), x2: x(dia(m)), y1: TOPO - 6, y2: altura - 4, class: "linhagem-grade" });
    add("text", { x: x(dia(m)), y: 14, class: "linhagem-eixo", "text-anchor": "middle" }).textContent = mesAno(m);
  }
  add("line", { x1: x(t1), x2: x(t1), y1: TOPO - 6, y2: altura - 4, class: "linhagem-hoje" });

  let y = TOPO;
  for (const l of linhas) {
    if (l.aba) {
      add("text", { x: ESQ, y: y + 17, class: "linhagem-aba" }).textContent = l.aba.nome.toUpperCase();
      y += 26;
      continue;
    }
    const v = l.vaga, meio = y + RAIA / 2;
    const g = add("g", { class: "linhagem-vaga", "data-ponta": v.ponta, "data-perfil": v.perfil, tabindex: "0", role: "button" });
    add("title", {}, g).textContent = `Abrir este fundo em Fundos`;
    add("rect", { x: 0, y, width: larg, height: RAIA, class: "linhagem-fundo-raia" }, g);
    let anterior = null;
    for (const c of v.cadeia) {
      const it = itens[c];
      if (!it) continue;
      const ini = inicioDe(it, arestas) || (anterior ? anterior.fim : new Date(t0).toISOString().slice(0, 10));
      const fim = it.fechadoEm || hoje;
      const x0 = x(dia(ini)), x1 = Math.max(x0 + 4, x(dia(fim)));
      const h = espessura(it.posicao);
      const barra = add("rect", { x: x0, y: meio - h / 2, width: x1 - x0, height: h, rx: Math.min(4, h / 2),
        class: `linhagem-barra ${it.fechadoEm ? "fechado" : "aberto"}` }, g);
      add("title", {}, barra).textContent =
        `${it.nome}\n${it.fechadoEm ? `fechado em ${mesAno(it.fechadoEm)}` : "aberto para aportes"}` +
        `\nposição ${fmtBRL(it.posicao || 0)}${ini ? ` · desde ${mesAno(ini)}` : ""}`;
      const rotulo = add("text", { x: x0 + 4, y: meio - h / 2 - 4, class: `linhagem-nome ${it.fechadoEm ? "fechado" : ""}` }, g);
      // O nome não pode invadir a coluna da direita (% da vaga).
      const cabe = Math.max(6, Math.min(48, Math.floor((larg - DIR - x0 - 10) / 6.3)));
      rotulo.textContent = it.nome.length > cabe ? `${it.nome.slice(0, cabe - 1)}…` : it.nome;
      if (anterior) {
        // Seta do fundo que fechou para o que entrou no lugar.
        const xa = anterior.x1, xb = x0;
        add("path", { d: `M${xa},${meio} C${(xa + xb) / 2},${meio} ${(xa + xb) / 2},${meio} ${xb},${meio}`,
          class: "linhagem-seta", "marker-end": "url(#linhagemPonta)" }, g);
        const aresta = arestas.find(a => a.origem === anterior.cnpj && a.destino === c);
        if (aresta?.motivo) add("title", {}, g.lastChild).textContent = aresta.motivo;
      }
      anterior = { cnpj: c, x1, fim };
    }
    // Lado direito: onde a vaga está em relação ao alvo.
    const abaixo = v.pctAtual != null && v.pctAtual < (v.percentual || 0) - 0.5;
    // Só o valor da vaga; o % e o alvo ficam na calculadora.
    add("text", { x: larg - DIR + 14, y: meio + 4, class: `linhagem-pct ${abaixo ? "abaixo" : ""}` }, g).textContent = fmtBRL(v.atual || 0);
    y += RAIA;
  }
  const defs = document.createElementNS(NS, "defs");
  defs.innerHTML = `<marker id="linhagemPonta" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0,0 L8,4 L0,8 z" class="linhagem-ponta"></path></marker>`;
  svg.prepend(defs);
  alvo.replaceChildren(svg);

  const trocas = arestas.length;
  $("linhagemSub").textContent = trocas
    ? `${vagas.length} vagas · ${trocas} substituição(ões) de fundo · clique numa vaga para abrir o fundo`
    : `${vagas.length} vagas · nenhum fundo substituído ainda · clique numa vaga para abrir o fundo`;
}

async function carregar() {
  try {
    DADOS = await pedir("/carteira?aporte=0");
    desenhar();
  } catch (e) {
    const alvo = $("linhagemGrafo");
    if (alvo) alvo.innerHTML = `<p class="linhagem-vazia">Não foi possível carregar a carteira: ${esc(e.message)}</p>`;
  }
}

function abrir(e) {
  const g = e.target.closest(".linhagem-vaga");
  if (!g || (e.type === "keydown" && !["Enter", " "].includes(e.key))) return;
  e.preventDefault();
  // A vaga abre no pop-up Fundos, onde se cadastra e marca substituição.
  window.abrirMeusFundos?.(g.dataset.ponta);
}

const alvo = $("linhagemGrafo");
if (alvo) {
  alvo.addEventListener("click", abrir);
  alvo.addEventListener("keydown", abrir);
  window.addEventListener("carteira-mudou", carregar);
  let espera;
  window.addEventListener("resize", () => { clearTimeout(espera); espera = setTimeout(desenhar, 150); });
  carregar();
}
})();
