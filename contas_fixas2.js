/* Contas Fixas 2.0 (teste): a tela original, com o cálculo simplificado.
 *
 * Tudo o que a tela original faz continua igual (lista, cadastro, vínculos,
 * cards das faturas). Este arquivo, carregado DEPOIS do script dela, troca
 * só a "Composição do mês" pelo cálculo de fixas2.py e acrescenta a tabela
 * de reembolsos:
 *
 *   despesas = faturas dos cartões + contas no cartão ainda não cobradas
 *            + contas fora do cartão − reembolsos
 *
 * Conta no cartão que já está na fatura não soma de novo; subdescontos
 * descontam da conta-pai (ver fixas2.py).
 */
let F2 = null;

async function carregarF2() {
  F2 = await pedir(`/fixas2?month=${encodeURIComponent(MES)}`);
  return F2;
}

// Substitui a composição da tela original pelo resumo da 2.0.
function renderComposicao() {
  if (!F2 || F2.mes !== MES) {
    carregarF2().then(() => { renderComposicao(); renderReembolsos(); })
      .catch(e => setEstado("estado", e.message, "erro"));
    return;
  }
  const r = F2.resumo;
  // Fora do cartão separado por tag, como na original (Pessoais × Empresa).
  const fora = { pessoais: 0, empresa: 0 };
  for (const it of F2.itens) {
    const parte = it.tag === "Contas Empresa" ? "empresa" : "pessoais";
    if (it.status === "fora_cartao") fora[parte] += it.somado;
    for (const d of it.descontos) if (d.status === "fora_cartao") fora[parte] += d.somado;
  }
  // No modo "atual" os cards das faturas não somam os hábitos estimados (só o
  // lançado e o que vai cair no cartão). A composição segue o mesmo modo,
  // senão Cartões aqui fica maior que a soma dos cards bem nos hábitos.
  const semHabitos = MODO_VALORES === "atual";
  const habitos = semHabitos ? r.faturasHabitos : 0;
  const cartoes = r.faturas + r.previstos - habitos;
  const despesas = r.total - habitos;
  const entradas = Number(EXTRATO_MES?.resumo?.entradas || 0);
  const saldo = entradas - despesas;

  el("compReceitas").textContent = fmtBRL(entradas);
  el("compDespesas").textContent = fmtBRL(despesas);
  el("compCartoes").textContent = fmtBRL(cartoes);
  // As partes da linha Cartões: o que já foi cobrado e o que ainda é
  // estimativa (parcelas, hábitos e contas fixas que vão cair no cartão).
  el("compCobrado").textContent = fmtBRL(r.faturasCobrado);
  el("compParcelas").textContent = fmtBRL(r.faturasParcelas);
  el("compHabitos").textContent = fmtBRL(semHabitos ? 0 : r.faturasHabitos);
  el("compHabitos").title = semHabitos
    ? `Fora do total no modo atual (${fmtBRL(r.faturasHabitos)} estimados). Mude para "previsto" para somar.` : "";
  el("compPrevistos").textContent = fmtBRL(r.previstos);
  el("compPessoais").textContent = fmtBRL(fora.pessoais);
  el("compEmpresa").textContent = fmtBRL(fora.empresa);
  el("compReembolsos").textContent = r.reembolsos ? `−${fmtBRL(r.reembolsos)}` : fmtBRL(0);
  el("compSaldo").textContent = fmtBRL(saldo);
  el("compSaldoCard").classList.toggle("positivo", saldo >= 0);
  el("compSaldoCard").classList.toggle("negativo", saldo < 0);
  el("compMes").textContent = labelMes(MES);
  const percentual = entradas > 0 ? despesas / entradas * 100 : 0;
  const fmtPct = v => v.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + "%";
  el("compBarraPreenchimento").style.width = `${entradas > 0 ? Math.max(0, Math.min(100, percentual)) : (despesas > 0 ? 100 : 0)}%`;
  el("compBarra").classList.toggle("excedida", saldo < 0);
  el("compComprometido").textContent = entradas > 0 ? `${fmtPct(percentual)} comprometidos` : "Sem receitas";
  el("compPercentualSaldo").textContent = entradas > 0
    ? (saldo >= 0 ? `${fmtPct(100 - percentual)} de saldo` : `${fmtPct(percentual - 100)} acima das receitas`)
    : (saldo < 0 ? "Saldo negativo" : "");
}

