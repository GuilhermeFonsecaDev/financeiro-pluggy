# Handoff — Integração Pluggy / Open Finance

> Documento de contexto para continuar este trabalho em outra sessão (ex: Claude Code).
> Última atualização: 2026-08-15.
>
> **Nota (2026-08-16):** este texto foi escrito quando o extrator vivia num
> projeto separado, `Fina2.0`. Ele foi unificado aqui — `pluggy_sync.py`, `.env`
> e `data/` agora ficam na raiz deste projeto, com caminhos relativos. As
> menções a `Fina2.0` abaixo são históricas: leia como "a raiz deste projeto".

---

## 1. Objetivo

Acessar **os dados bancários pessoais do próprio usuário** (Guilherme) via Open
Finance, usando a API da Pluggy, e salvá-los **localmente** em CSV/JSON.

Requisitos que orientaram as decisões:

- Uso **pessoal**, não corporativo. Custo precisa ser zero ou próximo disso.
- Os dados são financeiros e sensíveis: ficam **na máquina do usuário**, não
  são enviados para serviços de terceiros nem versionados.
- Entregável escolhido: **script Python simples** (não app web, não dashboard).

---

## 2. Ambiente

| Item | Valor |
|---|---|
| SO | Windows |
| Pasta do projeto | `C:\Users\guifo\OneDrive\Área de Trabalho\Fina2.0` |
| Python | 3.13 (Microsoft Store / WindowsApps) |
| Shell | PowerShell |
| Dependências | `requests`, `python-dotenv` (ver `requirements.txt`) |

**Atenção:** a pasta está dentro do OneDrive. Arquivos sincronizam
automaticamente — relevante se for versionar ou mover o projeto.

---

## 3. Estado atual dos arquivos

```
Fina2.0/
├── pluggy_sync.py     # script principal (CLI: check / inspect / connect / sync)
├── README.md          # documentação de uso
├── HANDOFF.md         # este arquivo
├── requirements.txt
├── .env               # credenciais REAIS (gitignored) -- NÃO commitar
├── .env.example       # template com placeholders
└── .gitignore         # cobre .env, .env.*, connect.html, data/
```

O script `pluggy_sync.py` está **funcional e testado** quanto a sintaxe, CLI e
geração de HTML. O bloqueio atual é de **acesso/permissão na conta Pluggy**,
não de código.

### Comandos disponíveis

```powershell
python pluggy_sync.py check                      # valida credenciais + lista conectores
python pluggy_sync.py config                     # config da app + allowlist de conectores
python pluggy_sync.py inspect [ID]               # metadados de um conector (diagnóstico)
python pluggy_sync.py connect --meupluggy --serve  # widget, conector 200, via localhost
python pluggy_sync.py connect --sandbox --serve    # widget, dados fictícios
python pluggy_sync.py connect --serve              # widget, lista completa
python pluggy_sync.py sync <ITEM_ID>               # baixa contas + transações
```

Flags extras do `connect`: `--connector ID`, `--types TIPO...`, `--filter-only`,
`--update-item ITEM_ID`, `--serve [PORTA]`.

---

## 4. Descobertas confirmadas (com evidência)

### 4.1 O dashboard da Pluggy é B2B e o trial expirou

- Trial de 14 dias, já **expirado** na conta.
- Após expirar, as conexões são pausadas até contratar plano. Dados
  preservados por 30 dias.
- Planos comerciais começam em **R$ 2.500/mês** (produto "Dados").
  Inviável para uso pessoal.

### 4.2 As credenciais continuam válidas mesmo pós-trial

`python pluggy_sync.py check` retornou:

```
1) Testando /auth com as credenciais do .env ...
   OK - apiKey gerada com sucesso (credenciais validas).
```

Ou seja, `POST /auth` funciona. O bloqueio não é de autenticação.

### 4.3 Sobrou exatamente 1 conector real: o 200 (MeuPluggy)

```
2) Listando conectores reais (Open Finance / bancos) ...
   1 conector(es) disponivel(is).
     - 200: MeuPluggy

2) Listando conectores sandbox (dados ficticios) ...
   14 conector(es) disponivel(is).
     - 200: MeuPluggy
     - 2: Pluggy Bank  [sandbox]
     - 8: Pluggy Bank Business  [sandbox]
     ... (variações do Pluggy Bank)
```

