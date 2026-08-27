# Financeiro Pluggy (Open Finance)

Projeto **automatizado**: puxa os dados bancários via Open Finance (Pluggy) e os
apresenta em Extrato, Cartões e Conexões.

É independente do `ExtratorVisor`, que é o dashboard de **entrada manual**.
Bancos separados, backends separados, portas separadas — os dois rodam ao mesmo
tempo sem se enxergar.

| | Este projeto | ExtratorVisor |
|---|---|---|
| Natureza | automatizado (Pluggy) | entrada manual |
| Banco | `pluggy.db` | `financeiro.db` |
| Porta | 8766 | 8765 |
| Subir | `iniciar_pluggy.bat` | `iniciar_dashboard.bat` |

## Como abrir

Dê dois cliques em:

```text
iniciar_pluggy.bat
```

Ele sobe o backend e abre:

```text
http://127.0.0.1:8766/transacoes.html
```

## Páginas

- `transacoes.html`: lançamentos com filtros, troca de categoria e exclusão dos cálculos.
- `contas_fixas.html`: contas recorrentes casadas com as transações reais.
- `categorias.html`: gastos por categoria pai e subcategoria.
- `cartoes_pluggy.html`: grade de faturas por cartão e mês.
- `emprestimos.html`: dinheiro emprestado, recebido de volta e empréstimos pegos.
- `extrato_regras.html`: gerenciador das regras de categorização.
- `conexoes_pluggy.html`: conectar bancos e acompanhar o estado das conexões.

O visual é compartilhado: `tema.css` tem os tokens e componentes, `app.js` tem o
popover, a navegação e os helpers. Antes cada página repetia ~200 linhas de CSS
quase iguais; mudar um token agora muda o projeto inteiro.

### Interações que valem conhecer

- **Trocar categoria**: clique no chip da categoria na linha. Abre uma caixinha
  com busca; buscar por uma subcategoria também mostra o pai, senão o filho
  apareceria solto.
- **Ignorar transação**: menu `⋯` no fim da linha. As duas automações desse
  menu levam para a tela de Regras já preenchida com a descrição — a regra vale
  para o histórico inteiro, então é melhor revisá-la antes de salvar.
- **Filtros**: botão que abre a janela; os filtros aplicados viram etiquetas
  removíveis ao lado.
- **Categorias**: clicar numa linha abre as transações daquela categoria, com o
  mês preservado. O lápis (aparece no hover) edita emoji, nome, cor e pai.
- **Criar categoria**: pelo botão no rodapé da caixinha de categoria — se você
  tinha buscado algo, o nome já vem preenchido, e a categoria criada é aplicada
  na transação na hora. Também dá para criar pela tela de Categorias.
- Clicar de novo no mesmo botão fecha o popover; `Esc` e clique fora também.

### Entradas projetadas quando o mês ainda não fechou

Duas fontes de renda, cada uma paga em duas parcelas por mês — pagamento do
mês e adiantamento do mês seguinte — somam 4 créditos esperados por mês
(`ENTRADAS_ESPERADAS_POR_MES` em `pluggy_extrato.py`). No mês corrente ou num
mês futuro que já tem alguma coisa lançada (fatura de cartão fechada, por
exemplo), enquanto os 4 ainda não aconteceram o valor recebido até agora é
parcial — mostrar Resultado com base nele dava um número artificialmente
negativo, quando na prática só falta a renda entrar.

Nesse caso as telas usam a **média das entradas dos meses já completos do ano
corrente** no lugar do valor parcial, e avisam: a tira de meses ganha borda
tracejada e `≈` no valor, com tooltip mostrando quanto já entrou de fato; o
resumo de Transações e o card "Saldo do mês" de Contas Fixas mostram a mesma
nota. Nada é escondido — o valor recebido de verdade sempre aparece ao lado.

Um mês só entra na média se teve as 4 entradas completas. A projeção não se
aplica com filtro de conta, cartão, categoria, status, tipo ou busca ativo —
sob um filtro estreito a fatia de entradas pode nem existir ali, e tirar média
dela só inventaria número.

### Mês do cartão é o mês em que a fatura vence

