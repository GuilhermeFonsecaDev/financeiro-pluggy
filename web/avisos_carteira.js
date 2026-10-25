/* Quadro "Avisos" do topo de Investimentos: o que está fora do esperado na
   carteira, do mais distante para o mais perto.

   - fundo cuja vaga está a mais de 2 pontos do % alvo na aba;
   - grupo de CDBs (por emissor) com meta, acima ou abaixo dela;
   - aba cujos alvos não somam 100%.

   Usa o mesmo /carteira da Carteira e da calculadora. Clicar num aviso abre
   a Carteira no item. Vive numa IIFE: a tela já usa `el` e `D`. */
(function () {
const $ = id => document.getElementById(id);
const VISIVEIS = 4;
const TOLERANCIA = 2;        // pontos percentuais
const pts = v => `${Number(v).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}`;
const pct = v => `${pts(v)}%`;
// Números do texto, arredondados: o valor exato fica na dica do mouse.
const pctCurto = v => `${Math.round(v)}%`;
const brlCurto = v => v >= 1000
  ? `R$ ${(v / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} mil`
  : `R$ ${Math.round(v).toLocaleString("pt-BR")}`;
const emissorDe = o => (o.emissor || o.nome.replace(/^CDB - /, "")).replace(/\s+S\.?A\.?$/i, "").trim();
const curto = nome => {
  // "EVEREST 90 FUNDO DE INVESTIMENTO EM COTAS..." -> "EVEREST 90"; nomes curtos ficam.
  const n = nome.replace(/\s+(FUNDO|FIC|FIRF|FICFIRF|FICFIDC|FIDC|FIA|FICFIA|RF|CrPr|RL)\b.*$/i, "").trim();
  const base = n.length >= 3 ? n : nome;
  // No aviso cabem duas palavras: "Solis Capital", "EVEREST 90", "V8 Cash".
  return base.split(/\s+/).slice(0, 2).join(" ");
};

function montar(C) {
  const avisos = [];
  const abas = Object.fromEntries((C.abas || []).map(a => [a.id, a]));
  for (const it of C.itens || []) {
    if (it.fechadoEm || it.pctAtual == null) continue;
    // Aba com alvos que não fecham 100%: todo fundo dela "sai do alvo". O
    // aviso da aba já cobre isso; repetir por fundo só faria barulho.
    const g = C.perfis?.[it.perfil];
    if (g && !g.somaFecha && g.somaPercentual) continue;
    const d = it.pctAtual - (it.percentual || 0);
    if (Math.abs(d) <= TOLERANCIA) continue;
    const aba = abas[it.perfil]?.nome || "aba";
    const total = C.perfis?.[it.perfil]?.totalAtual || 0;
    const valor = Math.abs(total * (it.percentual || 0) / 100 - (it.atualVaga || 0));
    const pontos = Math.round(Math.abs(d));
    avisos.push(d > 0 ? {
      tipo: "acima", peso: Math.abs(d), cnpj: it.cnpj,
      texto: `<b>${esc(curto(it.nome))}</b> está com <strong>${pctCurto(it.pctAtual)}</strong>, ${pontos}% acima do planejado. Os próximos aportes vão para os outros fundos até ele voltar ao lugar.`,
      dica: `${pct(it.pctAtual)} do ${aba}, alvo ${pct(it.percentual)} (${fmtBRL(valor)} além).`,
    } : {
      tipo: "abaixo", peso: Math.abs(d), cnpj: it.cnpj,
      texto: `<b>${esc(curto(it.nome))}</b> está com <strong>${pctCurto(it.pctAtual)}</strong>, ${pontos}% abaixo do planejado. Faltam cerca de ${brlCurto(valor)}, e ele é a prioridade do próximo aporte.`,
      dica: `${pct(it.pctAtual)} do ${aba}, alvo ${pct(it.percentual)} (faltam ${fmtBRL(valor)}).`,
    });
  }
  // CDBs: meta em reais por emissor.
  const grupos = {};
  for (const o of C.outros || []) if (!o.ignorado) (grupos[emissorDe(o)] ||= []).push(o);
  for (const [emissor, meta] of Object.entries(C.metasCdb || {})) {
    if (!meta || !grupos[emissor]) continue;
    const pos = grupos[emissor].reduce((s, o) => s + o.posicao, 0);
    const dif = pos - meta;
    if (Math.abs(dif) < 1) continue;
    const peso = 100 * Math.abs(dif) / meta;          // em % da meta, para ordenar junto
    const nome = `CDB ${emissor.replace(/^BANCO\s+/i, "").split(/\s+/)[0]}`;
    avisos.push(dif < 0 ? {
      tipo: "abaixo", peso, cdb: true,
      texto: `<b>${esc(nome)}</b> está <strong>${brlCurto(-dif)}</strong> abaixo da meta de ${brlCurto(meta)}.`,
      dica: `${fmtBRL(pos)} de ${fmtBRL(meta)} (faltam ${fmtBRL(-dif)}).`,
    } : {
      tipo: "acima", peso, cdb: true,
      texto: `<b>${esc(nome)}</b> está <strong>${brlCurto(dif)}</strong> acima da meta de ${brlCurto(meta)}. Dá para direcionar o dinheiro novo para outro lugar.`,
      dica: `${fmtBRL(pos)} para uma meta de ${fmtBRL(meta)} (${fmtBRL(dif)} acima).`,
    });
  }
  // Abas cujos alvos não fecham 100%: a conta de todo o resto fica torta.
  for (const [id, g] of Object.entries(C.perfis || {})) {
    if (g.somaFecha || !g.somaPercentual) continue;
    avisos.push({
      tipo: "config", peso: 1000, aba: id,
      texto: `Os alvos do <b>${esc(g.nome)}</b> somam <strong>${pct(g.somaPercentual)}</strong>, e não 100%. Revise os percentuais na Carteira.`,
      dica: `Hoje somam ${pct(g.somaPercentual)}.`,
    });
  }
  return avisos.sort((a, b) => b.peso - a.peso);
}

function desenhar(avisos) {
  const lista = $("avisosLista");
  $("avisosTitulo").textContent = avisos.length ? `Avisos · ${avisos.length}` : "Avisos";
  if (!avisos.length) {
    lista.innerHTML = `<li class="aviso ok"><span class="aviso-marca"></span><p class="aviso-frase"><b>Tudo certo.</b> Fundos perto do alvo e CDBs dentro da meta.</p></li>`;
    return;
  }
  const item = (a, i) => `<li class="aviso ${a.tipo}" data-aviso="${i}" tabindex="0" role="button" title="${esc((a.dica ? a.dica + " " : "") + "Clique para abrir na Carteira.")}">
      <span class="aviso-marca"></span><p class="aviso-frase">${a.texto}</p></li>`;
  const extras = avisos.length - VISIVEIS;
  lista.innerHTML = avisos.slice(0, VISIVEIS).map(item).join("")
    + (extras > 0 ? `<li class="aviso-mais" data-aviso="mais" tabindex="0" role="button">+ ${extras} na Carteira</li>` : "");
  lista.onclick = lista.onkeydown = e => {
    if (e.type === "keydown" && !["Enter", " "].includes(e.key)) return;
    const li = e.target.closest("[data-aviso]");
    if (!li) return;
    const a = avisos[li.dataset.aviso];
    window.abrirMeusFundos?.(a?.cnpj, a?.cdb ? "cdbs" : "fundos");
  };
}

async function carregar() {
  if (!$("avisosLista")) return;
  try { desenhar(montar(await pedir("/carteira?aporte=0"))); }
  catch (e) { $("avisosLista").innerHTML = `<li class="aviso config"><span class="aviso-marca"></span><p class="aviso-frase"><b>Não deu para checar a carteira.</b> ${esc(e.message)}</p></li>`; }
}

window.addEventListener("carteira-mudou", carregar);
carregar();
})();