/* Contas e subdescontos no cartão que ainda não têm cobrança vinculada. */
function semVinculo() {
  const lista = [];
  for (const it of F2?.itens || []) {
    if (it.status === "prevista")
      lista.push({ titulo: it.nome, cartao: it.formaNome || "Cartão", valor: it.somado || it.liquido || it.valor || 0 });
    for (const d of it.descontos)
      if (d.status === "prevista")
        lista.push({ titulo: `${it.nome} › ${d.descricao}`, cartao: d.formaNome || it.formaNome || "Cartão", valor: d.somado || d.valor || 0 });
  }
  return lista;
}

/* A lista do aviso laranja, no mesmo modal dos itens de cada cartão. */
function abrirSemVinculo() {
  if (typeof modalEmSecoes === "function") modalEmSecoes(false);
  const lista = semVinculo();
  if (!lista.length) return;
  const total = lista.reduce((s, p) => s + Math.round(p.valor * 100), 0) / 100;
  el("cardModalTitulo").textContent = "No cartão sem vínculo";
  el("cardModalSub").textContent =
    "Contas que vão ser pagas no cartão e ainda não apareceram na fatura. Somam como previsto até a cobrança cair; quando cair, o vínculo é feito sozinho ou pelo botão de vínculo da conta.";
  el("cardModalResumo").innerHTML = `
    <div><span>Contas</span><strong>${lista.length}</strong></div>
    <div><span>Total previsto</span><strong>${fmtBRL(total)}</strong></div>
    <div><span>Mês</span><strong>${esc(labelMes(MES))}</strong></div>`;
  el("cardModalLista").innerHTML = lista.map(p => `
    <div class="card-modal-linha">
      <span>${esc(p.titulo)}</span><span>${esc(p.cartao)}</span><strong>${fmtBRL(p.valor)}</strong>
    </div>`).join("");
  el("modalCard").hidden = false;
}

/* Aviso laranja ao lado de "N conta(s)": contas e subdescontos no cartão
 * que ainda não têm cobrança vinculada -- são os que estão somando como
 * previsto. A dica lista quais são. */
function renderAvisoSemVinculo() {
  let aviso = el("avisoSemVinculo");
  if (!aviso) {
    aviso = document.createElement("span");
    aviso.id = "avisoSemVinculo";
    aviso.className = "estado f2-aviso";
    el("estado").insertAdjacentElement("afterend", aviso);
  }
  const pendentes = semVinculo();
  const nomes = pendentes.map(p => p.titulo);
  aviso.hidden = !nomes.length;
  aviso.textContent = `${nomes.length} no cartão sem vínculo`;
  aviso.title = `Somando como previsto até a cobrança aparecer:\n${nomes.join("\n")}\n\nClique para ver a lista.`;
  aviso.setAttribute("role", "button");
  aviso.tabIndex = 0;
  aviso.style.cursor = "pointer";
  aviso.onclick = abrirSemVinculo;
  aviso.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); abrirSemVinculo(); } };

  // Segundo aviso: o que merece conferência -- conta que não apareceu na
  // fatura fechada, subdescontos acima da conta, hábito com cobertura
  // diferente entre as duas telas.
  let conferir = el("avisoConferir");
  if (!conferir) {
    conferir = document.createElement("span");
    conferir.id = "avisoConferir";
    conferir.className = "estado f2-aviso";
    aviso.insertAdjacentElement("afterend", conferir);
  }
  const lista = F2?.avisos || [];
  conferir.hidden = !lista.length;
  conferir.textContent = `${lista.length} para conferir`;
  conferir.title = lista.join("\n");
}