O mês de um gasto de cartão é sempre o mês em que a **fatura** vence, não a
data da compra: uma compra de agosto entra na fatura que vence em setembro, e
é assim que ela chega para ser pague. Sem isto, filtrar por setembro mostrava
vazio mesmo com fatura de setembro fechada — a compra continuava presa em
agosto, mês em que foi feita.

Conta corrente e poupança não têm essa ambiguidade: a competência cai na
própria data.

A regra vive numa coluna da view (`competencia_fatura`), não repetida em cada
consulta: ciclo fechado se reconhece pelo `billId` e vence no mês seguinte à
última compra dele; a fatura aberta ainda não tem `billId` e agrupa os PENDING.
Pagamento de fatura fica fora — ele quita a fatura, não é gasto dela.

Fatura é o padrão em toda tela que filtra por mês. A tela de Transações tem um
seletor **Fatura / Mês** para ver pela data da compra quando a pergunta é
"quanto gastei em agosto?" em vez de "quanto vou pagar em agosto?". No modo Mês
não há projeção de parcela futura: uma parcela que o banco ainda não cobrou não
tem data de compra futura para exibir.

### O valor da fatura fechada vem do banco, não da nossa soma

`GET /bills` devolve o `totalAmount` que o banco emitiu, e o `id` da fatura é o
mesmo `billId` que já vem em cada transação — casa 1:1, sem heurística. Isso é
gravado em `pluggy_faturas` e aplicado por último em `cartoes_payload()`
(`_aplicar_faturas_oficiais`), como autoridade final de todo mês fechado.

Somar transação por transação acerta quando a base está completa (conferido:
bate ao centavo nas 16 faturas de 2026 do Inter e do Nubank), mas erra nas
bordas — no primeiro mês importado faltam as compras anteriores ao início do
sync, e nos cartões Itaú o conector não trouxe o histórico de compras. Nesses
casos a fatura oficial é a única fonte correta.

Duas limitações que mantêm o cálculo próprio vivo:

- **A fatura em aberto não existe na API.** Ela só passa a existir quando o
  banco fecha o ciclo e atribui um `billId` (as transações do ciclo corrente
  têm `billId = null`). Não há filtro nem endpoint que a entregue — conferido
  contra a API, não só na documentação. O mês corrente e as parcelas futuras
  seguem estimados por `_completar_faturas_abertas_e_parcelas` e pelos ajustes
  por banco.
- **Alguns ciclos do Itaú vêm com `totalAmount` zerado** mesmo tendo tido
  gasto. Zero é ambíguo ("não coletei" x "não gastou nada"), então é ignorado e
  ali o cálculo por pagamento continua valendo. Valor negativo, ao contrário, é
  dado real (fatura com saldo credor) e entra normalmente.

## Contas fixas

Mesmo princípio do dashboard manual, com uma diferença: aqui a conta pode ser
**casada com uma transação real**, e é isso que decide se foi paga — em vez de
um checkbox que alguém precisa lembrar de marcar.

O vínculo é escrito à mão, no mesmo esquema das regras da tela de extrato:
**a descrição da transação CONTÉM este termo** (ex.: `CLARO`). A cada mês o
sistema procura, entre as **saídas daquele mês que entram nos cálculos**, uma
transação cuja descrição contenha o termo. Estorno e transferência própria
nunca entram: não são pagamento de conta.

O modal de cadastro mostra ao vivo o que o termo pega no mês — uma regra escrita
às cegas é uma regra que ninguém confere.

Uma transação casa com no máximo uma conta, e vice-versa. Quando o termo pega
mais de uma, vence a de valor mais próximo do previsto. Sem isso, duas contas de
valor parecido apontariam para o mesmo lançamento e o mês contaria pago duas
vezes.

**Não há detecção automática de contas fixas.** Existiu uma, que pontuava
recorrência no extrato, e foi removida: ela acertava o grosso mas trazia
parcelamento morto junto e deixava conta viva de fora, e conferir a lista dava
mais trabalho que cadastrar as contas na mão.

### Colunas

As mesmas do Dashboard Financeiro manual, e **só** elas: item, tag
(`Contas Pessoais` / `Contas Empresa`), valor bruto, descontos, valor líquido,
pago e pagamento. O bruto e os subdescontos se editam direto na linha.

