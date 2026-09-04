# Plano de correção: competência de fatura de cartão

Estado: **etapas 1 a 5 aplicadas** (02/09/2026), não commitadas. Etapa 6
pendente, porque exige migrar dados já cadastrados.

Rede de segurança: `python testes_faturas.py`. Ela roda sobre uma cópia do
banco, confere as invariantes e lista as células da grade que mudaram desde o
último snapshot aceito.

Este arquivo não contém valores de fatura nem dados pessoais — o repositório é
público. As verificações que precisam de valores reais rodam localmente e
gravam fora do git (ver etapa 5).

---

## O problema

O projeto não pergunta em que fatura uma compra caiu: ele **infere**, com a
regra "mês da última compra do grupo + 1". Duas consequências:

1. **Quebra em toda virada de mês.** As pendentes de um cartão formam um grupo
   único; no dia 1º chega a primeira compra do mês novo, o `MAX(data)` salta, e
   a fatura inteira migra para o mês seguinte. O mês que fechou fica vazio e o
   seguinte fica inflado. Aconteceu em 01/09/2026 e vai repetir em 01/10.
2. **Erra em cartão que fecha e vence no mesmo mês.** "Última compra + 1" supõe
   que a fatura vence no mês seguinte ao das compras. Num cartão que fecha dia
   10 e vence dia 17, a fatura fechada é etiquetada um mês adiante.

Há ainda um agravante independente: **duas implementações da mesma pergunta**.
A tela de Cartões (`pluggy_extrato.cartoes_payload`) e a view do extrato
(`extrato_camada._COMPETENCIA_FATURA`) calculam a competência cada uma do seu
jeito. É daí que vem a lista de lançamentos de um mês discordar do total
daquele mês.

## Evidência apurada (02/09/2026)

Medições feitas sobre a base real, todas reproduzíveis:

- **`creditCardMetadata.billForecastDate`** vem em cada transação de cartão e é
  o mês em que o ciclo **fecha** — não o mês em que a fatura vence. Qual ciclo,
  dentro daquele mês, quem diz é a data da compra: o que fecha ali, ou o
  seguinte se a compra vier depois do fechamento. Conferido contra as faturas
  fechadas:

  | conector | acerto da regra | deslocamento fixo de meses |
  |---|---|---|
  | Inter | 99,7% (583 amostras) | 98,1% |
  | Nubank | 99,6% (245) | 99,6% |
  | Itaú | 44% a 77% (52) — inutilizável | 67% |

  Um deslocamento fixo (+1 no Inter, 0 no Nubank) parece funcionar porque a
  diferença entre fechamento e vencimento é constante em cada cartão, mas ele
  erra justamente nas compras dos últimos dias do mês, feitas depois do
  fechamento: eram as 11 exceções que sobravam no Inter. Um cartão que fecha
  dia 30 e vence dia 7 tem vencimento no mês seguinte ao fechamento; um que
  fecha dia 2 e vence dia 9, no mesmo mês — daí a aparência de deslocamentos
  diferentes.

- **A janela do ciclo** (`[fechamento anterior, fechamento)`) é confirmada por 10
  faturas fechadas: em todas, a primeira compra da fatura é exatamente o dia do
  fechamento anterior. Mas o dia de fechamento **oscila** (um conector fechou
  nos dias 27, 29, 30, 30, 30 em meses seguidos), então projetar o próximo
  repetindo o último erra por um ou dois dias — e um dia de erro joga a compra
  daquele dia na fatura errada. Por isso a janela é o degrau 3, não o degrau 1.

- **`pluggy_faturas.valor_total`** bate ao centavo com a grade em 32 meses
  conferidos. Em 5 das 8 faturas de um dos cartões ele vem `0,00` mesmo tendo
  havido gasto — zero ali significa "não coletei", não "não gastou".

- **O maior pagamento atribuído a um ciclo é igual ao valor oficial da fatura**
  em 14 de 14 meses em que a API informa valor, nos três cartões. Atribuição:
  o ciclo cujo fechamento está mais próximo da data do pagamento. Usar o
  **maior** e não a soma é obrigatório — o mesmo pagamento chega repetido, com
  descrições diferentes em dias seguidos ou literalmente duplicado no mesmo dia.

- **`competencia` de `pluggy_faturas`** é sempre o mês de vencimento; conferido
  contra `vencimento` em todas as faturas.

## A solução

Uma definição só, em escada de autoridade, num módulo próprio (`ciclos.py`),
consumida pela view do extrato e pela tela de Cartões:

1. transação com `billId` → a competência da própria fatura;
2. `billForecastDate` (mês de fechamento) resolvido pela data da compra, só
   nos cartões em que a regra se mostrou confiável (≥90% em ≥20 amostras);
