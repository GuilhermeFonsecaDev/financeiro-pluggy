# Planejamento: cartões genéricos, tags e consolidação

Data: 04/09/2026. Planejamento executado e implementação concluída.

Este arquivo não traz valores nem o inventário real de cartões: o repositório é
público. Os exemplos usam identidades ilustrativas.

## 1. Comportamento definido

- Um cartão é um instrumento de crédito, separado de conta corrente e poupança.
- Uma rotina comum atende qualquer cartão, sem condições por banco, nome comercial ou final.
- Sem tag, mostrar a identidade do cartão: bandeira + quatro últimos dígitos. Exemplo: MASTERCARD 8113.
- Cada cartão pode ter uma tag opcional de exibição e agrupamento.
- Com tag INTER, MASTERCARD 8113 aparece como INTER em todas as telas.
- Cartões com a mesma tag são consolidados em todas as visões de resumo, filtros, gráficos e colunas.
- A conta bancária Inter continua sendo uma conta bancária, mesmo que exista uma tag INTER para cartões.
- Sem tag, dois cartões permanecem separados, inclusive quando pertencem à mesma conexão.
- Atribuir uma tag não cria uma fatura compartilhada nem altera compras, parcelas, pagamentos ou ciclos.
- As transações continuam individuais e rastreáveis. Na gestão e nos detalhes, a identidade original fica disponível para distinguir membros de uma tag.
- Novas conexões entram automaticamente no catálogo, com cartões individuais e sem tag predefinida.

## 2. Diagnóstico confirmado

- `cartoes.py` ainda sugere NUBANK para GOLD e INTER para PLATINUM PRIME DUO; grupo vazio usa a conexão e agrupa implicitamente.
- `pluggy_extrato.py` mantém outro sistema de rótulos e filtros de bancos, sem aplicar a identidade dos cartões em todos os caminhos.
- `fixas.py` restringe formas de pagamento a PIX/Itaú/Nubank/Inter; cartões desconhecidos podem virar PIX ou perder projeções.
- `contas_fixas.html` tem colunas e buscas dedicadas aos bancos conhecidos. `visao_geral.html` também deriva cores por nome de banco.
- O cálculo atual pode substituir um grupo inteiro pelas faturas oficiais de apenas alguns membros. A consolidação deve ocorrer depois do cálculo individual.
- A detecção de reconexão usa subtipo + número sem contexto suficiente e pode ocultar cartões distintos com mesmo final.
- IDs de grupo derivados do texto mudam ao renomear e podem colidir.
- A importação já preserva tipo CREDIT/subtipo CREDIT_CARD, bandeira, final e metadados de crédito: há dados suficientes para os nomes naturais.

Na base atual há quatro cartões, de três conexões, dois deles com o mesmo final. Não há identificações manuais salvas. Existem referências antigas a inter/itau/nubank nas formas de pagamento das contas fixas e descontos.

## 3. Modelo de identidade e tags

### Cartão

Criar uma identidade interna estável de cartão, com vínculo explícito aos identificadores recebidos da Pluggy. Campos de domínio: id, fontes, bandeira, final, nome original, instituição de origem, moeda, status e tag opcional. Os nomes recebidos da API são metadados, não decisões de agrupamento.

Manter `pluggy_contas` como armazenamento de origem compatível com a API, expondo no domínio e nos contratos coleções distintas de contas bancárias e cartões. Não renomear chaves estrangeiras de todo o histórico apenas porque o provedor chama seus recursos de accounts.

Se bandeira/final estiverem incompletos, usar nome informado e os dados disponíveis, sem inferir banco nem fundir cartões. Nome visível não é chave de identidade. Cartões distintos podem ter o mesmo final.

### Tag

Persistir tags com ID estável e nome editável. Normalizar espaços, maiúsculas/minúsculas e acentos para reconhecer grafias equivalentes; conservar uma grafia de exibição. Reutilizar tags existentes pelo seletor da tela Cartões.

- Tag vazia: cartão individual, sem sentinelas como só:ID e sem agrupamento pela conexão.
- Criar/atribuir: muda a apresentação e participação no grupo.
- Remover: cartão volta ao nome natural e à coluna própria.
- Renomear: atualiza todas as telas mantendo o ID da tag.
- Renomear para tag já existente: tratar explicitamente a união dos membros.
- Alterar tag individual não altera a identidade nem a origem de suas transações.

Uma fonte central deve fornecer nome de exibição, tipo do instrumento, ID do cartão, ID da tag, membros do grupo, cor e ordem. Nenhuma tela refaz inferências de banco.

