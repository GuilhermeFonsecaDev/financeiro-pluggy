# Hábitos e recorrências

## Objetivo e fluxo

Antecipar compromissos mensais independentemente de Contas fixas e, com eles,
responder **de que a fatura do mês é feita**. O botão e a janela se chamam
**Hábitos e recorrências**. A abertura mostra **Previsão**; **Ativas**,
**Descobertas** e **Pausadas** ficam em abas. Histórico e critérios aparecem
sob demanda.

Cada cadastro permite alterar nome, identificação no extrato, conta/cartão,
dia da cobrança, comportamento e valor. Permitir cadastro manual, pausa,
retomada e exclusão. Exclusão preserva as transações e dispensa a sugestão
correspondente. Cadastros existentes continuam disponíveis.

## Um cadastro, dois comportamentos

Hábito não é uma entidade separada: é a mesma previsão recorrente com outro
comportamento. O campo `comportamento` vale:

- **`cobranca`** — cobrança única. A transação real **substitui** a previsão
  daquela competência (assinatura, mensalidade, conta de consumo).
- **`reserva`** — orçamento do ciclo. As compras reais **consomem** o previsto,
  e só o restante continua previsto (mercado, posto, farmácia).

## Como um cadastro reconhece a cobrança

Três crivos, em OU, do mais específico para o mais amplo (`_casa`):

1. **Identificação** (`lojista`) -- igualdade sobre a forma normalizada
   (`_chave`). É a regra original e a única que nunca erra; vale para um
   estabelecimento só.
2. **Termos** (`termos[]`) -- por CONTÉM sobre a descrição normalizada.
   "posto" pega POSTO CENTRAL e POSTO SHELL. Mínimo de 3 letras: com duas,
   "ar" casaria dentro de "farmacia". Máximo de 12 termos.
3. **Categoria** (`categoriaId`) -- a categoria do lançamento, filhas
   incluídas. É o que faz "qualquer posto que eu abastecer" funcionar sem
   cadastrar um posto por vez, inclusive um que ainda não existe no histórico.

Sempre no **mesmo pagamento**: a reserva pertence a um cartão ou conta, para
que cada fatura preveja o que é dela. Um cadastro precisa de pelo menos um dos
três; uma reserva de categoria pode não ter estabelecimento nenhum.

**O mais específico ganha.** Um lançamento que outro cadastro ativo já
reivindica por identificação ou termo não entra por categoria em ninguém
(`_reservados_por_outros`) -- senão a reserva de "Postos de combustível" e a
do posto da esquina somariam o mesmo abastecimento duas vezes.

`testar()` conta, antes de salvar, quantos lançamentos cada crivo pega, em
quantos meses, com um exemplo por estabelecimento e a média dos três ciclos.
Um termo de três letras pode pegar meio extrato sem a pessoa perceber.

Cada compra que casa abate o previsto do ciclo; o que sobra é
`base − lançado`, e a base é a média dos três últimos ciclos com gasto (ou o
valor fixo, se `modoValor` for `fixo`).

## Descoberta por categoria

Mercado e restaurante têm dezenas de estabelecimentos: uma descoberta por
lojista produz lista inútil e nenhuma previsão que preste. `_habitos_por_categoria`
sugere a categoria inteira quando ela se espalha -- as mesmas regras de
presença e recência do hábito, mais **3 estabelecimentos distintos**, porque
com um só o cadastro do próprio estabelecimento já resolve e é mais preciso.

Cada lançamento conta para a sua categoria e para todas as mães, então a mãe
contém a filha. Fica a **mais específica**: uma reserva de gasolina é útil,
uma de "Automotivo" com estacionamento e manutenção dentro já não é. E o
lojista individual desaparece da lista quando a categoria dele virou reserva
-- estaria coberto duas vezes.

Fora: movimento entre contas, imposto, investimento, encargo -- e **"Outros"**,
que é o saco do que não foi classificado e engoliria todo hábito individual
cuja categoria ainda é desconhecida.

Dispensar vale para os dois: faltava o filtro de `recorrentes_ignorados` no
caminho dos hábitos, e o hábito dispensado reaparecia na lista.

O campo `tipo: "habito"` é a grafia antiga do segundo caso e continua sendo
gravado espelhado, para que cadastros e leituras antigas sigam valendo. A
conversão entre os dois é livre e **nunca muda a chave**: ela é a PK da tabela,
é o que a exclusão grava em `recorrentes_ignorados` e é o `compraId` que as
projeções, as contas fixas e as exclusões de parcela enxergam. O prefixo
`habito:` de quem nasceu hábito é histórico, não semântico.

O modo de valor (`ultimo`, `fixo`, `media`) é independente do comportamento:
conta de luz é cobrança única e varia todo mês.

Reserva vale em conta bancária, não só em cartão. Em cartão o ciclo é a fatura;
em conta, o mês da data.

## Previsão da fatura