Nada de contexto na tabela. Regra, vínculo, dados da transação, vencimento,
categoria e vigência ficam no painel do botão `i` — a linha existe para ser
varrida de relance, e cada informação a mais nela custa isso.

O triângulo à esquerda abre e fecha o cartão de descontos, como no dashboard.

A **forma de pagamento** é consolidada em PIX, Itaú, Nubank ou Inter. Quando a
conta está vinculada a uma transação, ela vem da conta daquela transação: é o
extrato dizendo por onde o dinheiro saiu, em vez de alguém declarar. O rótulo
`Automático (…)` mostra qual forma foi herdada, e escolher outra no select
sobrescreve só naquele mês.

### Subdescontos

Cada conta abre uma lista de subdescontos do mês, e cada um tem a sua própria
forma de pagamento — inclusive `Reembolso`, disponível apenas aqui e que tira o valor do total de
gastos. Rateio pago no cartão de outra pessoa continua sendo gasto seu; só muda
por onde saiu.

### Precedência

Segue a mesma lógica da camada de extrato:

```text
transação:  vínculo manual  >  regra CONTÉM        >  nenhuma
valor:      valor do mês    >  valor da transação  >  valor previsto
pago:       decisão manual  >  achou transação     >  pendente
forma:      escolha do mês  >  conta da transação  >  padrão do cadastro
```

Clicar no selo de status alterna entre forçar o contrário e voltar ao
automático. O rótulo diz de onde veio a informação (“pelo extrato” ou
“manual”), então nunca fica dúvida sobre o que está mandando.

### Regras de cálculo (portadas do dashboard manual)

```text
líquido = max(0, valor − todos os descontos)
gasto   = líquido + os descontos que não são reembolso
```

Um subdesconto que não é reembolso continua sendo gasto seu: ele só muda a forma
de pagamento. Reembolso de terceiros é o único que sai do total.

O gasto é somado a partir do líquido em vez de calculado como
`valor − reembolsos`. As duas contas dão o mesmo resultado, menos quando os
descontos passam do valor — aí só esta trava em zero, como o dashboard manual.

### Tabelas

| Tabela | Papel |
|---|---|
| `fixas_contas` | cadastro, independente de mês |
| `fixas_mes` | o que é específico de um mês (valor, override de pago, vínculo) |
| `fixas_descontos` | subdescontos daquele mês, cada um com sua forma de pagamento |

Excluir uma conta leva o histórico mensal junto (cascade) — diferente das
categorias, aqui isso é o desejado: o histórico não faz sentido sem a conta.

## Desempenho ao trocar de tela

Uma troca de tela custava ~730 ms. Hoje custa ~95 ms, e a maior parte disso
some atrás do cache.

O gargalo não era o banco: era **contar quantas transações cada categoria tem**
para mostrar um número no seletor de filtros. Contar por categoria *efetiva*
obriga a varrer a view `extrato_efetivo` inteira, e a view avalia as regras com
`norm()` — uma função Python chamada pelo SQLite — linha a linha. Eram 535 ms
dos 627 ms daquela chamada. O número saiu.

O resto veio de parar de refazer trabalho: `garantir_camada()` e
`garantir_tabelas()` rodavam a cada requisição, recriando a view e resemeando
35 categorias e 74 traduções. Agora rodam **uma vez por processo** — o schema
não muda entre duas requisições.

### O cache do navegador

`app.js` guarda em `sessionStorage` as respostas que mudam pouco
(`/extrato/filtros` e `/extrato/categorias`) e usa *stale-while-revalidate*:
pinta na hora com o que tem, refaz a chamada em segundo plano e repinta só se o
conteúdo mudou.

- **Invalidação**: qualquer escrita (POST/PUT/DELETE) limpa o cache inteiro. É
  grosso de propósito — cache errado mostraria número desatualizado, que é pior
  que a espera que ele evita.
- **`sessionStorage`, não `localStorage`**: expira ao fechar a aba, então nunca
  sobrevive a uma reimportação feita com o app fechado.
- Extrato e resumo de categorias **não** entram no cache: são o dado que você
  está olhando, e precisam estar sempre frescos.