## 4. Cálculo e consolidação

Sequência obrigatória:

1. Identificar cartões e fontes válidas, sem duplicação por reconexão.
2. Calcular compras, estornos, faturas oficiais, abertas e parcelas futuras de cada cartão.
3. Aplicar competência e vencimento individuais, reaproveitando o motor genérico de `ciclos.py`.
4. Consolidar os resultados por tag ou, sem tag, por cartão.
5. Entregar os mesmos grupos e totais a todas as telas.

Grupo com cartão fechado e cartão aberto preserva ambos os valores e informa origem mista. Tag não transforma pagamento de fatura em nova despesa nem muda a contabilização das compras.

Deduplicar uma mesma fatura somente com evidência da origem. Mesmo banco, mesma tag, mesmo valor ou mesmo final não comprovam duplicação. Limites compartilhados também não podem ser somados cegamente: apresentar por membro quando não houver evidência suficiente para um único limite. Quando vencimentos diferirem, mostrar múltiplos vencimentos e disponibilizar o detalhe.

Filtros por tag devem resolver seus membros antes de buscar dados, incluindo movimentos reais e projetados. Cartão original continua disponível para inspeção e edição.

## 5. Telas e contratos afetados

| Área | Mudança |
|---|---|
| Cartões | Trocar Apelido/Grupo pela identidade original e campo Tag opcional, com seleção de tag existente, criação, remoção e visualização dos membros. |
| Grade de faturas | Uma coluna por cartão sem tag ou por tag; cabeçalhos, totais e detalhes gerados pelo catálogo. |
| Visão Geral | Cards, gráficos, nomes, cores e links usam o mesmo agrupamento central. |
| Transações | Filtro Conta lista contas bancárias; filtro Cartão lista cartões/tags. Linhas mostram nome efetivo; detalhes preservam cartão original. |
| Contas Fixas | Formas de pagamento, projeções, totais, descontos e colunas dinâmicos, com referências a instrumentos/tags e sem listas de bancos. |
| Categorias | Adicionar filtro Cartão/tag e propagá-lo à consulta e aos links para Transações; separar cartões das contas não pode eliminar a filtragem de gastos de cartão. |
| Regras, Entradas e Empréstimos | Revisar seletores, nomes e links compartilhados para não reintroduzir o nome bancário no lugar do cartão. |
| Conexões | Instituição representa a origem da conexão; contas e cartões aparecem como tipos distintos quando houver detalhamento. |
| API e cache | Salvar tag invalida os dados de apresentação, sinaliza a mudança entre abas e revalida ao retornar à tela; sessionStorage isolado por aba não basta. |

A tag dos cartões não deve se misturar às tags de classificação já usadas em Contas Fixas.

## 6. Layout responsivo

- Gerar a quantidade de colunas a partir dos grupos retornados, sem referências fixas a três bancos.
- Distribuir o espaço disponível quando houver poucos cartões.
- Usar largura mínima legível e rolagem horizontal na própria tabela quando houver muitos; manter mês e cabeçalho acessíveis.
- Cards de resumo usam grade que se reorganiza automaticamente por largura.
- Em Contas Fixas, remover a grade de quatro colunas fixas; em Cartões, manter os quatro indicadores quando representam métricas, pois não são quatro cartões.
- Corrigir o contêiner da grade Cartões: hoje table-wrap não corresponde à classe tabela-wrap definida no CSS.
- Conferir 0, 1, 2, 4, 8 e 12 grupos, nomes longos e larguras de celular, notebook e monitor amplo.

## 7. Migração e preservação

Fazer backup antes de aplicar a migração e validar sobre cópia do SQLite. Migrações devem ser versionadas e idempotentes.

- Preservar transações, faturas, ajustes, parcelas, descontos e vínculos de contas fixas.
- Criar identidades estáveis a partir das fontes existentes sem transformar heurísticas antigas em tags escolhidas pelo usuário.
- Migrar eventual configuração manual comprovada, distinguindo-a de defaults e sentinelas do sistema antigo.
- Nas formas legadas inter/itau/nubank, resolver o cartão apenas quando o vínculo existente permitir identificação inequívoca, por exemplo por transação associada.
- Referências ambíguas permanecem registradas e são apresentadas para associação; não escolher arbitrariamente um dos cartões nem converter para PIX.
- Não atribuir automaticamente a tag Itaú aos dois cartões atuais: agrupamento passa a depender da escolha explícita do usuário.
- Em reconexões, manter tags somente com vínculo inequívoco entre fonte nova e identidade existente; casos ambíguos não são unidos silenciosamente.

## 8. Ordem de implementação