Nenhum banco direto (Itaú, Nubank, BB...) aparece mais. O conector 200 é o
"Conector 200" que a página de preços da Pluggy cita como **acesso pessoal
gratuito** via Meu Pluggy.

### 4.4 Arquitetura pretendida (conector 200)

Não são necessárias credenciais de API separadas do Meu Pluggy. O desenho é:

```
bancos reais
   │  (Open Finance — consentimento feito DENTRO do meu.pluggy.ai)
   ▼
conta gratuita em meu.pluggy.ai        ← agregação
   │  (conector 200, OAuth)
   ▼
este script (CLIENT_ID/SECRET do dashboard)
   ▼
./data/*.csv e *.json
```

Ordem importa: **os bancos precisam ser conectados primeiro no site do Meu
Pluggy**; o script só enxerga o que já estiver agregado lá.

### 4.5 Metadados do conector 200

`python pluggy_sync.py inspect 200`:

```json
{
  "id": 200,
  "name": "MeuPluggy",
  "institutionUrl": "https://meu.pluggy.ai/",
  "country": "BR",
  "type": "PERSONAL_BANK",
  "credentials": [],
  "hasMFA": false,
  "oauth": true,
  "health": { "status": "ONLINE", "stage": null },
  "products": ["ACCOUNTS","TRANSACTIONS","CREDIT_CARDS","INVESTMENTS",
               "INVESTMENTS_TRANSACTIONS","PAYMENT_DATA","IDENTITY","BROKERAGE_NOTE"],
  "isSandbox": false,
  "isOpenFinance": false,
  "supportsPaymentInitiation": false
}
```

Pontos relevantes: `oauth: true`, `credentials: []` (não pede usuário/senha no
widget — redireciona para o meu.pluggy.ai), `health: ONLINE`.

---

## 5. BLOQUEIO — RESOLVIDO (2026-08-15)

> **Status: destravado.** Causa raiz e correção em 5.4. O widget agora abre o
> formulário do MeuPluggy e segue para o OAuth. Falta apenas a autorização
> interativa e o `sync`.

Sintoma original: ao rodar `connect --meupluggy`, o widget PluggyConnect exibia

> **"O id do conector especificado 200 não está disponível ou não foi
> encontrado. Entre em contato com o suporte para revisar a configuração."**

**Causa:** a aplicação tem uma *allowlist de conectores* definida no servidor
(`connectorsFilters.ids`) que **não inclui o conector 200**. O widget aplica
essa allowlist no cliente; a API `/connectors` não a aplica. Daí a divergência
entre "a API lista o 200" e "o widget recusa o 200".

### 5.1 Evidência

`python pluggy_sync.py config` (comando novo, adicionado nesta sessão) retorna:

```
  environment        : DEMO
  isProductionEnabled: False
  companyName        : Demo
  itemsCreateLimit   : 100
  totalItems         : 0
  connectorsFilters  : 112 id(s) permitido(s)
    faixa: 601 a 882
    contem o MeuPluggy (200)? NAO
```

Fonte: `GET https://auth.pluggy.ai/connect/config`, autenticado com
`Authorization: Bearer <connectToken>` (não aceita `X-API-KEY`). É exatamente
a chamada que o widget faz ao carregar.

Os 112 ids liberados estão todos na faixa ≥ 601, que no bundle do Connect é a
faixa **Open Finance** (`id >= 600 → "open_finance"`). O conector 200 é um
conector direto, fora dessa faixa.

### 5.2 A lógica de filtro (extraída do bundle do Connect)

`https://connect.pluggy.ai/static/js/index.*.js`, seletor da lista:

```js
passaNaApp   = !allowlist.length || allowlist.includes(c.id)
passaNaProp  = !connectorIds     || connectorIds.includes(c.id)
passaSandbox = includeSandbox && c.isSandbox
exibe        = (passaNaApp && passaNaProp) || passaSandbox
```

Consequências:

- Conectores **sandbox furam a allowlist** (por isso o Pluggy Bank funciona).
- Conectores **reais fora da allowlist não têm escapatória**: nenhuma opção do
  widget (`selectedConnectorId`, `connectorIds`, `connectorTypes`,
  `includeSandbox`) contorna o filtro. Ele só muda no dashboard.
