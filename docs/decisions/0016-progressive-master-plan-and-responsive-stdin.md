# ADR 0016 — Plano progressivo e stdin responsivo

Status: implementação autorizada pelo proprietário em 2026-11-20.
Prevalece sobre o teto de 32 KiB do Plano Mestre da ADR 0012 e o envio integral
do Markdown. Não cria limite de tamanho para o documento ou para handoffs JSON.

## Plano Mestre

O SHA-256 é calculado em blocos de 64 KiB. O arquivo é percorrido para indexar
cabeçalhos ATX (`#` até `######`), sem manter o corpo completo em memória.
Assinatura de filesystem e nova verificação de digest detectam alterações
durante a indexação. O digest continua ligado ao objetivo/snapshot do marco.

O milestone pode declarar seções necessárias:

```json
{"id":"M1","type":"milestone","goal":"Primeiro fluxo","acceptance":["Teste de integração passa"],"depends_on":[],"plan_sections":["## 3. Arquitetura","## 4. Contrato","M1 Primeira fatia"]}
```

Cada seção inclui seu conteúdo e subseções até o próximo cabeçalho de nível
igual ou superior. Cabeçalhos dentro de fences de código e blocos indentados
não entram no índice. Títulos são comparados literalmente; duplicatas exigem
hierarquia (`Plano / Arquitetura / Contrato`) ou ID de linha (`L166`). Seção
ausente/ambígua falha com diagnóstico. Seleções sobrepostas não repetem texto.
`plan_sections` participa da identidade do milestone quando declarado.

Sem seleção, ou quando os recortes excedem o orçamento inicial, o prompt recebe
um índice dos cabeçalhos `#`/`##`/`###`, com path, SHA e linhas inclusivas.
Índices extensos são paginados por cursor, sem perder referências. Um título
individual muito extenso é representado por referência à linha de origem.

`plan_context_bytes` no objeto raiz de TASKS.json configura o orçamento desse
contexto, padrão 16384 bytes, mínimo 1024. Isso limita o pacote inicial/trechos,
nunca o arquivo. Não é limite global do JSON, backlog, handoff ou janela nativa
do modelo. Uma faixa que não caiba retorna um diagnóstico para solicitar menos
linhas; seu texto não é cortado. Uma linha individual maior que o orçamento
exige ajustar o orçamento ou a organização do documento, sem rejeitar o arquivo.

## Recuperação mediada

O planejador continua em workspace Git sintético, sem montar/copiar o repositório
original. Em vez de abrir caminhos arbitrários, pode responder:

```json
{"plan_read":{"sha256":"sha256:<digest completo>","start_line":166,"end_line":190}}
```

Ou pedir a próxima página do índice:

```json
{"plan_index":{"sha256":"sha256:<digest completo>","cursor":0}}
```

AgentFlow confere schema, digest e faixa, lê somente o Plano Mestre referenciado
e devolve o trecho como dado informativo. A leitura confere o digest antes/depois;
alteração ou indisponibilidade bloqueia o planejamento. Não há comandos shell,
paths arbitrários ou ampliação de permissões nesse protocolo.

O retorno estruturado final continua sujeito a todas as validações de plano,
escopo, DAG e critérios. O último trecho recuperado substitui o anterior para
não acumular histórico; o índice/contexto inicial permanece. O orçamento é
aplicado ao JSON serializado dos trechos, incluindo escapes e metadados.

O padrão é até oito recuperações, ajustável por `--planner-max-plan-reads` ou
pelo construtor do planejador. O limite de rodadas e o prazo total de
`--planner-timeout` são operacionais; não são limites de arquivo. Ao esgotá-los,
não se inventa o conteúdo ausente: o planejamento para com diagnóstico.

## Runtime local

Leitores stdout/stderr iniciam antes de uma thread auxiliar alimentar stdin
em blocos UTF-8. O watchdog permanece no fluxo principal; escrita bloqueada
não impede timeout, cancelamento, limite de logs ou controle de processo.
`BrokenPipeError`/fechamento antecipado são tratados. Outros erros de escrita
produzem `stdin_write_failed`. Após encerrar a árvore/grupo/Job Object, a thread
é aguardada; falha de encerramento gera erro, sem declarar sucesso silencioso.

O watchdog sem saída usa 300 segundos por padrão. `run`, `apl run`, `execute`
e `apl run-resume` aceitam `--idle-seconds N`; zero desativa. A API oferece
`LocalRuntime(..., default_idle_seconds=None)` para desativar ou um valor positivo
para configurar; um idle explícito de ExecutionPolicy tem precedência.
O timeout absoluto continua valendo. Atividade é medida em bytes de stdout/stderr,
não em consumo de stdin: inferência longa e silenciosa pode exigir ajuste.

Idle mantém status `timed_out`, com reason `idle_timeout`, separado do timeout
absoluto. Não significa falha funcional nem prova ausência de efeitos. Esta
mudança não autoriza retry automático de processo já iniciado.

## Aceite

Testes cobrem plano acima dos limites antigos, SHA, fences, seções aninhadas,
duplicatas, índice paginado, deriva de arquivo e protocolo real com runtime
simulado em workspace sintético. Processos Python locais de teste exercitam
stdin não consumido, cancelamento/timeout, pressão simultânea stdout/stdin,
heartbeat, idle, EOF antecipado e falha de encoding. Não houve inferência real
de agentes nem execução de tarefas do usuário.
