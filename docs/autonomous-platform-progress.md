# Evolução autônoma — inventário e progresso

Solicitação de referência: evolução A–M recebida em 2026-11-23. Este documento
registra entregas verificáveis e trabalho restante. A plataforma completa ainda
não está implementada. As restrições históricas de fases não impedem a evolução
solicitada; as invariantes de integridade e compatibilidade continuam vigentes.

> [!NOTE]
> Para o inventário funcional consolidado das 20 áreas, classificação e
> prioridades atuais, consulte [functional-status.md](functional-status.md).

## Consolidação 2026-11-24 — Integração Operacional Ponta a Ponta (P1)

- `AutonomousRuntime` (`src/agentflow/autonomous_runtime.py`) integra `WorkerPool`, `SubagentDispatcher`, `execute_with_fallback`, `LiveEventBus`, `CommunicationOptimizer` e `SharedContextRegistry`.
- `worker.py` emite eventos estruturados no `LiveEventBus` durante execução de comandos gerenciados.
- `mcp_server.py` expõe `agentflow_events` (com `CommunicationOptimizer` e cursor `since_seq`) e `agentflow_context_deltas` (com cursor `since_version`).
- Validado com `tests/test_p1_autonomous_execution.py` (**5 passed**) e `tests/test_end_to_end_p1_integration.py` (**1 passed**) usando executores simulados e subprocessos locais.
- Suíte completa (`pytest -q --tb=short`): **918 passed, 1 skipped** (0 falhas).
- Verificação de CLIs reais (`agentflow agents`): `antigravity` (`agy.exe` `1.5.16`) detectado com `autenticação=unknown` e `modelos=unsupported`; `codex`, `claude-code`, `gemini` e `opencode` `unavailable`. Inferência real com modelos externos permanece pendente de autenticação/homologação.

## Consolidação 2026-11-24 — Integração da Configuração Global no `start`

- Identificado e corrigido gap P0: `agentflow start` (`role_mode` em `src/agentflow/cli.py`)
  agora carrega `load_global_config()` e aplica `orchestrator`, `worker_default`
  e `adversarial` quando não há flags explícitas na CLI, preservando a
  imutabilidade de operações já configuradas no SQLite.
- Validado com `pytest -q tests/test_global_config.py` (**6 passed**).

## Incremento E–G — revisão Git e primeiro ciclo de correção

Referência: [ADR 0026](decisions/0026-git-aware-review-and-corrections.md).
Os incrementos anteriores permanecem disponíveis. Não houve commit/push,
instalação de dependências, alteração do protótipo ou regeneração do baseline
de dívida arquitetural.

### Comportamento entregue

- Baseline por tarefa com HEAD/ref, identidade do repositório, hash bruto do
  índice, entradas staged e hashes/conteúdo do working tree e untracked.
  Capturas duplicadas verificam estabilidade. Diffs identificam adição,
  modificação, remoção e renomeação exata, separando working tree e índice.
- Lease reutiliza o lock Git existente. Alterações preexistentes no escopo
  bloqueiam o executor antes de qualquer implementação. Arquivos já aprovados
  da mesma execução podem ser reutilizados com identidade exata. Alterações
  fora do escopo, concorrentes ou sem autoria segura bloqueiam correções.
  Arquivos ignorados pelo Git dentro do escopo também bloqueiam: sua preimagem
  não está na baseline normal. Caches operacionais conhecidos são excluídos.
- Contexto incremental inclui diffs, símbolos Python alterados, imports locais,
  contratos, testes pertinentes, critérios e resultados de gates. O revisor pode
  pedir arquivos adicionais em até duas expansões; limites não cortam evidência
  silenciosamente. Não depende de Codex nem de Code Context Graph.
- O revisor selecionado usa o adaptador/runtime existente em sessão própria.
  Respostas v1 têm estados `APPROVED`, `CORRECTIONS_REQUIRED`, `BLOCKED`,
  `INCOMPLETE` e `FAILED`. Findings exigem ID, caminho, linhas, severidade,
  citação verificável, descrição, impacto, sugestão, confiança e resolução.
  O Core gera ID estável e rejeita âncoras inventadas. Âncora válida prova
  proveniência do trecho, não a correção semântica de toda conclusão do modelo.
