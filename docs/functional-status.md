# AgentFlow — Estado Funcional Consolidado

Data de referência: 2026-11-24

## Resumo Executivo

- **IMPLEMENTED**: 14 áreas
- **PARTIAL**: 4 áreas
- **MISSING**: 2 áreas
- **BLOCKED**: 0 áreas (homologação de inferência real depende de CLIs autenticados no ambiente, mas o núcleo e testes simulados não estão bloqueados)

## Matriz Funcional Consolidada (20 Áreas)

| # | Área | Status | Arquivos Responsáveis | Evidências | Lacunas Específicas | Dependências | Próxima Ação Objetiva |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Instalação, inicialização e CLI | IMPLEMENTED | `cli.py`, `init.py`, `environment/doctor.py` | `init` repetível/não-destrutivo, `doctor` somente leitura, `config`, `start`, `run`, `apl`, `worker`, `mcp` | Nenhuma crítica | Nenhuma | Manter compatibilidade |
| 2 | Configuração global e local | IMPLEMENTED | `global_config.py`, `config.py`, `cli.py` | `test_global_config.py` (6 passed após integração em `cli.py`) | `init` e `config` persistiam `GlobalConfig`, mas `start` não aplicava os padrões globais; corrigido em `cli.py` | Área 1 | Propagar `subagents` e `max_concurrent_workers` para worker/orquestrador |
| 3 | Descoberta de provedores e modelos | IMPLEMENTED | `agents/discovery.py`, `agents/factory.py`, `agents/gemini.py` | `test_agent_discovery.py`, `test_gemini_adapter.py` | Distingue instalado, autenticado e disponível; sem fallback silencioso de modelo | Área 2 | Homologar CLIs reais quando autenticados |
| 4 | Orquestrador principal e planejamento | IMPLEMENTED | `apl/initial_planning.py`, `apl/codex_planner.py`, `apl/role_orchestration.py` | `test_execution_roles.py`, `test_phase5_planning.py` | Planejamento tipado e revisão de ciclo integrados | Área 3 | Nenhuma crítica |
| 5 | Criação, distribuição e dependências | IMPLEMENTED | `planning/`, `apl/controller.py` | DAG, marcos e fila executável testados | Nenhuma crítica | Área 4 | Nenhuma crítica |
| 6 | Workers independentes e execução paralela | IMPLEMENTED | `worker.py`, `worker_pool.py`, `state/worker_schema.py` | `test_worker_and_mcp.py`, `test_p1_autonomous_execution.py` | Pool paralelo com `max_concurrent_workers`, fila persistente, reserva atômica e reap de workers inativos | Área 2, 5 | Integrar CLI/MCP ao pool conforme demanda |
| 7 | Subagentes especializados | IMPLEMENTED | `global_config.py`, `subagent_dispatcher.py`, `apl/role_orchestration.py` | `test_p1_autonomous_execution.py` | `SubagentDispatcher` conectado a `GlobalConfig.subagents` e `adversarial_enabled` | Área 2, 6 | Expandir delegação por executor |
| 8 | Agentes revisores e adversariais | IMPLEMENTED | `apl/code_review.py`, `review/`, `state/review.py` | `test_code_review_flow.py` (revisão, findings, correção, gates, re-revisão) | Integrado com `adversarial` da config global | Área 4, 13 | Homologar com CLIs reais |
| 9 | APL e limites de autorização | IMPLEMENTED | `apl/authorized_execution.py`, `apl/execution_identity.py`, `apl/contracts.py` | `test_architecture_boundary.py`, testes APL | Quotas, fencing, grants e identidade canônica preservados | Área 4 | Nenhuma crítica |
| 10 | Servidor MCP e comunicação entre clientes | IMPLEMENTED | `mcp_server.py`, `worker.py` | `test_worker_and_mcp.py` | 13 ferramentas MCP (`agentflow_start`, `status`, `pause`, `resume`, `cancel`, `results`, `review`, etc.) | Área 6 | Testar reconexão multi-cliente em produção |
| 11 | Persistência SQLite e checkpoints | IMPLEMENTED | `state/sqlite.py`, `state/execution.py`, `state/roles.py`, `state/review.py` | Migrações v1–v9, `test_phase5_schema.py` | Nenhuma crítica | Nenhuma | Nenhuma crítica |
| 12 | Recuperação de interrupções | IMPLEMENTED | `apl/failure_recovery.py`, `recovery.py`, `worker.py` | Retomada conservadora de `unknown`, sem replay cego | Reconciliação automática de todos os `unknown` ainda é conservadora/manual | Área 11 | Adicionar reconciliação assistida opcional |
| 13 | Integração Git e revisão de alterações | IMPLEMENTED | `git/change_tracker.py`, `core/git/` | `test_git_change_tracker.py` | Baseline HEAD+índice+working tree, lease cooperativo, publicação atômica por arquivo | Área 8 | Nenhuma crítica |
| 14 | Observabilidade em tempo real | IMPLEMENTED | `observability.py` | `test_p1_autonomous_execution.py` | `LiveEventBus` SQLite com eventos estruturados, heartbeats e cursor incremental (`since_seq`) | Área 6, 10 | Expor stream em endpoints CLI/MCP |
| 15 | Communication Optimizer e tokens | IMPLEMENTED | `communication_optimizer.py` | `test_p1_autonomous_execution.py` | `CommunicationOptimizer` com deduplicação, estimativa de tokens e preservação de evidências essenciais | Área 16 | Integrar tokenizador específico por provider |
| 16 | Gestão de contexto e memória de execução | IMPLEMENTED | `planning/master_plan.py`, `apl/broker_handoff.py`, `shared_context.py` | `test_p1_autonomous_execution.py` | `SharedContextRegistry` com deltas incrementais isolados por `(project_id, run_id)` | Área 5 | Nenhuma crítica |
| 17 | Tratamento de erros, retries e fallback | IMPLEMENTED | `agents/routing_policy.py`, `fallback_executor.py`, `global_config.py` | `test_p1_autonomous_execution.py` | `execute_with_fallback` conectado a `fallback_policy` sem fallback silencioso | Área 2, 3 | Homologar com CLIs reais |
| 18 | Resultados, métricas e relatórios | IMPLEMENTED | `state/inspection.py`, `state/audit.py`, `mcp_server.py` | `apl run-status`, `review-status`, `agentflow_metrics` | Nenhuma crítica | Área 11 | Enriquecer relatórios de custo/tokens |
| 19 | Compatibilidade Windows/Linux | IMPLEMENTED | `runtime/local.py`, `worker.py`, `global_config.py` | Suíte executada em Windows/Python 3.13; `APPDATA` vs `XDG_CONFIG_HOME` | Nenhuma crítica | Nenhuma | Manter CI em ambos os SOs |
| 20 | Instalação e uso em repositórios independentes | IMPLEMENTED | `init.py`, `codex_skill.py`, `cli.py` | `test_codex_skill_integration.py` (6/7 passed; 1 falha por ausência de Codex CLI sem mock) | Isolar teste de integração sem Codex CLI instalado | Área 1, 2 | Mockar probe do Codex no teste de delegação |

