# ADR 0007: Spec-Driven Development transversal

Status: **princípios aprovados; execução do gate F7-01 documentada no ADR 0008 e bloqueada até aprovação independente isolada**.

## Contexto

AgentFlow versiona `.agentflow/spec.toml` e `.agentflow/tasks.toml`, valida schemas e DAG, calcula digests canônicos e mantém revisões imutáveis de definições de tarefa. Tentativas, workspaces/checkpoints, gates/evidências e seleções de modelo já possuem vínculos de revisão. Ainda faltam uma revisão imutável first-class da especificação inteira, aceite autenticado explícito, rastreabilidade requisito→critério de tarefa e análise de impacto/replanejamento abrangentes. O diagnóstico e os critérios executivos estão em `docs/implementation-plan.md`.

## Contratos normativos

Todos os snapshots usam o algoritmo de canonicalização estabelecido pelo ADR 0002 para conteúdo TOML validado: JSON UTF-8 canônico e SHA-256. Identidade de revisão, digest e sequência são campos distintos. Snapshots aceitos são append-only; não há update/delete operacional de seu conteúdo.

### `SpecificationRevision`

Identifica a revisão integral de `spec.toml`: `revision_id` estável e único no projeto, sequência monotônica, `project_id`, versão de schema, snapshot canônico, `spec_digest`, revisão anterior, provenance Git quando disponível e estado (`proposed`, `accepted`, `rejected`, `superseded`). Proposta registra operação/proponente; aceite ou rejeição é evento separado e referencia digest do snapshot e do ChangeImpact. O conteúdo da revisão aceita nunca muda.

### `PlanRevision`

Combina uma `SpecificationRevision` aceita com o snapshot integral de `tasks.toml`, schema version, `tasks_digest`, `plan_revision_id`, DAG validado, mapa `RequirementTrace`, relatório de cobertura e revisão de plano anterior. O estado só é `accepted` se todas as referências forem válidas e cada requisito estiver coberto ou tiver adiamento explicitamente justificado e aceito. `legacy_unmapped` não satisfaz cobertura estrita.

### `RequirementTrace`

Relaciona `requirement_id` (ID do critério/requisito em `spec.toml`) a um ou mais pares (`task_id`, `task_criterion_id`) em `tasks.toml`. A representação TOML candidata é `satisfies = ["requirement-id"]` no critério da tarefa; F7-01 fixa versão/chaves sem criar terceiro arquivo versionado. Referência inexistente, duplicada ambígua ou ciclo é erro. Cobertura nunca é inferida por descrição, título ou IDs coincidentes. Adiamento contém motivo, escopo afetado e aceite explícito associado à revisão.

### `ChangeImpact`

Imutavelmente identifica revisão aceita de origem e revisão proposta de destino, digests, diff semântico e inventário do estado operacional observado. Produz conjunto determinístico de requisitos/critério/tarefas alterados, dependentes transitivos no DAG e artefatos derivados relacionados: decisões de modelo, budgets, tentativas, assignments futuros, workspaces, checkpoints, gates, evidências e builds. Cada elemento recebe disposição (`unaffected`, `revalidate`, `replan`, `stale`, `unknown`). O digest inclui snapshots e IDs do inventário usado; mesmo input produz mesmo resultado. Incerteza ou elo ausente fica `unknown`, nunca é tratado como não afetado.

### `RevalidationDecision` e `Replan`

`RevalidationDecision` registra revisão antiga/nova, ChangeImpact digest, tarefas/critério afetados, operação requerida, motivo, principal autenticado, autorização e disposition. Não verifica a evidência nem altera seu resultado histórico. `Replan` é novo `PlanRevision` proposto, derivado da nova spec e tasks, com mapeamento de tarefas substituídas/retidas/decompostas, DAG revisto, cobertura completa/adiamentos e justificativas. Mudança em requisito, critério, dependência ou escopo executável exige replan ou decisão explícita de não impacto verificável antes de nova execução. Histórico incompatível permanece preservado e marcado stale; evidência não é promovida automaticamente.

## Aceite, identidade e autorização

Agente/adaptador pode propor texto ou revisão e fornecer análise, mas não pode aceitar/rejeitar requisito, aprovar ChangeImpact/Replan, emitir grant, nem produzir uma declaração interpretada como aceite humano. Conteúdo de agente, saída de processo, commit, variável de ambiente e campo `author` autodeclarado nunca autenticam aprovador.

Aceite requer ação deliberada por principal operador autenticado através do plano de controle do AgentFlow, fora do contexto/processo/workspace e credenciais do agente. O `ApprovalGrant` de uso único vincula principal autenticado, ação, projeto, `revision_id` e digest, `PlanRevision`/digest, ChangeImpact digest, escopo exato, `operation_id`, expiração e nonce. O verificador confirma assinatura/credencial pelo provider de identidade configurado, validade, escopo e consumo único; o registro durável guarda identidade/provedor/método/assurance e ID/digest do grant, nunca segredo ou token bearer.

O requisito arquitetural mínimo é autenticação independente com prova de presença/consentimento do operador e grant inacessível ao runtime/adaptador. Uma API/capability de aprovação não é exposta ao agente. Se plataforma/provedor de identidade não comprovar principal e consentimento independente, o aceite fica `unavailable` e revisão/execução dependente permanece bloqueada; não se rebaixa para nome de usuário ou prompt do agente. F7-01 fixa os providers/plataformas homologáveis. Operações administrativas que removam revisão ou alterem estado operacional não substituem o aceite.

## Mudanças, transações, idempotência e recuperação