- O executor escolhido recebe findings e devolve edições JSON com hashes
  anteriores. Só arquivos existentes atribuíveis à tarefa e citados em findings
  podem ser corrigidos automaticamente. Publicação verifica estado integral,
  preimagens e autorização; preserva índice/refs. Em seguida executa gates e
  revisão nova. Aprovação sem gates independentes aprovados não conclui tarefa.
- Pendências, motivos, findings e recibos voltam ao observador/orquestrador
  existente. Limites controlam correções, chamadas, tempo, payload local e
  ausência de progresso, também respeitando duração/tentativas do grant.

### Configuração, persistência e recuperação

Novos pedidos por papéis usam revisão automática por tarefa. A escolha do
revisor já persistida é reutilizada; não há nova seleção nem aprovação humana
entre correções. A política integra a identidade do pedido/contrato.

```shell
agentflow start "Implementar objetivo" --orchestrator-agent antigravity --executor-agent gemini --reviewer-agent same --review-max-corrections 2
agentflow start "Outro objetivo" --review-mode off
agentflow apl run-status --path caminho/do/projeto --run-id ID
agentflow review-status --path caminho/do/projeto --operation-id ID-DA-TAREFA
agentflow apl run-resume --path caminho/do/projeto --run-id ID
```

Outras flags: `--review-max-calls`, `--review-max-seconds`,
`--review-max-payload-bytes` e `--review-no-progress-limit`. Pedidos v8 sem
política de revisão mantêm IDs, digest e comportamento na retomada. Não se
ativa revisão retroativamente. Mudança do código do motor invalida a retomada
de um contrato que fixou outra implementação; o histórico fica preservado.

Migração aditiva v9 no SQLite existente, backup-first e transacional, persiste
sessões/eventos append-only ligados ao run/ciclo/job/operação oficiais, baseline,
papéis, contexto, fingerprints, findings, publicações, gates, uso e resolução.
Intenção e resultado de chamadas/publicação usam o journal idempotente anterior.
Não há nova fila nem outra máquina de estados para tarefas.

Cache de aprovação exige operação, estado atual, contexto, política, agentes e
implementação exatos. Dependências capturadas entram no fingerprint; mudanças
invalidam conservadoramente. Resultado durável não gera outra inferência.
Após crash com intenção sem resultado ou publicação não confirmada, retomada
preserva `unknown`, sem repetir executor ou aplicar correção novamente.
`review-status` é somente leitura e mostra chamadas pendentes. Reconciliação
automática de todas as fases incompletas ainda não foi implementada.

### Evidências e limites operacionais

O teste ponta a ponta usa CLIs simulados em subprocessos reais do LocalRuntime,
Git e SQLite reais e gates pytest de um projeto sintético: Antigravity planeja,
Gemini executa, Claude Code encontra o problema, Gemini devolve correção,
gates e nova revisão aprovam e Antigravity recebe o resultado. Repetição não
gera novas chamadas. Isto valida o protocolo e a integração, não homologa
inferência real de nenhum fornecedor.

Diagnóstico somente leitura em 2026-11-23: Codex 0.171.0 respondeu ao status de
login, mas não está nas versões homologadas pelo adaptador existente;
Antigravity 1.5.15 está acessível e anuncia flags headless, porém autenticação
permanece desconhecida por falta de interface suportada de verificação.
Gemini, Claude Code e OpenCode não foram encontrados. **Inferência real não
foi realizada**: não havia combinação com autenticação e suporte operacional
confirmados; as recusas de versão e os limites de autorização foram preservados.

Limitações deliberadas: lease cooperativo não é isolamento de filesystem;
processos externos podem escrever sem respeitá-lo. Publicação é atômica por
arquivo, não por conjunto/SQLite/Git. Janelas incertas bloqueiam sem rollback.
Arquivos binários, grandes, sensíveis conhecidos, links, submódulos e conflitos
exigem tratamento fora deste corretor. Renomeação com conteúdo alterado é
representada por remoção/adição. Correção de índice e restauração de removidos
não são automáticas. Contexto de outras linguagens é textual/local.
Sondagens usam a configuração restrita do provedor Git existente. Normalização
CRLF/LF dependente apenas de configuração global pode aparecer como alteração
preexistente; o motor bloqueia sem reescrever arquivos ou editar configuração.
Os repositórios sintéticos fixam `core.autocrlf=false` localmente para tornar
essa condição explícita e os resultados reproduzíveis.
Chamadas/bytes/segundos são métricas locais; não equivalem a tokens medidos ou
teto financeiro do provedor. Modos marco/sob demanda/adaptativo, reconciliação
de unknown, métricas verificáveis de tokens e homologação operacional controlada
são próximas prioridades. Worker independente, MCP e Communication Optimizer
não foram antecipados.

