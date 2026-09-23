/* Núcleo do simulador de mês.
 *
 * O que estes testes protegem são as três armadilhas de contagem: cartão
 * contado duas vezes pela chave errada, conta fixa somada por fora de uma
 * fatura que já a contém, e média achatada por meses que nunca existiram.
 *
 * Roda nos dois lugares, porque este projeto não tem Node instalado e a
 * verificação precisa acontecer em algum lugar:
 *
 *   node testes_simulador.js
 *   ou, no console de simulador.html:
 *       fetch('testes_simulador.js').then(r => r.text()).then(eval)
 */
const ehNode = typeof module !== 'undefined' && module.exports;
const assert = ehNode ? require('assert') : {
  strictEqual(a, b, msg) {
    if (a !== b) throw new Error(msg || `esperado ${JSON.stringify(b)}, veio ${JSON.stringify(a)}`);
  },
  deepStrictEqual(a, b, msg) {
    if (JSON.stringify(a) !== JSON.stringify(b)) {
      throw new Error(msg || `esperado ${JSON.stringify(b)}, veio ${JSON.stringify(a)}`);
    }
  },
};
const S = ehNode ? require('./simulador_mes.js') : SimuladorMes;

const testes = [];
const teste = (nome, fn) => testes.push([nome, fn]);

/* ---------------------------------------------------------------- média */

teste('média ignora mês sem dado e divide pelo que sobrou', () => {
  const pontos = [
    { valor: 0, origem: 'vazio' },
    { valor: 0, origem: 'vazio' },
    { valor: 100, origem: 'oficial' },
    { valor: 200, origem: 'pagamento' },
  ];
  // 300 / 2, não 300 / 4: os dois primeiros meses não existiram.
  assert.strictEqual(S.mediaHistorica(pontos), 15000);
});

teste('cartão com um mês só tem a média daquele mês', () => {
  const pontos = Array.from({ length: 11 }, () => ({ valor: 0, origem: 'vazio' }));
  pontos.push({ valor: 2227.28, origem: 'oficial' });
  assert.strictEqual(S.mediaHistorica(pontos), 222728);
});

teste('mês em aberto não entra na média', () => {
  const pontos = [
    { valor: 1000, origem: 'oficial' },
    { valor: 50, origem: 'aberta' },            // ciclo pela metade
    { valor: 800, origem: 'projecao' },         // só previsão
  ];
  assert.strictEqual(S.mediaHistorica(pontos), 100000);
});

teste('série sem nenhum mês real devolve zero, não NaN', () => {
  assert.strictEqual(S.mediaHistorica([{ valor: 0, origem: 'vazio' }]), 0);
  assert.strictEqual(S.mediaHistorica([]), 0);
});

teste('sem origem (receita) todo ponto conta', () => {
  assert.strictEqual(S.mediaHistorica([{ valor: 100 }, { valor: 0 }]), 5000);
});

/* --------------------------------------------------------- série cartão */

teste('série do cartão cruza o ano civil e usa a chave da coluna', () => {
  const porAno = {
    2025: { valores: { 'tag:x': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 500] },
            origens: { 'tag:x': Array(12).fill('vazio').fill('oficial', 11) } },
    2026: { valores: { 'tag:x': [700, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] },
            origens: { 'tag:x': Array(12).fill('vazio').fill('oficial', 0, 1) } },
  };
  const serie = S.serieCartao(porAno, 'tag:x', ['2025-12', '2026-01']);
  assert.deepStrictEqual(serie.map(p => p.valor), [500, 700]);
  assert.strictEqual(S.mediaHistorica(serie), 60000);
});

teste('cartão ausente do payload não quebra a série', () => {
  const serie = S.serieCartao({ 2026: { valores: {}, origens: {} } }, 'sumido', ['2026-01']);
  assert.deepStrictEqual(serie, [{ mes: '2026-01', valor: 0, origem: 'vazio' }]);
});

/* -------------------------------------------------- hábitos e recorrências */

