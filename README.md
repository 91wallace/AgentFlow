# AgentFlow

AgentFlow é um orquestrador híbrido de desenvolvimento de software. O núcleo e a CLI rodam localmente, com planejamento TOML, estado SQLite e execução de tarefas por adaptadores de CLIs de agentes.

## Uso local

`agentflow start` descobre agentes e pede orquestrador, executor e revisor por
execução. O revisor sugere usar o orquestrador. Escolhas e respostas parciais
ficam no SQLite para retomada; sem TTY, informe flags de papéis ou configure
posteriormente a execução em `AWAITING_CONFIGURATION`. Veja [uso e progresso](docs/autonomous-platform-progress.md).

No `run` legado, a seleção segue `--agent` → `--invoker-agent` informado pela integração → padrão
do projeto → Codex. Antigravity, OpenCode e Claude Code usam perfis próprios de
provider/modelo opcionais; sem eles, vale o padrão nativo do CLI. A retomada mantém o executor do job mesmo quando chamada por
outro agente. Veja [configuração e exemplos](docs/decisions/0014-invoker-agent-selection.md).

Requer Python 3.11+, Git e o CLI do agente escolhido para planejamento/execução. `init` usa Codex por padrão, ou a integração informada, e não executa tarefas. Não é necessário configurar perfil de segurança, senha AgentFlow ou WebAuthn. Docker é opcional; o fluxo `run` é local.

```shell
python -m pip install .
cd caminho/do/repositorio-git
agentflow init
agentflow doctor
agentflow agents --format json # inventário sem iniciar inferência
agentflow start # seleciona papéis; lê TASKS.json
agentflow run # lê TASKS.json da raiz; ou: agentflow run "<objetivo>"
```

`agentflow init --path caminho` inicializa a raiz do repositório Git que contém o caminho. Cria `.agentflow/config.toml` apenas quando ausente e preserva configurações existentes. Veja [instalação e uso](docs/installation.md) para esquema TOML, testes e códigos de saída.

`agentflow agents` consulta versões dos CLIs registrados e separa instalação,
capacidades declaradas, autenticação desconhecida e modelo configurado. Gemini
pode ser selecionado com `--agent gemini`, usando o padrão nativo do CLI. O novo
fluxo de papéis aceita modelo Gemini explícito como configuração não atestada,
com provider `native`, e rejeita divergências conhecidas. Veja [limites e progresso da evolução autônoma](docs/autonomous-platform-progress.md).

Novos pedidos com `agentflow start` incluem revisão Git incremental pelo revisor
selecionado. Correções fundamentadas voltam ao executor, passam por publicação
guardada por hashes, testes e nova revisão. Alterações preexistentes no escopo
bloqueiam o dispatch para preservá-las. Use `--review-mode off` para configurar
um pedido sem revisão, ou `--review-max-corrections 2` para limitar correções.
Pedidos antigos mantêm a política persistida. `agentflow apl run-status` mostra
o `operation_id` da tarefa; `agentflow review-status --path caminho --operation-id ID`
consulta baseline, eventos, resultados e operações pendentes. Consulte os
[contratos e limites de recuperação](docs/decisions/0026-git-aware-review-and-corrections.md).

`agent = "none"` legado usa Codex sem editar o arquivo. `agentflow run --tasks-file backlog.json` aceita outro backlog. O JSON deve conter uma lista de textos/objetos de tarefa ou um objeto com `tasks` (até 12 tarefas). O planejador deriva escopos e verificações, preservando IDs/dependências explícitos. Cada prompt do loop cobre somente a tarefa atual e pede resposta concisa. A execução local usa os acessos do host; requisitos de login do próprio Codex continuam aplicáveis. Detalhes e limites na [ADR 0011](docs/decisions/0011-local-default-agent-and-backlog.md).

## Planejamento

O Plano Mestre não tem teto de tamanho. `plan_sections` no milestone seleciona
recortes; sem seleção ou com recortes extensos, o planejador recebe um índice
paginado e pode pedir linhas verificadas pelo SHA-256. O runtime mantém stdin
em thread e watchdog de silêncio de 300s (`--idle-seconds 0` desativa).
Veja [contexto progressivo](docs/decisions/0016-progressive-master-plan-and-responsive-stdin.md).

O fluxo em duas etapas usa um Plano Mestre `.md` para objetivo, limites,
invariantes, marcos verticais e seus critérios de saída. Depois, `TASKS.json`
organiza nós `milestone` e tarefas `work` com `goal`, `acceptance`, `parent_id`
e `depends_on`. Apenas o marco ativo precisa estar detalhado. Consulte o
[schema e exemplo](docs/decisions/0012-master-plan-and-milestone-queue.md).

- [Arquitetura](docs/architecture.md)
- [Plano e fases](docs/implementation-plan.md)
- [Decisão de arquitetura híbrida](docs/decisions/0001-hybrid-architecture.md)

O ZIP `autonomous-development-runner-v0.1.zip` e seu diretório extraído são um protótipo legado `adr`, preservado como referência. Eles não fazem parte do pacote `agentflow` da raiz.

Licença: [Apache 2.0](LICENSE).

O loop usa [handoff JSON minificado](docs/decisions/0013-dependency-handoffs.md), persistido no outcome, somente para dependências diretas. Não há corte nem teto artificial em bytes de JSON; gates independentes permanecem obrigatórios.
