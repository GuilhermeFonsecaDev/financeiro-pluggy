# Otimização do Financeiro Pluggy — 06/09/2026

Este estudo é do projeto **FinanceiroPluggy**, servidor padrão na porta 8766.
A análise anterior em ExtratorVisor não se aplicava a ele.

## Causas confirmadas

Depois de alterações, o extrato reconstrói sua tabela calculada a partir das
transações, ajustes e regras. O perfil dessa reconstrução registrou 636.548
chamadas de normalização de texto, repetindo descrições e termos. A Visão Geral
também calculava faturas anuais dez vezes e assinaturas do histórico nove vezes
dentro de uma única carga. Consultas de identidade de cartões regravavam a
versão do esquema mesmo quando nada havia mudado.

## Alterações aplicadas

- Cache limitado a 16.384 textos para a normalização, que é uma função pura.
- Normalização do catálogo de categorias uma vez por consulta SQL, usando
  uma CTE materializada. A view continua aplicando as mesmas regras.
- Reutilização de faturas anuais e assinaturas somente dentro da mesma carga.
  Não há cache financeiro persistente entre requisições. Cada carga tem um
  contexto isolado; uma mudança no arquivo do banco invalida a reutilização.
  Transações em andamento não reutilizam a assinatura; a presença de journal
  ou WAL também desativa a reutilização.
- Remoção da regravação desnecessária de `cartoes_schema_versao`.
- Fechamento determinístico das conexões SQLite, com commit/rollback mantidos.
- Cabeçalhos `Server-Timing` e `X-Pluggy-Performance: 2026-09-06` para distinguir
  tempo do servidor e confirmar que o processo carregou esta versão.

## Comparação controlada

Cópias da mesma base, mesmo computador, código anterior preservado para
comparação. Cinco amostras por leitura após aquecimento; três para salvar e
recarregar. Medianas abaixo. Reconstrução: uma amostra por versão.

| Operação | Antes | Depois |
| --- | ---: | ---: |
| Visão Geral | 1.045,89 ms | 257,92 ms |
| Extrato | 226,74 ms | 128,17 ms |
| Filtros | 50,08 ms | 23,51 ms |
| Faturas anuais | 49,98 ms | 28,96 ms |
| Contas fixas | 145,76 ms | 83,71 ms |
| Investimentos | 51,75 ms | 54,26 ms |
| Extrato com reconstrução do cache | 1.263,45 ms | 361,88 ms |
| Salvar descrição + filtros + extrato | 1.698,80 ms | 535,84 ms |

Reduções aproximadas: 75% na Visão Geral e 68% no fluxo de alteração testado.
Investimentos não apresentou melhora mensurável nesta rodada.

Em uma instância HTTP de teste, as medianas foram 314 ms para Visão Geral,
156 ms para extrato e 102 ms para contas fixas. Isso mede as requisições;
não representa o tempo total de renderização no navegador.

## Validação

- Seis payloads financeiros e todas as linhas da view efetiva idênticos entre
  código anterior e otimizado, conferidos por hashes dos resultados completos.
- 70 testes passaram: identidades, reconexões, faturas, contas fixas, recorrentes,
  investimentos, conexões e os cinco novos testes de desempenho/persistência.
- Os novos testes cobrem isolamento de cargas, invalidação após gravação,
  proteção contra mutação de objetos reutilizados, rollback, fechamento e
  normalização dos textos.
- Visão Geral, Contas Fixas e Transações carregaram no navegador contra a
  instância otimizada de teste, com cópia do banco e sem sincronização externa.
- Nenhuma edição financeira de teste foi feita no banco de uso diário.

Comandos dos testes:

```powershell
python -m unittest testes_cartoes_genericos testes_faturas_genericas testes_fixas_genericas testes_reconexao_cartoes testes_recorrentes testes_investimentos -q
python -m unittest testes_desempenho testes_conexoes -q
```

## Ativação pendente

O servidor em uso foi identificado como uma instância do código anterior;
suas respostas ainda não tinham o cabeçalho de versão acima. A revisão
automática de segurança bloqueou o comando de reinício, sem detalhar o motivo
além de “blocked by policy”. A instância de teste foi encerrada após validação.

Para ativar, encerre a janela do backend antigo e abra novamente pelo atalho
**Financeiro Pluggy**. Depois atualize a página. Apenas F5 não recarrega o Python.
As melhorias já estão nos arquivos do projeto correto.
