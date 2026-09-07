# Recorrências

## Objetivo e fluxo

Gerenciar compromissos mensais independentemente de Contas fixas. O botão e a
janela se chamam **Recorrências**. A abertura mostra **Ativas**; **Sugestões** e
**Pausadas** ficam em abas. Histórico e critérios aparecem sob demanda. Retirar
possíveis de baixa confiança, diagnósticos de descartados e correções de contas
fixas desta janela.

Cada cadastro permite alterar nome, identificação no extrato, conta/cartão,
dia da cobrança e valor (última cobrança ou valor definido). Permitir cadastro
manual, pausa, retomada e exclusão. Exclusão preserva as transações e dispensa
a sugestão correspondente. Cadastros existentes continuam disponíveis.

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