teste('previstos do mês somam recorrências e reserva de todos os cartões', () => {
  const dados = {
    cartoes: [{ id: 'a' }, { id: 'b' }],
    componentes: {
      a: Array.from({ length: 12 }, () => ({ recorrencias: 0, reservaRestante: 0 })),
      b: Array.from({ length: 12 }, () => ({ recorrencias: 0, reservaRestante: 0 })),
      // Conta-membro de uma tag: não pode ser somada, senão conta duas vezes.
      membro: Array.from({ length: 12 }, () => ({ recorrencias: 999, reservaRestante: 999 })),
    },
  };
  dados.componentes.a[9] = { recorrencias: 20.9, reservaRestante: 227.1 };
  dados.componentes.b[9] = { recorrencias: 44.9, reservaRestante: 0 };
  assert.strictEqual(S.previstosDoMes(dados, '2026-10'), 29290);
});

/* ------------------------------------------------------------- preparar */

const cartoesTeste = [
  { id: 'tag:itau', contas: ['conta-1'], membros: [] },
  { id: 'tag:inter', contas: [], membros: [{ cartaoId: 'cartao-inter' }] },
];

teste('conta vira UMA linha editável, com o cartão já resolvido', () => {
  const itens = S.prepararItens([{
    id: 'a', nome: 'Celular', tag: 'Contas Pessoais', valor: 156, pago: false,
    forma: 'conta-1', incluidaCalculos: true,
    descontos: [{ descricao: 'Parte do outro', valor: 75, forma: 'conta-1' }],
  }], cartoesTeste);
  assert.strictEqual(itens.length, 1, 'o subdesconto não vira linha separada');
  assert.strictEqual(itens[0].bruto, 15600);
  assert.strictEqual(itens[0].cartao, 'tag:itau');
  assert.strictEqual(itens[0].descontos.length, 1);
  assert.strictEqual(S.liquido(itens[0]), 8100);
  assert.strictEqual(S.totalDescontos(itens[0]), 7500);
});

teste('reembolso segue o cartão do pai; subdesconto comum segue o seu', () => {
  const itens = S.prepararItens([{
    id: 'a', nome: 'Conta', valor: 100, forma: 'conta-1', incluidaCalculos: true,
    descontos: [
      { descricao: 'Volta', valor: 10, reembolso: true, forma: 'cartao-inter' },
      { descricao: 'Fatia', valor: 20, forma: 'cartao-inter' },
    ],
  }], cartoesTeste);
  assert.strictEqual(itens[0].descontos[0].cartao, 'tag:itau');
  assert.strictEqual(itens[0].descontos[1].cartao, 'tag:inter');
});

teste('projeção decide o cartão quando a forma não aponta para nenhum', () => {
  const itens = S.prepararItens([{
    id: 'a', nome: 'Academia', valor: 100, forma: 'pix', incluidaCalculos: true,
    projecao: { cartaoId: 'cartao-inter' }, descontos: [],
  }], cartoesTeste);
  assert.strictEqual(itens[0].cartao, 'tag:inter');
});

teste('membro de uma tag cai na coluna da tag, não numa chave própria', () => {
  const itens = S.prepararItens([{
    id: 'a', nome: 'X', valor: 10, forma: 'cartao-inter', incluidaCalculos: true, descontos: [],
  }], cartoesTeste);
  assert.strictEqual(itens[0].cartao, 'tag:inter');
});

/* --------------------------------------------------------------- totais */

const conta = (extra = {}) => ({
  id: 'x', nome: 'Conta', tag: 'Contas Pessoais', bruto: 0, descontos: [],
  pago: false, incluidaCalculos: true, cartao: '', ...extra,
});
const base = { itens: [], cartoes: [], receitas: 0, habitos: 0, somarHabitos: false };

teste('conta paga no cartão não é somada duas vezes', () => {
  const t = S.totais({
    ...base, receitas: 1000000,
    cartoes: [{ id: 'c', valor: 300000 }],
    itens: [conta({ nome: 'Academia', bruto: 100000, cartao: 'c' })],
  });
  // A academia já está dentro da fatura: o cartão entra por 2.000, não 3.000.
  assert.strictEqual(t.cartoes, 200000);
  assert.strictEqual(t.fixas, 100000);
  assert.strictEqual(t.despesas, 300000);
  assert.strictEqual(t.abatido, 100000);
  assert.strictEqual(t.saldo, 700000);
});