function renderReembolsos() {
  renderAvisoSemVinculo();
  if (!F2) return;
  el("reembolsosTotal").textContent = fmtBRL(F2.resumo.reembolsos);
  // Uma fonte só de reembolso na 2.0: esta tabela. Estorno no cartão fica
  // listado mas riscado -- a fatura já o desconta.
  const daTabela = F2.reembolsos.map(r => `<div class="vinculos-linha f2-reemb">
        <strong>${esc(r.descricao)}</strong>
        <span>${esc(r.formaNome)}</span>
        <span>${r.desconta ? "PIX / dinheiro" : "Estorno no cartão · já na fatura"}</span>
        <strong class="${r.desconta ? "" : "f2-nao-desconta"}">${esc(fmtBRL(r.valor))}</strong>
        <button class="sutil" data-remover-reembolso="${esc(r.id)}" aria-label="Excluir reembolso">Excluir</button>
      </div>`).join("");
  el("reembolsosLista").innerHTML = daTabela
    || `<div class="vinculos-linha f2-reemb f2-sem"><span>Nenhum reembolso em ${esc(labelMes(MES))}.</span></div>`;
  const atual = el("rForma").value;
  el("rForma").innerHTML = `<option value="">Compra em</option>`
    + F2.formas.map(f => `<option value="${esc(f.id)}">${esc(f.nome)}</option>`).join("");
  el("rForma").value = atual;
}

// Toda recarga da tela original passa a trazer também o cálculo da 2.0.
const carregarOriginal = carregar;
carregar = async function () {
  F2 = null;
  await carregarOriginal();
};

el("formReembolso").addEventListener("submit", async e => {
  e.preventDefault();
  el("reembolsoErro").textContent = "";
  try {
    await pedir("/fixas2/reembolsos", "POST", {
      mes: MES, descricao: el("rDescricao").value.trim(),
      forma: el("rForma").value, como: el("rComo").value, valor: lerValor(el("rValor").value),
    });
    el("rDescricao").value = "";
    el("rValor").value = "";
    await carregarF2();
    renderComposicao();
    renderReembolsos();
    el("rDescricao").focus();
  } catch (erro) { el("reembolsoErro").textContent = erro.message; }
});

el("reembolsosLista").addEventListener("click", async e => {
  const b = e.target.closest("[data-remover-reembolso]");
  if (!b || !confirm("Excluir este reembolso?")) return;
  try {
    await pedir(`/fixas2/reembolsos/${encodeURIComponent(b.dataset.removerReembolso)}`, "DELETE");
    await carregarF2();
    renderComposicao();
    renderReembolsos();
  } catch (erro) { setEstado("estado", erro.message, "erro"); }
});

// O script original já começou a carregar antes deste arquivo; quando o mês
// estiver definido, a composição se refaz com o cálculo da 2.0.
(function esperarMes() {
  if (typeof MES === "string" && MES) renderComposicao();
  else setTimeout(esperarMes, 100);
})();

// Na 2.0 o subdesconto não tem mais o tipo "Reembolso": reembolso é só na
// tabela de Reembolsos (uma fonte só, sem risco de contar duas vezes).
const opcoesFormaOriginal = opcoesForma;
opcoesForma = (selecionada, opcoes = {}) => opcoesFormaOriginal(selecionada, { ...opcoes, reembolso: false });

/* Barras de fundo da composição: cada linha mostra a fatia que ocupa das
 * despesas. Só apresentação -- lê os valores já escritos na tela, então não
 * mexe no cálculo nem depende de qual renderComposicao rodou. */
(function barrasComposicao() {
  const desp = el("compDespesas");
  if (!desp) return;
  const num = t => Number(String(t || "").replace(/[^\d,-]/g, "").replace(",", ".")) || 0;
  const cores = { compCartoes: "var(--azul)", compPessoais: "var(--laranja)", compEmpresa: "var(--roxo, var(--azul))", compReembolsos: "var(--verde)" };
  const pintar = () => {
    const total = Math.abs(num(desp.textContent));
    document.querySelectorAll(".f2-comp").forEach(linha => {
      const v = Math.abs(num(el(linha.dataset.comp)?.textContent));
      const frac = total > 0 ? Math.min(1, v / total) : 0;
      const f = linha.querySelector(".v-f");
      f.style.width = `${(frac * 100).toFixed(1)}%`;
      f.style.background = cores[linha.dataset.comp] || "var(--text-3)";
      linha.querySelector(".v-chip").textContent = total > 0 && v > 0 ? `${Math.round(frac * 100)}%` : "";
    });
  };
  const obs = new MutationObserver(pintar);
  [desp, ...document.querySelectorAll(".f2-comp b")].forEach(n => obs.observe(n, { childList: true, characterData: true, subtree: true }));
  pintar();
})();
