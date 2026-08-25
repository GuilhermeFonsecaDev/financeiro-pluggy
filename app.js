/* Base compartilhada das telas do projeto Pluggy.
 *
 * Popover, navegação e helpers ficavam duplicados em cada página. Aqui existem
 * uma vez só.
 */

const API = "http://127.0.0.1:8766/api";

const fmtBRL = v => Number(v || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const fmtBRLCurto = v => {
  const n = Math.abs(Number(v || 0));
  if (n >= 1000) return `${v < 0 ? "-" : ""}R$ ${(n / 1000).toFixed(1).replace(".", ",")}k`;
  return fmtBRL(v);
};
const fmtSinal = v => `${v < 0 ? "−" : "+"}${fmtBRL(Math.abs(v))}`;

const MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
               "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];

function labelMes(mesRef) {
  const [ano, mes] = String(mesRef || "").split("-");
  if (!ano || !mes) return "—";
  return `${MESES[Number(mes) - 1]} ${ano}`;
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
  return p;
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
function abrirPop(elemento, ancora, { alinhar = "esquerda" } = {}) {
  if (_popAberto === elemento && _ancoraAberta === ancora) {
    fecharPop();
    return false;
  }
  fecharPop();
  elemento.hidden = false;
  _popAberto = elemento;
  _ancoraAberta = ancora;

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

  const busca = elemento.querySelector('input[type="search"]');
  if (busca) { busca.value = ""; busca.dispatchEvent(new Event("input")); busca.focus(); }
  return true;
}

function fecharPop() {
  if (_popAberto) { _popAberto.hidden = true; _popAberto = null; _ancoraAberta = null; }
}

document.addEventListener("click", e => {
  if (!_popAberto) return;
  if (_popAberto.contains(e.target) || e.target.closest("[data-abre-pop]")) return;
  fecharPop();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") fecharPop(); });
window.addEventListener("resize", fecharPop);

/* ------------------------------------------------------------ navegação */

const PAGINAS = [
  { href: "contas_fixas.html", ic: "◫", nome: "Contas Fixas" },
  { href: "transacoes.html", ic: "☰", nome: "Transações" },
  { href: "categorias.html", ic: "◑", nome: "Categorias" },
  { href: "cartoes_pluggy.html", ic: "▤", nome: "Cartões" },
  { href: "investimentos.html", ic: "↗", nome: "Investimentos" },
  { href: "extrato_regras.html", ic: "ϟ", nome: "Regras" },
  { href: "entradas.html", ic: "↑", nome: "Entradas" },
  { href: "conexoes_pluggy.html", ic: "⇋", nome: "Conexões" },
];

function montarNav(atual) {
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

/* ------------------------------------------------------------- estados */

function setEstado(id, texto, tipo = "") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = texto;
  el.className = `estado ${tipo}`.trim();
}