A leitura TOML produz snapshot candidato e proposta; import, reopen e inspection não aceitam. Aceite só ocorre após validar que os digests observados ainda correspondem à proposta e ChangeImpact. SQLite grava proposta, impacto, decisão/grant consumido, revisão/plano aceito, `operation_id` e eventos em transação `BEGIN IMMEDIATE`; operações são curtas e não incluem subprocessos. Mesmo `operation_id` e request digest devolve o resultado persistido; divergência é conflito.

SQLite e filesystem/Git não formam transação distribuída. Revisões aceitas são conteúdo fechado no banco, referenciado por digests e provenance disponível. Mudança posterior dos TOMLs cria drift/proposta; não altera a revisão aceita. Crash antes do commit deixa operação ausente/incompleta reconciliável por `operation_id`; crash depois permite ler resultado durável. Estado ambíguo bloqueia replay e requer reconciliação. Migração schema v5→v6 é esperada para persistir revisões, traces, impactos e aceite; será aditiva, backup-first, transacional, com integrity/FK checks e rollback sem reinterpretação de histórico. Versão/tabelas finais são fixadas no inventário F7-01.

## Compatibilidade TOML e fases anteriores

`.agentflow/spec.toml` e `.agentflow/tasks.toml` continuam os únicos arquivos de definição versionados. Schema evolution é explícita e rejeita versão futura/chaves incompatíveis; nenhum rewrite in-place ocorre. Projetos existentes sem links `RequirementTrace` continuam legíveis como `legacy_unmapped`, com o comportamento manual atual, mas não podem declarar cobertura completa nem habilitar funcionalidades F7 que dependam dela até migração explícita, análise/aceite. Novos campos não mudam a semântica antiga.

F2 continua autoridade para DAG, revisão de tarefa, transições e idempotência; F3 para execução/recovery; F4 para escopo Git/checkpoints; F5 para validação independente e evidências exatas; F6 para adapters e seleção explícita. F7 acrescenta proveniência SDD e roteamento, mas não altera snapshots passados, não cria workers paralelos, não faz entrega e não conclui por roteamento, execução ou commit.

## Revisão de compatibilidade dos ADRs

- **ADR 0001:** núcleo modular e adapters substituíveis permanecem; catálogo/router dependem de contratos, nunca de classes concretas de provider.
- **ADR 0002:** os dois TOMLs versionados, canonicalização SHA-256, DAG, `operation_id`, SQLite transacional e snapshots de tarefas são reutilizados. Os campos de autor das operações F2 continuam autodeclarados; não são retroativamente elevados a identidade autenticada. SDD acrescenta snapshots/aceite sem alterar seu significado histórico.
- **ADR 0003:** execução continua explícita, limitada e sem retry automático de resultado desconhecido. Roteamento não inicia o agente e cada tentativa segue os limites e capacidades reais do runtime.
- **ADR 0004:** checkpoints continuam locais, pathset explícito e verificados; uma revisão ou decisão de modelo não autoriza Git nem altera checkpoints. Mudança de especificação só altera `.agentflow/spec.toml`/`tasks.toml` por revisão aprovada e operação Git já autorizada.
- **ADR 0005:** validação permanece independente; resultado de processo/router não é evidência de aceite. Evidências antigas continuam imutáveis e não servem para checkpoint/revisão divergente; build, entrega e conclusão seguem separados.
- **ADR 0006:** `AgentAdapter`, provider, modelo e seleção por tentativa mantêm identidades separadas; seleção manual e Codex legado persistem. A policy inerte da F6 só ganha avaliação/roteamento em F7; F6 não recebe fallback, escalation nem retry retroativos.

Não foi identificado conflito que exija relaxar ADR anterior. As mudanças são aditivas; a migração proposta não altera dados históricos ou credenciais.

## Fronteira das Fases 7–9

- **Fase 7:** seleciona modelo para tentativa autorizada, com classificação/catálogo/budgets, decisão auditável e possível proposta de escalonamento após evidência. É serial; sem fallback silencioso ou retry de resultado desconhecido.
- **Fase 8:** workers independentes recebem revisões/requisitos aceitos e assignment imutável; começa sequencial, paralelismo apenas após gate próprio.
- **Fase 9:** coordena propostas especializadas e integração, preservando replanejamento e aceite explícitos.

Para o desenho de enrollment/consentimento WebAuthn, grants de uso único, armazenamento, suporte Windows/Linux e bloqueio do runtime atual, ver [ADR 0008 — Fase 7](0008-phase-7-routing-and-approval.md). WebAuthn autentica a ceremony; não substitui uma UI confiável nem prova que detalhes da transação foram compreendidos.

## Riscos e consequências

Não há transação atômica entre TOML, SQLite e Git; digest/snapshot e reconciliação preservam a fonte de verdade operacional, mas uma edição externa concorrente precisa ser detectada como drift. Os provedores de identidade disponíveis diferem por Windows/Linux e podem não oferecer prova forte sem serviço interativo; nesses ambientes o fluxo de aceite permanece indisponível. `ChangeImpact` só pode garantir determinismo sobre os vínculos persistidos conhecidos; dependências indiretas desconhecidas devem permanecer `unknown` e bloquear conclusão segura. A granularidade dos IDs de requisito deve ser suficiente sem tornar especificações simples excessivamente extensas.

## ADRs relacionados

- [ADR 0002 — contratos e estado da Fase 2](0002-phase-2-state.md)
- [ADR 0003 — política de execução local](0003-local-execution-policy.md)
- [ADR 0004 — recuperação e Git](0004-phase-4-recovery-git.md)
- [ADR 0005 — validação independente e evidências](0005-phase-5-validation-delivery.md)
- [ADR 0006 — adapters e seleção de modelos](0006-agent-ecosystem.md)
- [Plano executivo da Fase 7](../implementation-plan.md#fase-7--intelligent-model-routing-planejada-não-implementada)
