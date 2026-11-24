/* Base compartilhada das telas do projeto Pluggy.
 *
 * Popover, navegação e helpers ficavam duplicados em cada página. Aqui existem
 * uma vez só.
 */

const API = /^https?:$/.test(location.protocol) && ["localhost", "127.0.0.1", "::1", "[::1]"].includes(location.hostname)
  ? `${location.origin}/api` : "http://127.0.0.1:8766/api";

const fmtBRL = v => Number(v || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const fmtBRLCurto = v => {
  const n = Math.abs(Number(v || 0));
  if (n >= 1000) return `${v < 0 ? "-" : ""}R$ ${(n / 1000).toFixed(1).replace(".", ",")}k`;
  return fmtBRL(v);
};
const fmtSinal = v => `${v < 0 ? "−" : "+"}${fmtBRL(Math.abs(v))}`;

const MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
               "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];

/* Dinheiro em campo de texto: sem "R$", com virgula decimal. Estava dentro de
   contas_fixas.html; saiu para ca porque o simulador espelha aquela tela e
   precisa ler e escrever os mesmos campos do mesmo jeito. */
const fmtValor = v => Number(v || 0).toLocaleString("pt-BR", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const lerValor = texto => {
  const limpo = String(texto ?? "").replace(/R\$\s?/g, "").trim();
  if (!limpo) return 0;
  const numero = Number(limpo.replace(/\./g, "").replace(",", "."));
  return Number.isFinite(numero) ? Math.max(0, numero) : 0;
};

/** "novembro de 2026" -- a forma por extenso, como se lê em voz alta.
 *
 *  Convive com `labelMes` ("Novembro 2026"), que é a forma curta usada em
 *  título, tabela e resumo, onde o "de" só ocuparia espaço. Aqui, num campo
 *  que a pessoa lê como frase, a forma longa é a natural.
 */
/* De onde veio o valor de uma fatura, como se lê num selo. Compartilhado por
 * Cartões e Contas, que mostram a mesma fatura. */
const ROTULO_ORIGEM = {
  oficial: "Oficial", pagamento: "Paga", confirmada: "Confirmada",
  aberta: "Aberta", aberta_projecao: "Aberta", projecao: "Parcelas a lançar",
  fechada: "Fechada", movimento: "Movimento", mista: "Mista", vazio: "\u2014",
};
function rotuloOrigem(origem) { return ROTULO_ORIGEM[origem] || origem; }

function labelMesLongo(mesRef) {
  const [ano, mes] = String(mesRef || "").split("-");
  if (!ano || !mes) return "—";
  return `${MESES[Number(mes) - 1].toLowerCase()} de ${ano}`;
}

function labelMes(mesRef) {
  const [ano, mes] = String(mesRef || "").split("-");
  if (!ano || !mes) return "—";
  return `${MESES[Number(mes) - 1]} ${ano}`;
}

/* Saldo mensal da carteira reconstruído de trás para frente: a API entrega a
 * posição de HOJE e o fluxo líquido de cada mês, não o histórico de saldo.
 *
 * Fica aqui, e não em cada tela, porque duas telas desenham a mesma curva --
 * duplicar a reconstrução seria manter duas verdades para o mesmo número.
 */
function serieSaldoInvestimentos(dados) {
  const meses = dados?.mesesPosicoes ?? dados?.meses ?? [];
  let saldo = Number(dados?.resumo?.liquido || 0);
  const serie = new Array(meses.length);
  for (let i = meses.length - 1; i >= 0; i--) {
    serie[i] = { mes: meses[i].mes, rotulo: labelMes(meses[i].mes), valor: saldo };
    saldo -= Number(meses[i].liquido || 0);
  }
  return serie;
}

function fmtData(iso) {
  const [ano, mes, dia] = String(iso ?? "").split("-");
  if (!ano || !mes || !dia) return "—";
  return `${dia}/${mes}/${ano.slice(2)}`;
}

function esc(t) {
  return String(t ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function pedir(caminho, metodo = "GET", corpo = null) {
  const opcoes = { method: metodo, headers: { "Content-Type": "application/json" } };
  if (corpo) opcoes.body = JSON.stringify(corpo);
  const r = await fetch(`${API}${caminho}`, opcoes);
  const p = await r.json().catch(() => ({}));
  if (!r.ok || p.ok === false) throw new Error(p.error || `HTTP ${r.status}`);
  // Qualquer escrita pode ter mudado categoria, regra ou ajuste — o cache
  // inteiro cai. É grosso de propósito: cache errado aqui mostraria número
  // desatualizado, que é pior que a espera que ele evita.
  if (metodo !== "GET") limparCache();
  if (metodo !== "GET" && /^\/cartoes-(identidade|tags)(?:\/|$)/.test(caminho)) {
    avisarIdentidadeCartoes();
  }
  return p;
}

// Identidade é compartilhada: cada aba revalida suas visões após uma tag mudar.
const CHAVE_IDENTIDADE_CARTOES = "pluggy:identidade-cartoes";
function avisarIdentidadeCartoes() {
  try { localStorage.setItem(CHAVE_IDENTIDADE_CARTOES, `${Date.now()}:${Math.random()}`); } catch { }
}

function observarIdentidadeCartoes(atualizar) {
  let pendente = false, atualizando = false, agendado = null;
  const executar = async () => {
    agendado = null;
    if (!pendente || atualizando || document.hidden) return;
    // Não repinta formulários enquanto a pessoa está editando.
    if (document.querySelector(".modal-fundo:not([hidden])")) {
      agendado = setTimeout(executar, 750);
      return;
    }
    pendente = false;
    atualizando = true;
    limparCache();
    try { await atualizar(); }
    catch (erro) { setEstado("estado", erro.message, "erro"); }
    finally { atualizando = false; if (pendente && !agendado) agendado = setTimeout(executar, 100); }
  };
  const solicitar = () => {
    pendente = true;
    if (!agendado) agendado = setTimeout(executar, 80);
  };
  window.addEventListener("storage", evento => {
    if (evento.key === CHAVE_IDENTIDADE_CARTOES) solicitar();
  });
  window.addEventListener("focus", solicitar);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) solicitar(); });
}

function nomeInstrumento(instrumento) {
  return instrumento?.nomeExibicao || instrumento?.nome || instrumento?.nomeOriginal || "Cartão";
}

function corInstrumento(instrumento) {
  const cor = String(instrumento?.cor || "");
  return /^#[a-f\d]{6}$/i.test(cor) ? cor : "#4a95ea";
}

function opcoesInstrumentos(instrumentos, selecionado = "", todos = "") {
  const ehCartao = c => c.tipo === "CREDIT" || c.tipo === "cartao" || c.tipo === "CREDIT_CARD"
    || c.subtipo === "CREDIT_CARD" || !!c.cartaoId;
  const opcao = c => {
    const nome = nomeInstrumento(c);
    const original = c.nomeOriginal && c.nomeOriginal !== nome ? ` · ${c.nomeOriginal}` : "";
    return `<option value="${esc(c.contaId || c.id)}" ${(c.contaId || c.id) === selecionado ? "selected" : ""}>${esc(nome + original)}</option>`;
  };
  const anterior = selecionado && !instrumentos.some(c => (c.contaId || c.id) === selecionado)
    ? instrumentos.find(c => c.cartaoId === selecionado || c.tagId === selecionado) : null;
  const preservada = selecionado && !instrumentos.some(c => (c.contaId || c.id) === selecionado)
    ? `<option value="${esc(selecionado)}" selected>${esc(anterior ? nomeInstrumento(anterior) : "Origem salva · conexão anterior")}</option>` : "";
  return (todos ? `<option value="">${esc(todos)}</option>` : "") + preservada
    + [["Contas bancárias", instrumentos.filter(c => !ehCartao(c))], ["Cartões", instrumentos.filter(ehCartao)]]
      .filter(([, itens]) => itens.length)
      .map(([titulo, itens]) => `<optgroup label="${titulo}">${itens.map(opcao).join("")}</optgroup>`).join("");
}

/* ----------------------------------------------------------------- cache
 *
 * O custo real de trocar de tela é a rede + o render, não o banco (as
 * chamadas caem para ~10 ms depois que o desperdício saiu do servidor). O que
 * ainda pesa é a tela ficar em branco esperando a primeira resposta.
 *
 * Então: guarda a última resposta em sessionStorage e usa a estratégia
 * stale-while-revalidate — pinta na hora com o que tem, refaz a chamada em
 * segundo plano e repinta só se o conteúdo mudou. Só vale para payloads que
 * mudam pouco (filtros, categorias); extrato e resumo continuam sempre
 * frescos, porque são o dado que o usuário está olhando.
 *
 * sessionStorage e não localStorage: expira ao fechar a aba, então nunca
 * sobrevive a uma reimportação feita com o app fechado.
 */

const CACHEAVEIS = ["/extrato/filtros", "/extrato/categorias"];
const PREFIXO = "pluggy:cache:";

function limparCache() {
  for (let i = sessionStorage.length - 1; i >= 0; i--) {
    const k = sessionStorage.key(i);
    if (k && k.startsWith(PREFIXO)) sessionStorage.removeItem(k);
  }
}

/** Busca com cache. `aoAtualizar` é chamado se a revalidação trouxer algo
 * diferente do que foi pintado — pode não ser chamado nenhuma vez. */
async function pedirCache(caminho, aoAtualizar) {
  if (!CACHEAVEIS.some(c => caminho.startsWith(c))) return pedir(caminho);

  const chave = PREFIXO + caminho;
  let guardado = null;
  try { guardado = JSON.parse(sessionStorage.getItem(chave) || "null"); } catch { }

  const revalidar = pedir(caminho).then(fresco => {
    const serial = JSON.stringify(fresco);
    if (serial !== JSON.stringify(guardado)) {
      try { sessionStorage.setItem(chave, serial); } catch { }
      if (guardado && aoAtualizar) aoAtualizar(fresco);
    }
    return fresco;
  });

  // Sem cache ainda: espera a rede, senão a tela pintaria vazia.
  if (!guardado) return revalidar;
  revalidar.catch(() => { });   // já entreguei o cache; falhar aqui é silencioso
  return guardado;
}

/* -------------------------------------------------------------- popover */

let _popAberto = null;
let _ancoraAberta = null;

/** Abre um popover ancorado a um elemento — ou fecha, se já estiver aberto ali.
 *
 * O toggle é pela âncora, não só pelo popover: o mesmo elemento de filtros
 * reabrindo no mesmo botão deve fechar, mas o popover de categoria mudando de
 * linha deve reposicionar em vez de sumir.
 *
 * Posição é calculada em coordenadas de viewport (position: fixed) e corrigida
 * quando estouraria a borda da tela — uma linha no fim da tabela abriria o menu
 * para fora se ancorasse sempre para baixo.
 */
/** Coloca o popover abaixo da âncora, virando para cima se não couber. */
/** Zoom da página (tema.css, "escala"). Medidas da tela (getBoundingClientRect,
 *  innerWidth) vêm já ampliadas; top/left em px são multiplicados pelo zoom,
 *  então a posição é dividida por ele antes de ir para o estilo. */
function zoomDaPagina() {
  return parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
}

function posicionarPop(elemento, ancora, alinhar = "esquerda") {
  const a = ancora.getBoundingClientRect();
  const p = elemento.getBoundingClientRect();
  const margem = 8;

  let topo = a.bottom + 6;
  if (topo + p.height > window.innerHeight - margem) {
    topo = Math.max(margem, a.top - p.height - 6);
  }
  let esq = alinhar === "direita" ? a.right - p.width : a.left;
  esq = Math.min(Math.max(margem, esq), window.innerWidth - p.width - margem);

  const z = zoomDaPagina();
  elemento.style.top = `${topo / z}px`;
  elemento.style.left = `${esq / z}px`;
  // Guarda onde ficou em relação à âncora: ao rolar a página ele acompanha.
  elemento._ancoragem = { ancora, dy: topo - a.top, dx: esq - a.left };
}

/** Popover aberto anda junto com o botão que o abriu quando a página (ou um
 *  painel com rolagem) rola. Sem isto ele ficava parado na tela, descolado do
 *  botão -- e a parte de baixo de um painel alto nunca aparecia. */
function acompanharPops() {
  for (const pop of document.querySelectorAll(".pop:not([hidden])")) {
    const ref = pop._ancoragem;
    if (!ref || !ref.ancora.isConnected) continue;
    const a = ref.ancora.getBoundingClientRect();
    const z = zoomDaPagina();
    pop.style.top = `${(a.top + ref.dy) / z}px`;
    pop.style.left = `${(a.left + ref.dx) / z}px`;
  }
}
// Captura: rolagem de elemento não sobe até a janela, mas passa por ela aqui.
window.addEventListener("scroll", acompanharPops, { capture: true, passive: true });

function abrirPop(elemento, ancora, { alinhar = "esquerda" } = {}) {
  if (_popAberto === elemento && _ancoraAberta === ancora) {
    fecharPop();
    return false;
  }
  fecharPop();
  elemento.hidden = false;
  _popAberto = elemento;
  _ancoraAberta = ancora;

  posicionarPop(elemento, ancora, alinhar);

  const busca = elemento.querySelector('input[type="search"]');
  if (busca) { busca.value = ""; busca.dispatchEvent(new Event("input")); busca.focus(); }
  return true;
}

function fecharPop() {
  if (_popAberto) { _popAberto.hidden = true; _popAberto = null; _ancoraAberta = null; }
}

document.addEventListener("click", e => {
  if (!_popAberto) return;
  // Qualquer popover, não só o aberto: um popover aninhado (o seletor de mês
  // dentro do painel de Filtros) vive no body, então não está "dentro" do pai
  // -- clicar num mês fechava o painel inteiro, e o Aplicar nunca chegava a
  // acontecer. Clique dentro de popover é interação com o que está na frente,
  // nunca um clique fora.
  if (e.target.closest(".pop") || e.target.closest("[data-abre-pop]")) return;
  fecharPop();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") fecharPop(); });
window.addEventListener("resize", fecharPop);

/* --------------------------------------------------- seletor de mês próprio */

const MESES_CURTOS = ["jan", "fev", "mar", "abr", "mai", "jun",
                      "jul", "ago", "set", "out", "nov", "dez"];

// Calendário, e não um chevron: é o ícone do campo nativo, e diz o que o
// controle abre. SVG inline porque herda a cor do texto e acompanha o tema.
const ICONE_CALENDARIO = `<svg class="mes-icone" viewBox="0 0 16 16" width="15" height="15"
  fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true">
  <rect x="1.8" y="3.2" width="12.4" height="11" rx="2" />
  <path d="M1.8 6.6h12.4M5.2 1.8v2.6M10.8 1.8v2.6" stroke-linecap="round" />
</svg>`;

/** Troca o calendário nativo de um input[type=month] por um do projeto.
 *
 *  O input CONTINUA no DOM e continua sendo o dono do valor -- quem já lê
 *  `input.value` ou escuta `change` não muda nada. O que sai é só a interface:
 *  o popup nativo é uma janela do sistema, onde nenhum CSS nosso entra, e por
 *  isso ele aparecia branco e com o azul do sistema no meio de uma tela
 *  escura.
 *
 *  `obrigatorio` tira o "Limpar" dos campos que não admitem mês vazio.
 *  `data-rotulo` troca o "Escolher mês" do campo vazio.
 */
function campoMes(input, { obrigatorio = false } = {}) {
  if (!input || input.dataset.mesPronto) return;
  input.dataset.mesPronto = "1";
  // Fora da tela em vez de display:none: campo escondido assim continua
  // sendo enviado por formulário e continua focável por rótulo.
  Object.assign(input.style, {
    position: "absolute", width: "1px", height: "1px",
    opacity: "0", pointerEvents: "none",
  });

  const botao = document.createElement("button");
  botao.type = "button";
  botao.className = "mes-campo";
  botao.setAttribute("data-abre-pop", "");
  botao.setAttribute("aria-haspopup", "dialog");
  botao.setAttribute("aria-expanded", "false");
  if (input.getAttribute("aria-label")) {
    botao.setAttribute("aria-label", input.getAttribute("aria-label"));
  }
  input.insertAdjacentElement("afterend", botao);

  const pop = document.createElement("div");
  pop.className = "pop pop-mes";
  pop.hidden = true;
  pop.setAttribute("role", "dialog");
  document.body.appendChild(pop);

  const hoje = new Date().toISOString().slice(0, 7);
  let anoAberto = Number((input.value || hoje).slice(0, 4));

  const pintarBotao = () => {
    botao.innerHTML = `<span>${input.value ? esc(labelMesLongo(input.value)) : esc(input.dataset.rotulo || "Escolher mês")}</span>
      ${ICONE_CALENDARIO}`;
    // "sem-mes", não "vazio": `.vazio` é o estado-vazio global do tema, uma
    // caixa tracejada de 28px de padding -- o botão virava um bloco enorme.
    botao.classList.toggle("sem-mes", !input.value);
  };

  const pintarPop = () => {
    const escolhido = input.value;
    pop.innerHTML = `
      <div class="pop-ano">
        <button type="button" data-ano="-1" aria-label="Ano anterior">‹</button>
        <strong>${anoAberto}</strong>
        <button type="button" data-ano="1" aria-label="Próximo ano">›</button>
      </div>
      <div class="pop-meses">
        ${MESES_CURTOS.map((nome, i) => {
          const valor = `${anoAberto}-${String(i + 1).padStart(2, "0")}`;
          return `<button type="button" data-mes="${valor}"
            class="${valor === escolhido ? "escolhido" : ""} ${valor === hoje ? "hoje" : ""}"
            ${valor === escolhido ? 'aria-current="true"' : ""}>${nome}</button>`;
        }).join("")}
      </div>
      <div class="pop-rodape">
        ${obrigatorio ? "<span></span>" : '<button type="button" data-limpar>Limpar</button>'}
        <button type="button" class="acento" data-hoje>Este mês</button>
      </div>`;
  };

  // O valor pode mudar por fora, e quase sempre muda: a tela carrega o mês da
  // URL com `input.value = ...`, e atribuição por código NÃO dispara `change`
  // -- o rótulo ficaria dizendo "Escolher mês" com o valor já preenchido.
  // Envolver o setter do próprio elemento resolve sem pedir nada a quem usa:
  // `input.value` continua sendo `input.value`, e o botão repinta junto.
  const nativo = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(input), "value");
  Object.defineProperty(input, "value", {
    configurable: true,
    get() { return nativo.get.call(this); },
    set(valor) { nativo.set.call(this, valor); pintarBotao(); },
  });

  const definir = valor => {
    input.value = valor;
    pintarBotao();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };
  input.addEventListener("change", pintarBotao);

  // Dentro de outro popover (o painel de Filtros), `abrirPop` não serve: a
  // primeira coisa que ele faz é fechar o popover aberto, e o painel que
  // contém este campo sumiria no clique. Aqui o popover do mês é aninhado:
  // abre por cima do pai e devolve o foco a ele ao fechar.
  const aninhado = () => !!botao.closest(".pop");
  const fechar = () => {
    if (aninhado()) { pop.hidden = true; botao.setAttribute("aria-expanded", "false"); }
    else fecharPop();
  };

  botao.addEventListener("click", () => {
    anoAberto = Number((input.value || hoje).slice(0, 4));
    const jaAberto = !pop.hidden;
    pintarPop();
    let abriu;
    if (aninhado()) {
      pop.hidden = jaAberto;
      if (!pop.hidden) posicionarPop(pop, botao);
      abriu = !pop.hidden;
    } else {
      abriu = abrirPop(pop, botao);
    }
    botao.setAttribute("aria-expanded", String(abriu));
    if (abriu) pop.querySelector(".pop-meses button.escolhido, .pop-meses button")?.focus();
  });

  // Clique fora e Escape fecham o aninhado, que o handler global não conhece.
  document.addEventListener("click", e => {
    if (pop.hidden || !aninhado()) return;
    if (pop.contains(e.target) || botao.contains(e.target)) return;
    fechar();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !pop.hidden && aninhado()) { fechar(); botao.focus(); }
  });

  pop.addEventListener("click", e => {
    const alvo = e.target.closest("button");
    if (!alvo) return;
    if (alvo.dataset.ano) {
      // pintarPop troca o HTML e tira o botão clicado da página: sem parar o
      // clique aqui, o "clique fora" do documento não acha o alvo dentro do
      // popover e fecha antes de dar tempo de escolher o mês do outro ano.
      e.stopPropagation();
      anoAberto += Number(alvo.dataset.ano);
      pintarPop();
      pop.querySelector(`[data-ano="${alvo.dataset.ano}"]`)?.focus();
      return;
    }
    if (alvo.dataset.mes) definir(alvo.dataset.mes);
    else if (alvo.dataset.hoje !== undefined) definir(hoje);
    else if (alvo.dataset.limpar !== undefined) definir("");
    fechar();
    botao.setAttribute("aria-expanded", "false");
    botao.focus();
  });

  // O popover também fecha por Escape e por clique fora, que passam longe do
  // handler acima; sem isto o aria-expanded ficaria mentindo para o leitor de
  // tela.
  const sincronizar = () => {
    if (!aninhado()) botao.setAttribute("aria-expanded", String(!pop.hidden));
  };
  document.addEventListener("click", sincronizar);
  document.addEventListener("keydown", sincronizar);

  pintarBotao();
  return botao;
}

/** Converte todo input[type=month] da página de uma vez. */
function campoMesTodos(raiz = document) {
  raiz.querySelectorAll('input[type="month"]').forEach(i =>
    campoMes(i, { obrigatorio: i.required || i.dataset.mesObrigatorio === "1" }));
}


/* ------------------------------------------------------------ navegação */

/* Ícones do app: um conjunto só em SVG, mesmo traço e mesmo peso, na cor do
 * texto (o impeccable condena glifos Unicode como ícone: cada um vem de uma
 * fonte, com peso e alinhamento diferentes). `icone(nome)` devolve o <svg>. */
const ICONES = {
  painel: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  calendario: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18M9 15.5l2 2 4-4"/>',
  transacoes: '<path d="M7 4 3 8l4 4M3 8h14M17 12l4 4-4 4M21 16H7"/>',
  categorias: '<path d="M21 12a9 9 0 1 1-9-9v9z"/><path d="M15.5 3.7A9 9 0 0 1 20.3 8.5H15.5z"/>',
  cartao: '<rect x="2.5" y="5" width="19" height="14" rx="2"/><path d="M2.5 10h19M6.5 15h4"/>',
  banco: '<path d="M3 9.5 12 4l9 5.5M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18"/>',
  investimentos: '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  entradas: '<path d="M12 3v12M7 10l5 5 5-5M4 20h16"/>',
  regras: '<path d="M3 5h18l-7 8v6l-4-2v-4z"/>',
  meta: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/>',
  emprestimo: '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
  marca: '<path d="M12 3 21 12 12 21 3 12z"/>',
  fechar_lista: '<path d="M6 15l6-6 6 6"/>',
  inverter: '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  seta_direita: '<path d="M9.5 6l6 6-6 6"/>',
  fechar: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  mais_novo: '<path d="M12 5v14M5 12h14"/>',
  local: '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  calculadora: '<rect x="5" y="3" width="14" height="18" rx="2.5"/><path d="M8.5 7.5h7"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01M8.5 15.5h.01M12 15.5h.01M15.5 15.5h.01M8.5 18.5h.01M12 18.5h.01M15.5 18.5h.01"/>',
  alerta: '<path d="M12 6v7.5"/><path d="M12 17.6v.1"/>',
  traco: '<path d="M7 12h10"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.7v.1"/>',
  mais: '<path d="M6 12h.01M12 12h.01M18 12h.01"/>',
};
function icone(nome, tamanho = 16) {
  const caminho = ICONES[nome];
  if (!caminho) return "";
  return `<svg class="icone-svg" viewBox="0 0 24 24" width="${tamanho}" height="${tamanho}" fill="none" stroke="currentColor"
    stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${caminho}</svg>`;
}

const PAGINAS = [
  { href: "visao_geral.html", ic: "painel", nome: "Visão Geral" },
  { href: "contas_fixas2.html", ic: "calendario", nome: "Contas Fixas" },
  { href: "transacoes.html", ic: "transacoes", nome: "Transações" },
  { href: "categorias.html", ic: "categorias", nome: "Categorias" },
  { href: "cartoes_pluggy.html", ic: "cartao", nome: "Cartões e Contas" },
  { href: "investimentos.html", ic: "investimentos", nome: "Investimentos" },
  { href: "entradas.html", ic: "entradas", nome: "Entradas e Regras" },
  { href: "planejamento.html", ic: "meta", nome: "Planejamento e Empréstimos" },
];

/* Ícone da aba = o ícone da página no menu, o mesmo SVG em azul claro. */
function iconeDaAba(nome) {
  const caminho = ICONES[nome];
  if (!caminho) return;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#8cc0f5"
    stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${caminho}</svg>`;
  let link = document.querySelector('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/svg+xml";
  link.href = "data:image/svg+xml," + encodeURIComponent(svg);
}

function montarNav(atual) {
  iconeDaAba(PAGINAS.find(p => p.href === atual)?.ic);
  const el = document.querySelector(".nav");
  if (!el) return;
  el.innerHTML = `
    <div class="nav-marca"><span class="selo">${icone("marca", 13)}</span><span>Open Finance</span></div>
    <div class="nav-secao">Pluggy</div>
    ${PAGINAS.map(p => `
      <a href="${p.href}" class="${p.href === atual ? "ativo" : ""}"
         ${p.href === atual ? 'aria-current="page"' : ""}>
        <span class="ic">${icone(p.ic)}</span><span>${p.nome}</span>
      </a>`).join("")}
    <div class="nav-rodape">Guilherme</div>`;
}

/* Telas que dividem uma entrada do menu e se alternam por um seletor no
 * topo (ex.: Cartões | Contas). A primeira da lista é a que o menu abre. */
const GRUPOS_TELAS = {
  cartoes: [
    { href: "cartoes_pluggy.html", ic: "cartao", nome: "Cartões" },
    { href: "contas.html", ic: "banco", nome: "Contas" },
  ],
  planejamento: [
    { href: "planejamento.html", ic: "meta", nome: "Planejamento" },
    { href: "emprestimos.html", ic: "emprestimo", nome: "Empréstimos" },
  ],
  entradas: [
    { href: "entradas.html", ic: "entradas", nome: "Entradas" },
    { href: "extrato_regras.html", ic: "regras", nome: "Regras" },
  ],
};

/** Desenha o seletor do grupo em `alvo` e devolve a página que o menu marca. */
function montarAlternador(grupo, atual, alvo = document.getElementById("alternador")) {
  const telas = GRUPOS_TELAS[grupo] || [];
  if (alvo) {
    // O seletor ocupa o lugar do título, na mesma linha das ações da tela.
    // O <h1> continua no HTML (leitor de tela, título da aba), só escondido.
    const h1 = document.querySelector("header.topo h1");
    if (h1 && !h1.parentElement.contains(alvo)) {
      h1.classList.add("titulo-oculto");
      h1.insertAdjacentElement("afterend", alvo);
    }
    alvo.className = "alternador";
    alvo.setAttribute("role", "tablist");
    alvo.innerHTML = telas.map(t => `
      <a href="${t.href}" role="tab" aria-selected="${t.href === atual}" class="${t.href === atual ? "ativo" : ""}">
        <span class="ic" aria-hidden="true">${icone(t.ic, 14)}</span>${esc(t.nome)}</a>`).join("");
  }
  return telas[0]?.href || atual;
}

/* --------------------------------------------------------- select com busca */
/* Troca a lista nativa de um <select> por um botão que abre uma lista com
 * campo de busca. O <select> continua sendo a fonte da verdade (value,
 * change), só fica escondido: quem preenche as opções não muda nada.
 *
 * Opções com data-grupo="1" viram títulos de grupo (não selecionáveis se
 * data-escolhivel="0"); as demais ficam recuadas embaixo do último título. */
function selectComBusca(select, { placeholder = "Buscar…" } = {}) {
  if (!select || select.dataset.comBusca) return;
  select.dataset.comBusca = "1";
  select.style.display = "none";
  const botao = document.createElement("button");
  botao.type = "button";
  botao.className = "sb-botao";
  botao.setAttribute("data-abre-pop", "");
  botao.setAttribute("aria-haspopup", "listbox");
  select.insertAdjacentElement("afterend", botao);
  const pop = document.createElement("div");
  pop.className = "pop sb-pop";
  pop.hidden = true;
  pop.innerHTML = `<input class="sb-busca" type="search" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}" />
    <div class="sb-lista" role="listbox"></div>`;
  document.body.appendChild(pop);
  const busca = pop.querySelector(".sb-busca"), lista = pop.querySelector(".sb-lista");
  let ativo = -1;

  const rotulo = () => {
    const o = select.options[select.selectedIndex];
    botao.textContent = o ? o.textContent.trim() : "—";
  };
  const semAcento = s => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const desenhar = () => {
    const termo = semAcento(busca.value.trim());
    let grupo = "", html = "", visiveis = 0;
    const itens = [...select.options].map(o => ({ o, grupo: o.dataset.grupo === "1" }));
    // Um grupo aparece se o título ou algum filho casa com a busca.
    const blocos = [];
    for (const it of itens) {
      if (it.grupo || !blocos.length) blocos.push({ titulo: it.grupo ? it : null, filhos: [] });
      if (!it.grupo) blocos.at(-1).filhos.push(it);
    }
    for (const b of blocos) {
      const tituloCasa = b.titulo && semAcento(b.titulo.o.textContent).includes(termo);
      const filhos = b.filhos.filter(f => !termo || tituloCasa || semAcento(f.o.textContent).includes(termo));
      if (termo && !tituloCasa && !filhos.length) continue;
      if (b.titulo) {
        const escolhivel = b.titulo.o.dataset.escolhivel !== "0";
        html += `<div class="sb-grupo ${escolhivel ? "sb-item" : ""}" ${escolhivel ? `role="option" data-v="${esc(b.titulo.o.value)}"` : ""}
          aria-selected="${b.titulo.o.value === select.value}">${esc(b.titulo.o.textContent.trim())}</div>`;
        if (escolhivel) visiveis++;
      }
      html += filhos.map(f => `<div class="sb-item sb-filho" role="option" data-v="${esc(f.o.value)}" aria-selected="${f.o.value === select.value}">${esc(f.o.textContent.trim())}</div>`).join("");
      visiveis += filhos.length;
    }
    lista.innerHTML = html || `<div class="sb-vazio">Nada encontrado.</div>`;
    const itensDom = [...lista.querySelectorAll(".sb-item")];
    ativo = termo ? 0 : itensDom.findIndex(e => e.getAttribute("aria-selected") === "true");
    marcar();
  };
  const marcar = () => {
    const itensDom = [...lista.querySelectorAll(".sb-item")];
    itensDom.forEach((e, i) => e.classList.toggle("ativo", i === ativo));
    itensDom[ativo]?.scrollIntoView({ block: "nearest" });
  };
  const escolher = valor => {
    select.value = valor;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    rotulo();
    fecharPop();
    botao.focus();
  };
  botao.addEventListener("click", () => {
    busca.value = "";
    desenhar();
    pop.style.minWidth = `${botao.getBoundingClientRect().width / zoomDaPagina()}px`;
    if (abrirPop(pop, botao)) setTimeout(() => busca.focus(), 0);
  });
  busca.addEventListener("input", desenhar);
  busca.addEventListener("keydown", e => {
    const n = lista.querySelectorAll(".sb-item").length;
    if (e.key === "ArrowDown") { ativo = Math.min(n - 1, ativo + 1); marcar(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { ativo = Math.max(0, ativo - 1); marcar(); e.preventDefault(); }
    else if (e.key === "Enter") { const el = lista.querySelectorAll(".sb-item")[ativo]; if (el) escolher(el.dataset.v); e.preventDefault(); }
  });
  lista.addEventListener("mousedown", e => e.preventDefault());
  lista.addEventListener("click", e => { const el = e.target.closest(".sb-item"); if (el) escolher(el.dataset.v); });
  // O valor pode mudar por código (abrir o modal com outra transação).
  const nativo = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  Object.defineProperty(select, "value", { configurable: true,
    get() { return nativo.get.call(this); }, set(v) { nativo.set.call(this, v); rotulo(); } });
  select.addEventListener("change", rotulo);
  rotulo();
  return { atualizar: rotulo };
}

/* ------------------------------------------------------------- estados */

function setEstado(id, texto, tipo = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = texto;
  el.className = `estado ${tipo}`.trim();
}

/* ------------------------------------------------------- filtro "Onde" */

/** Contas e cartões numa lista só, com busca e vários marcados por clique.
 *
 *  Monta um campo + lista embutida em `raiz` (não é popover: fica dentro do
 *  painel de filtros sem fechá-lo). O valor é o par `{ card, account }` das
 *  telas, cada um com ids separados por vírgula -- o mesmo da URL.
 *  `obter()` devolve o valor atual; `mudar(novo)` recebe o novo par.
 */
function montarOnde(raiz, { obter, mudar, aoAbrir = () => {} }) {
  raiz.innerHTML = `<button type="button" class="pf-campo" aria-expanded="false"></button>
    <div class="pf-lista" hidden>
      <div class="pf-busca"><input type="search" placeholder="Buscar conta ou cartão…" autocomplete="off" aria-label="Buscar conta ou cartão" />
        <button type="button" class="pf-fechar" data-onde-fechar aria-label="Fechar lista">${icone("fechar_lista", 14)}</button></div>
      <div class="pf-itens tudo"></div>
      <div class="pf-lista-rodape"><span></span><button type="button" data-onde-limpar>Limpar</button></div>
    </div>`;
  const [botao, lista] = raiz.children;
  const busca = lista.querySelector("input"), itens = lista.querySelector(".pf-itens");
  let cartoes = [], contas = [];
  const ids = v => String(v || "").split(",").filter(Boolean);
  const especial = id => id === "todos" || id === "nenhum";

  // Conta não tem cor própria: usa a do cartão da mesma tag (contas:tag:X).
  const cor = id => (cartoes.find(c => c.id === id || `contas:${c.id}` === id) || {}).cor || "var(--text-3)";
  const nome = (tipo, id) => tipo === "card" && id === "todos" ? "Só cartões"
    : tipo === "card" && id === "nenhum" ? "Fora do cartão"
    : ((tipo === "card" ? cartoes : contas).find(c => c.id === id) || {}).nome || id;
  // Fora da lista (campo e chips) o tipo vai junto: cartão e conta do mesmo
  // banco têm o mesmo nome.
  const rotulo = (tipo, id) => especial(id) ? nome(tipo, id) : `${tipo === "card" ? "Cartão" : "Conta"} ${nome(tipo, id)}`;
  const selecionados = () => {
    const v = obter();
    return [...ids(v.card).map(id => ({ tipo: "card", id })), ...ids(v.account).map(id => ({ tipo: "account", id }))];
  };

  const pintar = () => {
    const sel = selecionados(), nomes = sel.map(o => rotulo(o.tipo, o.id));
    botao.innerHTML = (sel.length
        ? `<span class="pf-pontos">${sel.slice(0, 3).map(o => `<i style="background:${cor(o.id)}"></i>`).join("")}</span>` : "")
      + `<span class="txt ${sel.length ? "" : "sem-valor"}">${esc(!sel.length ? "Todas as contas e cartões"
          : nomes.length <= 2 ? nomes.join(", ") : `${nomes.length} selecionados`)}</span>`;
    const v = obter(), termo = busca.value.trim().toLowerCase();
    const casa = x => !termo || x.nome.toLowerCase().includes(termo);
    const item = (tipo, x, extra) => `<button type="button" class="pf-item ${ids(v[tipo]).includes(x.id) ? "sel" : ""}"
        data-onde="${tipo}" data-id="${esc(x.id)}"><i style="background:${cor(x.id)}"></i>${esc(x.nome)}${extra ? `<small>${esc(extra)}</small>` : ""}</button>`;
    // Com o grupo inteiro marcado, o mesmo botão desmarca.
    const tudo = (tipo, todos) => todos.length && todos.every(x => ids(v[tipo]).includes(x.id));
    const grupo = (titulo, tipo, todos, extra) => todos.filter(casa).length
      ? `<div class="pf-grupo">${titulo}<button type="button" data-onde-todos="${tipo}">${tudo(tipo, todos) ? "desmarcar todos" : "marcar todos"}</button></div>`
        + todos.filter(casa).map(x => item(tipo, x, extra(x))).join("") : "";
    itens.innerHTML = grupo("Cartões", "card", cartoes, c => c.contas > 1 ? `${c.contas} cartões` : (c.membros?.[0]?.numero || ""))
      + grupo("Contas", "account", contas, () => "") || '<div class="pf-grupo">Nada encontrado</div>';
    const n = sel.filter(o => !especial(o.id)).length;
    lista.querySelector(".pf-lista-rodape span").textContent = n ? `${n} selecionado${n > 1 ? "s" : ""}` : "Todas as contas e cartões";
  };

  // Aberta, a lista toma o lugar do campo (a busca fica onde ele estava);
  // fecha no Esc ou clicando fora dela.
  const fechar = () => {
    lista.hidden = true; botao.hidden = false; botao.setAttribute("aria-expanded", "false");
  };
  botao.addEventListener("click", e => {
    e.stopPropagation();
    aoAbrir();
    lista.hidden = false; botao.hidden = true; botao.setAttribute("aria-expanded", "true");
    busca.value = ""; pintar(); busca.focus();
  });
  busca.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); fechar(); botao.focus(); } });
  // Fase de captura: outros cliques do painel param a propagação.
  document.addEventListener("click", e => { if (!lista.hidden && !raiz.contains(e.target)) fechar(); }, true);
  busca.addEventListener("input", pintar);
  // O clique troca o HTML da lista; sem parar aqui, o "clique fora" global
  // não acharia o alvo dentro do painel e o fecharia.
  lista.addEventListener("click", e => {
    e.stopPropagation();
    const v = obter(), it = e.target.closest("[data-onde]"), todos = e.target.closest("[data-onde-todos]");
    const novo = { card: ids(v.card).filter(x => !especial(x)).join(","), account: v.account || "" };
    if (it) {
      const atual = ids(novo[it.dataset.onde]);
      novo[it.dataset.onde] = (atual.includes(it.dataset.id)
        ? atual.filter(x => x !== it.dataset.id) : atual.concat(it.dataset.id)).join(",");
    } else if (todos) {
      const tipo = todos.dataset.ondeTodos, grupo = (tipo === "card" ? cartoes : contas).map(x => x.id);
      const marcados = ids(novo[tipo]);
      novo[tipo] = grupo.every(id => marcados.includes(id)) ? "" : grupo.join(",");
    } else if (e.target.closest("[data-onde-fechar]")) {
      fechar(); botao.focus(); return;
    } else if (e.target.closest("[data-onde-limpar]")) {
      novo.card = novo.account = "";
    } else return;
    mudar(novo);
  });

  return {
    definir(listaCartoes, listaContas) { cartoes = listaCartoes || []; contas = listaContas || []; pintar(); },
    pintar, fechar, cor, nome, rotulo, selecionados,
  };
}

/* Barras crescem só na primeira pintura da tela (tema.css, "movimento"):
   redesenhar ao filtrar não deve repetir a animação. */
document.documentElement.classList.add("primeira-pintura");
setTimeout(() => document.documentElement.classList.remove("primeira-pintura"), 2500);

/* Esqueletos de carregamento (tema.css): contêiner vazio marcado com
   data-esqueleto="tipo:quantos" ganha blocos no formato do conteúdo. Tipos:
   linhas (texto/lista), barras (listas com barra de fundo), cards. */
(function preencherEsqueletos() {
  document.querySelectorAll("[data-esqueleto]").forEach(el => {
    if (el.children.length) return;
    const [tipo, qtd] = el.dataset.esqueleto.split(":");
    const n = Number(qtd) || 4;
    const um = tipo === "cards" ? '<div class="esq-card" aria-hidden="true"><i class="esq"></i><i class="esq"></i></div>'
      : tipo === "barras" ? '<i class="esq esq-barra" aria-hidden="true"></i>'
      : '<i class="esq esq-linha" aria-hidden="true"></i>';
    const html = um.repeat(n);
    el.innerHTML = el.tagName === "TBODY" ? `<tr><td colspan="99">${html}</td></tr>` : html;
    el.setAttribute("aria-busy", "true");
  });
})();

/* Números que rolam até o valor novo (ex.: trocar de mês). Só o que está
   marcado com data-contar="chave" e só quando o valor MUDA: abrir a tela não
   anima nada. Quem pediu para reduzir movimento vê a troca direto. */
(function contadores() {
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const anteriores = new Map();
  const RE = /([−-])?\s*R\$\s*([\d.]+,\d{2})/;
  const numero = s => Number(s.replace(/\./g, "").replace(",", "."));
  const formatar = v => v.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function animar(el) {
    const texto = el.textContent, m = texto.match(RE);
    if (!m) return;
    const alvo = numero(m[2]) * (m[1] ? -1 : 1);
    const chave = el.dataset.contar, de = anteriores.get(chave);
    anteriores.set(chave, alvo);
    if (de === undefined || Math.abs(de - alvo) < 0.005) return;
    const inicio = performance.now(), duracao = 450;
    el._animando = true;
    const passo = agora => {
      const t = Math.min(1, (agora - inicio) / duracao), e = 1 - Math.pow(1 - t, 3);
      const v = de + (alvo - de) * e;
      el.textContent = texto.replace(m[0], `${m[1] && t === 1 ? m[1] : (v < 0 ? "−" : (m[1] || ""))}R$ ${formatar(Math.abs(v))}`);
      if (t < 1) requestAnimationFrame(passo);
      else { el.textContent = texto; el._ultimo = texto; el._animando = false; }
    };
    requestAnimationFrame(passo);
  }

  let agendado = false;
  new MutationObserver(() => {
    if (agendado) return;
    agendado = true;
    requestAnimationFrame(() => {
      agendado = false;
      document.querySelectorAll("[data-contar]").forEach(el => {
        if (el._animando || el.textContent === el._ultimo) return;
        el._ultimo = el.textContent;
        animar(el);
      });
    });
  }).observe(document.body, { subtree: true, childList: true, characterData: true });
})();

/* ---------------------------------------------------- ícones das categorias */
/* Cada categoria guarda no campo "emoji" o nome do ícone com o prefixo "ic:"
   ("ic:carrinho"), ou "ic:@" para a inicial do nome (pessoas). Emoji antigo,
   sem prefixo, continua aparecendo como era. Mesmo traço do menu. */
const ICONES_CATEGORIA = {
  troca: "<path d=\"M7 4 3 8l4 4M3 8h14M17 12l4 4-4 4M21 16H7\"/>",
  ciclo: "<path d=\"M17 3l3 3-3 3M4 12v-1a5 5 0 0 1 5-5h11M7 21l-3-3 3-3M20 12v1a5 5 0 0 1-5 5H4\"/>",
  raio: "<path d=\"M13 2 4 14h7l-1 8 9-12h-7z\"/>",
  cartao: "<rect x=\"2.5\" y=\"5\" width=\"19\" height=\"14\" rx=\"2\"/><path d=\"M2.5 10h19M6.5 15h4\"/>",
  banco: "<path d=\"M3 9.5 12 4l9 5.5M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18\"/>",
  codigo: "<path d=\"M4 5v14M7 5v14M10 5v14M13.5 5v14M16 5v14M20 5v14\"/>",
  carrinho: "<circle cx=\"9\" cy=\"20\" r=\"1.3\"/><circle cx=\"17.5\" cy=\"20\" r=\"1.3\"/><path d=\"M2.5 3h3l2.6 12.2a1.5 1.5 0 0 0 1.5 1.2h8a1.5 1.5 0 0 0 1.5-1.2L21 7H6.4\"/>",
  talheres: "<path d=\"M4 3v6a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2V3M7 3v18\"/><path d=\"M20 15V3a4.5 4.5 0 0 0-4.5 4.5V13a2 2 0 0 0 2 2H20zm0 0v6\"/>",
  lanche: "<path d=\"M4 10a8 5 0 0 1 16 0z\"/><path d=\"M3 14h18M5 18h14a1 1 0 0 0 1-1v-1H4v1a1 1 0 0 0 1 1z\"/>",
  pessoas: "<circle cx=\"9\" cy=\"8\" r=\"3.5\"/><path d=\"M2.5 20a6.5 6.5 0 0 1 13 0\"/><path d=\"M16 4.5a3.5 3.5 0 0 1 0 7M21.5 20a6.5 6.5 0 0 0-4-6\"/>",
  pata: "<circle cx=\"8\" cy=\"7\" r=\"1.7\"/><circle cx=\"16\" cy=\"7\" r=\"1.7\"/><circle cx=\"4.5\" cy=\"12\" r=\"1.7\"/><circle cx=\"19.5\" cy=\"12\" r=\"1.7\"/><path d=\"M12 12c-3 0-5.5 4-5.5 6.2 0 1.5 1.3 2 2.5 1.6 1-.3 2-.8 3-.8s2 .5 3 .8c1.2.4 2.5-.1 2.5-1.6C17.5 16 15 12 12 12z\"/>",
  tesoura: "<circle cx=\"6\" cy=\"7\" r=\"2.5\"/><circle cx=\"6\" cy=\"17\" r=\"2.5\"/><path d=\"M8 8.5 20 18M8 15.5 20 6\"/>",
  sacola: "<path d=\"M5 8h14l-1 12H6z\"/><path d=\"M9 8V6a3 3 0 0 1 6 0v2\"/>",
  camisa: "<path d=\"M8 3 3 6l2 4 2-1v12h10V9l2 1 2-4-5-3a4 4 0 0 1-8 0z\"/>",
  casa: "<path d=\"M3 11 12 4l9 7\"/><path d=\"M5 10v10h14V10\"/><path d=\"M10 20v-6h4v6\"/>",
  notebook: "<rect x=\"4\" y=\"5\" width=\"16\" height=\"11\" rx=\"1.5\"/><path d=\"M2 19h20\"/>",
  joia: "<path d=\"M6 4h12l3 5-9 11L3 9z\"/><path d=\"M3 9h18M10 4l2 5 2-5\"/>",
  grafico: "<path d=\"M3 17l6-6 4 4 8-8\"/><path d=\"M15 7h6v6\"/>",
  moedas: "<ellipse cx=\"12\" cy=\"6\" rx=\"7\" ry=\"3\"/><path d=\"M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6\"/>",
  tela: "<rect x=\"3\" y=\"4\" width=\"18\" height=\"13\" rx=\"2\"/><path d=\"M10 8.5v5l4.5-2.5z\"/><path d=\"M8 21h8\"/>",
  play: "<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M10 8.5v7l6-3.5z\"/>",
  carro: "<path d=\"M5 17H3.5a1 1 0 0 1-1-1v-3.2a2 2 0 0 1 .6-1.4L6 8.6A2 2 0 0 1 7.4 8h7.2a2 2 0 0 1 1.5.7l2.4 2.8 2.3.6a1.5 1.5 0 0 1 1.2 1.5V16a1 1 0 0 1-1 1H19\"/><path d=\"M9 17h6M7 11.5h11\"/><circle cx=\"7\" cy=\"17\" r=\"2\"/><circle cx=\"17\" cy=\"17\" r=\"2\"/>",
  estrada: "<path d=\"M5 20 9 4M19 20 15 4M12 6v2M12 11v2M12 16v2\"/>",
  estacionar: "<rect x=\"4\" y=\"4\" width=\"16\" height=\"16\" rx=\"3\"/><path d=\"M10 16V8h3a2.5 2.5 0 0 1 0 5h-3\"/>",
  bomba: "<path d=\"M4 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16M3 21h12M4 10h10\"/><path d=\"M14 8h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0V9l-3-3\"/>",
  contrato: "<path d=\"M7 3h7l5 5v13H7z\"/><path d=\"M14 3v5h5M10 13h6M10 17h4\"/>",
  escudo: "<path d=\"M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z\"/>",
  escudo_ok: "<path d=\"M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z\"/><path d=\"M9 12l2 2 4-4\"/>",
  chave_inglesa: "<path d=\"M15 4a4 4 0 0 0-4.6 5.4L4 15.8 6.2 18l6.4-6.4A4 4 0 0 0 18 7l-2.4 2.4-2.1-.5-.5-2.1z\"/>",
  documento: "<path d=\"M7 3h7l5 5v13H7z\"/><path d=\"M14 3v5h5M10 13h6M10 17h6\"/>",
  identidade: "<rect x=\"3\" y=\"5\" width=\"18\" height=\"14\" rx=\"2\"/><circle cx=\"9\" cy=\"11\" r=\"2\"/><path d=\"M6 16a3 3 0 0 1 6 0M14 10h4M14 14h3\"/>",
  brilho: "<path d=\"M12 3l1.8 4.7 4.7 1.8-4.7 1.8L12 16l-1.8-4.7-4.7-1.8 4.7-1.8z\"/><path d=\"M18.5 15l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z\"/>",
  aviao: "<path d=\"M10 14 3 11l1-2 8 1 4-5a2 2 0 0 1 3 3l-5 4 1 8-2 1-3-7z\"/>",
  ingresso: "<path d=\"M3 7h18v3a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4z\"/><path d=\"M15 7v2M15 11v2M15 15v2\"/>",
  claquete: "<rect x=\"3\" y=\"9\" width=\"18\" height=\"11\" rx=\"1.5\"/><path d=\"M3 9l1.5-4.5h15L21 9M8.5 4.5 7 9M13.5 4.5 12 9M18.5 4.5 17 9\"/>",
  controle: "<path d=\"M7.3 6h9.4a3.6 3.6 0 0 1 3.55 3.05l.95 6.2A2.6 2.6 0 0 1 18.6 18.3c-.8 0-1.3-.4-1.8-.9L15.5 16h-7l-1.3 1.4c-.5.5-1 .9-1.8.9a2.6 2.6 0 0 1-2.6-3.05l.95-6.2A3.6 3.6 0 0 1 7.3 6z\"/><path d=\"M7 11h3M8.5 9.5v3M15 10.5h.01M17 12.5h.01\"/>",
  coracao: "<path d=\"M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z\"/><path d=\"M8 12h2l1-2 2 4 1-2h2\"/>",
  halter: "<path d=\"M6 8v8M18 8v8M3 10v4M21 10v4M6 12h12\"/>",
  pilula: "<path d=\"M10.5 4.5a4.95 4.95 0 0 1 7 7l-6 6a4.95 4.95 0 0 1-7-7z\"/><path d=\"M8 9l7 7\"/>",
  onibus: "<rect x=\"5\" y=\"3\" width=\"14\" height=\"15\" rx=\"2\"/><path d=\"M5 11h14M8 21v-3M16 21v-3\"/><circle cx=\"8.5\" cy=\"14.5\" r=\".9\"/><circle cx=\"15.5\" cy=\"14.5\" r=\".9\"/>",
  pino: "<path d=\"M12 21s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z\"/><circle cx=\"12\" cy=\"9\" r=\"2.5\"/>",
  maleta: "<rect x=\"3\" y=\"7\" width=\"18\" height=\"13\" rx=\"2\"/><path d=\"M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M3 12h18\"/>",
  sinal: "<path d=\"M5 20v-3M10 20v-7M15 20V9M20 20V5\"/>",
  capelo: "<path d=\"M2 9l10-5 10 5-10 5z\"/><path d=\"M6 11v5c0 1.5 3 3 6 3s6-1.5 6-3v-5M22 9v6\"/>",
  livro: "<path d=\"M3 5h6a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H3zM21 5h-6a3 3 0 0 0-3 3v12a2 2 0 0 1 2-2h7z\"/>",
  escola: "<path d=\"M3 21h18M5 21V10l7-5 7 5v11M10 21v-5h4v5\"/>",
  recibo: "<path d=\"M6 3h12v18l-3-2-3 2-3-2-3 2z\"/><path d=\"M9 15l6-6\"/><circle cx=\"9.5\" cy=\"9.5\" r=\".9\"/><circle cx=\"14.5\" cy=\"14.5\" r=\".9\"/>",
  porcento: "<path d=\"M19 5 5 19\"/><circle cx=\"7\" cy=\"7\" r=\"2.5\"/><circle cx=\"17\" cy=\"17\" r=\"2.5\"/>",
  entrada: "<path d=\"M12 3v12M7 10l5 5 5-5M4 20h16\"/>",
  nota: "<rect x=\"2.5\" y=\"6\" width=\"19\" height=\"12\" rx=\"2\"/><circle cx=\"12\" cy=\"12\" r=\"2.5\"/><path d=\"M6 9.5h.01M18 14.5h.01\"/>",
  presente: "<rect x=\"3\" y=\"8\" width=\"18\" height=\"4\" rx=\"1\"/><path d=\"M5 12v8h14v-8M12 8v12M12 8c-1.5-3-5-3.5-5-1.5S10 8 12 8zm0 0c1.5-3 5-3.5 5-1.5S14 8 12 8z\"/>",
  celular: "<rect x=\"7\" y=\"2.5\" width=\"10\" height=\"19\" rx=\"2\"/><path d=\"M11 18h2\"/>",
  pontos: "<circle cx=\"6\" cy=\"12\" r=\"1.1\"/><circle cx=\"12\" cy=\"12\" r=\"1.1\"/><circle cx=\"18\" cy=\"12\" r=\"1.1\"/>",
  chave: "<circle cx=\"8\" cy=\"15\" r=\"4\"/><path d=\"M11 12l9-9M17 6l3 3M14 9l2 2\"/>",
  predio: "<rect x=\"5\" y=\"3\" width=\"14\" height=\"18\" rx=\"1\"/><path d=\"M9 7h1M14 7h1M9 11h1M14 11h1M9 15h1M14 15h1M11 21v-3h2v3\"/>",
  panela: "<path d=\"M4 10h16v6a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4z\"/><path d=\"M2 10h2M20 10h2M9 6.5c0-1 1-1.3 1-2.3M14 6.5c0-1 1-1.3 1-2.3\"/>"
};
const ROTULOS_ICONE = {
  troca: "transferência troca setas empréstimo",
  ciclo: "transferência própria entre contas",
  raio: "pix rápido raio",
  cartao: "cartão crédito fatura",
  banco: "banco ted tarifa instituição",
  codigo: "boleto código de barras",
  panela: "alimentação comida cozinha panela",
  carrinho: "supermercado mercado compras carrinho",
  talheres: "restaurante comida garfo faca",
  lanche: "delivery lanche hambúrguer",
  pessoas: "pessoas família",
  pata: "pet cachorro gato animal",
  tesoura: "barbeiro cabelo tesoura salão",
  sacola: "compras sacola loja",
  camisa: "roupas vestuário camisa",
  casa: "casa moradia lar",
  notebook: "eletrônicos computador notebook",
  joia: "joias acessórios diamante",
  grafico: "investimentos gráfico alta",
  moedas: "rendimentos moedas dinheiro",
  tela: "serviços digitais tela",
  play: "streaming apps assinatura vídeo",
  carro: "automotivo carro veículo",
  estrada: "pedágio estrada viagem",
  estacionar: "estacionamento parking",
  bomba: "combustível posto gasolina",
  contrato: "financiamento contrato",
  escudo: "seguro proteção",
  escudo_ok: "seguros proteção",
  chave_inglesa: "manutenção conserto oficina",
  documento: "documento imposto de renda papel",
  identidade: "habilitação identidade documento",
  brilho: "lazer diversão",
  aviao: "viagem avião",
  ingresso: "shows eventos ingresso",
  claquete: "cinema filme",
  controle: "jogos games videogame",
  coracao: "saúde coração",
  halter: "academia exercício",
  pilula: "farmácia remédio",
  onibus: "transporte ônibus",
  pino: "aplicativo uber táxi corrida",
  maleta: "serviços trabalho",
  sinal: "telecomunicações celular internet",
  capelo: "educação formatura",
  livro: "cursos estudo livro",
  escola: "faculdade escola",
  recibo: "impostos taxas recibo",
  porcento: "iof juros porcentagem",
  entrada: "receitas entrada",
  nota: "renda salário dinheiro",
  presente: "cashback presente",
  celular: "celular telefone",
  pontos: "outros",
  chave: "aluguel chave",
  predio: "condomínio prédio apartamento"
};

/** Conteúdo do quadradinho da categoria (o quadrado colorido fica com quem chama). */
function simboloCategoria(cat, tamanho = 16) {
  const valor = String(cat?.emoji || "");
  const cor = /^#[0-9a-f]{6}$/i.test(cat?.cor || "") ? cat.cor : "currentColor";
  const inicial = () => `<b class="cat-inicial" style="color:${cor};font-size:${Math.round(tamanho * .8)}px">${esc((cat?.nome || "?").trim().charAt(0).toUpperCase())}</b>`;
  if (valor === "ic:@" || !valor) return inicial();
  if (valor.startsWith("ic:")) {
    const caminho = ICONES_CATEGORIA[valor.slice(3)];
    if (!caminho) return inicial();
    return `<svg class="cat-svg" viewBox="0 0 24 24" width="${tamanho}" height="${tamanho}" fill="none" stroke="${cor}"
      stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${caminho}</svg>`;
  }
  return `<span style="font-size:${tamanho}px;line-height:1">${esc(valor)}</span>`;
}

/** Para texto puro (opções de select, legendas): só o emoji antigo; ícone não vira texto. */
function emojiTexto(cat) {
  const valor = String(cat?.emoji || "");
  return valor && !valor.startsWith("ic:") ? valor + " " : "";
}

/** Catálogo do seletor de ícones: [{valor, nome, busca}]. */
function catalogoIcones() {
  return [{ valor: "ic:@", nome: "Inicial do nome", busca: "inicial letra pessoa nome" }]
    .concat(Object.keys(ICONES_CATEGORIA).map(k => ({ valor: "ic:" + k, nome: (ROTULOS_ICONE[k] || k).split(" ")[0], busca: ROTULOS_ICONE[k] || k })));
}

/* ------------------------------------------------------------------
   Editor de condições E / OU (regras do extrato, entradas e vínculo das
   contas fixas). Uma linha por condição: operador + termo + remover; entre
   duas linhas, o botão E / OU. Quem lê é backend/condicoes.py: o E vale
   antes do OU, como no SQL.
   ------------------------------------------------------------------ */
const OPERADORES_COND = [["contem", "contém"], ["nao_contem", "não contém"], ["comeca_com", "começa com"], ["igual", "é igual a"]];

function montarCondicoes(caixa, lista, { aoMudar = () => {}, placeholder = "ex.: CLARO" } = {}) {
  const estado = (lista && lista.length ? lista : [{}]).map((c, i) => ({
    termo: typeof c === "string" ? c : (c.termo || ""),
    operador: (typeof c === "object" && c.operador) || "contem",
    ligacao: i && typeof c === "object" && c.ligacao === "E" ? "E" : "OU",
  }));
  caixa._cond = estado;
  caixa.classList.add("cond-editor");
  const pintar = (foco = -1) => {
    caixa.innerHTML = estado.map((c, i) => `
      ${i ? `<div class="cond-lig"><span class="cond-tg">${["E", "OU"].map(l =>
        `<button type="button" data-cond-lig="${i}" data-v="${l}" class="${c.ligacao === l ? "ativo" : ""}">${l}</button>`).join("")}</span></div>` : ""}
      <div class="cond-linha">
        <select data-cond-op="${i}" aria-label="Operador" class="${c.operador === "nao_contem" ? "nao" : ""}">${OPERADORES_COND.map(([v, n]) =>
          `<option value="${v}" ${v === c.operador ? "selected" : ""}>${n}</option>`).join("")}</select>
        <input type="text" data-cond-termo="${i}" value="${esc(c.termo)}" placeholder="${i ? "" : esc(placeholder)}" autocomplete="off" aria-label="Termo ${i + 1}" />
        <button type="button" class="icone" data-cond-rem="${i}" aria-label="Remover condição" title="Remover">×</button>
      </div>`).join("") + `<button type="button" class="cond-add" data-cond-add>+ condição</button>`;
    if (foco >= 0) caixa.querySelector(`[data-cond-termo="${foco}"]`)?.focus();
  };
  if (!caixa._condLigado) {
    caixa._condLigado = true;
    caixa.addEventListener("input", e => {
      const i = e.target.dataset.condTermo;
      if (i == null) return;
      caixa._cond[i].termo = e.target.value;
      caixa._aoMudar?.();
    });
    caixa.addEventListener("change", e => {
      const i = e.target.dataset.condOp;
      if (i == null) return;
      caixa._cond[i].operador = e.target.value;
      e.target.classList.toggle("nao", e.target.value === "nao_contem");
      caixa._aoMudar?.();
    });
    caixa.addEventListener("keydown", e => {
      if (e.key !== "Enter" || e.target.dataset.condTermo == null) return;
      e.preventDefault();
      caixa._cond.push({ termo: "", operador: "contem", ligacao: "E" });
      caixa._pintar(caixa._cond.length - 1);
    });
    caixa.addEventListener("click", e => {
      const b = e.target.closest("[data-cond-lig],[data-cond-rem],[data-cond-add]");
      if (!b) return;
      if (b.dataset.condLig) caixa._cond[b.dataset.condLig].ligacao = b.dataset.v;
      else if (b.dataset.condRem) {
        caixa._cond.splice(Number(b.dataset.condRem), 1);
        if (!caixa._cond.length) caixa._cond.push({ termo: "", operador: "contem", ligacao: "OU" });
        caixa._cond[0].ligacao = "OU";
      } else caixa._cond.push({ termo: "", operador: "contem", ligacao: "E" });
      caixa._pintar(b.dataset.condAdd != null ? caixa._cond.length - 1 : -1);
      caixa._aoMudar?.();
    });
  }
  caixa._aoMudar = aoMudar;
  caixa._pintar = pintar;
  pintar();
}

/** Condições preenchidas, prontas para o backend (vazias ficam de fora). */
function lerCondicoes(caixa) {
  const saida = [];
  for (const c of caixa?._cond || []) {
    const termo = String(c.termo || "").trim();
    if (!termo) continue;
    saida.push({ termo, operador: c.operador || "contem", ligacao: saida.length ? c.ligacao : "OU" });
  }
  return saida;
}

/** Frase curta para listas e cards: contém "A" E não contém "B". */
function frasesCondicoes(lista) {
  const nomes = Object.fromEntries(OPERADORES_COND);
  return (lista || []).map((c, i) => `${i ? `<b class="cond-k">${c.ligacao === "E" ? "E" : "OU"}</b> ` : ""}${nomes[c.operador] || "contém"} <span class="cond-t">“${esc(c.termo)}”</span>`).join(" ");
}
