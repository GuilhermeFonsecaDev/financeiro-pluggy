/* Tela do simulador: a mesma de Contas Fixas, com tudo editável.
 *
 * O cálculo vive em simulador_mes.js, sem DOM. Aqui só entra o que precisa do
 * navegador -- e uma regra que vale para o arquivo inteiro: NENHUMA escrita.
 * Só requisições GET, nenhum localStorage, nenhum rascunho. Recarregar a
 * página zera a simulação, e é assim que tem de ser.
 */
(() => {
  const $ = id => document.getElementById(id);
  const moeda = c => fmtBRL(c / 100);
  const emReais = c => fmtValor(c / 100);

  let estado = null;          // tudo o que a simulação é; some com a aba
  let versao = 0;             // descarta resposta atrasada que já não vale
  const ABERTAS = new Set();  // quais linhas estão com os descontos abertos

  const aviso = texto => setEstado("estado", texto);

  function mesesAte(mes, quantos) {
    const ano = Number(mes.slice(0, 4)), numero = Number(mes.slice(5, 7));
    return Array.from({ length: quantos }, (_, i) => {
      const total = ano * 12 + (numero - 1) - (quantos - 1 - i);
      return `${String(Math.floor(total / 12)).padStart(4, "0")}-`
        + `${String(total % 12 + 1).padStart(2, "0")}`;
    });
  }

  /* --------------------------------------------------------- cards de cima */

  function opcoesCartao(selecionado) {
    return ['<option value="">Fora dos cartões</option>']
      .concat(estado.cartoes.map(c =>
        `<option value="${esc(c.id)}"${c.id === selecionado ? " selected" : ""}>${esc(c.nome)}</option>`))
      .join("");
  }

  function renderCards() {
    const t = SimuladorMes.totais(estado);
    const coberto = SimuladorMes.cobertura(estado.itens);
    const maximo = Math.max(1, ...estado.cartoes.map(c => Math.max(0, c.valor)));
    $("metricas").innerHTML = estado.cartoes.map((c, i) => {
      const pct = Math.min(100, Math.max(0, c.valor) / maximo * 100);
      const dentro = coberto[c.id] || 0;
      const dica = c.media
        ? `média ${moeda(c.media)} · ${c.mesesComDado} ${c.mesesComDado === 1 ? "mês" : "meses"}`
        : "sem fatura fechada";
      return `
        <article class="fatura-card banco">
          <div class="fatura-topo"><span class="fatura-rotulo">${esc(c.nome)}</span></div>
          <div class="fatura-valor"><span class="moeda">R$</span>
            <input type="text" inputmode="decimal" data-cartao="${i}"
                   value="${emReais(c.valor)}" aria-label="Fatura simulada de ${esc(c.nome)}" /></div>
          <div class="fatura-barra"><span style="width:${pct.toFixed(1)}%;background:${c.cor}"></span></div>
          <div class="fatura-linhas"><span>${esc(dica)}</span>
            <span>${dentro ? `${moeda(dentro)} de contas dentro` : "nenhuma conta dentro"}</span></div>
        </article>`;
    }).join("") || '<p class="muted">Nenhum cartão ativo.</p>';
    return t;
  }

  /* ------------------------------------------------------- linhas de conta */

  function linha(item, i) {
    const aberta = ABERTAS.has(item.id);
    const desc = SimuladorMes.totalDescontos(item);
    const liq = SimuladorMes.liquido(item);
    return `
    <div class="fixa ${item.incluidaCalculos ? "" : "informativa"}">
      <div class="colunas">
        <button class="icone seta ${aberta ? "aberta" : ""}" data-abre-desc="${i}"
                aria-expanded="${aberta}" aria-label="${aberta ? "Fechar" : "Abrir"} descontos"
                ${item.descontos.length ? "" : "disabled"}>▶</button>
        <div class="nome"><input type="text" data-nome="${i}" value="${esc(item.nome)}"
             aria-label="Nome da conta" style="width:100%" /></div>
        <div class="col-tag">
          <button class="pilula ${item.tag === "Contas Empresa" ? "tag-empresa" : "tag-pessoais"}"
                  data-tag="${i}" title="Alternar entre pessoais e empresa">${esc(item.tag)}</button>
        </div>
        <label class="dinheiro-input"><span>R$</span>
          <input class="bruto" type="text" inputmode="decimal" data-bruto="${i}"
                 value="${emReais(item.bruto)}" aria-label="Valor bruto" /></label>
        <button class="pilula desc col-desc ${desc ? "" : "zero"}" data-abre-desc="${i}"
                ${item.descontos.length ? "" : "disabled"}>−R$ ${emReais(desc)}</button>
        <div class="liquido ${liq ? "" : "nulo"}">R$ ${emReais(liq)}</div>
        <button class="pilula ${item.incluidaCalculos ? (item.pago ? "pago" : "pendente") : "informativa"}"
                ${item.incluidaCalculos ? `data-pago="${i}"` : "disabled"}
                title="${item.incluidaCalculos ? "Alternar entre pago e pendente" : "Visível apenas nos gastos da empresa"}">
          ${item.incluidaCalculos ? (item.pago ? "Pago" : "Pendente") : "Informativa"}</button>
        <div class="col-forma">
          <select class="forma" data-cartao-item="${i}" aria-label="Pagamento de ${esc(item.nome)}">
            ${opcoesCartao(item.cartao)}</select>
        </div>
        <div class="acoes-cel">
          <button class="icone" data-remover="${i}" aria-label="Remover ${esc(item.nome)}">×</button>
        </div>
      </div>
      ${aberta ? item.descontos.map((d, j) => `
        <div class="sub-linha">
          <span class="sub-nome">${esc(d.descricao)}${d.reembolso ? " · reembolso" : ""}</span>
          <label class="dinheiro-input"><span>R$</span>
            <input type="text" inputmode="decimal" data-desconto="${i}.${j}"
                   value="${emReais(d.valor)}" aria-label="Valor de ${esc(d.descricao)}" /></label>
        </div>`).join("") : ""}
    </div>`;
  }

  /* ------------------------------------------------------------ composição */

  function renderComposicao(t) {
    const pessoais = t.porTag["Contas Pessoais"] || 0;
    const empresa = t.porTag["Contas Empresa"] || 0;
    $("compSaldo").textContent = moeda(t.saldo);
    $("compSaldoCard").classList.toggle("positivo", t.saldo >= 0);
    $("compSaldoCard").classList.toggle("negativo", t.saldo < 0);
    $("compDespesas").textContent = moeda(t.despesas);
    $("compCartoes").textContent = moeda(t.cartoes);
    $("compPessoais").textContent = moeda(pessoais);
    $("compEmpresa").textContent = moeda(empresa);

    const pct = t.receitas > 0 ? Math.min(100, t.despesas / t.receitas * 100) : 0;
    $("compBarraPreenchimento").style.width = `${pct.toFixed(1)}%`;
    $("compComprometido").textContent = t.receitas > 0
      ? `${pct.toFixed(1).replace(".", ",")}% comprometidos` : "Sem receitas";
    $("compPercentualSaldo").textContent = t.receitas > 0
      ? `${(100 - pct).toFixed(1).replace(".", ",")}% de saldo` : "";

    $("simNotaHabitos").textContent = estado.habitos
      ? `${moeda(estado.habitos)} de hábitos e recorrências previstos para este mês. `
        + "É acréscimo ao que você digitou nos cartões."
      : "Nenhuma previsão de hábito ou recorrência para este mês.";
  }

  function render() {
    const t = renderCards();
    $("lista").innerHTML = estado.itens.map(linha).join("");
    $("vazio").innerHTML = estado.itens.length ? ""
      : `<div class="vazio">
          <div>Nenhuma conta na simulação.</div>
          <button id="simClonar" data-abre-pop style="margin-top:14px">Clonar um mês</button>
          <button data-nova style="margin:14px 0 0 7px">Criar nova conta</button>
        </div>`;
    $("simHabitos").checked = estado.somarHabitos;
    $("compReceitasInput").value = emReais(estado.receitas);
    renderComposicao(t);
  }

  // Só os números, sem redesenhar os campos: redesenhar a cada tecla tiraria
  // o foco de quem está digitando.
  function recalcular() {
    const t = SimuladorMes.totais(estado);
    renderComposicao(t);
    const coberto = SimuladorMes.cobertura(estado.itens);
    const maximo = Math.max(1, ...estado.cartoes.map(c => Math.max(0, c.valor)));
    document.querySelectorAll("#metricas .fatura-card").forEach((card, i) => {
      const c = estado.cartoes[i];
      if (!c) return;
      card.querySelector(".fatura-barra span").style.width =
        `${Math.min(100, Math.max(0, c.valor) / maximo * 100).toFixed(1)}%`;
      const dentro = coberto[c.id] || 0;
      card.querySelectorAll(".fatura-linhas span")[1].textContent =
        dentro ? `${moeda(dentro)} de contas dentro` : "nenhuma conta dentro";
    });
    estado.itens.forEach((item, i) => {
      const bloco = $("lista").children[i];
      if (!bloco) return;
      bloco.querySelector(".liquido").textContent = `R$ ${emReais(SimuladorMes.liquido(item))}`;
      bloco.querySelector(".col-desc").textContent =
        `−R$ ${emReais(SimuladorMes.totalDescontos(item))}`;
    });
  }

  /* -------------------------------------------------------- carregamento */

  async function carregar(mes) {
    const token = ++versao;
    aviso("Carregando…");
    try {
      const meses = mesesAte(mes, 12);
      const anos = [...new Set(meses.map(m => m.slice(0, 4)))];
      // As contas NÃO vêm junto: a simulação começa com a lista vazia, e
      // clonar um mês é uma escolha explícita.
      const [serie, ...porAnoLista] = await Promise.all([
        pedir(`/extrato/entradas/serie?meses=12&ate=${encodeURIComponent(mes)}`),
        ...anos.map(a => pedir(`/pluggy-cartoes?year=${encodeURIComponent(a)}&group=fatura`)),
      ]);
      if (token !== versao) return;

      const porAno = Object.fromEntries(anos.map((a, i) => [a, porAnoLista[i]]));
      const doMes = porAno[mes.slice(0, 4)];
      const cartoes = doMes?.cartoes || [];

      estado = {
        mes,
        // Começam zerados de propósito: a tela é para montar um mês, não para
        // repetir o que já aconteceu.
        receitas: 0,
        mediaReceitas: SimuladorMes.centavos(serie.total?.media),
        cartoes: cartoes.map(c => {
          const pontos = SimuladorMes.serieCartao(porAno, c.id, meses);
          return {
            id: c.id, nome: nomeInstrumento(c), cor: corInstrumento(c), valor: 0,
            media: SimuladorMes.mediaHistorica(pontos),
            mesesComDado: pontos.filter(p => SimuladorMes.DEFINITIVAS.has(p.origem)).length,
          };
        }),
        // Preserva o que já foi montado ao trocar de mês: perder as contas
        // clonadas só porque a pessoa foi olhar outro mês seria cruel.
        itens: estado?.itens || [],
        cartoesPayload: cartoes,
        habitos: SimuladorMes.previstosDoMes(doMes, mes),
        somarHabitos: estado?.somarHabitos || false,
      };
      render();
      aviso(estado.itens.length ? `${estado.itens.length} contas na simulação`
        : "Tudo zerado — clone um mês ou adicione contas");
    } catch (erro) {
      if (token === versao) aviso(`Não foi possível carregar: ${erro.message}`, "erro");
    }
  }

  /* ------------------------------------------------------------- eventos */

  const lerCentavos = campo => SimuladorMes.centavos(lerValor(campo.value));

  document.addEventListener("input", e => {
    if (!estado) return;
    const d = e.target.dataset;
    if (d.nome !== undefined) { estado.itens[+d.nome].nome = e.target.value; return; }
    if (e.target.id === "compReceitasInput") estado.receitas = lerCentavos(e.target);
    else if (d.cartao !== undefined) estado.cartoes[+d.cartao].valor = lerCentavos(e.target);
    else if (d.bruto !== undefined) estado.itens[+d.bruto].bruto = lerCentavos(e.target);
    else if (d.desconto !== undefined) {
      const [i, j] = d.desconto.split(".").map(Number);
      estado.itens[i].descontos[j].valor = lerCentavos(e.target);
    } else return;
    recalcular();
  });

  document.addEventListener("change", e => {
    if (!estado) return;
    if (e.target.dataset.cartaoItem !== undefined) {
      estado.itens[+e.target.dataset.cartaoItem].cartao = e.target.value;
      recalcular();
    } else if (e.target.id === "simHabitos") {
      estado.somarHabitos = e.target.checked;
      recalcular();
    }
  });

  document.addEventListener("click", e => {
    const alvo = e.target.closest("button");
    if (!alvo || !estado) return;
    const d = alvo.dataset;
    if (alvo.id === "simClonar") {
      $("popClonarLista").innerHTML = mesesAte(proximoMes(estado.mes), 24).reverse()
        .map(m => `<button class="pop-item" data-clonar="${m}">${labelMes(m)}</button>`).join("");
      abrirPop($("popClonar"), alvo);
    } else if (d.clonar !== undefined) {
      clonar(d.clonar);
    } else if (d.nova !== undefined) {
      adicionarConta();
    } else if (d.abreDesc !== undefined) {
      const item = estado.itens[+d.abreDesc];
      if (ABERTAS.has(item.id)) ABERTAS.delete(item.id); else ABERTAS.add(item.id);
      render();
    } else if (d.tag !== undefined) {
      const item = estado.itens[+d.tag];
      item.tag = item.tag === "Contas Empresa" ? "Contas Pessoais" : "Contas Empresa";
      render();
    } else if (d.pago !== undefined) {
      estado.itens[+d.pago].pago = !estado.itens[+d.pago].pago;
      render();
    } else if (d.remover !== undefined) {
      estado.itens.splice(+d.remover, 1);
      render();
    }
  });

  $("simMediaReceitas").addEventListener("click", () => {
    if (!estado) return;
    estado.receitas = estado.mediaReceitas;
    render();
    aviso(`Receitas na média dos últimos 12 meses (${moeda(estado.mediaReceitas)})`);
  });

  $("simMediaCartoes").addEventListener("click", () => {
    if (!estado) return;
    for (const c of estado.cartoes) c.valor = c.media;
    render();
    aviso("Cartões na média das faturas já fechadas");
  });

  $("simLimpar").addEventListener("click", () => {
    if (!estado) return;
    estado.receitas = 0;
    estado.itens = [];
    estado.somarHabitos = false;
    for (const c of estado.cartoes) c.valor = 0;
    ABERTAS.clear();
    render();
    aviso("Tudo zerado");
  });

  function adicionarConta() {
    if (!estado) return;
    estado.itens.push({
      id: `nova-${Date.now()}`, nome: "Nova conta", tag: "Contas Pessoais",
      bruto: 0, descontos: [], pago: false, incluidaCalculos: true, cartao: "",
    });
    render();
    $("lista").lastElementChild?.querySelector("[data-nome]")?.focus();
  }

  $("simAdicionar").addEventListener("click", adicionarConta);

  async function clonar(mes) {
    if (!estado) return;
    fecharPop();
    aviso(`Clonando ${labelMes(mes)}…`);
    try {
      // O mês clonado vira o mês simulado: é ele que define a janela das
      // médias e a previsão de hábitos. Um controle só, sem dois meses
      // diferentes na mesma tela.
      if (mes !== estado.mes) await carregar(mes);
      const fixas = await pedir(`/fixas?month=${encodeURIComponent(mes)}`);
      const novas = SimuladorMes.prepararItens(fixas.itens || [], estado.cartoesPayload);
      estado.itens = estado.itens.concat(novas);
      render();
      aviso(novas.length ? `${novas.length} contas de ${labelMes(mes)} adicionadas`
        : `${labelMes(mes)} não tem contas para clonar`);
    } catch (erro) {
      aviso(`Não foi possível clonar: ${erro.message}`, "erro");
    }
  }

  function proximoMes(mes) {
    const base = mes || new Date().toISOString().slice(0, 7);
    const total = Number(base.slice(0, 4)) * 12 + Number(base.slice(5, 7)) - 1 + 6;
    return `${String(Math.floor(total / 12)).padStart(4, "0")}-${String(total % 12 + 1).padStart(2, "0")}`;
  }

  montarNav("");
  const pedido = new URLSearchParams(location.search).get("mes") || "";
  carregar(/^\d{4}-(0[1-9]|1[0-2])$/.test(pedido)
    ? pedido : new Date().toISOString().slice(0, 7));
})();