3. a janela do ciclo em que a data cai;
4. mês da compra + 1, para cartão recém-conectado sem nenhuma fatura fechada.

E o valor de cada mês:

- **fatura fechada que a API entregou com valor → `valor_total`, exatamente.**
  Sem reconstrução, sem abatimento, sem clamp;
- **fechada mas sem valor na API (0,00, ou ainda não emitida) → o maior
  pagamento atribuído ao ciclo**;
- **aberta e não paga → soma das pendentes do ciclo**, ou o valor confirmado na
  tela, se houver;
- **meses futuros → projeção das parcelas conhecidas**, nunca sobre mês que já
  fechou.

Nada disso olha nome de banco. Cartão novo ganha ciclo próprio na primeira
fatura que fechar e passa a usar o `billForecastDate` quando acumular amostra.

## Etapas

Cada etapa é aplicável e verificável sozinha; nenhuma depende da seguinte.

**1. Rede de segurança (fazer antes de tudo).** — FEITO (`testes_faturas.py`)
Teste que fixa a grade de 12 meses × N cartões e as invariantes. Sem isso, cada
correção é conferida à mão e é fácil quebrar o que já estava certo.
Verificação: o teste passa no código atual, antes de qualquer mudança.

**2. `ciclos.py`: ciclos e deslocamento medido.** — FEITO
Tabelas `pluggy_ciclos` e `pluggy_cartao_previsao`, reconstruídas junto com a
camada do extrato. Nada consome ainda.
Verificação: toda compra pendente cai em exatamente um ciclo; o deslocamento
medido reproduz a competência das faturas fechadas.

**3. A escada de competência, em um só lugar.** — FEITO
`ciclos.expressao_competencia()` passa a alimentar `_COMPETENCIA_FATURA` e as
consultas da tela de Cartões. Inclui a tabela nova na assinatura do cache do
extrato, senão o cache serve competência velha depois de uma sincronização.
Verificação: nenhuma transação de cartão sem competência; nenhuma compra com
duas parcelas da mesma série na mesma fatura; o teste da etapa 1 mostra
exatamente quais células mudaram, para revisão.

**4. Valor: API primeiro, pagamento no buraco.** — FEITO
Remove a reconstrução de mês fechado (a consulta que somava transações e
abatia pagamentos). `_aplicar_faturas_oficiais` continua por último.
Verificação: todo mês marcado como `oficial` é idêntico ao `valor_total`.

**5. Fatura aberta por ciclo e projeção com piso.** — FEITO
Pendentes agrupadas por ciclo (hoje: um balaio por cartão); bloqueio de
duplicação por **compra**, não por cartão; nenhuma projeção em mês já fechado.
Verificação: os primeiros dias do mês, quando dois ciclos estão abertos ao mesmo
tempo, é o caso que quebra — conferir outubro e novembro juntos.

**6. Genérico de verdade.** — PENDENTE
Sai o que ainda é por banco: `NOMES_BANCOS` e `_grupos_bancarios`, usados pela
*forma de pagamento* das contas fixas. Isso exige migrar dados já cadastrados
(`fixas_contas.forma` guarda `itau`/`nubank`/`inter`), então é a última etapa e
precisa de backup do banco antes.

## Decisões que não são minhas

- **Coluna consolidada soma as faturas dos dois cartões.** Isso muda dois meses
  para cima em relação ao cálculo antigo, que olhava só um dos dois. Se a coluna
  representa os dois cartões, o valor maior é o correto; se você quer o número
  antigo, é outra regra.
- **Total x lista.** Em mês cujo valor vem da API ou de um pagamento, o total não
  é igual à soma dos lançamentos listados: o total é oficial, a lista é o que o
  conector entregou. Alternativa: exibir a soma da lista e aceitar que o valor
  deixe de bater com o extrato bancário.

## Riscos conhecidos

- Um dos conectores entrega várias parcelas da mesma compra com a mesma data.
  Isso infla a **lista**, não o total. Pré-existente.
- Um dos conectores entrega faturas sem as compras delas e com `valor_total`
  zerado; esses meses dependem inteiramente do pagamento.
- A conta substituída por reconexão tem `billId` sem fatura correspondente. Ela
  já fica fora de tudo por `contas_ativas()`; qualquer mudança aqui deve manter
  isso.

## Nota sobre dados nos testes

O teste da etapa 1 precisa de valores reais para ter valor, e valores reais não
podem entrar no repositório. O formato: o teste grava e lê um snapshot em
`data/` (ignorado pelo git); o arquivo versionado contém apenas as invariantes e
o comparador. Rodar o teste em uma máquina nova gera o snapshot em vez de
falhar.
