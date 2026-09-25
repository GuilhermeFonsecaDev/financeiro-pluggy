/* Tela Contas: um banco por faixa, com as conexões dele dentro.
 *
 * Duas fontes, de propósito. `/pluggy-status` é leve e é consultado a cada
 * 3–15 s: diz o que a sincronização está fazendo agora. `/contas` é o resumo
 * por banco (saldos, cartões, faturas, investimentos) e é pedido ao abrir e
 * quando uma rodada termina -- pedir isso a cada 15 s seria recalcular a
 * tela inteira para mostrar o mesmo número.
 *
 * O resto -- conectar pela janela da Pluggy, atualizar, arquivar, histórico --
 * é o que a tela de Conexões já fazia, e continua igual.
 */
(() => {
  const el = id => document.getElementById(id);
  let atual = null, dados = null;
  let ocupado = false, conectando = false, carregando = false, timer;
  let estavaAtualizando = false;
  let assinaturaLista = '';
  // Bancos com o detalhe aberto. A tela se redesenha sozinha; o estado não
  // pode morar no DOM, senão fecha a cada rodada do polling.
  const abertos = new Set();

  /* A logo vem do backend: ele sabe qual arquivo é de qual código de banco e
   * se ela precisa ficar branca para ter contraste no tema escuro. Banco sem
   * logo fica com as iniciais na mesma caixa, para a coluna não entortar. */
  function marca(b) {
    return b.logo
      ? `<span class="ct-logo" aria-hidden="true"><img class="${b.logo.branca ? 'branca' : ''}"
           style="${b.logo.escala && b.logo.escala !== 1 ? `width:${b.logo.escala * 100}%;height:${b.logo.escala * 100}%` : ''}"
           src="${esc(b.logo.url)}" alt="" decoding="async"></span>`
      : `<span class="ct-logo sem-logo" aria-hidden="true">${esc(b.iniciais)}</span>`;
  }

  const etapas = { fila: 'Na fila', baixando: 'Buscando dados…', importando: 'Importando dados…' };
  const resultados = { ok: 'Em dia', aviso: 'Com ressalvas', erro: 'Falhando' };
  const selos = { ok: 'Em dia', aviso: 'Atenção', erro: 'Falhando', vazio: 'Sem dados', ativo: 'Atualizando' };
  const resumoRodada = { ok: 'concluída', ok_parcial: 'concluída com ressalvas', erro: 'falhou', sem_itens: 'sem instituições' };

  function avisar(texto, tipo = '') {
    el('aviso').textContent = texto;
    el('aviso').className = `cx-aviso ${tipo}`;
    el('aviso').hidden = !texto;
  }

  function data(v) {
    if (!v) return 'ainda não registrada';
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? 'data indisponível'
      : d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function dia(v) {
    if (!v) return null;
    const [ano, mes, d] = String(v).slice(0, 10).split('-');
    return (d && mes && ano) ? `${d}/${mes}` : null;
  }
  /* O laranja é relativo ao uso do banco, e a conta vem pronta do backend.
   * O balão diz o porquê, para o laranja não parecer arbitrário. */
  function dicaSilencio(s) {
    if (!s || s.diasSemNovidade == null) return '';
    const ha = `último lançamento há ${quantidade(s.diasSemNovidade, 'dia', 'dias')}`;
    if (s.esperadoAte == null) return `${ha} · sem histórico para comparar`;
    return s.fora
      ? `${ha} — mais do que o uso deste banco explica (até ${s.esperadoAte} dias seria normal)`
      : `${ha} · normal para o uso deste banco (até ${s.esperadoAte} dias)`;
  }
  function quantidade(n, singular, plural) { return `${n} ${n === 1 ? singular : plural}`; }
  // "nada", não "vazio": `.vazio` é o estado-vazio global do tema, uma caixa
  // tracejada -- o mesmo tropeço que o seletor de mês já tinha dado.
  const vazio = '<b class="nada">—</b>';

  /* Estado vivo de uma conexão, vindo do polling. `/contas` sabe o que a
   * conexão entrega; só `/pluggy-status` sabe se ela está rodando agora. */
  function vivo(id) {
    return (atual?.conexoes || []).find(c => c.id === id) || {};
  }
  function atualizando(banco) {
    return banco.conexoes.some(c => vivo(c.id).etapa);
  }

  function botoes() {
    const parar = ocupado || !!atual?.emAndamento || conectando;
    el('btnSync').disabled = !atual?.conexoes?.length || parar;
    el('btnSync').textContent = atual?.emAndamento ? 'Atualização em andamento…' : '↻ Atualizar todas';
    document.querySelectorAll('[data-sync], [data-arquivar], [data-restaurar]').forEach(b => b.disabled = parar);
    document.querySelectorAll('[data-conectar]').forEach(b => b.disabled = conectando);
    el('btnConectar').disabled = conectando;
  }

  /* ------------------------------------------------------------ a faixa */

  function faixaBanco(b) {
    const aberto = abertos.has(b.chave);
    const r = b.resumo || {};
    const estado = atualizando(b) ? 'ativo' : b.estado;
    const partes = [];
    if (b.contas.length) partes.push(quantidade(b.contas.length, 'conta', 'contas'));
    if (b.cartoes.length) partes.push(quantidade(b.cartoes.length, 'cartão', 'cartões'));
    if (b.investimentos) partes.push('investimentos');
    const oque = partes.join(' · ') || quantidade(b.conexoes.length, 'conexão', 'conexões');

    return `<article class="ct-banco ${estado === 'aviso' || estado === 'erro' ? 'cautela' : ''} ${b.chave === 'vazio' ? 'sem-dados' : ''}"
               data-banco="${esc(b.chave)}">
      <div class="ct-faixa" role="button" tabindex="0" aria-expanded="${aberto}" data-abrir="${esc(b.chave)}"
           aria-label="${esc(`${b.nome}: ${aberto ? 'recolher' : 'ver'} detalhes`)}">
        <div class="ct-quem">
          ${marca(b)}
          <div><b>${esc(b.nome)}</b><small>${esc(oque)}</small></div>
        </div>
        <div class="ct-num"><span>Em conta</span>${r.emConta == null ? vazio : `<b>${esc(fmtBRL(r.emConta))}</b>`}</div>
        <div class="ct-num"><span>Limite usado</span>${r.limiteUsadoPct == null ? vazio
          : `<b>${esc(String(Math.round(r.limiteUsadoPct)))}%</b>`}</div>
        <div class="ct-num"><span>Próx. vencimento</span>${r.proximoVencimento
          ? `<b>${esc(dia(r.proximoVencimento))}</b>` : vazio}</div>
        <div class="ct-num"><span>Dado mais recente</span>${r.dadoMaisRecente
          ? `<b class="${r.silencio?.fora ? 'antigo' : ''}" title="${esc(dicaSilencio(r.silencio))}">${esc(dia(r.dadoMaisRecente))}</b>`
          : vazio}</div>
        <span class="ct-selo ${esc(estado === 'vazio' ? 'sem-dados' : estado)}">${esc(selos[estado] || '')}</span>
        <span class="ct-seta" aria-hidden="true">${aberto ? '▴' : '▾'}</span>
      </div>
      ${aberto ? detalhe(b) : ''}
    </article>`;
  }

  function cartaoConsolidado(lista) {
    const um = lista.length === 1;
    const finais = lista.map(c => c.final).filter(Boolean);
    const titulo = um
      ? ([lista[0].marca, finais[0] ? `···· ${finais[0]}` : ''].filter(Boolean).join(' ') || lista[0].nome)
      : `${lista.length} cartões${finais.length ? ` · ${finais.map(f => `···· ${f}`).join(' · ')}` : ''}`;
    // Datas: iguais viram uma; diferentes, "vencem dia 9 e 17".
    const juntar = (dias, uma, varias) => {
      const unicos = [...new Set(dias.filter(Boolean))].sort((a, b) => a - b);
      if (!unicos.length) return '';
      return unicos.length === 1 ? `${uma} dia ${unicos[0]}` : `${varias} dia ${unicos.join(' e ')}`;
    };
    const datas = [juntar(lista.map(c => c.diaFechamento), 'fecha', 'fecham'),
                   juntar(lista.map(c => c.diaVencimento), 'vence', 'vencem')].filter(Boolean).join(' · ')
      || 'sem fatura fechada ainda';
    const comLimite = lista.filter(c => c.limite);
    const limite = comLimite.reduce((s, c) => s + c.limite, 0);
    const usado = comLimite.reduce((s, c) => s + Math.max(0, c.limite - (c.disponivel || 0)), 0);
    const pct = limite ? usado / limite * 100 : 0;
    const ultima = lista.map(c => c.ultimaVista).filter(Boolean).sort().pop();
    // "Parado" só quando todos estão parados: um cartão vivo ao lado de um
    // parado não é um banco parado.
    const congelado = lista.every(c => c.congelado);
    return `<div class="ct-item">
      <div class="linha"><span class="nome"><b>${esc(titulo)}</b>${parado(congelado)}</span>
        <span class="datas">${esc(datas)}</span></div>
      ${limite ? `<div class="trilha"><i style="width:${Math.min(100, pct)}%"></i></div>` : ''}
      <div class="meta">${esc([limite ? `${fmtBRL(usado)} de ${fmtBRL(limite)} · ${Math.round(pct)}%` : 'limite não informado',
                                ultima ? `última transação ${dia(ultima)}` : ''].filter(Boolean).join(' · '))}</div>
    </div>`;
  }

  const parado = sim => sim ? '<span class="ct-parado" title="Vem de uma conexão que falha ou foi arquivada: não vai mais atualizar">parado</span>' : '';

  function detalhe(b) {
    const avisos = b.avisos.length
      ? `<div class="ct-avisos">${b.avisos.map(a => `<div class="ct-aviso-linha">${esc(a.texto)}</div>`).join('')}</div>` : '';

    // Uma linha de apoio por item, com as datas juntas: seis tipos de linha
    // cinza pequena, cada uma sozinha, disputavam a atenção.
    const meta = partes => {
      const texto = partes.filter(Boolean).join(' · ');
      return texto ? `<div class="meta">${esc(texto)}</div>` : '';
    };
    // O selo vai colado no nome, sempre no mesmo lugar -- solto na linha ele
    // flutuava no meio, empurrado pelo espaço entre nome e datas.
    const nome = (texto, congelado) => `<span class="nome"><b>${esc(texto)}</b>${parado(congelado)}</span>`;

    const contas = b.contas.length ? `<section class="ct-secao contas"><h4>Contas</h4>${b.contas.map(c => `
      <div class="ct-item">
        <div class="linha">${nome(c.tipo, c.congelada)}<span class="val">${esc(fmtBRL(c.saldo))}</span></div>
        ${meta([[c.nome !== c.tipo ? c.nome : '', c.agencia ? `ag ${c.agencia}` : '', c.final].filter(Boolean).join(' · ')])}
        ${meta([`saldo de ${dia(c.atualizadoEm) || '—'}`, c.ultimaVista ? `última transação ${dia(c.ultimaVista)}` : ''])}
      </div>`).join('')}</section>` : '';

    // A fatura é da tag, não de um cartão: sobe para o título da seção, uma
    // vez só. Parecia um terceiro cartão na lista. Sem valor, sem selo -- o
    // "—" numa caixa parecia um botão sem função.
    const faturaNoTitulo = b.faturas.map(f => {
      const mes = labelMes(f.mes).split(' ')[0].toLowerCase();
      const selo = f.origem && f.origem !== 'vazio'
        ? ` <span class="selo-origem ${esc(f.origem)}">${esc(rotuloOrigem(f.origem))}</span>` : '';
      return `<span class="fatura">fatura de ${esc(mes)} <b>${esc(fmtBRL(f.valor))}</b>${selo}</span>`;
    }).join('');

    // Dois ou mais cartões viram uma linha só, com o limite somado: o que a
    // faixa pergunta é quanto do crédito do banco está usado, não o de cada
    // plástico. O detalhe de cada um continua na tela de Cartões.
    const cartoes = b.cartoes.length ? `<section class="ct-secao cartoes">
      <h4>Cartões${faturaNoTitulo}</h4>${cartaoConsolidado(b.cartoes)}</section>` : '';

    const inv = b.investimentos;
    const investimentos = inv ? `<section class="ct-secao investimentos"><h4>Investimentos</h4>
      <div class="ct-item">
        <div class="linha">${nome(quantidade(inv.posicoes, 'posição', 'posições'), inv.congelado)}
          <span class="val">${esc(fmtBRL(inv.liquido))}</span></div>
        ${meta([`${fmtBRL(inv.disponivelResgate)} disponível para resgate`, `coletado em ${dia(inv.coletadoEm) || '—'}`])}
      </div></section>` : '';

    const conexoes = `<div class="ct-conexoes"><div class="ct-secao"><h4>${esc(quantidade(b.conexoes.length, 'conexão', 'conexões'))}</h4></div>
      ${b.conexoes.map(c => conexao(c)).join('')}</div>`;

    return `<div class="ct-detalhe">${avisos}
      <div class="ct-secoes">${contas}${cartoes}${investimentos}</div>
      ${conexoes}</div>`;
  }

  function conexao(c) {
    const v = vivo(c.id);
    const etapa = etapas[v.etapa];
    const resultado = v.sincronizacao?.resultado || c.resultado;
    const estado = c.arquivada ? 'Arquivada' : etapa || resultados[resultado] || 'Sem tentativa registrada';
    const classe = c.arquivada ? '' : v.etapa ? 'ativo' : resultado || '';
    const e = c.entrega || {};
    const entrega = [e.contas ? quantidade(e.contas, 'conta', 'contas') : '',
                     e.cartoes ? quantidade(e.cartoes, 'cartão', 'cartões') : '',
                     e.investimentos ? quantidade(e.investimentos, 'investimento', 'investimentos') : '']
      .filter(Boolean).join(' · ') || 'não entrega nada';
    const acoes = c.arquivada
      ? `<button data-restaurar="${esc(c.id)}">Reativar</button>`
      : `<button data-sync="${esc(c.id)}" title="Buscar dados desta conexão">↻ Atualizar</button>
         <button data-arquivar="${esc(c.id)}" title="Parar atualizações e preservar o histórico">Arquivar</button>`;
    return `<div class="ct-conexao ${c.arquivada ? 'arquivada' : ''}" data-item="${esc(c.id)}">
      <span class="cx-estado ${esc(classe)}">${esc(estado)}</span>
      <span class="quando">importada ${esc(data(c.importadoEm))}</span>
      <span class="entrega">${esc(entrega)}${c.dadoMaisRecente ? ` · último lançamento ${esc(dia(c.dadoMaisRecente))}` : ''}</span>
      <span class="acoes">${acoes}</span>
    </div>`;
  }

  /* ------------------------------------------------------------ render */

  function render() {
    if (!atual) return;
    const bancos = (dados?.bancos || []).filter(b => b.chave !== 'vazio' || b.conexoes.some(c => !c.arquivada));
    const reais = bancos.filter(b => b.chave !== 'vazio');
    const pendentes = reais.filter(b => b.estado === 'erro' || b.estado === 'aviso');
    const contas = reais.reduce((n, b) => n + b.contas.length, 0);
    const cartoes = reais.reduce((n, b) => n + b.cartoes.length, 0);

    // O estado geral ("1 banco pedindo atenção") saiu daqui: o painel de
    // pendências logo abaixo já diz isso, com o nome do banco.
    el('saudeResumo').innerHTML = dados ? `<b>${reais.length}</b> ${reais.length === 1 ? 'banco' : 'bancos'}`
      + ` · <b>${contas}</b> ${contas === 1 ? 'conta' : 'contas'}`
      + ` · <b>${cartoes}</b> ${cartoes === 1 ? 'cartão' : 'cartões'}` : '';
    el('saudeQuando').innerHTML = atual.ultimaImportacao
      ? `última rodada <b>${esc(data(atual.ultimaImportacao))}</b> · ${esc(resumoRodada[atual.ultimoResultado] || 'não registrada')}`
      : 'nenhuma rodada registrada';
    el('saudeIntervalo').innerHTML = atual.intervaloHoras
      ? `busca automática a cada <b>${esc(String(atual.intervaloHoras))} h</b>` : '';

    el('pendencias').hidden = !pendentes.length;
    if (pendentes.length) {
      el('pendencias').innerHTML = `<strong>${quantidade(pendentes.length, 'banco pede', 'bancos pedem')} atenção</strong>`
        + `<p>Abra ${esc(pendentes.map(b => b.nome).join(', '))} para ver o que está parado e o que fazer.</p>`;
    }

    // Redesenha só se algo mudou: o polling roda a cada poucos segundos e,
    // sem isso, a lista piscaria e roubaria o foco no meio de um clique.
    const vivos = (atual.conexoes || []).map(c => [c.id, c.etapa, c.sincronizacao?.resultado]);
    const assinatura = JSON.stringify([bancos, vivos, [...abertos]]);
    if (assinatura !== assinaturaLista) {
      const foco = document.activeElement;
      const focoBanco = foco?.closest('[data-banco]')?.dataset.banco;
      const focoAcao = foco?.matches('[data-abrir]') ? '[data-abrir]'
        : foco?.dataset?.sync ? `[data-sync="${CSS.escape(foco.dataset.sync)}"]`
        : foco?.dataset?.arquivar ? `[data-arquivar="${CSS.escape(foco.dataset.arquivar)}"]` : null;

      el('bancos').innerHTML = !dados
        ? '<div class="cx-carregando">Consultando bancos e produtos…</div>'
        : bancos.length ? bancos.map(faixaBanco).join('')
        : `<div class="cx-vazio"><h3>Seu financeiro começa pelas contas</h3>
             <p>Conecte sua primeira instituição para reunir contas e cartões e acompanhar as próximas importações.</p>
             <button class="primario" data-conectar="">+ Conectar instituição</button></div>`;
      assinaturaLista = assinatura;
      if (focoBanco && focoAcao) {
        document.querySelector(`[data-banco="${CSS.escape(focoBanco)}"] ${focoAcao}`)?.focus();
      }
    }

    el('bancos').setAttribute('aria-busy', 'false');
    botoes();
  }

  /* ------------------------------------------------------------ cargas */

  async function carregarBancos() {
    try {
      dados = await pedir('/contas');
    } catch (e) {
      avisar(`Não foi possível montar o resumo dos bancos. ${e.message}`, 'erro');
    }
    render();
  }

  async function carregar() {
    if (carregando) return;
    carregando = true;
    try {
      atual = await pedir('/pluggy-status');
      if (el('aviso').dataset.rede) { avisar(''); delete el('aviso').dataset.rede; }
      // Rodada acabou: os saldos e cartões podem ter mudado.
      if (estavaAtualizando && !atual.emAndamento) carregarBancos();
      estavaAtualizando = !!atual.emAndamento;
      render();
    } catch (e) {
      avisar(`Não foi possível consultar as conexões. ${e.message}`, 'erro');
      el('aviso').dataset.rede = '1';
      el('bancos').setAttribute('aria-busy', 'false');
    } finally {
      carregando = false;
      clearTimeout(timer);
      timer = setTimeout(() => { if (!document.hidden) carregar(); }, atual?.emAndamento ? 3000 : 15000);
    }
  }

  async function sincronizar(itemId = '') {
    ocupado = true; botoes();
    try {
      const r = await pedir('/pluggy-sync', 'POST', itemId ? { itemId } : {});
      avisar(r.jaEmAndamento
        ? 'Já existe uma atualização em andamento. Aguarde a conclusão.'
        : 'Atualização iniciada. Os resultados aparecerão nesta página.', 'ok');
      estavaAtualizando = true;
      await carregar();
    } catch (e) {
      avisar(`Não foi possível iniciar a atualização: ${e.message}`, 'erro');
    } finally {
      ocupado = false; botoes();
    }
  }

  async function arquivar(itemId, arquivada) {
    if (ocupado) return;
    const banco = (dados?.bancos || []).find(b => b.conexoes.some(c => c.id === itemId));
    const nome = banco?.nome || 'esta instituição';
    if (arquivada && !confirm(`Arquivar esta conexão do ${nome} (${itemId.slice(0, 8)})?\n\nEla deixará de ser atualizada. Contas, transações e vínculos já importados serão preservados no financeiro. Você poderá reativá-la depois.`)) return;
    ocupado = true; botoes();
    try {
      await pedir('/pluggy-items/arquivar', 'POST', { itemId, arquivada });
      avisar(arquivada ? 'Conexão arquivada. Histórico financeiro preservado.'
        : 'Conexão reativada. Clique em Atualizar para buscar os dados.', 'ok');
      await Promise.all([carregar(), carregarBancos()]);
    } catch (e) { avisar(e.message, 'erro'); }
    finally { ocupado = false; botoes(); }
  }

  async function conectar(itemId = '') {
    if (conectando) return;
    conectando = true; botoes();
    let concluida = false;
    const liberar = () => { conectando = false; botoes(); };
    avisar(itemId ? 'Preparando a renovação do acesso…' : 'Preparando a conexão com sua instituição…');
    try {
      const { connectToken } = await pedir('/pluggy-connect-token' + (itemId ? `?itemId=${encodeURIComponent(itemId)}` : ''));
      if (typeof PluggyConnect === 'undefined') {
        throw new Error('A janela da Pluggy não carregou. Verifique sua conexão e tente novamente.');
      }
      new PluggyConnect({
        connectToken,
        ...(itemId ? { updateItem: itemId } : {}),
        includeSandbox: false,
        language: 'pt',
        onSuccess: async resposta => {
          concluida = true;
          try {
            const item = resposta?.item || resposta;
            if (!item?.id && !item?.itemId) throw new Error('A instituição não devolveu o identificador da conexão.');
            if (itemId && (item.id || item.itemId) !== itemId) {
              throw new Error('O identificador recebido não corresponde à conexão em renovação.');
            }
            const r = await pedir('/pluggy-items', 'POST', { item });
            avisar(r.sincronizando
              ? 'Acesso registrado. Importando os dados da instituição…'
              : 'Acesso registrado. Aguarde a rodada atual terminar e clique em Atualizar.', 'ok');
            estavaAtualizando = true;
            await Promise.all([carregar(), carregarBancos()]);
          } catch (e) {
            avisar(`O acesso foi autorizado, mas o registro local falhou: ${e.message}`, 'erro');
          } finally { liberar(); }
        },
        onError: () => {
          avisar('Não foi possível concluir o acesso. Tente novamente na janela da instituição.', 'erro');
          liberar();
        },
        onClose: () => {
          if (!concluida) { avisar('Janela fechada sem concluir a autorização.'); liberar(); }
        },
      }).init();
    } catch (e) {
      avisar(e.message, 'erro');
      liberar();
    }
  }

  /* ------------------------------------------------------------ eventos */

  function alternar(chave) {
    abertos.has(chave) ? abertos.delete(chave) : abertos.add(chave);
    render();
    document.querySelector(`[data-banco="${CSS.escape(chave)}"] [data-abrir]`)?.focus();
  }

  el('btnSync').addEventListener('click', () => sincronizar());
  el('btnConectar').addEventListener('click', () => conectar());
  el('bancos').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (b) {
      if (b.hasAttribute('data-sync')) sincronizar(b.dataset.sync);
      if (b.hasAttribute('data-arquivar')) arquivar(b.dataset.arquivar, true);
      if (b.hasAttribute('data-restaurar')) arquivar(b.dataset.restaurar, false);
      if (b.hasAttribute('data-conectar')) conectar(b.dataset.conectar);
      return;
    }
    const faixa = e.target.closest('[data-abrir]');
    if (faixa) alternar(faixa.dataset.abrir);
  });
  el('bancos').addEventListener('keydown', e => {
    const faixa = e.target.closest('[data-abrir]');
    if (faixa && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); alternar(faixa.dataset.abrir); }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) carregar(); else clearTimeout(timer);
  });
  window.addEventListener('beforeunload', () => clearTimeout(timer));

  montarNav('contas.html');
  observarIdentidadeCartoes(() => Promise.all([carregar(), carregarBancos()]));
  carregar();
  carregarBancos();
})();
