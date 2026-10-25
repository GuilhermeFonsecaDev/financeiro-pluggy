/* Núcleo de cálculo da simulação de mês.
 *
 * Só funções puras, sem DOM e sem rede: a tela (simulador.html) monta o estado
 * a partir da API e chama isto. Separado para dar para testar -- é o que
 * `testes_simulador.js` faz, no Node ou no console da própria tela.
 *
 * As contas ficam no MESMO formato que /api/fixas devolve, e não num modelo
 * achatado: a tela espelha Contas Fixas coluna por coluna, e converter ida e
 * volta só criaria duas verdades sobre o mesmo mês. Valores em centavos, para
 * somar sem erro de ponto flutuante.
 *
 * Nada aqui persiste nada. A simulação inteira vive na memória da aba.
 */
const SimuladorMes = (() => {
  const centavos = valor => Math.round((Number(valor) || 0) * 100);

  // Mês cujo valor de fatura é fato consumado. Mesma definição de
  // previsao_fatura.ORIGENS_DEFINITIVAS -- um mês ainda em aberto está pela
  // metade e não serve de base para média.
  const DEFINITIVAS = new Set(['oficial', 'pagamento', 'confirmada', 'fechada']);
  const REEMBOLSO = '__reembolso__';

  /* Média dos meses REAIS de uma série, em centavos.
   *
   * O denominador é a quantidade de meses com dado, não 12 fixo: um cartão
   * conectado há três meses tem média de três meses, não de três valores
   * diluídos em nove zeros que nunca existiram.
   *
   * `pontos`: [{valor, origem}] -- `origem` só existe para cartão; para
   * receita o chamador já filtrou os meses cobertos pelo extrato.
   */
  function mediaHistorica(pontos) {
    const reais = pontos.filter(p => p.origem === undefined || DEFINITIVAS.has(p.origem));
    if (!reais.length) return 0;
    const soma = reais.reduce((total, p) => total + centavos(p.valor), 0);
    return Math.max(0, Math.round(soma / reais.length));
  }

  /* Série de 12 meses de um cartão, a partir dos payloads por ano civil.
   *
   * `porAno`: {2025: payload, 2026: payload}. A chave do cartão é sempre o id
   * da coluna (payload.cartoes[].id) -- `valores` guarda também as contas
   * membro de uma tag, e varrer todas as chaves contaria a mesma fatura duas
   * vezes.
   */
  function serieCartao(porAno, cartaoId, meses) {
    return meses.map(mes => {
      const dados = porAno[mes.slice(0, 4)];
      const i = Number(mes.slice(5, 7)) - 1;
      return {
        mes,
        valor: Number(dados?.valores?.[cartaoId]?.[i] || 0),
        origem: dados?.origens?.[cartaoId]?.[i] || 'vazio',
      };
    });
  }

  /* Hábitos e recorrências previstos para um mês, somando todos os cartões.
   *
   * Sai de `componentes`, que o payload de cartões já traz decomposto. NÃO usar
   * /api/pluggy-cartoes/previsao: aquele endpoint grava um instantâneo, e a
   * simulação não pode deixar rastro.
   */
  function previstosDoMes(dados, mes) {
    const i = Number(mes.slice(5, 7)) - 1;
    return (dados?.cartoes || []).reduce((total, c) => {
      const comp = dados.componentes?.[c.id]?.[i];
      return total + centavos(comp?.recorrencias) + centavos(comp?.reservaRestante);
    }, 0);
  }

  /* Mapa de qualquer referência de pagamento -> id da coluna de cartão.
   *
   * Uma conta pode apontar para a conta física, para o cartão, para o membro de
   * uma tag ou para a própria coluna; tudo tem de cair no mesmo lugar.
   */
  function indiceDeCartoes(cartoes) {
    const referencias = new Map();
    for (const c of cartoes) {
      referencias.set(c.id, c.id);
      for (const r of c.contas || []) {
        referencias.set(typeof r === 'string' ? r : r.contaId || r.id, c.id);
      }
      for (const m of c.membros || []) {
        for (const id of [m.id, m.contaId, m.cartaoId]) if (id) referencias.set(id, c.id);
      }
    }
    return referencias;
  }

  /* Em qual coluna de cartão esta conta cai.
   *
   * A projeção manda quando não há transação: a conta pode estar cadastrada
   * como PIX e a cobrança cair num cartão -- é a projeção que sabe disso.
   */
  function cartaoDoItem(item, referencias) {
    const porProjecao = item.projecao
      && referencias.get(item.projecao.grupoId || item.projecao.cartaoId || item.projecao.contaId);
    return referencias.get(item.formaReferencia || item.forma) || porProjecao || '';
  }

  /* Prepara os itens vindos de /api/fixas para edição: valores em centavos e
   * o cartão de cada linha já resolvido.
   */
  function prepararItens(itens, cartoes) {
    const referencias = indiceDeCartoes(cartoes);
    return (itens || []).map(item => {
      const descontos = (item.descontos || []).map(d => ({
        descricao: d.descricao || 'Desconto',
        valor: centavos(d.valor),
        reembolso: !!d.reembolso,
        cartao: d.reembolso ? cartaoDoItem(item, referencias)
          : referencias.get(d.formaReferencia || d.forma) || '',
      }));
      const bruto = centavos(item.valor);
      return {
        id: item.id,
        nome: item.nome,
        tag: item.tag || 'Contas Pessoais',
        bruto,
        descontos,
        pago: !!item.pago,
        incluidaCalculos: item.incluidaCalculos !== false,
        cartao: cartaoDoItem(item, referencias),
      };
    });
  }

  const totalDescontos = item => item.descontos.reduce((s, d) => s + d.valor, 0);
  const liquido = item => Math.max(0, item.bruto - totalDescontos(item));

  /* Quanto de cada cartão já está coberto por conta fixa.
   *
   * A conta paga no cartão não é despesa nova: ela já está dentro do valor da
   * fatura. Somar os dois inteiros contaria o mesmo gasto duas vezes.
   */
  function cobertura(itens) {
    const coberto = {};
    const somar = (cartao, valor) => {
      if (cartao) coberto[cartao] = (coberto[cartao] || 0) + valor;
    };
    for (const item of itens) {
      if (!item.incluidaCalculos) continue;
      somar(item.cartao, liquido(item));
      for (const d of item.descontos) somar(d.cartao, d.valor);
    }
    return coberto;
  }

  /* Os totais da simulação, tudo em centavos.
   *
   * Conta informativa fica fora de tudo, como na tela de Contas Fixas: ela é
   * visível mas não entra no gasto do mês.
   */
  function totais(estado) {
    const coberto = cobertura(estado.itens);
    const gastoDoItem = item => liquido(item)
      + item.descontos.reduce((s, d) => s + (d.reembolso ? -d.valor : d.valor), 0);

    let fixas = 0;
    const porTag = {};
    for (const item of estado.itens) {
      if (!item.incluidaCalculos) continue;
      const gasto = gastoDoItem(item);
      fixas += gasto;
      porTag[item.tag] = (porTag[item.tag] || 0) + gasto;
    }

    const bruto = estado.cartoes.reduce((s, c) => s + c.valor, 0);
    const cartoes = estado.cartoes.reduce(
      (s, c) => s + Math.max(0, c.valor - (coberto[c.id] || 0)), 0);
    const habitos = estado.somarHabitos ? (estado.habitos || 0) : 0;
    const despesas = fixas + cartoes + habitos;
    return {
      receitas: estado.receitas, fixas, bruto, abatido: bruto - cartoes, cartoes,
      habitos, despesas, porTag, saldo: estado.receitas - despesas,
    };
  }

  return {
    centavos, mediaHistorica, serieCartao, previstosDoMes, indiceDeCartoes,
    cartaoDoItem, prepararItens, totalDescontos, liquido, cobertura, totais,
    DEFINITIVAS, REEMBOLSO,
  };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = SimuladorMes;
