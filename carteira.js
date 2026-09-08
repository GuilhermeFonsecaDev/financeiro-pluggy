/* Carteira-alvo e distribuição de aportes.

   Tudo aqui vive numa IIFE: a tela de Investimentos define `el`, `D`, `render`
   e `carregar` com esses mesmos nomes, e só `abrirCarteira` precisa sair.
   A primeira carga acontece ao abrir a janela, não ao carregar a página --
   quem só quer ver a carteira do Pluggy não paga a consulta. */

(function () {
const el = id => document.getElementById(id);
let D = null;
let APORTE = 0;
let SALVANDO;
// A carga inicial e um recalculo podem estar no ar ao mesmo tempo -- digitar
// um valor logo ao abrir a tela. Cada leitura leva um numero e só a última
// tem permissão de desenhar, senão a resposta antiga volta e zera o aporte.
let PEDIDO = 0;

/* ------------------------------------------------------------ formatação */

// Aceita "1.234,56" e "1234.56": a planilha e o teclado numérico discordam.
// O caso que engana é o ponto sozinho -- "1.500" são mil e quinhentos aqui,
// mas "1500.50" veio de uma planilha em inglês. Grupos de exatamente três
// dígitos depois de cada ponto denunciam o separador de milhar.
function lerNumero(texto) {
  const limpo = String(texto || "").replace(/[^\d,.-]/g, "");
  if (!limpo) return 0;
  let normalizado = limpo;
  if (limpo.includes(",")) normalizado = limpo.replace(/\./g, "").replace(",", ".");
  else if (/^-?\d{1,3}(\.\d{3})+$/.test(limpo)) normalizado = limpo.replace(/\./g, "");
  const valor = Number.parseFloat(normalizado);
  return Number.isFinite(valor) ? valor : 0;
}

const pct = v => `${Number(v || 0).toLocaleString("pt-BR", { maximumFractionDigits: 2 })}%`;

// Milhar com ponto e decimal com vírgula, do jeito que se escreve dinheiro
// aqui. A formatação acontece a cada tecla, então preserva a vírgula ainda
// sendo digitada ("1.500," continua assim) em vez de completar casas sozinha.
function formatarValor(bruto) {
  let texto = String(bruto == null ? "" : bruto).replace(/[^\d,.]/g, "");
  // Ponto com uma ou duas casas no fim veio de planilha em inglês: é decimal.
  if (!texto.includes(",") && /\.\d{1,2}$/.test(texto)) {
    texto = texto.replace(/\.(?=\d{1,2}$)/, ",");
  }
  const partes = texto.replace(/\./g, "").split(",");
  const inteiro = partes[0].replace(/^0+(?=\d)/, "");
  const agrupado = inteiro.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  if (partes.length === 1) return agrupado;
  return `${agrupado || "0"},${partes.slice(1).join("").slice(0, 2)}`;
}

// Inserir pontos muda o tamanho do texto e jogaria o cursor para o fim. A
// posição é reconstruída pela contagem de dígitos, não de caracteres.
function digitosAte(texto, posicao) {
  return (String(texto).slice(0, posicao).match(/\d/g) || []).length;
}

function posicaoApos(texto, digitos) {
  if (digitos <= 0) return 0;
  let vistos = 0;
  for (let i = 0; i < texto.length; i++) {
    if (texto[i] >= "0" && texto[i] <= "9" && ++vistos === digitos) return i + 1;
  }
  return texto.length;
}

function formatarCampo(campo) {
  const digitos = digitosAte(campo.value, campo.selectionStart);
  campo.value = formatarValor(campo.value);
  const posicao = posicaoApos(campo.value, digitos);
  campo.setSelectionRange(posicao, posicao);
  ajustarLargura(campo);
}

// O input cresce com o texto para que "R$ 50.000" fique centralizado como um
// bloco. Com largura fixa, o R$ encostava na borda e o valor saía do meio.
function ajustarLargura(campo) {
  campo.style.width = `${Math.max(4, campo.value.length + 0.5)}ch`;
}

// A data é derivada da liquidez, então é texto, não campo: editar a data não
// faria sentido -- quem manda é o D+N ao lado.
function celulaDataResgate(item) {
  if (!item.dataResgate) {
    return `<span class="data-resgate vazia" title="informe a liquidez">—</span>`;
  }
  const titulo = `resgatando hoje, o dinheiro cai em ${fmtData(item.dataResgate)}`
    + ` (D+${item.diasResgate}, rolando fim de semana; feriado pode atrasar mais)`;
  return `<span class="data-resgate" title="${esc(titulo)}">${fmtData(item.dataResgate)}</span>`;
}

/* ------------------------------------------------------------- desenho */

const IQ_ROTULO = { sim: "Sim", nao: "Não", "": "—" };

function linha(item) {
  const abrir = item.url
    ? `<a class="abrir" href="${esc(item.url)}" target="_blank" rel="noopener"
          title="Abrir ${esc(item.nome)} no BTG${item.urlComo === "classe" ? " (o BTG lista a classe deste fundo)" : item.urlComo === "fundo" ? " (o BTG lista o fundo desta classe)" : ""}">↗</a>`
    : `<span class="abrir sem" title="Fora do catálogo público do BTG">↗</span>`;
  // O link mora na coluna fixa, ao lado do nome: na última coluna ele só
  // apareceria depois de rolar a tabela inteira para a direita.
  return `<tr data-cnpj="${esc(item.cnpj)}" class="${item.abaixoDoMinimo ? "abaixo" : ""} ${item.redistribuido ? "fora" : ""}">
    <td class="col-fundo">
      <div class="fundo-linha">
        ${abrir}
        <div class="fundo-identidade">
          <span class="fundo-nome" title="${esc(item.nome)}">${esc(item.nome)}</span>
          <span class="fundo-cnpj">${esc(item.cnpjFormatado)}${item.noCatalogo ? "" : " · fora do catálogo do BTG"}</span>
        </div>
      </div>
    </td>
    <td class="num"><span class="com-unidade">
      <input class="pct" data-campo="percentual" value="${item.percentual}" inputmode="decimal" aria-label="Alocação de ${esc(item.nome)}" /><i>%</i>
    </span></td>
    <td class="num">
      <span class="aporte-linha ${item.aporte ? "" : "zerado"}">${fmtBRL(item.aporte)}</span>
      ${item.falta ? `<span class="falta">falta ${fmtBRL(item.falta)} para o mínimo</span>` : ""}
      ${item.redistribuido ? `<span class="nota-iq">restrito a IQ · ${pct(item.percentual)} redistribuídos</span>` : ""}
    </td>
    <td class="num"><span class="com-unidade">
      <input class="dias" data-campo="diasResgate" value="${item.diasResgate == null ? "" : item.diasResgate}" inputmode="numeric" aria-label="Liquidez em dias de ${esc(item.nome)}" /><i>dias</i>
    </span></td>
    <td class="num">${celulaDataResgate(item)}</td>
    <td>
      <select class="iq" data-campo="qualificado" aria-label="Investidor qualificado">
        ${["", "sim", "nao"].map(v => `<option value="${v}" ${v === item.qualificado ? "selected" : ""}>${IQ_ROTULO[v]}</option>`).join("")}
      </select>
    </td>
    <td><div class="acoes">
      <button class="icone" data-excluir="${esc(item.cnpj)}" title="Remover da carteira">×</button>
    </div></td>
  </tr>`;
}

// Redesenhar a tabela inteira no meio da digitação tiraria o foco e comeria
// teclas: o salvamento automático responde 700ms depois da última tecla, e
// nesse tempo a pessoa já está no campo seguinte. Enquanto o foco está na
// tabela, só os números calculados são atualizados; as células editáveis
// ficam como estão.
function render() {
  if (!D) return;
  const editando = el("cartCorpo").contains(document.activeElement);
  if (editando) { renderCalculos(); return; }
  const itens = D.itens || [];
  el("cartCorpo").innerHTML = itens.map(linha).join("");
  el("cartVazia").hidden = itens.length > 0;
  el("cartRodape").hidden = itens.length === 0;
  renderCalculos();
}

function renderCalculos() {
  const itens = D.itens || [];
  // Campo que só o backend com redistribuição por IQ devolve. Sem ele, a
  // conta na tela seria a antiga -- e errada, sem dizer por quê.
  const servidorAtualizado = typeof D.redistribuidos === "number";
  el("cartAviso").hidden = servidorAtualizado;
  for (const item of itens) {
    const tr = el("cartCorpo").querySelector(`tr[data-cnpj="${item.cnpj}"]`);
    if (!tr) continue;
    tr.classList.toggle("abaixo", Boolean(item.abaixoDoMinimo));
    tr.classList.toggle("fora", Boolean(item.redistribuido));
    const celula = tr.querySelector(".num .aporte-linha").parentElement;
    celula.innerHTML = `
      <span class="aporte-linha ${item.aporte ? "" : "zerado"}">${fmtBRL(item.aporte)}</span>
      ${item.falta ? `<span class="falta">falta ${fmtBRL(item.falta)} para o mínimo</span>` : ""}
      ${item.redistribuido ? `<span class="nota-iq">restrito a IQ · ${pct(item.percentual)} redistribuídos</span>` : ""}`;
  }
  el("cartSoma").textContent = pct(D.somaPercentual);
  el("cartSoma").className = `num ${D.somaFecha ? "" : "desalinhado"}`;
  el("cartTotal").textContent = fmtBRL(D.totalDistribuido);
  // A soma fora de 100% é o aviso que mais engana em silêncio: os pesos são
  // normalizados pela própria soma, então a conta "fecha" mesmo errada. Já a
  // redistribuição por IQ é o comportamento pedido -- informa, não alarma.
  const problemas = [];
  if (!D.somaFecha && itens.length) {
    problemas.push(`as alocações somam ${pct(D.somaPercentual)}, não 100% — o aporte foi repartido na proporção informada`);
  }
  if (D.semElegivel) problemas.push("todos os fundos estão marcados como IQ: não há onde alocar o aporte");
  if (D.abaixoDoMinimo) problemas.push(`${D.abaixoDoMinimo} fundo(s) abaixo do aporte mínimo`);
  const notas = [];
  if (D.redistribuidos && !D.semElegivel) {
    notas.push(`${D.redistribuidos === 1 ? "1 fundo restrito a IQ" : `${D.redistribuidos} fundos restritos a IQ`} fora do aporte: ${pct(D.percentualRedistribuido)} redistribuídos entre os demais`);
  }
  if (!servidorAtualizado) {
    setEstado("cartEstado", "Reinicie o backend para carregar a atualização", "erro");
  } else if (problemas.length) {
    setEstado("cartEstado", problemas.concat(notas).join(" · "), "erro");
  } else if (notas.length) {
    setEstado("cartEstado", notas.join(" · "));
  } else {
    setEstado("cartEstado", `${itens.length} fundo(s) na carteira`, "ok");
  }
}

/* --------------------------------------------------------------- dados */

async function carregar() {
  const pedido = ++PEDIDO;
  try {
    const dados = await pedir(`/carteira?aporte=${APORTE}`);
    if (pedido !== PEDIDO) return;
    D = dados;
    render();
  } catch (e) { if (pedido === PEDIDO) setEstado("cartEstado", e.message, "erro"); }
}

function itensDoFormulario() {
  return [...document.querySelectorAll("#corpo tr")].map(tr => {
    const valor = campo => {
      const alvo = tr.querySelector(`[data-campo="${campo}"]`);
      return alvo ? alvo.value.trim() : "";
    };
    // Só o que a tabela mostra. Nome, Anbima e aporte mínimo não são enviados,
    // e o backend mantém o que já estava guardado para eles.
    return {
      cnpj: tr.dataset.cnpj,
      percentual: lerNumero(valor("percentual")),
      diasResgate: valor("diasResgate"),
      qualificado: valor("qualificado"),
    };
  });
}

async function salvar() {
  clearTimeout(SALVANDO);
  setEstado("cartSalvo", "salvando…");
  el("cartErro").textContent = "";
  const pedido = ++PEDIDO;
  try {
    const dados = await pedir("/carteira", "POST", { itens: itensDoFormulario(), aporte: APORTE });
    if (pedido !== PEDIDO) return;
    D = dados;
    render();
    setEstado("cartSalvo", "salvo", "ok");
    setTimeout(() => setEstado("cartSalvo", ""), 2200);
  } catch (e) {
    el("cartErro").textContent = e.message;
    setEstado("cartSalvo", "não salvo", "erro");
  }
}

// Digitar não deve gravar a cada tecla, mas também não pode exigir um botão:
// a tabela salva sozinha quando a digitação para.
function salvarDepois() {
  clearTimeout(SALVANDO);
  setEstado("cartSalvo", "alterações não salvas");
  SALVANDO = setTimeout(salvar, 700);
}

/* --------------------------------------------------------------- eventos */

el("cartAporte").addEventListener("input", e => {
  formatarCampo(e.target);
  APORTE = lerNumero(e.target.value);
  clearTimeout(SALVANDO);
  // Só recalcula: o aporte não é cadastro, não fica guardado.
  SALVANDO = setTimeout(carregar, 250);
});
el("cartCorpo").addEventListener("input", e => {
  if (!e.target.matches("input")) return;
  salvarDepois();
});
el("cartCorpo").addEventListener("change", e => {
  if (e.target.matches("select")) salvar();
});
el("cartCorpo").addEventListener("click", async e => {
  const botao = e.target.closest("[data-excluir]");
  if (!botao) return;
  const item = (D.itens || []).find(i => i.cnpj === botao.dataset.excluir);
  if (!confirm(`Remover ${item ? item.nome : "este fundo"} da carteira?`)) return;
  try {
    await pedir("/carteira/excluir", "POST", { cnpj: botao.dataset.excluir });
    await carregar();
  } catch (err) { el("cartErro").textContent = err.message; }
});

/* ------------------------------------------------------------- cadastro */

let ESPERA_CNPJ;
let PEDIDO_CNPJ = 0;

function abrirCadastro() {
  el("cartNovoCnpj").value = "";
  el("cartNovoPct").value = "";
  el("cartAchado").textContent = "";
  el("cartAchado").className = "cadastro-achado";
  el("cartCadastroErro").textContent = "";
  el("cartModalCadastro").hidden = false;
  el("cartNovoCnpj").focus();
}

function fecharCadastro() {
  clearTimeout(ESPERA_CNPJ);
  el("cartModalCadastro").hidden = true;
}

// Mostra de qual fundo é o CNPJ antes de cadastrar: um dígito trocado põe
// outro fundo na carteira, e o número sozinho não denuncia isso.
async function conferirCnpj() {
  const digitos = el("cartNovoCnpj").value.replace(/\D/g, "");
  const achado = el("cartAchado");
  if (digitos.length !== 14) {
    achado.textContent = digitos.length ? "CNPJ incompleto" : "";
    achado.className = "cadastro-achado";
    return;
  }
  const pedido = ++PEDIDO_CNPJ;
  achado.textContent = "procurando…";
  achado.className = "cadastro-achado";
  try {
    const r = await pedir(`/fundos?consulta=${encodeURIComponent(digitos)}`);
    if (pedido !== PEDIDO_CNPJ) return;
    const item = (r.resultados || [])[0] || {};
    const nome = (item.btg && item.btg.nome) || (item.cvm && item.cvm.denominacao);
    achado.textContent = nome
      ? (item.btg ? nome : `${nome} — fora do catálogo do BTG`)
      : "CNPJ não encontrado nos catálogos";
    achado.className = `cadastro-achado ${nome ? "" : "nao"}`;
  } catch (e) {
    if (pedido === PEDIDO_CNPJ) { achado.textContent = e.message; achado.className = "cadastro-achado nao"; }
  }
}

async function cadastrar() {
  el("cartCadastroErro").textContent = "";
  el("cartConfirmar").disabled = true;
  try {
    await pedir("/carteira/adicionar", "POST", {
      cnpj: el("cartNovoCnpj").value, percentual: lerNumero(el("cartNovoPct").value),
    });
    fecharCadastro();
    await carregar();
  } catch (e) { el("cartCadastroErro").textContent = e.message; }
  finally { el("cartConfirmar").disabled = false; }
}

el("cartBtnCadastrar").addEventListener("click", abrirCadastro);
el("cartFecharCadastro").addEventListener("click", fecharCadastro);
el("cartModalCadastro").addEventListener("click", e => {
  if (e.target === el("cartModalCadastro")) fecharCadastro();
});
el("cartConfirmar").addEventListener("click", cadastrar);
el("cartNovoCnpj").addEventListener("input", () => {
  clearTimeout(ESPERA_CNPJ);
  ESPERA_CNPJ = setTimeout(conferirCnpj, 250);
});
for (const id of ["cartNovoCnpj", "cartNovoPct"]) {
  el(id).addEventListener("keydown", e => { if (e.key === "Enter") cadastrar(); });
}

el("cartAporte").addEventListener("focus", e => ajustarLargura(e.target));
/* --------------------------------------------------------------- abertura */

let CARREGOU = false;

async function abrirCarteira() {
  el("cartModal").hidden = false;
  if (!CARREGOU) {
    CARREGOU = true;
    ajustarLargura(el("cartAporte"));
    await carregar();
  }
}

function fecharCarteira() {
  clearTimeout(SALVANDO);
  el("cartModal").hidden = true;
}

el("btnCarteira").addEventListener("click", abrirCarteira);
el("cartFechar").addEventListener("click", fecharCarteira);
el("cartModal").addEventListener("click", e => {
  // Clique no escurecido fecha; dentro da caixa, não.
  if (e.target === el("cartModal")) fecharCarteira();
});
document.addEventListener("keydown", e => {
  // Esc fecha a janela de cima primeiro: o cadastro abre sobre a carteira.
  if (e.key !== "Escape") return;
  if (!el("cartModalCadastro").hidden) fecharCadastro();
  else if (!el("cartModal").hidden) fecharCarteira();
});
})();
