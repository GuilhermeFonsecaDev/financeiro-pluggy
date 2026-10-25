/* Pop-up "Carteira": os fundos em que se aporta e as outras posições (CDBs).

   CDBs e afins não recebem aporte da calculadora, mas podem entrar numa aba
   com uma meta em % da aba: o valor deles soma no total da aba e a tela
   mostra quanto estão acima ou abaixo da meta. Uma posição com dado errado
   vindo do banco pode ser ignorada e sai de todas as contas.

   É aqui que se cadastra um fundo, edita os dados dele (aba, % alvo,
   liquidez, IQ, aporte mínimo) e marca que ele fechou para aportes e qual
   entrou no lugar. A calculadora de rebalanceamento só calcula.

   Os formulários de cadastro (com conferência do CNPJ) e de substituição
   são os de carteira.js, expostos em window.carteiraCadastrar e
   window.carteiraSubstituir. Vive numa IIFE: a tela já usa `el` e `D`. */
(function () {
const $ = id => document.getElementById(id);
let C = null;               // payload de /carteira
let FILTRO = "abertos";
let ALVO_MENU = null;
let EDITANDO = null;
let ALVO_POS = null;
let VISAO = "fundos";       // "fundos" ou "cdbs"
let ALVO_GRUPO = null;      // grupo de CDBs do menu ⋯
const emissorDe = o => (o.emissor || o.nome.replace(/^CDB - /, "")).replace(/\s+S\.?A\.?$/i, "").trim();        // CDB do menu ⋯
const pctTxt = v => `${Number(v || 0).toLocaleString("pt-BR", { maximumFractionDigits: 2 })}%`;
const MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
const mesAno = iso => iso ? `${MESES[Number(iso.slice(5, 7)) - 1]}/${iso.slice(0, 4)}` : "";
const lerNumero = texto => {
  const s = String(texto ?? "").replace(/[^\d,.-]/g, "");
  if (!s) return null;
  const n = Number(s.includes(",") ? s.replace(/\./g, "").replace(",", ".") : s);
  return Number.isFinite(n) ? n : null;
};

const porCnpj = () => Object.fromEntries((C?.itens || []).map(i => [i.cnpj, i]));
const sucessor = cnpj => (C?.substituicoes || []).find(s => s.origem === cnpj);
const antecessor = cnpj => (C?.substituicoes || []).find(s => s.destino === cnpj);

async function carregar() {
  try {
    C = await pedir("/carteira?aporte=0");
    $("mfErro").textContent = "";
    render();
  } catch (e) { $("mfErro").textContent = e.message; }
}

/* Alocação real x alvo: laranja quando passa de 1 ponto acima ou abaixo. */
const alocClasse = it => {
  if (it.fechadoEm || it.pctAtual == null) return "";
  const d = it.pctAtual - (it.percentual || 0);
  return Math.abs(d) <= 1 ? "" : d > 0 ? "mf-acima" : "mf-abaixo";
};

/* Uma linha por fundo; o aberto da vaga traz a linhagem dele embaixo. */
function linhaFundo(it) {
  const itens = porCnpj();
  const ant = antecessor(it.cnpj), suc = sucessor(it.cnpj);
  const cadeia = [];
  // Linhagem: do mais antigo até este fundo.
  for (let a = ant; a; a = antecessor(a.origem)) {
    cadeia.unshift(`${esc(itens[a.origem]?.nome || a.origem)} <span class="muted">fechou em ${mesAno(a.data)}${a.motivo ? ` · ${esc(a.motivo)}` : ""}</span>`);
  }
  const fechado = !!it.fechadoEm;
  const status = fechado
    ? `<span class="mf-status fechado">fechado</span>`
    : `<span class="mf-status aberto">aberto</span>`;
  const desfaz = fechado && suc && !sucessor(suc.destino);
  return `<div class="mf-fundo ${fechado ? "fechado" : ""}" data-cnpj="${esc(it.cnpj)}">
    <div class="mf-ident">
      ${status}
      <div class="mf-texto">
        <div class="mf-nome"><b>${esc(it.nome)}</b></div>
        <div class="mf-cnpj">${esc(it.cnpjFormatado)}${fechado ? ` · fechou em ${mesAno(it.fechadoEm)}` : ""}${fechado && suc ? ` · substituído por <b>${esc(itens[suc.destino]?.nome || suc.destino)}</b>` : ""}</div>
        ${!fechado && cadeia.length ? `<div class="mf-linhagem">no lugar de: ${cadeia.join(" → ")}</div>` : ""}
      </div>
    </div>
    <div class="mf-num"><span>Alvo</span><b>${fechado ? "—" : pctTxt(it.percentual)}</b></div>
    <div class="mf-num" title="Quanto a vaga deste fundo representa hoje na aba (soma os fundos que ele substituiu)"><span>Atual</span><b class="${alocClasse(it)}">${fechado || it.pctAtual == null ? "—" : pctTxt(it.pctAtual)}</b></div>
    <div class="mf-num"><span>Posição</span><b>${fmtBRL(it.posicao || 0)}</b></div>
    <div class="mf-num"><span>Liquidez</span><b>${it.diasResgate == null ? "—" : `D+${it.diasResgate}`}</b></div>
    <div class="mf-acoes">${fechado
      ? (desfaz ? `<button class="icone" data-menu="${esc(it.cnpj)}" data-abre-pop aria-label="Ações de ${esc(it.nome)}">⋯</button>` : "")
      : `<button class="icone" data-menu="${esc(it.cnpj)}" data-abre-pop aria-label="Ações de ${esc(it.nome)}">⋯</button>`}</div>
  </div>`;
}

/* Diferença para a meta: +100,00 em verde, -100,00 em vermelho. */
const sinalBRL = v => {
  const txt = Math.abs(v).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return Math.abs(v) < 0.005 ? '<span class="mf-ok">R$ 0,00</span>'
    : `<span class="${v > 0 ? "mf-pos" : "mf-neg"}">${v > 0 ? "+" : "-"} R$ ${txt}</span>`;
};

/* CDB e afins: meta editável e quanto falta ou sobra para ela. */
const dataCurta = iso => iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(2, 4)}` : "";
function linhaPosicao(o) {
  const dif = o.diferenca;
  const difTxt = o.ignorado || !o.perfil ? "—"
    : dif == null ? '<span class="muted">sem meta</span>'
    : sinalBRL(dif);
  const detalhe = [o.emissor && o.emissor !== o.nome.replace(/^CDB - /, "") ? esc(o.emissor) : "",
    o.vencimento ? `vence ${dataCurta(o.vencimento)}` : "",
    o.taxa != null ? `${String(o.taxa).replace(".", ",")}% ${esc(o.tipoTaxa || "")}` : ""].filter(Boolean).join(" · ");
  return `<div class="mf-fundo mf-posicao ${o.ignorado ? "ignorado" : ""}" data-pos="${esc(o.id)}">
    <div class="mf-ident">
      <span class="mf-status ${o.ignorado ? "fechado" : "cdb"}">${o.ignorado ? "ignorado" : esc(o.tipo || "CDB")}</span>
      <div class="mf-texto">
        <div class="mf-nome"><b>${esc(o.nome)}</b></div>
        <div class="mf-cnpj">${detalhe}</div>
      </div>
    </div>
    <div class="mf-num"><span>Meta</span>${o.perfil && !o.ignorado
      ? `<b class="mf-meta"><input type="text" inputmode="decimal" data-meta="${esc(o.id)}" value="${String(o.percentual || 0).replace(".", ",")}" aria-label="Meta de ${esc(o.nome)} em % da aba" /><i>%</i></b>`
      : "<b>—</b>"}</div>
    <div class="mf-num"><span>Posição</span><b>${fmtBRL(o.posicao || 0)}</b></div>
    <div class="mf-num mf-dif"><span>Atual</span><b>${difTxt}</b></div>
    <div class="mf-num"><span>Hoje</span><b>${o.pctAtual != null && !o.ignorado ? pctTxt(o.pctAtual) : "—"}</b></div>
    <div class="mf-acoes"><button class="icone" data-menu-pos="${esc(o.id)}" data-abre-pop aria-label="Ações de ${esc(o.nome)}">⋯</button></div>
  </div>`;
}

function render() {
  if (!C) return;
  const abas = C.abas || [];
  const lista = (C.itens || []).filter(i =>
    FILTRO === "todos" || (FILTRO === "abertos" ? !i.fechadoEm : !!i.fechadoEm));
  document.querySelectorAll("#mfFiltro [data-filtro]").forEach(b => b.classList.toggle("ativo", b.dataset.filtro === FILTRO));
  document.querySelectorAll("#mfVisao [data-visao]").forEach(b => b.classList.toggle("ativo", b.dataset.visao === VISAO));
  // Abertos/Fechados e o cadastro só fazem sentido para fundos.
  $("mfFiltro").style.display = VISAO === "cdbs" ? "none" : "";
  $("mfNovo").style.display = VISAO === "cdbs" ? "none" : "";
  let blocosHtml = abas.map(aba => {
    const daAba = lista.filter(i => i.perfil === aba.id)
      .sort((a, b) => (!!a.fechadoEm - !!b.fechadoEm) || (b.percentual - a.percentual));
    const posAba = VISAO === "cdbs" ? (C.outros || []).filter(o => o.perfil === aba.id) : [];
    if (VISAO === "cdbs") daAba.length = 0;
    if (!daAba.length && !posAba.length) return "";
    const soma = (C.itens || []).filter(i => i.perfil === aba.id && !i.fechadoEm).reduce((s, i) => s + (i.percentual || 0), 0)
      + (C.outros || []).filter(o => o.perfil === aba.id && !o.ignorado).reduce((s, o) => s + (o.percentual || 0), 0);
    return `<section class="mf-aba">
      <div class="mf-aba-tit">
        <span>${esc(aba.nome)}
          <label class="mf-fatia" title="Quanto de cada aporte vai para esta aba. Dentro dela, os fundos dividem 100% dessa fatia.">
            <input type="text" inputmode="decimal" data-fatia="${esc(aba.id)}" value="${String(aba.percentual).replace(".", ",")}"
                   aria-label="Fatia do aporte para ${esc(aba.nome)}" /><i>% do aporte</i></label></span>
        <span class="${Math.abs(soma - 100) < .01 ? "" : "mf-desalinhado"}">carteira soma ${pctTxt(soma)} da aba</span></div>
      ${daAba.map(linhaFundo).join("")}
      ${gruposCdb(posAba, aba.id)}
    </section>`;
  }).join("");
  if (VISAO === "cdbs") {
    TOTAL_CDB = soma((C.outros || []).filter(o => !o.ignorado));
    blocosHtml = (C.outros || []).length ? consolidado() + `<section class="mf-aba">${gruposCdb(C.outros)}</section>` : "";
  }
  $("mfLista").innerHTML = blocosHtml || `<p class="mf-vazio">${VISAO === "cdbs" ? "Nenhum CDB ativo nas contas conectadas." : FILTRO === "fechados"
    ? "Nenhum fundo fechado ainda." : "Nenhum fundo cadastrado. Use + Cadastrar fundo."}</p>`;
}

/* CDBs do mesmo emissor viram uma linha só: soma das posições e das metas.
   A meta do grupo é repartida entre os CDBs na proporção da posição. */
function gruposCdb(lista) {
  const grupos = {};
  for (const o of lista) (grupos[emissorDe(o)] ||= []).push(o);
  return Object.entries(grupos).sort((a, b) => soma(b[1]) - soma(a[1])).map(([emissor, cdbs]) => {
    const ativos = cdbs.filter(o => !o.ignorado);
    const pos = soma(ativos);
    const meta = (C.metasCdb || {})[emissor];
    const dif = pos - (meta || 0);
    const difTxt = !ativos.length ? "—"
      : !meta ? '<span class="muted">sem meta</span>'
      : sinalBRL(dif);
    const venc = cdbs.map(o => o.vencimento).filter(Boolean).sort();
    const taxas = [...new Set(cdbs.map(o => o.taxa != null ? `${String(o.taxa).replace(".", ",")}% ${o.tipoTaxa || ""}`.trim() : "").filter(Boolean))];
    const vencTxt = venc.length > 1 ? `vencem de ${dataCurta(venc[0])} a ${dataCurta(venc.at(-1))}` : venc.length ? `vence ${dataCurta(venc[0])}` : "";
    return `<div class="mf-fundo mf-posicao" data-grupo="${esc(emissor)}">
      <div class="mf-ident">
        <span class="mf-status ${ativos.length ? "cdb" : "fechado"}">${ativos.length ? "CDB" : "ignorado"}</span>
        <div class="mf-texto">
          <div class="mf-nome"><b>CDB - ${esc(emissor)}</b></div>
          <div class="mf-cnpj">${[vencTxt, esc(taxas.join(", "))].filter(Boolean).join(" · ")}</div>
        </div>
      </div>
      <div class="mf-num"><span>Meta</span><b>${meta ? fmtBRL(meta) : "—"}</b></div>
      <div class="mf-num"><span>Posição</span><b>${fmtBRL(pos)}</b></div>
      <div class="mf-num mf-dif"><span>Atual</span><b>${difTxt}</b></div>
      <div class="mf-num"><span>Dos CDBs</span><b>${ativos.length && TOTAL_CDB ? pctTxt(100 * pos / TOTAL_CDB) : "—"}</b></div>
      <div class="mf-acoes"><button class="icone" data-menu-grupo="${esc(emissor)}" data-abre-pop aria-label="Ações dos CDBs ${esc(emissor)}">⋯</button></div>
    </div>`;
  }).join("");
}
let TOTAL_CDB = 0;
const soma = lista => lista.reduce((s, o) => s + (o.posicao || 0), 0);
const cdbsDoGrupo = emissor => (C.outros || []).filter(o => emissorDe(o) === emissor);


function abrirMenuGrupo(botao) {
  ALVO_GRUPO = botao.dataset.menuGrupo;
  ALVO_MENU = null; ALVO_POS = null;
  $("mfMenuLista").innerHTML = `
    <button class="pop-item" data-acao-grupo="editar"><span class="mf-ic">✎</span>Editar meta</button>
    <div class="pop-sep"></div>
    ${cdbsDoGrupo(ALVO_GRUPO).every(o => o.ignorado)
      ? `<button class="pop-item" data-acao-grupo="considerar"><span class="mf-ic">✓</span>Voltar a considerar</button>`
      : `<button class="pop-item" data-acao-grupo="ignorar" style="color:var(--vermelho)" title="Tira estes CDBs de todas as contas da carteira"><span class="mf-ic">⊘</span>Ignorar nos cálculos</button>`}`;
  abrirPop($("mfMenu"), botao, { alinhar: "direita" });
}

async function configurarVarios(mudancas) {
  for (const [id, m] of mudancas) C = await pedir("/carteira/posicao", "POST", { id, ...m });
  $("mfErro").textContent = "";
  render();
  window.dispatchEvent(new Event("carteira-mudou"));
}

let META_GRUPO = null;
function abrirMeta(emissor) {
  META_GRUPO = emissor;
  const meta = (C.metasCdb || {})[emissor];
  $("mfMetaSub").textContent = `CDB - ${emissor} · posição ${fmtBRL(soma(cdbsDoGrupo(emissor).filter(o => !o.ignorado)))}`;
  $("mfMetaValor").value = meta ? String(meta).replace(".", ",") : "";
  $("mfMetaErro").textContent = "";
  $("mfModalMeta").hidden = false;
  $("mfMetaValor").focus();
}
const fecharMeta = () => { $("mfModalMeta").hidden = true; };
async function salvarMeta() {
  const texto = $("mfMetaValor").value.trim();
  const valor = texto ? lerNumero(texto) : 0;
  if (valor == null || valor < 0) { $("mfMetaErro").textContent = "Informe um valor em reais."; return; }
  try {
    C = await pedir("/carteira/meta-cdb", "POST", { grupo: META_GRUPO, meta: valor });
    fecharMeta(); render();
    window.dispatchEvent(new Event("carteira-mudou"));
  } catch (e) { $("mfMetaErro").textContent = e.message; }
}
$("mfFecharMeta").addEventListener("click", fecharMeta);
$("mfSalvarMeta").addEventListener("click", salvarMeta);
$("mfMetaValor").addEventListener("keydown", e => { if (e.key === "Enter") salvarMeta(); });
$("mfModalMeta").addEventListener("click", e => { if (e.target === $("mfModalMeta")) fecharMeta(); });

/* Consolidado dos CDBs: total, por emissor e por aba (com a meta somada). */
function consolidado() {
  const ativos = (C.outros || []).filter(o => !o.ignorado);
  if (!ativos.length) return "";
  const total = ativos.reduce((s, o) => s + o.posicao, 0);
  const soma = (lista, chave) => {
    const g = {};
    for (const o of lista) { const k = chave(o); (g[k] ||= { n: 0, v: 0, meta: 0, temMeta: false }); g[k].n++; g[k].v += o.posicao;
      if (o.meta != null && o.percentual) { g[k].meta += o.meta; g[k].temMeta = true; } }
    return Object.entries(g).sort((a, b) => b[1].v - a[1].v);
  };
  const nomeAba = id => (C.abas || []).find(a => a.id === id)?.nome || "Fora das abas";
  const emissor = o => (o.emissor || o.nome.replace(/^CDB - /, "")).replace(/\s+S\.?A\.?$/i, "");
  const chip = (rotulo, g, comMeta) => {
    const dif = g.v - g.meta;
    const metaTxt = comMeta && g.temMeta
      ? (Math.abs(dif) < 0.5 ? '<i class="mf-ok">na meta</i>'
        : `<i class="${dif < 0 ? "mf-abaixo" : "mf-acima"}">${fmtBRL(Math.abs(dif))} ${dif < 0 ? "abaixo" : "acima"}</i>`) : "";
    return `<div class="mf-cons-item"><span>${esc(rotulo)} <small>${g.n} ${g.n === 1 ? "CDB" : "CDBs"}</small></span>
      <b>${fmtBRL(g.v)}</b><small>${total ? pctTxt(100 * g.v / total) : ""} dos CDBs</small>${metaTxt}</div>`;
  };
  const ignorados = (C.outros || []).length - ativos.length;
  return `<section class="mf-consolidado">
    <div class="mf-cons-total"><span>Total em CDBs</span><b>${fmtBRL(total)}</b>
      <small>${ativos.length} ${ativos.length === 1 ? "posição" : "posições"}${ignorados ? ` · ${ignorados} ignorada(s)` : ""}</small></div>
    <div class="mf-cons-grupo"><span class="mf-cons-tit">Por emissor</span>${soma(ativos, emissor).map(([k, g]) => chip(k, g, false)).join("")}</div>
  </section>`;
}

/* CDBs que ainda não estão em nenhuma aba: ficam fora das contas até
   serem colocados numa. */
function semAba() {
  const lista = (C.outros || []).filter(o => !o.perfil);
  if (!lista.length) return "";
  return `<section class="mf-aba">
    <div class="mf-aba-tit"><span>Fora das abas</span><span>escolha uma aba no ⋯ para contar na carteira</span></div>
    ${gruposCdb(lista, "")}
  </section>`;
}

/* --------------------------------------------------------------- abrir */

async function abrir(cnpj, visao) {
  if (visao) VISAO = visao;
  $("mfModal").hidden = false;
  await carregar();
  if (cnpj) {
    const fundo = (C?.itens || []).find(i => i.cnpj === cnpj);
    if (fundo?.fechadoEm && FILTRO === "abertos") { FILTRO = "todos"; render(); }
    const linha = $("mfLista").querySelector(`[data-cnpj="${cnpj}"]`);
    if (linha) {
      linha.scrollIntoView({ block: "center" });
      linha.classList.add("destaque");
      setTimeout(() => linha.classList.remove("destaque"), 1600);
    }
  }
}
const fechar = () => { $("mfModal").hidden = true; };
window.abrirMeusFundos = abrir;

/* ---------------------------------------------------------------- menu */

function abrirMenu(botao) {
  ALVO_MENU = (C?.itens || []).find(i => i.cnpj === botao.dataset.menu);
  if (!ALVO_MENU) return;
  ALVO_POS = null; ALVO_GRUPO = null;
  // Fundo fechado: a única ação é reabrir (desfazer a substituição).
  if (ALVO_MENU.fechadoEm) {
    $("mfMenuLista").innerHTML = `<button class="pop-item" data-acao="desfazer" title="Reabre o fundo e tira o substituto da vaga"><span class="mf-ic">↩</span>Desfazer substituição</button>`;
    return abrirPop($("mfMenu"), botao, { alinhar: "direita" });
  }
  const outrasAbas = (C.abas || []).filter(a => a.id !== ALVO_MENU.perfil);
  $("mfMenuLista").innerHTML = `
    <button class="pop-item" data-acao="editar"><span class="mf-ic">✎</span>Editar</button>
    <button class="pop-item" data-acao="substituir" title="O fundo fechou para aportes: escolha o que entra no lugar"><span class="mf-ic">⇢</span>Substituir</button>
    ${outrasAbas.map(a => `<button class="pop-item" data-acao="mover" data-aba="${esc(a.id)}"><span class="mf-ic">⇄</span>Mover para ${esc(a.nome)}</button>`).join("")}
    <div class="pop-sep"></div>
    <button class="pop-item" data-acao="remover" style="color:var(--vermelho)"><span class="mf-ic">×</span>Remover</button>`;
  abrirPop($("mfMenu"), botao, { alinhar: "direita" });
}

function abrirMenuPos(botao) {
  ALVO_POS = (C?.outros || []).find(o => o.id === botao.dataset.menuPos);
  if (!ALVO_POS) return;
  ALVO_MENU = null; ALVO_GRUPO = null;
  const outrasAbas = abasCdb().filter(a => a.id !== ALVO_POS.perfil);
  $("mfMenuLista").innerHTML = `
    ${outrasAbas.map(a => `<button class="pop-item" data-acao-pos="mover" data-aba="${esc(a.id)}"><span class="mf-ic">⇄</span>${ALVO_POS.perfil ? "Mover para" : "Colocar em"} ${esc(a.nome)}</button>`).join("")}
    ${ALVO_POS.perfil ? `<button class="pop-item" data-acao-pos="mover" data-aba=""><span class="mf-ic">↩</span>Tirar das abas</button>` : ""}
    <div class="pop-sep"></div>
    ${ALVO_POS.ignorado
      ? `<button class="pop-item" data-acao-pos="considerar"><span class="mf-ic">✓</span>Voltar a considerar</button>`
      : `<button class="pop-item" data-acao-pos="ignorar" style="color:var(--vermelho)" title="Dado errado vindo do banco: sai de todas as contas da carteira"><span class="mf-ic">⊘</span>Ignorar nos cálculos</button>`}`;
  abrirPop($("mfMenu"), botao, { alinhar: "direita" });
}

async function configurarPos(id, mudanca) {
  C = await pedir("/carteira/posicao", "POST", { id, ...mudanca });
  $("mfErro").textContent = "";
  render();
  window.dispatchEvent(new Event("carteira-mudou"));
}

async function acaoMenuPos(b) {
  const alvo = ALVO_POS;
  fecharPop();
  try {
    if (b.dataset.acaoPos === "mover") return await configurarPos(alvo.id, { perfil: b.dataset.aba });
    if (b.dataset.acaoPos === "ignorar") {
      if (!confirm(`Ignorar ${alvo.nome} (${fmtBRL(alvo.posicao)}) nos cálculos da carteira?`)) return;
      return await configurarPos(alvo.id, { ignorado: true });
    }
    if (b.dataset.acaoPos === "considerar") return await configurarPos(alvo.id, { ignorado: false });
  } catch (err) { $("mfErro").textContent = err.message; }
}

/* Edição grava a carteira inteira, como a calculadora: a rota só aceita a
   lista completa (quem sai dela sai do banco). */
async function gravar(mudar) {
  const itens = structuredClone(C.itens);
  mudar(itens);
  const percentuais = Object.fromEntries((C.abas || []).map(a => [a.id, a.percentual]));
  C = await pedir("/carteira", "POST", { itens, aporte: 0, percentuais });
  render();
  window.dispatchEvent(new Event("carteira-mudou"));
}

async function acaoMenu(e) {
  const bg = e.target.closest("[data-acao-grupo]");
  if (bg && ALVO_GRUPO) {
    const cdbs = cdbsDoGrupo(ALVO_GRUPO), aba = bg.dataset.aba;
    fecharPop();
    if (bg.dataset.acaoGrupo === "editar") return abrirMeta(ALVO_GRUPO);
    if (bg.dataset.acaoGrupo !== "mover") {
      try { await configurarVarios(cdbs.map(o => [o.id, { ignorado: bg.dataset.acaoGrupo === "ignorar" }])); }
      catch (err) { $("mfErro").textContent = err.message; }
      return;
    }
    try {
      // Mover o grupo leva a meta junto (a meta é em % da aba).
      await configurarVarios(cdbs.map(o => [o.id, { perfil: aba }]));
    } catch (err) { $("mfErro").textContent = err.message; }
    return;
  }
  const bp = e.target.closest("[data-acao-pos]");
  if (bp && ALVO_POS) return acaoMenuPos(bp);
  const b = e.target.closest("[data-acao]");
  if (!b || !ALVO_MENU) return;
  const alvo = ALVO_MENU;
  fecharPop();
  $("mfErro").textContent = "";
  try {
    if (b.dataset.acao === "editar") return abrirEdicao(alvo);
    if (b.dataset.acao === "desfazer") {
      if (!confirm(`Reabrir ${alvo.nome} e desfazer a substituição?`)) return;
      C = await pedir("/carteira/substituir/desfazer", "POST", { origem: alvo.cnpj });
      render();
      window.dispatchEvent(new Event("carteira-mudou"));
      return;
    }
    if (b.dataset.acao === "substituir") return window.carteiraSubstituir?.(alvo.cnpj);
    if (b.dataset.acao === "mover") {
      return await gravar(itens => { itens.find(i => i.cnpj === alvo.cnpj).perfil = b.dataset.aba; });
    }
    if (b.dataset.acao === "remover") {
      if (antecessor(alvo.cnpj) || sucessor(alvo.cnpj)) {
        $("mfErro").textContent = "Este fundo faz parte de uma linhagem. Desfaça a substituição antes de removê-lo.";
        return;
      }
      if (!confirm(`Remover ${alvo.nome} da carteira?`)) return;
      C = await pedir("/carteira/excluir", "POST", { cnpj: alvo.cnpj });
      render();
      window.dispatchEvent(new Event("carteira-mudou"));
    }
  } catch (err) { $("mfErro").textContent = err.message; }
}

/* -------------------------------------------------------------- edição */

function abrirEdicao(it) {
  EDITANDO = it.cnpj;
  $("mfTituloEditar").textContent = "Editar fundo";
  $("mfEditarCnpj").textContent = `${it.cnpjFormatado} · ${it.nome}`;
  $("mfNome").value = it.nomeProprio ? it.nome : "";
  $("mfNome").placeholder = it.nomeProprio ? "" : it.nome;
  $("mfAba").innerHTML = (C.abas || []).map(a => `<option value="${esc(a.id)}">${esc(a.nome)}</option>`).join("");
  $("mfAba").value = it.perfil;
  $("mfPct").value = String(it.percentual ?? "").replace(".", ",");
  $("mfDias").value = it.diasResgate ?? "";
  $("mfMinimo").value = it.aporteMinimoProprio && it.aporteMinimo != null ? String(it.aporteMinimo).replace(".", ",") : "";
  $("mfMinimo").placeholder = !it.aporteMinimoProprio && it.aporteMinimo != null ? `catálogo: ${fmtBRL(it.aporteMinimo)}` : "";
  $("mfIq").value = it.qualificado || "";
  $("mfEditarErro").textContent = "";
  $("mfModalEditar").hidden = false;
  $("mfNome").focus();
}
const fecharEdicao = () => { $("mfModalEditar").hidden = true; };

async function salvarEdicao() {
  const pct = lerNumero($("mfPct").value);
  if (pct == null || pct < 0 || pct > 100) { $("mfEditarErro").textContent = "Informe um % entre 0 e 100."; return; }
  const dias = $("mfDias").value.trim();
  if (dias && !/^\d+$/.test(dias)) { $("mfEditarErro").textContent = "Liquidez em dias inteiros."; return; }
  const minimo = $("mfMinimo").value.trim();
  $("mfSalvarEditar").disabled = true;
  try {
    await gravar(itens => {
      const it = itens.find(i => i.cnpj === EDITANDO);
      const nome = $("mfNome").value.trim();
      it.nome = nome; it.nomeProprio = !!nome;
      it.perfil = $("mfAba").value;
      it.percentual = pct;
      it.diasResgate = dias;
      it.aporteMinimo = minimo ? lerNumero(minimo) : null;
      it.aporteMinimoProprio = !!minimo;
      it.qualificado = $("mfIq").value;
    });
    fecharEdicao();
  } catch (e) { $("mfEditarErro").textContent = e.message; }
  finally { $("mfSalvarEditar").disabled = false; }
}

/* ------------------------------------------------------------- eventos */

$("btnMeusFundos").addEventListener("click", () => abrir());
$("mfFechar").addEventListener("click", fechar);
$("mfModal").addEventListener("click", e => { if (e.target === $("mfModal")) fechar(); });
$("mfNovo").addEventListener("click", () => window.carteiraCadastrar?.(C?.abas?.[0]?.id));
$("mfVisao").addEventListener("click", e => {
  const b = e.target.closest("[data-visao]");
  if (b) { VISAO = b.dataset.visao; render(); }
});
$("mfFiltro").addEventListener("click", e => {
  const b = e.target.closest("[data-filtro]");
  if (b) { FILTRO = b.dataset.filtro; render(); }
});
$("mfLista").addEventListener("click", async e => {
  const menu = e.target.closest("[data-menu]");
  if (menu) return abrirMenu(menu);
  const menuPos = e.target.closest("[data-menu-pos]");
  if (menuPos) return abrirMenuPos(menuPos);
  const menuGrupo = e.target.closest("[data-menu-grupo]");
  if (menuGrupo) return abrirMenuGrupo(menuGrupo);

  const desfazer = e.target.closest("[data-desfazer]");
  if (!desfazer) return;
  const nome = porCnpj()[desfazer.dataset.desfazer]?.nome || "este fundo";
  if (!confirm(`Reabrir ${nome} e desfazer a substituição?`)) return;
  try {
    C = await pedir("/carteira/substituir/desfazer", "POST", { origem: desfazer.dataset.desfazer });
    render();
    window.dispatchEvent(new Event("carteira-mudou"));
  } catch (err) { $("mfErro").textContent = err.message; }
});
$("mfMenuLista").addEventListener("click", acaoMenu);

/* Fatia de cada aba no aporte (ex.: 95% Conservador, 5% Arrojado). As abas
   têm de somar 100%: mudar uma reparte a diferença nas outras, na proporção
   que elas já tinham -- a mesma conta do campo Porcentagem da calculadora. */
$("mfLista").addEventListener("change", async e => {
  const metaGrupo = e.target.closest("[data-meta-grupo]");
  if (metaGrupo) {
    const v = lerNumero(metaGrupo.value);
    if (v == null || v < 0 || v > 100) { $("mfErro").textContent = "A meta vai de 0 a 100% da aba."; render(); return; }
    const ativos = cdbsDoGrupo(metaGrupo.dataset.metaGrupo).filter(o => !o.ignorado);
    const total = soma(ativos);
    // Reparte em centésimos de ponto, sem perder o arredondamento.
    let usado = 0;
    const partes = ativos.map((o, i) => {
      const c = i === ativos.length - 1 ? Math.round(v * 100) - usado
        : Math.floor(v * 100 * (total ? o.posicao / total : 1 / ativos.length));
      usado += c;
      return [o.id, { percentual: c / 100 }];
    });
    try { await configurarVarios(partes); } catch (err) { $("mfErro").textContent = err.message; render(); }
    return;
  }
  const meta = e.target.closest("[data-meta]");
  if (meta) {
    const v = lerNumero(meta.value);
    if (v == null || v < 0 || v > 100) { $("mfErro").textContent = "A meta vai de 0 a 100% da aba."; render(); return; }
    try { await configurarPos(meta.dataset.meta, { percentual: v }); } catch (err) { $("mfErro").textContent = err.message; render(); }
    return;
  }
  const campo = e.target.closest("[data-fatia]");
  if (!campo || !C) return;
  const valor = lerNumero(campo.value);
  if (valor == null || valor < 0 || valor > 100) { $("mfErro").textContent = "A fatia da aba vai de 0 a 100%."; render(); return; }
  const abas = C.abas || [];
  const outras = abas.filter(a => a.id !== campo.dataset.fatia);
  const restante = 10000 - Math.round(valor * 100);
  const soma = outras.reduce((n, a) => n + a.percentual, 0);
  const percentuais = { [campo.dataset.fatia]: valor };
  let usado = 0;
  outras.forEach((a, i) => {
    const cents = i === outras.length - 1 ? restante - usado
      : Math.floor(restante * (soma ? a.percentual / soma : 1 / outras.length));
    percentuais[a.id] = cents / 100; usado += cents;
  });
  try {
    C = await pedir("/carteira", "POST", { itens: C.itens, aporte: 0, percentuais });
    $("mfErro").textContent = "";
    render();
    window.dispatchEvent(new Event("carteira-mudou"));
  } catch (err) { $("mfErro").textContent = err.message; render(); }
});
$("mfFecharEditar").addEventListener("click", fecharEdicao);
$("mfSalvarEditar").addEventListener("click", salvarEdicao);
$("mfModalEditar").addEventListener("click", e => { if (e.target === $("mfModalEditar")) fecharEdicao(); });
// Cadastro e substituição (feitos nos formulários de carteira.js) atualizam a lista.
window.addEventListener("carteira-mudou", () => { if (!$("mfModal").hidden) carregar(); });
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!$("mfModalMeta").hidden) fecharMeta();
  else if (!$("mfModalEditar").hidden) fecharEdicao();
  else if (!$("mfModal").hidden && $("cartModalCadastro").hidden && $("cartModalSubst").hidden) fechar();
});
})();