`tema.css` e `app.js` passaram de `no-store` para `no-cache` — eram rebaixados
a cada navegação. Com `no-cache` o navegador revalida e recebe `304`, então
editar o tema continua aparecendo no próximo carregamento.

## Atualização automática

Subir o backend já dispara a busca de dados novos, **em segundo plano**: a tela
abre na hora com os últimos dados importados e nunca espera a rede. Se a Pluggy
estiver fora do ar ou o consentimento tiver vencido, o motivo aparece em
vermelho na página de Extrato.

```powershell
python atualizar_pluggy.py            # atualiza se já passou do intervalo
python atualizar_pluggy.py --forcar   # atualiza agora
python atualizar_pluggy.py --status   # estado da última tentativa
```

Para subir sem atualizar: `python backend_pluggy.py --open --sem-sync`.

**Intervalo mínimo de 6h** (`PLUGGY_INTERVALO_HORAS`): os itens do Meu Pluggy
são atualizados uma vez por dia e não dá para forçar pela API, então
sincronizar a cada abertura gastaria chamada sem trazer nada novo.

## Estrutura

| Arquivo | Papel |
|---|---|
| `banco.py` | conexão, backup e criação do `pluggy.db` |
| `pluggy_sync.py` | fala com a API da Pluggy e baixa extratos e faturas para `./data` |
| `atualizar_pluggy.py` | orquestra sync + import, guarda o estado |
| `importar_pluggy.py` | grava os JSON do extrator nas tabelas `pluggy_*` |
| `pluggy_extrato.py` | leituras do extrato e dos cartões |
| `extrato_camada.py` | camada local: categorias, regras e ajustes manuais |
| `pluggy_conexoes.py` | connect token para o widget |
| `emprestimos.py` | dinheiro emprestado (dado manual) + importador do dashboard antigo |
| `backend_pluggy.py` | servidor HTTP local |

## Dinheiro emprestado

Dado 100% manual — nada vem da Pluggy. Um Pix para o mecânico é
indistinguível de qualquer outro Pix no extrato, então quem sabe que aquilo era
empréstimo é a pessoa, não o banco.

Três tipos na mesma tabela `emprestimos`, separados pela coluna `tipo`: o que
saiu (`emprestado`), o que voltou (`pagamento`) e o que peguei
(`emprestimo_pego`). Ficam juntos porque formam um saldo só — "quanto ainda
tenho para receber" — e a tela mostra os três lado a lado.

Pagamento **não** aponta para qual empréstimo quitou: é assim no dado de
origem, e o total recebido é global, não por item. Amarrar os dois mudaria o
significado do número que já existe.

Nos empréstimos pegos, o número de parcelas não é cadastrado — sai dos meses
entre início e fim, contando as duas pontas (setembro a março é 7x). Quando
falta uma das datas a tela não mostra parcela nenhuma, em vez de chutar. E
`status = finalizado` vale mais que a conta pelas datas, para um empréstimo
liquidado antes do prazo não ficar com parcela fantasma em aberto.

Para trazer os dados do dashboard manual (`ExtratorVisor/financeiro.db`):

```bash
python emprestimos.py importar ../ExtratorVisor/financeiro.db
```

Mostra o que faria sem gravar; `--aplicar` grava. É idempotente — o id de lá
vira o id daqui, então rodar de novo atualiza em vez de duplicar. Registro sem
descrição e sem valor é descartado (linha criada por engano e nunca preenchida).

## Camada local sobre as transações

A transação importada **nunca é alterada**. Sobre ela existe uma camada em
tabelas próprias (`extrato_*`), então uma nova sincronização não apaga
categorias, regras nem exclusões manuais.

**Categoria efetiva**, em ordem de precedência:

```text
edição manual > regra do usuário > tradução da categoria Pluggy > Outros
```

**Participação nos cálculos**:

```text
decisão manual > exclusão automática por regra > incluída
```

`calculo_override` tem três estados: `NULL` (automático), `0` (fora dos
cálculos), `1` (forçar inclusão). Por isso uma transferência entre contas
próprias sai dos totais sozinha, mas pode ser reincluída na mão.

### Regras de sistema