- A allowlist da app e a lista devolvida pela API **não se intersectam**: a API
  entrega `[200] + 13 sandbox`, e a allowlist só tem ids 601–882. Por isso o
  widget hoje só consegue exibir os conectores sandbox.

### 5.3 Hipóteses ELIMINADAS

| # | Hipótese | Por que caiu |
|---|---|---|
| 1 | Credenciais inválidas / app desativado | `/auth` retorna 200 e gera apiKey |
| 2 | Widget filtra por tipo de conector | `type` é `PERSONAL_BANK`, tipo padrão do widget |
| 3 | Conector fora do ar | `health.status = ONLINE` |
| 4 | Conector é sandbox e faltava `includeSandbox` | `isSandbox: false`; widget já usa `includeSandbox: true` |
| 5 | (A) Origin `file://` quebrando o OAuth | O erro é da allowlist, anterior ao OAuth; o widget carregado por `https://` dá o mesmo resultado |
| 6 | (B) `selectedConnectorId` rejeitado, conector existindo na lista | O conector é removido da lista **antes** da pré-seleção; `--filter-only` não muda nada |
| 7 | Escopo reduzido do `connectToken` | `connectToken` e `apiKey` enxergam `/connectors` e `/connectors/200` de forma idêntica |
| 8 | Trial expirado matou a conta | A conta está num plano **Free ativo**: `POST /items` responde `CREATE_ITEMS_API_FREE_DISABLED` — *"Free subscription can only create items through our Connect Widget"* |

### 5.4 A correção (aplicada em 2026-08-15)

Foi uma **configuração da aplicação**, não código. No dashboard:

`dashboard.pluggy.ai` → **Customização** → aba **Conectores** → **Pessoal** →
sub-aba **Conectores Diretos** → ligar **`(200) MeuPluggy`** → **Salvar**.

