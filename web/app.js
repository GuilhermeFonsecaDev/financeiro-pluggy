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

  elemento.style.top = `${topo}px`;
  elemento.style.left = `${esq}px`;
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
    pop.style.top = `${a.top + ref.dy}px`;
    pop.style.left = `${a.left + ref.dx}px`;
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

const PAGINAS = [
  { href: "visao_geral.html", ic: "◎", nome: "Visão Geral" },
  { href: "contas_fixas2.html", ic: "◫", nome: "Contas Fixas" },
  { href: "transacoes.html", ic: "☰", nome: "Transações" },
  { href: "categorias.html", ic: "◑", nome: "Categorias" },
  { href: "cartoes_pluggy.html", ic: "▤", nome: "Cartões e Contas" },
  { href: "investimentos.html", ic: "↗", nome: "Investimentos" },
  { href: "entradas.html", ic: "↑", nome: "Entradas e Regras" },
  { href: "planejamento.html", ic: "◇", nome: "Planejamento e Empréstimos" },
];

/* Ícone da aba = o símbolo da página no menu, em PNG de fundo transparente.
 * Desenhado num canvas e não um arquivo por tela: toda página já chama
 * montarNav, então uma página nova ganha o ícone dela sem ninguém desenhar.
 * 64px para ficar nítido em tela de alta densidade; o navegador reduz. */
function iconeDaAba(simbolo) {
  if (!simbolo) return;
  const lado = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = lado;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  // Mede e desenha com a MESMA linha de base: medir com "middle" e desenhar
  // com "alphabetic" deixava o símbolo no alto do quadrado.
  const fonte = px => `600 ${px}px "Segoe UI Symbol", "Segoe UI", "Apple Symbols", sans-serif`;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  const medir = px => {
    ctx.font = fonte(px);
    const m = ctx.measureText(simbolo);
    return { esq: m.actualBoundingBoxLeft || 0, dir: m.actualBoundingBoxRight || m.width,
             topo: m.actualBoundingBoxAscent || px * .7, base: m.actualBoundingBoxDescent || 0 };
  };
  // Tamanho pelo desenho real, não pela fonte: "⇋" é largo e "☰" é baixo, e
  // os dois têm de ocupar o mesmo tanto do quadrado. Alvo: 58 de 64px.
  const referencia = 100;
  const r = medir(referencia);
  const escala = 58 / Math.max(r.esq + r.dir, r.topo + r.base, 1);
  const m = medir(referencia * escala);
  ctx.fillStyle = "#8cc0f5";
  ctx.fillText(simbolo, (lado - (m.esq + m.dir)) / 2 + m.esq, (lado - (m.topo + m.base)) / 2 + m.topo);
  let link = document.querySelector('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.type = "image/png";
  link.href = canvas.toDataURL("image/png");
}

function montarNav(atual) {
  iconeDaAba(PAGINAS.find(p => p.href === atual)?.ic);
  const el = document.querySelector(".nav");
  if (!el) return;
  el.innerHTML = `
    <div class="nav-marca"><span class="selo">◆</span><span>Open Finance</span></div>
    <div class="nav-secao">Pluggy</div>
    ${PAGINAS.map(p => `
      <a href="${p.href}" class="${p.href === atual ? "ativo" : ""}"
         ${p.href === atual ? 'aria-current="page"' : ""}>
        <span class="ic">${p.ic}</span><span>${p.nome}</span>
      </a>`).join("")}
    <div class="nav-rodape">Guilherme</div>`;
}

/* Telas que dividem uma entrada do menu e se alternam por um seletor no
 * topo (ex.: Cartões | Contas). A primeira da lista é a que o menu abre. */
const GRUPOS_TELAS = {
  cartoes: [
    { href: "cartoes_pluggy.html", ic: "▤", nome: "Cartões" },
    { href: "contas.html", ic: "⇋", nome: "Contas" },
  ],
  planejamento: [
    { href: "planejamento.html", ic: "◇", nome: "Planejamento" },
    { href: "emprestimos.html", ic: "⇄", nome: "Empréstimos" },
  ],
  entradas: [
    { href: "entradas.html", ic: "↑", nome: "Entradas" },
    { href: "extrato_regras.html", ic: "ϟ", nome: "Regras" },
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
        <span class="ic" aria-hidden="true">${t.ic}</span>${esc(t.nome)}</a>`).join("");
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
    pop.style.minWidth = `${botao.getBoundingClientRect().width}px`;
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
        <button type="button" class="pf-fechar" data-onde-fechar aria-label="Fechar lista">▴</button></div>
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