teste('conta fora do cartão soma inteira', () => {
  const t = S.totais({
    ...base, cartoes: [{ id: 'c', valor: 100000 }],
    itens: [conta({ nome: 'Aluguel', bruto: 200000 })],
  });
  assert.strictEqual(t.despesas, 300000);
  assert.strictEqual(t.abatido, 0);
});

teste('conta maior que a fatura não deixa o cartão negativo', () => {
  const t = S.totais({
    ...base, cartoes: [{ id: 'c', valor: 50000 }],
    itens: [conta({ bruto: 90000, cartao: 'c' })],
  });
  assert.strictEqual(t.cartoes, 0);
  assert.strictEqual(t.despesas, 90000);
});

teste('informativa não entra em nada', () => {
  const t = S.totais({
    ...base, cartoes: [{ id: 'c', valor: 100000 }],
    itens: [conta({ nome: 'DAS', bruto: 69000, cartao: 'c', incluidaCalculos: false })],
  });
  assert.strictEqual(t.fixas, 0);
  assert.strictEqual(t.cartoes, 100000, 'nem abate a fatura');
  assert.deepStrictEqual(t.porTag, {});
});

teste('subdesconto compõe o bruto; reembolso volta para o bolso', () => {
  const comFatia = S.totais({
    ...base,
    itens: [conta({ bruto: 15600,
      descontos: [{ descricao: 'Parte', valor: 7500, reembolso: false, cartao: '' }] })],
  });
  assert.strictEqual(comFatia.fixas, 15600, 'líquido 81 + fatia 75 = o bruto 156');

  const comVolta = S.totais({
    ...base,
    itens: [conta({ bruto: 15600,
      descontos: [{ descricao: 'Volta', valor: 7500, reembolso: true, cartao: '' }] })],
  });
  assert.strictEqual(comVolta.fixas, 600, 'reembolso não é gasto');
});

teste('subdesconto em outro cartão abate aquele cartão', () => {
  const t = S.totais({
    ...base,
    cartoes: [{ id: 'a', valor: 100000 }, { id: 'b', valor: 100000 }],
    itens: [conta({ bruto: 50000, cartao: 'a',
      descontos: [{ descricao: 'Fatia', valor: 20000, reembolso: false, cartao: 'b' }] })],
  });
  // 'a' cobre o líquido (30.000), 'b' cobre a fatia (20.000).
  assert.strictEqual(t.cartoes, 70000 + 80000);
});

teste('totais por tag separam pessoal de empresa', () => {
  const t = S.totais({
    ...base,
    itens: [conta({ bruto: 100000 }), conta({ bruto: 69000, tag: 'Contas Empresa' })],
  });
  assert.strictEqual(t.porTag['Contas Pessoais'], 100000);
  assert.strictEqual(t.porTag['Contas Empresa'], 69000);
  assert.strictEqual(t.fixas, 169000);
});

teste('hábitos só entram quando ligados', () => {
  const estado = { ...base, receitas: 100000, habitos: 30000 };
  assert.strictEqual(S.totais(estado).despesas, 0);
  assert.strictEqual(S.totais({ ...estado, somarHabitos: true }).despesas, 30000);
  assert.strictEqual(S.totais({ ...estado, somarHabitos: true }).saldo, 70000);
});

teste('receita é um número único e vira saldo', () => {
  assert.strictEqual(S.totais({ ...base, receitas: 750000 }).saldo, 750000);
});

/* ----------------------------------------------------------------- run */

const problemas = [];
for (const [nome, fn] of testes) {
  try { fn(); }
  catch (erro) { problemas.push(`${nome}: ${erro.message}`); }
}
const resumo = `${testes.length} testes, ${problemas.length} falha${
  problemas.length === 1 ? '' : 's'}`;
for (const p of problemas) console.log(`  FALHOU  ${p}`);
console.log(resumo);
if (ehNode) process.exit(problemas.length ? 1 : 0);
({ testes: testes.length, falhas: problemas.length, problemas, resumo });