Validação em Windows/Python 3.13: rodada focada com **66 aprovados**, em
361,04 segundos; teste adicional de confidencialidade staged com **1 aprovado**.
Os dois testes adicionais de fonte ignorada passaram; a verificação final de
proteções/publicação e ponta a ponta terminou com **19 aprovados**, em 66,80s.
Comandos reproduzíveis:

```shell
.venv/Scripts/python.exe -m pytest -q tests/test_code_review_flow.py tests/test_git_change_tracker.py tests/test_execution_roles.py tests/test_architecture_boundary.py tests/test_phase5_schema.py --tb=short
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest -q tests/test_git_change_tracker.py tests/test_code_review_flow.py::test_real_runtime_end_to_end_review_correct_gates_re_review_and_orchestrator
```

Suíte geral final: **902 aprovados e 1 ignorado**, em **758,56 segundos**
(12min38s), sem falhas. A primeira rodada ampla também passou, com 900 aprovados
e 1 ignorado, antes dos dois novos testes de fonte ignorada. O auditor preserva
zero ciclos e as dez exceções anteriores; `tests/architecture_baseline.json` e
o protótipo não foram alterados. `git diff --check` passou. A proteção final
de publicação reutiliza também a política canônica de caminhos do provedor
existente; sua rodada focada acima inclui esse último ajuste.

## Incremento B — registro da entrega anterior

Referência: [ADR 0025](decisions/0025-execution-roles-and-delegation.md).
O primeiro incremento foi preservado. `agentflow start` descobre agentes e exige
orquestrador, executor e revisor antes de inferência. Em TTY há perguntas; sem
TTY há flags ou estado persistido `AWAITING_CONFIGURATION`, com opções em JSON.
O revisor sugere `same`, com a mesma seleção do orquestrador. Flags de papéis
em `run` também ativam o fluxo; o `run` legado permanece compatível.

Escolhas completas/parciais ficam no SQLite existente, com migração aditiva v8,
backup e rollback. Persistem agente/versão/API/executável, capacidades declaradas,
modelo pedido, disponibilidade e fonte. A configuração completa é imutável e
integra o digest da política do contrato local. Retomada não pergunta respostas
já registradas nem troca agentes/modelos; revalida as identidades. O manifesto
antigo de executor continua compatível.

O orquestrador selecionado usa o planejador existente, produz tarefas tipadas e
recebe o plano executável. O controlador APL anterior delega ao executor,
coleta recibos/handoffs e executa gates independentes. O resultado persistido e
as evidências voltam ao orquestrador antes da próxima tarefa. Decisões versionadas
`CONTINUE`/`BLOCK` são validadas e registradas; não dispensam gates nem ampliam
autorização. Mesmo agente em vários papéis usa processos headless separados.
Nesta entrega anterior, o revisor era apenas configurado; o incremento E–G
acima passa a executar a revisão incremental.

Padrão nativo é permitido sem atestação completa. Modelos manuais suportados podem
ficar `configured-unverified`; observação desconhecida é aceita nesse novo
caminho, divergência conhecida bloqueia. Gemini aceita modelo explícito com
provider `native`, sem alegar fornecedor efetivo. A API legada conserva a recusa
anterior. Codex conserva suas versões/limitações anteriores. Catálogo anunciado
ou configuração não comprovam acesso nem homologação real.

