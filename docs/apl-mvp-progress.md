# MVP Developer Preview v0.1.0 — checkpoint local

## MVP-01 — integração

**Registro histórico do MVP-01.** Naquele checkpoint, `agentflow execute` exigia configuração explícita do sandbox. A política vigente, atualizada em 2026-11-14, removeu a seleção/validação de sandbox do AgentFlow: o Codex usa sua configuração nativa, enquanto os gates operacionais próprios continuam aplicados. O restante deste checkpoint preserva o estado histórico das fases e seus bloqueios.

## MVP-02 — CLI

MVP-04 adiciona modo Developer supervisionado: `agentflow apl start --profile developer --objective ... --scope ...` cria um ciclo a partir do plano atual e persiste digests spec/tasks, `next` só exibe uma proposta, `submit-result --attempt-id ...` associa resultado já persistido e validado, e `pause`, `resume`, `cancel` controlam o ciclo sem iniciar processos. Permanecem `status`, `history` e `inspect` como consultas. Escopo é obrigatório; caminhos declarados de escrita precisam estar incluídos. Tarefas legadas sem pathset exigem token lógico `task:<id>`, que só autoriza a proposta no Developer Mode e não concede permissão ao executor.

## MVP-03 — homologação

Testes determinísticos usam `ExecutionStore` existente com tentativa sintética, evidência `verified` persistida e evento de validação. A associação consulta a lista de evidências do evento validado e é única por tentativa em `apl_result_refs`; snapshots usam revisão CAS. Não homologa provider real, prova humana de evidência, concorrência interprocesso sob falhas de energia nem execução APL automática. A suíte completa deve ser executada após cada avanço autorizado.

## MVP-05 — replanejamento semântico supervisionado

Contratos `PlannerAdapter`, `ReplanContext`, `PlanProposal`, `TaskChange`, `ValidatedResult`, `EvidenceReference` e `RemainingLimits` implementados. SQLite persiste `apl_plan_revisions`, `apl_plan_proposals` e `apl_proposal_events`; payloads/revisões/eventos são append-only, propostas vinculam revisão do plano e revisão CAS do ciclo. Replanejamento CLI usa somente baseline determinístico sem alterações e declara que inferência semântica não está disponível. Validação protege scope, DAG, IDs, critérios, resultados prévios, evidências e bloqueios. Aceite é explícito e transacional; nunca executa job nem altera TOML versionado.

## MVP-06 — verificação do planejador local

**BLOCKED para adapter semântico real.** Codex CLI local 0.167.0 expõe execução de comandos e não fornece na interface verificada uma allowlist de ferramentas que prove ausência de shell/escrita/subagentes/MCP/efeitos externos ou isolamento de credenciais. A versão também diverge do `CodexAdapter` homologado (0.166.0). Não foi iniciada uma sessão Codex nem alterada configuração/sandbox. `replan` exige seleção explícita `--planner deterministic`; não existe escolha `semantic`. Decisão e referências em `docs/apl-mvp06-capability-review.md`.

`proposal_from_mapping` decodifica saída não confiável com schema estrito; resposta validada ainda passa por `validate_proposal`. Testes simulados cobrem saída válida/malformada/command-shaped, escopo/evidência inválidos, falha, timeout, cancelamento e ausência de planejador seguro. Isso não comprova confinamento de um processo semântico real.

## MVP-08 — Restricted Inference Harness

Contratos públicos tipados e validação JSON limitada foram adicionados em `agentflow.apl.restricted_worker`. Harness não inicia processos e retorna `BLOCKED`: não existe backend OS que comprove identidade restrita, filesystem/segredos inacessíveis, rede negada, limites e término. Job Object existente fornece apenas controle de árvore, não sandbox completo. Testes MVP-08 comprovam validação e bloqueio fail-closed; cenários adversariais são recusados antes de dispatch e não são evidência de negação pelo OS. Windows não está homologado; Linux não foi executado (WSL ausente). Ver `docs/apl-mvp08-restricted-inference-harness.md`.