Três regras vêm de fábrica e tiram dos cálculos o que não é gasto:
transferência entre contas próprias, resgate de cofrinho e pagamento de fatura.

Elas **não aparecem na lista da tela de Regras e não podem ser editadas nem
excluídas** — são infraestrutura de cálculo, não preferência. Mexer no termo de
uma delas reintroduz transferência própria ou pagamento de fatura nos totais, e
o estrago aparece longe da causa: uma categoria com total negativo, um mês que
dobra de tamanho. A tela cita quantas são e o que fazem, para os números
continuarem explicáveis.

A comparação ignora maiúsculas, minúsculas e acentos.

### Hierarquia das categorias

São 16 categorias pai com subcategorias (Alimentação → Supermercado,
Restaurantes, Delivery; Automotivo → Postos, Manutenção; e assim por diante). O
total de um pai soma os filhos **mais** o que caiu direto nele: uma transação
pode ficar em "Transporte" sem passar por nenhuma subcategoria.

Os ids das categorias nunca mudam — `extrato_ajustes.categoria_id_manual`
aponta para eles, e renomear um id apagaria edição manual sua. A hierarquia foi
montada por cima dos ids que já existiam.

São dois níveis, não mais: uma subcategoria não pode receber filhas. Com três
níveis o total do pai na tela de Categorias ficaria ambíguo.

### Personalização e o flag `personalizada`

Emoji, nome, cor e pai de qualquer categoria são editáveis, inclusive das
categorias padrão. Há uma armadilha aqui: o seed roda a **cada requisição** e
atualiza as categorias padrão, então sem proteção ele desfaria sua edição na
chamada seguinte.

Por isso existe a coluna `personalizada`. Editar uma categoria marca `1`, e o
seed só atualiza linhas com `0` — assim eu ainda posso corrigir uma categoria
padrão que você nunca tocou, sem passar por cima do que você mudou.

Excluir só é permitido para categoria sem uso nenhum (sem subcategorias, sem
transações editadas à mão, sem regras, sem tradução da Pluggy apontando para
ela). Apagar em cascata levaria junto exatamente o trabalho manual que esta
camada existe para proteger. Categorias padrão não são excluíveis — voltariam
no próximo seed.

### Cores das categorias

As 8 categorias de despesa mais frequentes usam a paleta categórica validada
para superfície escura (sem alteração de hex). Da nona em diante são passos das
mesmas famílias de cor: estáveis e persistentes, mas **sem garantia de
separação para daltonismo** — por isso o chip sempre traz o nome da categoria
junto da cor, e a identidade nunca depende só da cor.

## De onde vêm os dados

Tudo acontece dentro deste projeto. O `pluggy_sync.py` fala com a API da Pluggy
e grava JSON/CSV em `./data`; o `atualizar_pluggy.py` chama esse script e
importa os arquivos para o `pluggy.db`.

Os caminhos são relativos ao próprio arquivo, então a pasta pode ser movida ou
clonada sem editar código.

### Credenciais

Ficam no `.env` deste projeto (veja `.env.example`):

```dotenv
PLUGGY_CLIENT_ID=seu_client_id
PLUGGY_CLIENT_SECRET=seu_client_secret
```

O segredo só é usado no backend, para gerar tokens curtos do Pluggy Connect —
nunca é devolvido à página.

### Comandos do sincronizador

```powershell
python pluggy_sync.py check              # valida credenciais e lista conectores
python pluggy_sync.py config             # config da aplicação e allowlist de conectores
python pluggy_sync.py item <ITEM_ID>     # sonda uma conexão: status, contas, transações
python pluggy_sync.py sync <ITEM_ID>     # baixa contas e transações para ./data
python pluggy_sync.py connect --meupluggy --serve   # widget para autorizar um banco
```

## Backups

Antes de cada importação o banco é copiado para `backups/`. Os arquivos contêm
extratos bancários pessoais e estão no `.gitignore`.

> **Não apague linhas de `pluggy_itens`.** As chaves estrangeiras são
> `ON DELETE CASCADE`: remover um item apaga junto todas as suas contas e
> transações. Uma conexão antiga e quebrada é inofensiva — a atualização apenas
> a reporta e segue com as demais.