```shell
# Perguntas em TTY; espera persistida sem TTY:
agentflow start "Implementar o objetivo"

# Exemplo com CLIs instalados e acessíveis:
agentflow start "Implementar o objetivo" --orchestrator-agent antigravity --executor-agent gemini --reviewer-agent same --operation-id exemplo-b

# Configurar sem inferência, depois continuar com a mesma entrada/chave:
agentflow start "Implementar o objetivo" --orchestrator-agent antigravity --executor-agent gemini --configure-only --operation-id exemplo-b

# Modelo Gemini pedido explicitamente, sem alegar provider efetivo:
agentflow start "Outro objetivo" --orchestrator-agent antigravity --executor-agent gemini --executor-provider native --executor-model ID-DO-MODELO

agentflow apl run-status --path caminho/do/projeto --run-id ID
agentflow apl run-resume --path caminho/do/projeto --run-id ID
```

`--tasks-file`/`--plan-dir` continuam disponíveis. Sem objetivo, `start` usa
`TASKS.json` e o marco ativo. Repetir entrada/chave reutiliza a configuração;
outra entrada com a mesma chave é conflito. `run-status` mostra os papéis e
`orchestrator_seconds` nos ciclos configurados.

Intents são persistidos antes da inferência. Resultados confirmados são
reaproveitados; intents sem resultado permanecem ambíguos e não são reemitidos
automaticamente. Uma interrupção entre checkpoint e decisão não reexecuta a
tarefa. Versão/capacidade alterada ou decisão inválida bloqueia, preservando
evidências. A reconciliação de intents ambíguos ainda não tem liberação automática.

O teste ponta a ponta comprova planejamento, duas delegações, gates reais,
retorno ao orquestrador, cache e retomada com subprocessos locais reais que
simulam protocolos de agentes. Há cenários de mesmo agente em papéis diferentes,
bloqueio sem segunda delegação, gates falhos apesar de `CONTINUE`, concorrência,
modelos não atestados, migração/rollback e configuração parcial. Isso não homologa
inferência real. Nenhuma instalação, commit ou push no projeto foi realizado.

Validação final em Windows/Python 3.13: **869 aprovados e 1 ignorado**, em
408,47 segundos. A rodada focada de papéis/seleção/controlador/arquitetura passou
com **103 testes**; os contratos de descoberta/Gemini e demais regressões também
integram a suíte geral. São 20 casos novos de papéis, incluindo parametrizações.
O baseline permanece com dez exceções e zero ciclos; o protótipo legado não foi
alterado. `git diff --check` e os links locais da documentação passaram.

Reprodução da validação:

```shell
.venv/Scripts/python.exe -m pytest -q tests/test_execution_roles.py
.venv/Scripts/python.exe -m pytest -q
```

## Inventário do primeiro incremento e componentes reutilizáveis

O inventário estático cobre todos os módulos de `src/agentflow`, seus imports,
classes, funções, constantes de schemas e documentação. Na captura deste
incremento: 110 módulos Python e 81 documentos Markdown em `docs`. A inspeção
dos fluxos públicos e de suas suítes identifica os pontos abaixo. Inventário
estático não comprova comportamento de integrações reais nem auditoria semântica
exaustiva de todas as combinações de execução.