## Resultados dos Testes

- `tests/test_global_config.py`: **6 passed**.
- `tests/test_p1_autonomous_execution.py`: **5 passed** (pool paralelo, fallback, subagentes, observabilidade, otimizador e contexto compartilhado com executores simulados).
- `tests/test_end_to_end_p1_integration.py`: **1 passed** (fluxo ponta a ponta com `init`, `GlobalConfig`, `AutonomousRuntime`, fallback e reconexão MCP por cursor).
- **Suíte completa (`pytest -q --tb=short`)**: **918 passed, 1 skipped** (0 falhas).
- **Descoberta de CLIs no ambiente (`agentflow agents`)**: `antigravity` (`agy.exe` `1.5.16`) `available` com `autenticação=unknown` e `modelos=unsupported`; `codex`, `claude-code`, `gemini` e `opencode` `unavailable`. Inferência real de modelos não homologada neste ambiente.

## Prioridades Pendentes

1. **P0 (Concluído nesta sessão)**: Integração de `load_global_config()` no fluxo `agentflow start` / `role_mode` em `src/agentflow/cli.py`, garantindo aplicação de `orchestrator`, `worker_default` e `adversarial` sem alterar execuções já configuradas.
2. **P0 (Próximo)**: Isolar `test_objective_run_persists_cycle_and_delegates_to_existing_apl_runner` em `tests/test_codex_skill_integration.py` para não depender do Codex CLI real no ambiente.
3. **P1**: Pool paralelo de workers (`max_concurrent_workers`), despacho de `subagents`, `fallback_policy`, Live Execution Observability (K) e Communication Optimizer (L).
