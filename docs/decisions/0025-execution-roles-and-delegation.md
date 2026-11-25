# ADR 0025 — papéis por execução e primeira delegação multiagente

Status: implementação da fatia solicitada após a ADR 0024. Mantém APL como
coordenador, o runtime anterior e os comandos legados. Sem commit/push.

## Entrada, contratos e seleção

`agentflow start` reutiliza o fluxo de `run`, exigindo seleção de orquestrador,
executor e revisor antes de inferência. Flags de papéis em `run` também ativam
esse fluxo; `run` sem elas preserva seu comportamento histórico. Não há nova
fila, scheduler ou implementação paralela de execução.

Em TTY, a CLI apresenta o inventário e pergunta agentes/modelos. Sem TTY, flags
ou respostas persistidas configuram os papéis. Caso faltem respostas, registra
`AWAITING_CONFIGURATION`, devolve opções em JSON e retorna 2 sem inferência.
`--configure-only` configura sem começar planejamento/dispatch. Respostas
parciais são duráveis. O revisor sugere `same`, reutilizando a seleção do
orquestrador. O chamador não determina automaticamente o orquestrador.

Os contratos puros estão em `agents.roles`; regras de seleção/revalidação em
`apl.role_selection`, apresentação em `role_cli`, composição na CLI/fábrica.
Esses serviços recebem registros e stores por interface, sem importar
adaptadores concretos para APL. `ResultObserver` é um protocolo do contrato APL.
O revisor está selecionado/persistido, mas seu motor de revisão não é ativado.
Há um executor sequencial; vários executores ficam para outra fatia.

## Modelos e compatibilidade

Cada papel conserva agente, API/versão, executável observado, capacidades e
fonte, seleção de modelo e disponibilidade. Retomada revalida tudo sem perguntas
repetidas ou troca silenciosa. `PinnedRoleAdapter` também confere a identidade
do `ProcessSpec` antes do runtime. Configuração de cwd não prova isolamento.

Ausência de catálogo/atestação não impede usar o padrão nativo. `cli-default`
não inventa nem congela um modelo não selecionado. O perfil AgentFlow mostrado
no inventário é distinto da configuração nativa do CLI; Enter escolhe esta
última. Modelos manuais conservam o pedido como `available` ou
`configured-unverified`; disponibilidade de catálogo não é identidade efetiva.
Observação efetiva desconhecida é permitida no caminho de papéis, divergência
conhecida é recusada. Pré-checagens e limitações dos adaptadores permanecem.

Gemini admite modelo explícito nesse novo caminho com `provider=native`, um
marcador para manter o provider nativo, sem alegar fornecedor efetivo. Um
provider arbitrário que não possa ser selecionado pelo CLI é recusado. A fábrica
legada mantém a recusa anterior de modelos Gemini não atestados. Codex mantém
as versões e limitações anteriores e permite seu padrão nativo quando disponível.
Não se contornam autenticação, trust, sandbox ou permissões.

## SQLite v8 e identidade

Migração aditiva no migrador atual, backup-first e transacional. Leitores de
execução, inspeção e auditoria reconhecem v8. O teste de versão futura usa v9,
com a mesma recusa sem mutação. Não se reescrevem revisões, hashes ou transações
históricas. As tabelas novas são:

| Tabela | Conteúdo |
| --- | --- |
| `role_requests` | Entrada/digest do pedido e identidade de workspace, objetivo, plano e limites. |
| `role_configurations` | Snapshot imutável dos três papéis e digest. |
| `role_request_events` | Espera, descoberta, respostas parciais e configuração concluída. |
| `role_cycle_bindings` | Vínculo único com o ciclo APL existente, com FK. |
| `role_calls` | Intenção imutável, tipo, entrada/digest da chamada ao orquestrador. |
| `role_call_results` | Resposta estruturada/digest e duração monotônica observada. |

Todas têm proteção contra update/delete. Tarefas, tentativas e outcomes continuam
nas tabelas/stores atuais. O manifesto histórico de executor precisa concordar
com a configuração de papéis. O digest da política do contrato local inclui os
papéis somente nos ciclos novos configurados. Mesmo `operation-id` com entrada
diferente é conflito; configuração completa exige nova execução para mudança.

## Fluxo e recuperação

1. Descobrir, selecionar, persistir e revalidar papéis.
2. O orquestrador usa o planejador estruturado existente para produzir tarefas;
   DAG, critérios, escopos e geração de gates continuam validados localmente.
3. O orquestrador recebe o plano executável, inclusive TOMLs prontos/reutilizados,
   para decisão estratégica.
4. O controlador autorizado anterior delega ao executor selecionado, coleta
   recibos/logs/handoffs, executa gates e persiste o outcome no journal existente.
5. Tarefa, resultado, handoff, estado de validação e evidências voltam ao
   orquestrador antes de outra tarefa ou conclusão. A resposta estrita é versão
   1, decisão `CONTINUE`/`BLOCK` e motivo.

Decisões não alteram escopo, seleção, testes, autorização ou política de retry.
`CONTINUE` não aprova gates falhos. Falhas com outcome estruturado também voltam
ao orquestrador, mantendo o bloqueio IFR. Falhas sem outcome permanecem sob a
recuperação conservadora anterior. Cada invocação headless usa processo próprio;
planejamento/decisões usam o workspace sintético já existente, executores usam
o workspace vinculado ao contrato. Git/checkpoints não foram substituídos.

A intenção é persistida antes da inferência. Resultado confirmado é reutilizado;
intenção sem resultado fica `unknown_or_running`, sem replay por expiração ou
reinício. Retomada entre checkpoint da tarefa e decisão não reexecuta a tarefa.
`BLOCK`, resposta inválida ou chamada ambígua impedem novas delegações e mantêm
o histórico. Não há liberação automática nem UI para apagar/editar intents.

O ID da chamada determina os logs em
`runs/apl-planner/role-<digest-sem-prefixo>-<sequência>/`; sequência distingue
leituras progressivas dentro do planejamento. Logs/metadados, planos e journals
referenciados são essenciais à auditoria, sem limpeza nova por idade.

Timeouts das decisões são limitados pelo saldo de duração. Durações monotônicas
concluídas são persistidas e descontadas desse saldo na retomada; cache não cobra
uma segunda inferência. Excesso bloqueia a conclusão. `orchestrator_seconds` é
informado separadamente do tempo dos executores/gates. Custos/tokens reais ainda
dependem do que os provedores reportem; estimativa não é hard cap financeiro.

## Evidências e limites

Testes cobrem configuração interativa/sem TTY, respostas parciais, modelos não
atestados, indisponibilidade/identidade, migração/backup/rollback, concorrência,
bloqueios, gates falhos, cancelamento, agentes iguais em papéis diferentes e
crash entre checkpoint e decisão. O cenário ponta a ponta usa subprocessos reais
pelo `LocalRuntime` e gates reais em um projeto sintético do próprio teste, com
protocolos de agentes simulados. Não homologa inferência real de agentes.

MCP, worker independente, revisão Git-Aware e Communication Optimizer não foram
antecipados. Baseline arquitetural inalterado; resultados finais no
[progresso](../autonomous-platform-progress.md).