| Área | Implementação existente a reutilizar | Integração / lacuna |
| --- | --- | --- |
| Composição e CLI | `cli`, `config`, `agents.factory`, `init`, `environment.doctor` | Manter CLI histórica; acrescentar controles de execução com contratos comuns às futuras ferramentas MCP. |
| Agentes | `AgentAdapter`, `LifecycleAgentAdapter`, `ProcessSpec`, `AgentAvailability`, `AgentCapabilities`, `AgentRegistry` | Descoberta agregada adicionada; seleção de papéis ainda pendente. |
| Modelos | `ModelDescriptor`, `ProviderDescriptor`, `ModelSelectionRequest`, observações por tentativa | Reutilizar proveniência e disponibilidade; configuração não prova acesso. |
| Planejamento | `planning.loader`, `task_source`, `task_queue`, `master_plan`, `apl.initial_planning`, `codex_planner` | DAG, marcos e leitura por SHA já existem; separar escolha de planejador/orquestrador e executor. |
| Coordenação | `apl.controller`, `supervised`, `authorized_execution` | Estender APL; preservar identidades canônicas, quotas, fencing e limites. Não criar segundo coordenador em `core`. |
| Processos | `runtime.local`, `windows_job` e contratos de processo | Reutilizar stdin em thread, watchdog, cancelamento, identidade e logs. Sessão nativa e processo independente durável são requisitos diferentes. |
| Persistência | `state.sqlite`, `execution`, `git`, `validation`, `sdd`, `budgets`; stores APL no mesmo SQLite | Usar migrações compatíveis para novos papéis/revisões/métricas. Este incremento não altera schema ou transações. |
| Escolha atual | `execution_selection` e snapshots/observações SQLite por tentativa | O manifesto de executor é imutável e vinculado ao ciclo. Migrar/ampliar por execução sem inventar uma segunda fonte de autoridade. |
| Git | `GitLocalProvider`, `WorkspaceLock`, `GitCoordinator`, `GitStateStore` | Reutilizar worktrees, locks, staging isolado e reconciliação. Falta baseline de revisão HEAD+índice+working tree e atribuição conservadora dos deltas. |
| Aceite | `validation.scheduler`, snapshots, validators e `PlanGateAcceptanceVerifier` | Gates independentes já existem. Aprovação semântica do revisor não os substitui. |
| Recuperação | `apl.failure_recovery`, controller, estado de operações Git e processos | Reutilizar `unknown`, recuperação sem replay cego e limites por assinatura de falha. Correção de finding não equivale automaticamente a retry sem efeito. |
| Autorização avançada | `approval`, `security`, `teg`, `recovery` | Preservar contratos avançados opcionais. Broker/TEG não é servidor MCP e não deve virar pré-requisito de job local. |
| Contexto | Plano Mestre por digest/linhas, handoffs de dependências diretas, snapshots canônicos | Reutilizar referências; ainda faltam registries compartilhados, deltas e comparação por tokenizador. |
| Custos | Políticas de budget e roteamento em `agents.routing*`, `state.budgets`, limites APL | Separar estimativa de custo real e de hard cap do provider; faltam métricas completas do ciclo. |
| Legado | `autonomous-development-runner/` | Preservado integralmente; sem migração implícita. |

O auditor continua encontrando zero ciclos entre pacotes e as mesmas dez
exceções arquiteturais revisadas. A classificação de adaptadores concretos foi
ampliada para incluir Gemini; nenhuma exceção foi acrescentada ao baseline.

## Progresso por fase

| Fase | Estado verificável | Próxima saída necessária |
| --- | --- | --- |
| A | Inventário transversal e pontos de integração registrados; ADR 0024 | Continuar inspeção semântica específica antes de cada mudança de contrato. |
| B | Descoberta, três papéis, respostas parciais, SQLite v8, retomada e `AWAITING_CONFIGURATION` implementados | Ampliar sondagens suportadas de autenticação/catálogos e seleção de múltiplos executores. |
| C | Orquestrador selecionado planeja e decide sobre resultados delegados no APL existente | Replanejamento estratégico automático e escaladas mais amplas. |
| D | Gemini integrado, padrão nativo e modelo manual não atestado no novo fluxo | Sessões nativas recuperáveis e distribuição segura entre múltiplos executores. |
| E | Baselines HEAD/índice/working tree/untracked, diffs, renomeação exata, ambiguidade e fingerprint integrados ao APL | Ampliar suporte conservador a arquivos grandes/binários e reconciliação de conflitos, sem sobrescrever conteúdo alheio. |
| F | Contexto incremental, revisor selecionado, findings ancorados e estados persistidos no SQLite v9 | Ampliar análise de dependências de outras linguagens e homologação operacional. |
| G | Correção por hash, gates, nova revisão, limites, ausência de progresso e pendências ao orquestrador | Reconciliação segura de fases incompletas e consumo verificável por provedor. |
| H | Cache restrito à operação e identidades de conteúdo/contexto/contratos/configuração | Cache seletivo mais eficiente e integração semântica opcional. |
| I | Identidades de processos e recuperação existentes | Worker gerenciado independente, exclusão de instâncias e continuidade após desconexão. |
| J | Pendente | Servidor MCP stdio com protocolo e ferramentas de controle, usando o worker durável. |
| K | Handoffs e leitura seletiva existentes | Registries/dicionários/deltas, validação semântica e comparação de tokens por ciclo. |
| L | Políticas determinísticas de roteamento e budgets existentes | Aplicação por papel com disponibilidade verificada e sem troca silenciosa de seleção. |
| M | Suíte automatizada existente preservada e ampliada | Testes dos novos fluxos completos, concorrência, crash/restart e benchmarks mensuráveis. |