A aba Previsão **decompõe** o total, nunca o recalcula. O total é
`pluggy_extrato.cartoes_payload(ano, "fatura")`, que já passou pela fatura
aberta, pelas parcelas projetadas, pelos recorrentes, pela confirmação manual,
pelo pagamento e pela fatura oficial. Recalcular em paralelo perderia as duas
proteções contra dupla contagem (`_fixas_cobrem` e `fixas._casar_projecao`) e a
soberania da fatura fechada.

O assunto da tela é **gasto que se repete todo mês e não é parcela**: posto,
mercado, Netflix, Livelo. Parcela e valor já lançado são fato conhecido -- eles
compõem o total da fatura e aparecem na tela de Cartões, mas não se decide nada
sobre eles aqui. Por isso o card de cada mês mostra:

1. **Previsto aqui** -- a soma das recorrências e reservas daquele mês, com um
   item por linha. Reserva mostra quanto dela já foi consumido.
2. **Sem previsão** -- o gasto novo típico (abaixo) e quantos gastos repetidos
   daquele cartão ainda não viraram previsão, com atalho para Descobertas.
3. **Contexto**, uma linha discreta no pé: o total da fatura prevista, quanto
   já foi lançado e quanto é parcela.

Uma tentativa anterior colocou "definido" (lançado + parcelas) e "previsto"
como dois grupos de igual peso. Não funcionou: o definido é quase sempre maior,
virou protagonista, e a tela deixou de parecer ser sobre hábitos.

**Gasto novo típico.** A previsão não cobre a compra avulsa que ainda vai
entrar até o fechamento — e é isso que faz a fatura fechar acima do previsto.
A tela estima esse buraco pela mediana do que entrou nos últimos N dias dos
ciclos já fechados do cartão, descontando parcelas (previstas uma a uma) e as
cobranças dos cadastros ativos (já previstas). Exige pelo menos três ciclos
fechados; abaixo disso a tela não arrisca número. Ele nunca entra no total:
é um aviso, não uma parcela da conta.

Mês cujo valor veio de fonte completa (oficial, pagamento, confirmada, fechada)
não se decompõe — o total deixa de ser a soma do que conhecemos.

## Acurácia

`previsao_fatura_historico` congela a previsão de cada ciclo na primeira vez
que ela é olhada, e nunca a sobrescreve. Sem isso não há medida honesta: a
projeção só olha para a frente, e reconstruir depois usaria cobranças que só se
souberam mais tarde. Guarda também a quantos dias do fechamento a previsão foi
feita, que é o que torna o acerto comparável entre meses. A referência é
`pluggy_faturas.valor_total`, ignorando `0` (ambíguo entre "não coletei" e "não
gastou nada").

## Regras de sugestão

- Janela de 12 meses, somente débitos incluídos nos cálculos, em contas ativas.
- Pelo menos três cobranças, com dois últimos intervalos mensais e pelo menos
  80% dos intervalos entre 24 e 38 dias. Considerar datas, inclusive viradas de
  mês, em vez de apenas contar meses-calendário.
- Última cobrança nos últimos 45 dias. Não usar transações futuras.
- Identificação conservadora por conta/cartão e estabelecimento; aliases
  explícitos para descritores conhecidos do mesmo serviço.
- Excluir parcelamentos, duplicidades ambíguas, compras frequentes, sequências
  irregulares e variações erráticas. Aceitar reajuste sequencial consistente.
- Planos de valores diferentes só são separados se cada sequência tiver
  evidência própria. Uma compra extra não vira mensalidade.
- Não sugerir novamente o que já foi cadastrado ou dispensado, nem duplicar
  contas fixas que já cobrem a mesma cobrança no mesmo escopo.

## Previsões e conciliação

Somente cadastros ativos geram previsão, no horizonte de 12 competências a
partir do mês atual. Conta bancária usa mês da cobrança; cartão usa seu ciclo
de fatura, com a regra de mês seguinte apenas quando não existe ciclo.
O dia é ajustado ao último dia em meses curtos. A cobrança real incluída,
na conta escolhida e com a identificação/faixa correspondente, substitui a
previsão daquela competência. A fatura oficial fechada continua soberana.

Valor automático acompanha a última cobrança disponível para a competência;
valor definido permanece o informado. Trocar pagamento direciona previsões
e conciliação para a nova conta, preservando o histórico original. Pausar ou
excluir remove previsões, sem editar lançamentos importados.

## Implementação e validação

1. Endurecer detector e expor evidências/critério legíveis.
2. Adicionar gestão validada e compatível com os cadastros JSON existentes.
3. Integrar gestão e calendário às projeções compartilhadas de cartões/extrato.
4. Reorganizar janela e conferir estados vazios, erro, edição e exclusão.
5. Rodar regressões em SQLite temporário: cadência, reajuste, parcelas, extras,
   troca de pagamento, valor fixo, exclusão, fatura fechada e substituição pelo
   real. Conferir interface e API numa cópia local sem sincronização externa.