## MVP-13 — retomada funcional local (checkpoint parcial)

Adicionado adapter Codex CLI experimental opt-in para gerar apenas propostas APL, com entrada/saída limitada, timeout, JSON estrito, validação existente e persistência append-only/idempotente de pedidos. Testes simulados do adapter e APL focado passaram (61). Codex CLI instalado é 0.167.0 versus 0.166.0 homologado no executor; o opt-in experimental permite investigação, não constitui homologação. Nenhuma inferência real foi chamada. Dispatch automático pelo APL permanece bloqueado pela autorização Fase 7/Broker/TEG e não foi contornado. MVP-13 parcial; detalhes em `docs/apl-mvp13-windows-local-functional-homologation.md`.

## MVP-13.1 — chamada real Codex (bloqueada)

Preflight de 0.167.0 passou; corrigida posição de opção global `--ask-for-approval`. Um processo real com objetivo sintético e workspace temporário terminou antes da inferência porque o CLI recusou a pasta como não confiável. Sem proposta/model output; ciclo sintético registrou started/failed, zero propostas, zero revisões novas e zero tentativas de execução. Não foi ignorado o gate nem alterada a configuração global. Testes focados 71 e suíte completa 443 passaram. Parecer `BLOCKED`; relatório em `docs/apl-mvp13-1-real-codex-planner-homologation.md`.

## MVP-13.2 — planner real recuperado

Registro histórico MVP-13.2: workspace Git sintético sem remotes, com trust override de invocação, produziu uma proposta real Codex 0.167.0 persistida e validada; nenhum job foi executado. **Política vigente 2026-11-14:** esse `--config` de confiança foi removido. Ver a nota de manutenção em `docs/apl-mvp13-2-real-codex-planner-recovery.md`.

## Uso seguro e diagnóstico

Instalação de desenvolvimento: `python -m pip install -e ".[test]"`; inicialize o projeto com `agentflow init` e carregue definições com os comandos de planejamento existentes. Exemplo de ciclo com três tarefas sem executar nada pelo APL:

```powershell
agentflow apl start --path . --profile developer --objective "Concluir a mudança aprovada" --scope task:first --scope task:second --scope task:third
agentflow apl next --path . --cycle-id <id>
# Separadamente, uma operação de execução existente e explicitamente autorizada produz tentativa/evidência.
agentflow apl submit-result --path . --cycle-id <id> --attempt-id <attempt-id>
agentflow apl next --path . --cycle-id <id>
agentflow apl pause --path . --cycle-id <id>
agentflow apl resume --path . --cycle-id <id>
agentflow apl cancel --path . --cycle-id <id>
agentflow apl status --path .
agentflow apl history --path . --cycle-id <id>
agentflow apl inspect --path . --cycle-id <id>
```

Um resultado associado deve corresponder ao próximo job, ter status do processo `process_succeeded`, validação `validation_passed` e evidências `verified` para todos os critérios. Texto livre nunca é aceito como evidência. `unknown` bloqueia e não pode ser retomado. Ciclos não iniciam providers. Para consultar ciclo persistido: `agentflow apl status --path .`; `agentflow apl history --path . --cycle-id <id>`; `agentflow apl inspect --path . --cycle-id <id>`.

Para execução de tarefa individual continuam valendo escopo, aprovação, timeout e validação documentados por `agentflow execute`; sandbox pertence à configuração nativa do CLI. APL não transforma essa operação em autorização para loop. Limite de custo APL é custo reportado pelo adaptador, não teto financeiro garantido por provider. Ciclos desconhecidos/ambíguos permanecem bloqueados. Fase 7, Broker/TEG operacional, providers reais via APL e release permanecem não homologados.

**Política vigente de adapters (2026-11-14):** AgentFlow não injeta `--sandbox` ou `--config` no Codex e não exige sandbox genérico para outros adapters. Registros históricos MVP-13.1/MVP-13.2 documentam argumentos usados naquela homologação; estes não representam a política atual.