1. Registrar os totais individuais e agregados atuais como base de comparação; inventariar referências legadas.
2. Implementar identidade estável, tags e catálogo central, com migração.
3. Corrigir a sequência de cálculo individual e consolidação genérica.
4. Atualizar API de identificação, filtros e contratos de cartão/conta.
5. Implementar a gestão de tags e grade responsiva em Cartões.
6. Integrar Transações e Visão Geral.
7. Migrar formas, referências e colunas em Contas Fixas e revisar demais consumidores.
8. Remover caminhos de runtime que dependam de nomes de bancos ou de cartões específicos; compatibilidade legada fica restrita à migração.
9. Validar comportamento, cálculos e layout; conferir a versão efetivamente servida pelo backend antes de considerar a entrega aplicada.

Arquivos centrais: `cartoes.py`, `pluggy_extrato.py`, `fixas.py`, `backend_pluggy.py`, `visao_geral.py`, `cartoes_pluggy.html`, `transacoes.html`, `contas_fixas.html`, `visao_geral.html`, `app.js` e `tema.css`. Importador, sincronização, ciclos e camada de ajustes só mudam onde forem necessários para os contratos e vínculos, preservando a origem.

## 9. Critérios de aceite

- MASTERCARD 8113 sem tag aparece com esse nome em todas as visões de cartão.
- Atribuir INTER altera as visões de cartão, sem renomear ou agrupar a conta bancária Inter.
- Dois cartões com tag Itaú aparecem em um grupo; o total é a soma correta dos resultados individuais.
- Remover/renomear tag atualiza nomes, filtros, links, cores e agrupamento sem alterar registros financeiros.
- Cartão novo de banco desconhecido aparece automaticamente, inclusive projeções e formas de pagamento.
- Dois cartões na mesma conexão sem tags continuam separados.
- Dois cartões com mesmo final em instituições distintas não desaparecem.
- Reconexão inequívoca preserva identidade/tag sem duplicar dados.
- Uma fatura oficial e outra aberta no mesmo grupo não perdem valores.
- Limite compartilhado não é duplicado por causa da tag; vencimentos distintos são preservados.
- Selecionar tag no extrato inclui movimentos reais e projetados de todos os membros.
- Categorias também permite selecionar cartão/tag; a edição de uma transação continua escolhendo o cartão individual, nunca um grupo como origem.
- A soma financeira permanece invariável ao atribuir/remover tags; diferenças decorrentes de defeitos antigos corrigidos são explicitamente demonstradas.
- Formas antigas de Contas Fixas não são perdidas ou transformadas silenciosamente em PIX.
- Layout funciona com os cenários de quantidade/largura definidos acima.
- Backend e página carregados servem a mesma versão; a entrega não se limita a arquivos salvos em disco.

## 10. Validação

Ampliar os testes de faturas para os cenários de grupo misto e conservação dos totais, evitando usar nome de exibição como chave de snapshot. Testar identidade, tags e migração com banco temporário, integração sobre cópia dos dados atuais e inspeção das telas em larguras diferentes. Não executar escritas ou sincronizações externas durante o planejamento.

## 11. Resultado da implementação

- Catálogo central separa contas bancárias, cartões físicos e grupos de tag por IDs estáveis.
- Tags são opcionais, editáveis e reutilizáveis; cartões sem tag usam bandeira e quatro últimos dígitos.
- Cálculos ocorrem primeiro por cartão físico e só depois são consolidados por tag.
- Cartões, Visão Geral, Transações, Categorias e Contas Fixas usam o mesmo catálogo e os mesmos grupos.
- Formas de pagamento e edições preservam a referência física, inclusive quando a apresentação está agrupada.
- Reconexões só reaproveitam identidade com histórico inequívoco e nunca unem cartões que coexistem na mesma conexão.
- Contas bancárias de uma mesma conexão e mesmo número permanecem distintas quando representam produtos diferentes.
- A grade foi validada com 0, 1, 2, 4, 8 e 12 grupos em 390, 1366 e 1920 pixels.
- Foram validadas 34 competências de fatura. A correção recuperou duas parcelas, em dezembro de 2025 e janeiro de 2026, de um dos cartões: lançamentos que já existiam e que a consolidação anterior descartava ao substituir o grupo inteiro pelas faturas oficiais de apenas alguns membros.
- O backend ativo serve a versão 2 do catálogo e expõe quatro cartões físicos; seis contas bancárias aparecem separadas.
- Investimentos contempla todas as conexões e expõe a aplicação identificada apenas no extrato como Aguardando posição, com o valor aplicado.
