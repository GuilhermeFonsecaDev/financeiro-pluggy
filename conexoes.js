/* Tela de Conexões.
 *
 * O histórico descreve importações locais de contas/cartões.
 *
 * A decisão que organiza este arquivo: o campo `conector` que a Pluggy devolve
 * vem "MeuPluggy" em todas as conexões deste projeto, então usá-lo como título
 * produzia N instituições homônimas e indistinguíveis. O nome passa a ser
 * derivado dos produtos da própria conexão -- ver `nome()`.
 */
(() => {
  const el = id => document.getElementById(id);
  let atual = null, ocupado = false, conectando = false, carregando = false, timer;
  let assinaturaLista = '', assinaturaHistorico = '';
  // Quais instituições estão com o detalhe aberto. Em tabela o detalhe é uma
  // linha irmã, não um <details>, então o estado não pode morar no DOM: a tela
  // se redesenha sozinha a cada 15 s.
  const detalhesAbertos = new Set();
  const labels = { ok: 'Importação concluída', aviso: 'Importado com ressalvas', erro: 'Falha na última tentativa' };
  const etapas = { fila: 'Na fila', baixando: 'Buscando dados…', importando: 'Importando dados…' };
  const resumoRodada = { ok: 'concluída', ok_parcial: 'concluída com ressalvas', erro: 'falhou', sem_itens: 'sem instituições' };

  function avisar(texto, tipo = '') {
    el('aviso').textContent = texto;
    el('aviso').className = `cx-aviso ${tipo}`;
    el('aviso').hidden = !texto;
  }

  function data(v) {
    if (!v) return 'Ainda não registrada';
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? 'Data indisponível'
      : d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  /* Nome da instituição, deduzido do que a conexão trouxe.
   *
   * Em ordem: a tag de um cartão dela (é o nome que o usuário mesmo deu, e o
   * que aparece nas outras telas); senão o prefixo comum dos nomes das contas
   * ("BTG Banking" + "BTG Investimentos" -> "BTG"); senão o primeiro nome de
   * conta; e só então o conector.
   *
   * Sem lista de bancos: qualquer instituição nova cai nas mesmas regras. */
  function nome(c) {
    // Texto como ele vem, sem normalizar caixa: "BTG" nao pode virar "Btg", e
    // a tag e o nome que o usuario escreveu -- tem de bater com a tela de
    // Cartoes, onde ela aparece do jeito que ele digitou.
    const tag = (c.cartoes || []).find(Boolean);
    if (tag) return tag;

    const contas = (c.contas || []).filter(Boolean);
    if (contas.length > 1) {
      const palavras = contas.map(n => n.trim().split(/\s+/));
      const comuns = [];
      for (let i = 0; i < palavras[0].length; i++) {
        const p = palavras[0][i];
        if (palavras.every(lista => (lista[i] || '').toUpperCase() === p.toUpperCase())) comuns.push(p);
        else break;
      }
      if (comuns.length) return comuns.join(' ');
    }
    // "Nu Pagamentos S.A. - Instituicao de Pagamento" -> "Nu Pagamentos S.A."
    if (contas.length) return contas[0].split(/\s+-\s+/)[0];
    return c.conector || `Instituição ${c.idCurto}`;
  }

  function inicial(texto) {
    const partes = String(texto).trim().split(/\s+/).filter(Boolean);
    return (partes.length > 1 ? partes[0][0] + partes[1][0] : (partes[0] || '?').slice(0, 2)).toUpperCase();
  }

  /* Só o dia, para a coluna de dado mais recente: hora ali não diz nada, o
   * lançamento vem datado do dia. */
  function dia(v) {
    if (!v) return null;
    const [ano, mes, d] = String(v).slice(0, 10).split('-');
    return (d && mes && ano) ? `${d}/${mes}/${ano.slice(2)}` : null;
  }

  /* Quantos dias desde a data, para avisar quando uma conexão parou de
   * alimentar o financeiro sem que a rodada tenha falhado. */
  function diasAtras(v) {
    if (!v) return null;
    const quando = new Date(`${String(v).slice(0, 10)}T12:00:00`);
    if (Number.isNaN(quando.getTime())) return null;
    return Math.floor((Date.now() - quando.getTime()) / 86400000);
  }

  function precisa(c) { return ['erro', 'aviso'].includes(c.sincronizacao?.resultado); }
  function quantidade(n, singular, plural) { return `${n} ${n === 1 ? singular : plural}`; }

  function botoes() {
    el('btnSync').disabled = !atual?.conexoes?.length || ocupado || atual?.emAndamento || conectando;
    el('btnSync').textContent = atual?.emAndamento ? 'Atualização em andamento…' : '↻ Atualizar todas';
    document.querySelectorAll('[data-sync]').forEach(b => b.disabled = ocupado || !!atual?.emAndamento || conectando);
    document.querySelectorAll('[data-conectar]').forEach(b => b.disabled = conectando);
    el('btnConectar').disabled = conectando;
  }

  function linhaInstituicao(c, abertos) {
    const s = c.sincronizacao || {};
    const classe = c.etapa ? 'ativo' : s.resultado || '';
    const rotulo = etapas[c.etapa] || labels[s.resultado] || 'Sem tentativa registrada';
    const recente = dia(c.dadoMaisRecente);
    const dias = diasAtras(c.dadoMaisRecente);
    // Três dias sem lançamento novo já é sinal: as contas correntes deste
    // projeto recebem movimento quase diário.
    const parado = dias !== null && dias > 3;
    const aberto = abertos.has(c.id);

    const lista = (rotuloLista, itens) => itens?.length
      ? `<div><h4>${rotuloLista}</h4><ul>${itens.map(n => `<li>${esc(n)}</li>`).join('')}</ul></div>` : '';

    const principal = `<tr class="${precisa(c) ? 'cautela' : ''}" data-item="${esc(c.id)}">
      <td>
        <div class="cx-quem">
          <span class="cx-marca" aria-hidden="true">${esc(inicial(nome(c)))}</span>
          <b>${esc(nome(c))}</b>
        </div>
      </td>
      <td class="num">${c.contas?.length || '<span class="cx-vazia">—</span>'}</td>
      <td class="num">${c.cartoes?.length || '<span class="cx-vazia">—</span>'}</td>
      <td class="num ${parado ? 'cx-parado' : ''}" title="${recente ? esc(`há ${dias} ${dias === 1 ? 'dia' : 'dias'}`) : 'nenhum lançamento importado'}">
        ${recente ? esc(recente) : '<span class="cx-vazia">—</span>'}
      </td>
      <td class="num">${s.sucessoEm ? esc(data(s.sucessoEm)) : s.tentativaEm ? esc(data(s.tentativaEm)) : '<span class="cx-vazia">—</span>'}</td>
      <td><span class="cx-estado ${esc(classe)}">${esc(rotulo)}</span></td>
      <td class="acoes">
        <button data-sync="${esc(c.id)}" title="Buscar dados desta instituição">↻ Atualizar</button>
        <button class="icone" data-detalhe="${esc(c.id)}" aria-expanded="${aberto}" aria-label="Detalhes de ${esc(nome(c))}">${aberto ? '▴' : '▾'}</button>
      </td>
    </tr>`;

    if (!aberto) return principal;

    return principal + `<tr class="cx-detalhe-linha" data-detalhe-de="${esc(c.id)}">
      <td colspan="7">
        <div class="cx-detalhe">
          ${lista('Contas vinculadas', c.contas)}
          ${(c.cartoesDetalhe || []).length ? `<div><h4>Cartões vinculados</h4><ul>${
            c.cartoesDetalhe.map(m => `<li>${esc(m.nome)}${m.tag ? ` <em>tag ${esc(m.tag)}</em>` : ''}</li>`).join('')
          }</ul></div>` : lista('Cartões vinculados', c.cartoes)}
          <div>
            <h4>Última tentativa</h4>
            <p>${esc(data(s.tentativaEm))}${s.detalhe ? `<br>${esc(s.detalhe)}` : ''}</p>
          </div>
          <div>
            <h4>Identificador</h4>
            <p><code>${esc(c.id)}</code></p>
          </div>
        </div>
      </td>
    </tr>`;
  }

  /* Uma linha por RODADA. A rodada processa todas as conexões e registrava um
   * evento por instituição -- quatro linhas com o mesmo horário e o mesmo
   * texto. Agrupa por minuto e só nomeia instituição quando alguma falhou. */
  function rodadas(hist, cs) {
    const porMinuto = new Map();
    for (const h of hist) {
      const chave = String(h.tentativaEm || '').slice(0, 16);
      if (!porMinuto.has(chave)) porMinuto.set(chave, []);
      porMinuto.get(chave).push(h);
    }
    return [...porMinuto.entries()].slice(0, 8).map(([chave, eventos]) => {
      const problemas = eventos.filter(h => h.resultado === 'erro' || h.resultado === 'aviso');
      const nomeDe = id => nome(cs.find(c => c.id === id) || { idCurto: String(id).slice(0, 8) });
      const situacao = problemas.length
        ? `${quantidade(problemas.length, 'instituição', 'instituições')} com ressalva`
        : 'concluída';
      const detalhe = problemas.length
        ? `<span>${esc([...new Set(problemas.map(h => nomeDe(h.itemId)))].join(', '))}</span>` : '';
      return `<div class="cx-rodada">
        <time datetime="${esc(eventos[0].tentativaEm || '')}">${data(eventos[0].tentativaEm)}</time>
        <div class="quem">${quantidade(eventos.length, 'instituição', 'instituições')}${detalhe}</div>
        <span class="cx-estado ${problemas.length ? 'aviso' : 'ok'}">${esc(situacao)}</span>
      </div>`;
    }).join('');
  }

  function render(st) {
    atual = st;
    const cs = st.conexoes || [], pendentes = cs.filter(precisa);
    const contas = cs.reduce((n, c) => n + (c.contas || []).length, 0);
    const cartoes = cs.reduce((n, c) => n + (c.cartoes || []).length, 0);

    // Linha de saúde: o estado geral primeiro, e o número de problemas só
    // aparece quando existe algum.
    const saude = el('saudeEstado');
    saude.textContent = st.emAndamento ? 'Atualizando agora'
      : pendentes.length ? `${quantidade(pendentes.length, 'conexão', 'conexões')} com ressalva`
      : 'Tudo importado';
    saude.className = `cx-estado ${st.emAndamento ? 'ativo' : pendentes.length ? 'aviso' : 'ok'}`;
    el('saudeResumo').innerHTML = `<b>${cs.length}</b> ${cs.length === 1 ? 'instituição' : 'instituições'}`
      + ` · <b>${contas}</b> ${contas === 1 ? 'conta' : 'contas'}`
      + ` · <b>${cartoes}</b> ${cartoes === 1 ? 'cartão' : 'cartões'}`;
    el('saudeQuando').innerHTML = st.ultimaImportacao
      ? `última rodada <b>${esc(data(st.ultimaImportacao))}</b> · ${esc(resumoRodada[st.ultimoResultado] || 'não registrada')}`
      : 'nenhuma rodada registrada';
    el('saudeIntervalo').innerHTML = st.intervaloHoras
      ? `busca automática a cada <b>${esc(String(st.intervaloHoras))} h</b>` : '';

    el('estado').textContent = st.emAndamento
      ? 'Buscando dados em segundo plano. Você pode continuar usando o app.'
      : 'Dado mais recente é a data do último lançamento que cada uma entregou.';

    el('pendencias').hidden = !pendentes.length;
    if (pendentes.length) {
      el('pendencias').innerHTML = `<strong>${quantidade(pendentes.length, 'conexão', 'conexões')} com ressalvas</strong>`
        + `<p>Confira o detalhe de ${esc(pendentes.map(nome).join(', '))} e tente atualizar de novo. Se o banco pedir nova autorização, reconecte a instituição.</p>`;
    }
    el('intervalo').textContent = `Ao abrir o app, a busca automática respeita um intervalo mínimo de ${st.intervaloHoras ?? '—'} horas desde a última importação. Durante uma rodada, uma instituição é processada por vez.`;

    const listaNova = JSON.stringify(cs);
    if (listaNova !== assinaturaLista) {
      // Re-render preserva o que o usuário abriu e onde estava o foco: a tela
      // se atualiza sozinha a cada 15s e sem isso ela fecha os detalhes e
      // rouba o foco no meio de um clique.
      const abertos = new Set(detalhesAbertos);
      const foco = document.activeElement?.closest('[data-sync], [data-detalhe], [data-conectar]');
      const focoItem = foco?.closest('[data-item]')?.dataset.item;
      const focoTipo = foco?.hasAttribute('data-detalhe') ? '[data-detalhe]'
        : foco?.hasAttribute('data-sync') ? '[data-sync]' : '[data-conectar]';

      el('lista').innerHTML = cs.length
        ? [...cs].sort((a, b) => Number(precisa(b)) - Number(precisa(a)) || nome(a).localeCompare(nome(b)))
            .map(c => linhaInstituicao(c, abertos)).join('')
        : `<tr><td colspan="7"><div class="cx-vazio"><h3>Seu financeiro começa pelas conexões</h3>
             <p>Conecte sua primeira instituição para reunir contas e cartões e acompanhar as próximas importações.</p>
             <button class="primario" data-conectar="">+ Conectar instituição</button></div></td></tr>`;
      assinaturaLista = listaNova;
      if (focoItem) {
        [...document.querySelectorAll('tr[data-item]')]
          .find(c => c.dataset.item === focoItem)?.querySelector(focoTipo)?.focus();
      }
    }

    const hist = st.historico || [];
    const assinatura = JSON.stringify([hist, cs.map(c => [c.id, nome(c)])]);
    if (assinatura !== assinaturaHistorico) {
      el('historico').innerHTML = hist.length ? rodadas(hist, cs)
        : '<p class="sub">As próximas atualizações aparecerão aqui. O histórico anterior por instituição não está disponível.</p>';
      assinaturaHistorico = assinatura;
    }

    el('lista').setAttribute('aria-busy', 'false');
    botoes();
  }

  async function carregar() {
    if (carregando) return;
    carregando = true;
    try {
      render(await pedir('/pluggy-status'));
      if (el('aviso').dataset.rede) { avisar(''); delete el('aviso').dataset.rede; }
    } catch (e) {
      avisar(`Não foi possível consultar as conexões. ${e.message}`, 'erro');
      el('aviso').dataset.rede = '1';
      el('estado').textContent = 'Sem comunicação com o servidor. Tentando novamente…';
      el('lista').setAttribute('aria-busy', 'false');
      if (!atual) el('lista').innerHTML = '<div class="cx-vazio">A lista estará disponível quando o servidor responder.</div>';
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
      await carregar();
    } catch (e) {
      avisar(`Não foi possível iniciar a atualização: ${e.message}`, 'erro');
    } finally {
      ocupado = false; botoes();
    }
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
        onSuccess: async dados => {
          concluida = true;
          try {
            const item = dados?.item || dados;
            if (!item?.id && !item?.itemId) throw new Error('A instituição não devolveu o identificador da conexão.');
            if (itemId && (item.id || item.itemId) !== itemId) {
              throw new Error('O identificador recebido não corresponde à conexão em renovação.');
            }
            const r = await pedir('/pluggy-items', 'POST', { item });
            avisar(r.sincronizando
              ? 'Acesso registrado. Importando os dados da instituição…'
              : 'Acesso registrado. Aguarde a rodada atual terminar e clique em Atualizar nesta instituição.', 'ok');
            await carregar();
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

  el('btnSync').addEventListener('click', () => sincronizar());
  el('btnConectar').addEventListener('click', () => conectar());
  el('lista').addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.hasAttribute('data-sync')) sincronizar(b.dataset.sync);
    if (b.hasAttribute('data-conectar')) conectar(b.dataset.conectar);
    if (b.hasAttribute('data-detalhe')) {
      const id = b.dataset.detalhe;
      detalhesAbertos.has(id) ? detalhesAbertos.delete(id) : detalhesAbertos.add(id);
      assinaturaLista = '';           // força o redesenho da lista
      if (atual) render(atual);
      [...document.querySelectorAll('tr[data-item]')]
        .find(t => t.dataset.item === id)?.querySelector('[data-detalhe]')?.focus();
    }
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) carregar(); else clearTimeout(timer);
  });
  window.addEventListener('beforeunload', () => clearTimeout(timer));

  montarNav('conexoes_pluggy.html');
  observarIdentidadeCartoes(carregar);
  carregar();
})();