A descoberta é automática em `start` e no fluxo de papéis de `run`. A precedência
histórica de executor no `run` legado continua compatível e não representa
seleção de orquestrador.
Não há ferramentas MCP ou serviço autônomo independente disponíveis só por
existir o inventário. Revisão automática depende do fluxo de papéis configurado
com a política da ADR 0026.

## Uso entregue

```shell
agentflow agents --path caminho/do/projeto
agentflow agents --path caminho/do/projeto --format json
agentflow run --agent gemini "objetivo"
```

`agents` funciona sem repositório inicializado e não escreve configuração ou
estado AgentFlow. Só consulta versões dos adaptadores instalados. Adicionar um
adaptador confiável ao `AgentRegistry` permite inventariá-lo sem modificar o
serviço de descoberta. Não há carregamento automático de plugins externos.

O relatório distingue executável/versão, capacidades declaradas, autenticação
desconhecida, catálogo não suportado e modelo configurado. Os built-ins ainda
não enumeram modelos nesse fluxo. Um hook opcional `discover_models` deve usar
um mecanismo suportado e devolver `ModelDescriptor` com fonte e timestamp;
erros ou dados inválidos ficam inconclusivos, sem catálogo fabricado.

Gemini usa stdin não interativo e `stream-json`, sem mudar permissões, sandbox,
trust ou autenticação nativos. Denials e erros permanecem falhas; limites e
cancelamento pertencem ao runtime existente. Sem configuração manual, vale a
seleção nativa; aliases de `init.model` não são tratados como modelo efetivo
congelado. Só estatísticas que nomeiem um único modelo produzem uma observação
de modelo. Provider continua desconhecido. A fábrica legada recusa seleção
manual sem atestação; o novo fluxo de papéis admite modelo com provider `native`
e observação desconhecida explicitamente registrada, mantendo a recusa de
divergências e de seleção de provider que o CLI não suporta.

## Validação e continuidade

O checkpoint de progresso é este documento junto aos fontes e testes no working
tree; nenhum commit ou push é criado. A suíte anterior terminou com 815 testes
aprovados e 1 ignorado. A validação ampla deste incremento terminou com **848
aprovados e 1 ignorado**, em 376,96 segundos, no Windows/Python 3.13. Após o ajuste
final que permite recuperação de falhas de ferramenta na mesma sessão, os **44
testes focados** de descoberta/Gemini/arquitetura passaram novamente. O teste novo
de recuperação de ferramenta pertence a essa rodada focada. `git diff --check`
passou. A sondagem real somente de versões encontrou Antigravity 1.5.15 e Codex
0.171.0; este último permaneceu não homologado pelas regras existentes. Gemini,
Claude Code e OpenCode não foram encontrados no PATH. Testes simulados não
homologam agentes reais. Não foram iniciados
inferência real, Docker, instalações de dependências ou projetos de terceiros.

PRD/A–M permanece como escopo geral, sem antecipar essas implementações neste incremento.

## Checkpoint de recuperação (Worker Independente + MCP)

Recuperação iniciada em 2026-11-23 após interrupção do Codex CLI durante a implementação do Worker Independente + Servidor MCP.

### Inventário encontrado
- Arquivos modificados: `README.md`, `docs/architecture.md`, `docs/implementation-plan.md`, `src/agentflow/agents/factory.py`, `src/agentflow/apl/authorized_execution.py`, `src/agentflow/apl/codex_planner.py`, `src/agentflow/apl/contracts.py`, `src/agentflow/cli.py`, `src/agentflow/config.py`, `src/agentflow/environment/doctor.py`, `src/agentflow/state/audit.py`, `src/agentflow/state/inspection.py`, `src/agentflow/state/sqlite.py`, `tests/architecture_audit.py`, `tests/test_phase5_schema.py`.
- Arquivos untracked relevantes ao incremento atual: `src/agentflow/worker.py`, `src/agentflow/state/worker_schema.py`, `src/agentflow/mcp_server.py`.
- Verificação inicial: `tests/test_phase5_schema.py` apresentou 1 falha (`test_v4_reader_rejects_future_database_version_without_mutation`: `assert 11 == 10`).