Isso bate com o passo 3 do README oficial do
[`pluggyai/meu-pluggy`](https://github.com/pluggyai/meu-pluggy): *"Customize
your application settings to include MeuPluggy in your connector list"*.

Detalhe importante do painel: *"Todos os conectores Open Finance (Regulado)
estão habilitados por padrão"* — é isso que produzia os 112 ids na faixa
601–882. Conectores **diretos** (o MeuPluggy é um) ficam fora desse padrão e
precisam ser ligados manualmente.

Resultado, via `python pluggy_sync.py config`:

```
  connectorsFilters  : 0 id(s) permitido(s)
    allowlist vazia -> filtro desligado, todo conector passa
    MeuPluggy (200) liberado no widget? SIM
```

Salvar com "Padrão para todos selecionados" ligado **esvaziou** a allowlist em
vez de listar ids — o que desliga o filtro por completo (`passaNaApp` vira
sempre verdadeiro). Efeito melhor que o pretendido.

Verificação independente: abrindo
`https://connect.pluggy.ai/?connect_token=<TOKEN>&selected_connector_id=200`,
o widget renderiza *"Verificação de Segurança — MeuPluggy"* com o botão
**Conectar**, sem nenhuma mensagem de erro, e o clique leva ao OAuth em
`my-pluggy.us.auth0.com`. Antes, essa mesma URL dava
`selected_connector_id_missing`.

Observação: `environment` continua `DEMO` e `isProductionEnabled` continua
`false`. Não impediu o conector 200 — mas é o que sustenta o rodapé
"Aplicação demo" e o limite de `itemsCreateLimit: 100`.

### 5.4-bis O BLOQUEIO REAL APARECEU DEPOIS (2026-08-15, ~1h após o sync)

O conector foi liberado, o OAuth funcionou, o `sync` rodou e trouxe 2 contas e
861 transações às 18:14. **Cerca de uma hora depois, os dados sumiram da API.**

Evidência (item `6a1a72ea-…`, conector 200):

| Chamada | Resultado |
|---|---|
| `GET /items/{id}` | 200 — `status: UPDATED`, `executionStatus: SUCCESS`, `error: null` |
| `GET /items` | **401 Unauthorized** — a credencial não pode listar itens |
| `GET /accounts?itemId=` | 200, `total: 0` |
| `GET /accounts/{id}` (ids conhecidos) | **404 `ACCOUNT_NOT_FOUND`** nas duas contas |
| `GET /transactions?accountId=` | 200, `total: 0` |
| `auth.pluggy.ai/connect/config` | `environment: DEMO`, `isProductionEnabled: false`, **`totalItems: 0`** |

O ponto decisivo é o `totalItems: 0` junto com o item ainda resolvendo por id:
a aplicação **não conta mais nenhum item como seu**. As contas não foram
apenas ocultadas da listagem — foram removidas (404 por id direto).

Interpretação: a aplicação DEMO consegue **criar** o item e fazer **uma**
leitura, mas não retém dados reais de Open Finance. Bate com a mensagem que já
estava no bundle do widget: *"Contas de teste só podem conectar conectores
sandbox (Pluggy Bank). Solicite acesso a dados reais para conectar contas
reais."* O bloqueio de plano, que a seção 5.5 previa, é este.

**Não há contorno no código.** `check` continua OK, o conector 200 continua
liberado, as credenciais continuam válidas — só não existe dado para buscar.

Teste que confirma (interativo): reconectar pelo widget, rodar `sync` na hora e
observar se os dados somem de novo em ~1h. Se sumirem, está confirmado.

**Os 861 registros já importados são a única cópia** e estão em
`ExtratorVisor/financeiro.db` (com backups em `ExtratorVisor/backups/`) e em
`Fina2.0/data/`. Não apagar.

### 5.5 Fallback, caso algo volte a travar

**Acesso nativo do Meu Pluggy.** O Meu Pluggy anuncia acesso próprio por **API
REST, MCP e CLI** (`https://meu.pluggy.ai/api-guide`, acessível logado), com
modelo de credencial independente do dashboard corporativo. Nesse caminho o
script troca só a camada de autenticação; `sync` e a serialização em CSV/JSON
continuam iguais.

Se o erro que aparecer for `trial_item_create_not_allowed` (*"Contas de teste
só podem conectar conectores sandbox (Pluggy Bank)"*), aí o bloqueio é de
plano, não de configuração — nenhuma mudança no dashboard resolve.

---

## 6. Validação independente do bloqueio

O caminho sandbox **não depende** de nada acima e serve para provar que
`connect` + `sync` funcionam de ponta a ponta:

```powershell
python pluggy_sync.py connect --sandbox --serve
# autorizar no widget com as credenciais de teste que ele mesmo indica
python pluggy_sync.py sync <ITEM_ID>
```

Se isso gerar arquivos em `./data/`, o pipeline está correto e o problema
restante é exclusivamente de **liberação de acesso**, não de código.

Vale fazer isso mesmo com a causa raiz já identificada: é o único trecho do
fluxo (`connect` → widget → `onSuccess` → `sync`) que ainda não rodou de ponta
a ponta. A allowlist não atrapalha aqui — conectores sandbox a contornam
(ver 5.2). O passo de autorização é interativo e precisa ser feito por você.

---

## 7. Referência da API Pluggy

Base: `https://api.pluggy.ai`

| Endpoint | Método | Uso |
|---|---|---|
| `/auth` | POST | `{clientId, clientSecret}` → `{apiKey}`, validade ~2h |
| `/connect_token` | POST | header `X-API-KEY` → `{accessToken}`, validade ~30min, escopo limitado |
| `/connectors` | GET | lista conectores; query `?sandbox=true` |
| `/connectors/{id}` | GET | metadados de um conector |
| `/items/{id}` | GET | status da conexão (`UPDATING`, `UPDATED`, `LOGIN_ERROR`...) |
| `/accounts?itemId=` | GET | contas do item |
| `/transactions?accountId=&from=&to=` | GET | transações (paginado: `page`, `pageSize`) |
| `/items` | POST | **bloqueado no plano Free** (`CREATE_ITEMS_API_FREE_DISABLED`) — itens só via widget |

Autenticação nas chamadas: header `X-API-KEY: <apiKey>`.

### Endpoint de configuração da aplicação (não documentado publicamente)

Base: `https://auth.pluggy.ai`

| Endpoint | Método | Uso |
|---|---|---|
| `/connect/config` | GET | config da app: `environment`, `isProductionEnabled`, `connectorsFilters.ids`, `itemsCreateLimit` |
| `/date` | GET | relógio do servidor (o widget usa p/ calcular `serverDateDeltaInMs`) |

Autenticação: `Authorization: Bearer <connectToken>`. **Não** aceita
`X-API-KEY` (retorna 401), nem o `apiKey`. Encapsulado em
`python pluggy_sync.py config`.

### Widget PluggyConnect

CDN: `https://cdn.pluggy.ai/pluggy-connect/v2.7.0/pluggy-connect.js`
(pode usar `latest` no lugar da versão). O loader é só um wrapper *zoid* que
embute um iframe de `https://connect.pluggy.ai`.

**Atalho de diagnóstico:** o Connect aceita as mesmas opções via query string,
o que permite abri-lo direto no navegador, sem o `connect.html`:

```
https://connect.pluggy.ai/?connect_token=<TOKEN>&with_sandbox=true
```

Parâmetros: `connect_token`, `with_sandbox`, `selected_connector_id`,
`connector_ids`, `connector_types`, `countries`, `theme`,
`allow_connect_in_background`, `connectors_sort_alphabetically`. Nesse modo a
página é o documento de topo (não um iframe cross-origin), o que torna o estado
inspecionável pelo devtools.

Opções relevantes do construtor:

| Opção | Tipo | Uso |
|---|---|---|
| `connectToken` | string | obrigatório |
| `includeSandbox` | boolean | exibe conectores de teste |
| `connectorIds` | number[] | filtra a lista |
| `selectedConnectorId` | number | pré-seleciona um conector |
| `connectorTypes` | string[] | filtra por tipo |
| `updateItem` | string | reautoriza item existente |
| `language` / `theme` | string | `'pt'` / `'light'\|'dark'` |
| `onSuccess(itemData)` | callback | `itemData.item.id` = ITEM_ID |
| `onError(error)` | callback | — |

---

## 8. Restrições de segurança a respeitar

Estas regras foram seguidas até aqui e devem continuar valendo:

1. **Credenciais só no `.env`**, nunca hardcoded, nunca em prints, nunca
   commitadas. `.gitignore` cobre `.env` e `.env.*`, com exceção do
   `.env.example`.
2. O `.env.example` é template — **não** preencher com valores reais (já
   houve esse deslize uma vez; o arquivo foi restaurado com placeholders).
3. O servidor local do `--serve` publica um **diretório temporário isolado**,
   contendo apenas o `connect.html`. Não servir a pasta do projeto: ela contém
   o `.env`. (Verificado: `GET /.env` → 404.)
4. Os dados baixados ficam em `./data/`, gitignored. São extratos bancários
   pessoais — não subir para repositórios, nuvens de terceiros ou serviços
   externos.
5. Não expor o `CLIENT_SECRET` em capturas de tela. Se vazar, regenerar no
   dashboard.

---

## 9. Resumo para quem assume daqui

O bloqueio do conector 200 **foi resolvido**: era a allowlist
`connectorsFilters` da aplicação, ajustável em Customização → Conectores no
dashboard (seção 5.4). Não havia nada errado no código. `python
pluggy_sync.py config` verifica o estado a qualquer momento.

A conta **não** está morta: o plano Free está ativo e permite criar itens pelo
widget (`CREATE_ITEMS_API_FREE_DISABLED` ao tentar pela API confirma isso por
contraste).

**O que falta — tudo interativo, precisa do usuário:**

1. Conectar os bancos em `meu.pluggy.ai`. O script só enxerga o que já estiver
   agregado lá.
2. `python pluggy_sync.py connect --meupluggy --serve`, autorizar, copiar o
   `ITEM_ID`. Com vários bancos no Meu Pluggy, **repetir uma vez por banco** —
   cada um vira um `ITEM_ID`.
3. `python pluggy_sync.py sync <ITEM_ID>` e conferir o `./data/`.

Nada disso foi executado ainda: o `sync` nunca rodou contra um item real, então
essa parte do pipeline segue não validada. O teste sandbox da seção 6 continua
valendo como forma barata de exercitá-la.

Expectativa a ajustar: o widget avisa que *"Os Itens Meu Pluggy só podem ser
atualizados manualmente pelo aplicativo e sempre são atualizados diariamente"*.
O `sync` lê o que o Meu Pluggy já agregou; não força atualização no banco.