### Lacunas e inconsistências identificadas na inspeção inicial
1. `managed_workers.state_db` concatenava `str(path) + "\0" + run_id` em vez de armazenar apenas o caminho do banco e usar coluna dedicada para o run ativo.
2. `worker.py` executava `main(args)` sincronamente no loop `_serve`, impedindo heartbeat e processamento de comandos `pause`/`cancel` durante execuções ativas.
3. `worker.py` acessava `status["run"]["apl"]` sem tratar runs ainda em `queued` (sem registro APL) e usava `sqlite3.connect(path)` sem `busy_timeout` em pontos do `_serve`.
4. `worker.launch` mantinha `BEGIN IMMEDIATE` aberto durante o spawn do subprocesso, causando deadlock de escrita no SQLite (`worker_start_timeout`).
5. `cli.py` tinha erro de indentação na linha 809 (`if args.apl_command == "run":`).
6. Faltavam testes ponta a ponta de worker independente + servidor MCP stdio com múltiplos clientes.

## Entrega Worker Independente + Servidor MCP (continuidade)

### Alterações realizadas
- `src/agentflow/state/worker_schema.py`: adicionada coluna dedicada `active_run_id TEXT` em `managed_workers` e restrição `CHECK` de estados válidos em `managed_worker_runs.state`.
- `src/agentflow/worker.py`:
  - Removida a concatenação `\0` em `managed_workers.state_db`; `state_db` guarda apenas o caminho do banco e `active_run_id` guarda o run ativo.
  - Liberada a transação `BEGIN IMMEDIATE` antes de iniciar o subprocesso em `launch()`, eliminando o deadlock no SQLite.
  - `_serve()` agora executa `start` e `resume` em `threading.Thread` para manter o loop principal ativo atualizando `heartbeat_at` e processando `pause`/`cancel`.
  - `_connect(path)` (com `busy_timeout=10000`) padronizado em todo o `_serve()`, e `inspect()`/`command()` tratam graciosamente runs ainda sem registro em `apl_authorized_runs`.
- `src/agentflow/cli.py`: corrigida a indentação em `if args.apl_command == "run":`.
- `tests/test_phase5_schema.py`: corrigida asserção de versão futura (`11` preservado sem mutação).
- `tests/test_worker_and_mcp.py`: adicionados testes reais com subprocessos para schema, idempotência de `worker.launch`, sobrevivência do worker após encerramento do cliente e handoff entre dois clientes MCP stdio distintos.

### Validação da Task-93 e limitações de ambiente
- `pytest -q tests/test_apl_security_boundary.py tests/test_architecture_boundary.py tests/test_worker_and_mcp.py`: **21 passed**.
- `pytest -q` (suíte completa): **905 passed, 1 skipped**, com 1 falha isolada de ambiente em `tests/test_codex_skill_integration.py::test_objective_run_persists_cycle_and_delegates_to_existing_apl_runner` (`Codex CLI não encontrado` no PATH).
- Sondagem real de agentes (`agentflow agents --format json`): `antigravity` 1.5.15 disponível com autenticação desconhecida; `codex`, `claude`, `gemini` e `opencode` ausentes no PATH. Inferência real não realizada.
- Próximo incremento pendente no plano: **Live Execution Observability** e **Communication Optimizer** (itens K/L da tabela A–M).

## Configuração Global de Agentes e Modelos (`init` + `config`)

- [global_config.py](src/agentflow/global_config.py): persistência global atômica em TOML (`%APPDATA%\AgentFlow\config.toml` no Windows; `${XDG_CONFIG_HOME:-~/.config}/agentflow/config.toml` no Linux), com suporte a orquestrador, worker padrão, limite de concorrência, política de distribuição, subagentes especializados, agentes adversariais, fallback e preferências, rejeição de segredos em texto puro e migração v0 -> v1.
- [cli.py](src/agentflow/cli.py): `agentflow init` detecta configuração global existente (oferecendo manter ou alterar em modo interativo, e reutilizando automaticamente com `--non-interactive` / `--keep-global`); adicionado `agentflow config` (`show`, `set`, `check`, `reset`, `wizard`).
- [test_global_config.py](tests/test_global_config.py): **5 passed**; combinado com `test_apl_security_boundary.py` e `test_architecture_boundary.py`: **22 passed** em 20,71s.
