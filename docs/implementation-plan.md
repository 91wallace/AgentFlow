# Plano de implementação

Fatia autorizada: [ADR 0016](decisions/0016-progressive-master-plan-and-responsive-stdin.md),
Plano Mestre sem teto de tamanho, hash streaming, plan_sections/índice paginado,
recuperação por linhas no planejador sintético e stdin assíncrono com idle de
300s configurável. Limites de contexto não rejeitam nem truncam o documento.

Correção de seleção: [ADR 0015](decisions/0015-native-cli-model-defaults.md).
Perfis provider/modelo deixam de ser obrigatórios; planejamento e execução
aceitam o padrão nativo do CLI e registram esse modo, distinto de seleção manual.

Fatia de seleção autorizada: [ADR 0014](decisions/0014-invoker-agent-selection.md),
config v2 compatível com v1, identidade explícita da integração, composição dos
quatro adaptadores, planejamento selecionado/plano pronto e retomada vinculada
ao executor persistido. Homologação real permanece separada dos testes sintéticos.

Extensão de continuidade: [ADR 0013](decisions/0013-dependency-handoffs.md),
handoff JSON minificado persistido no outcome, dependências diretas no prompt,
sem tetos artificiais de tamanho de JSON e sem substituir gates independentes.

## Fatia autorizada em 2026-11-20 — entrada local simplificada

Conforme [ADR 0011](decisions/0011-local-default-agent-and-backlog.md): Codex
padrão no `init`, compatibilidade não destrutiva com `none`, retirada dos
pré-requisitos de perfil/autenticação local, leitura automática de `TASKS.json`
no `run` sem objetivo e prompts concisos de uma tarefa por vez. CLI antiga de
enrollment/consentimento local fica inativa com diagnóstico de migração.
Contratos, limites, evidências e recuperação existentes permanecem. Validação
usa pytest e executores simulados; não homologa inferência real nem executa o
backlog do usuário. O roadmap abaixo fica sujeito à supersessão dessa ADR.

Extensão autorizada: [ADR 0012](decisions/0012-master-plan-and-milestone-queue.md)
acrescenta Plano Mestre `.md` e fila `milestone`/`work`, com goal/acceptance,
decomposição apenas do marco ativo e verificação de pré-requisitos em SQLite.
Não gera Markdown, não detalha marcos futuros nem avança automaticamente.

## Resumo e regras de fase

Fase 1 estabelece um pacote `agentflow` instalável, comandos seguros `--version`, `init` e `doctor`, configuração TOML, testes pytest, CI e instruções de instalação. O protótipo `autonomous-development-runner/` permanece intacto. Nenhuma tarefa desta fase inicia agentes, cria contêineres, grava SQLite ou faz commits. Cada tarefa abaixo deve ser concluída e verificada antes das dependentes.

## SDD transversal — diagnóstico e contrato evolutivo

### O que já existe

- `.agentflow/spec.toml` e `.agentflow/tasks.toml` são os arquivos de definição versionados; TOML tem schema versionado, IDs estáveis, critérios de aceite, dependências DAG e digest SHA-256 canônico.
- F2 persiste `projects.spec_digest/tasks_digest/plan_revision` e snapshots `task_definitions` imutáveis com `definition_revision`, `definition_digest` e `spec_digest`. Import idempotente preserva a mesma revisão; mudanças criam novas definições. Tarefas iniciadas/alteradas requerem revalidação explícita; eventos e evidências permanecem históricos.
- F3 vincula reserva/tentativa à revisão e digest; execução bem-sucedida não conclui sem critérios verificados. F4 associa workspace/branch/checkpoint à tarefa/revisão e escopo autorizado. F5 vincula validation run, gate, snapshot, validator, evidência e artifact ao definition digest e checkpoint; resultado incompatível/stale não justifica conclusão.
- F6 `ModelSelection` é snapshot por tentativa, persistido com provider/model/adapter e policy digests; hoje sua ligação à spec é indireta via tentativa e `definition_revision`/digest.

### Lacunas verificadas

1. Não há registro imutável first-class `SpecificationRevision` com snapshot completo da spec e trilha própria de proposta/aceite. A tabela `projects` retém somente os digests correntes; snapshots de tarefa guardam digest da spec, mas não substituem o snapshot do documento inteiro.
2. Critérios de tarefa não têm vínculo explícito versionado com IDs dos critérios/requisitos globais da spec. Dependências entre tarefas existem, mas não medem cobertura de requisitos.
3. Não há relatório de impacto que relacione requisito alterado com tarefas dependentes, tentativas, roteamentos, workers, checkpoints, gates, evidências e outputs derivados; a revalidação atual é por tarefa e pode marcar evidência velha incompatível, mas não é planejador de mudanças.
4. `import_plan` troca transacionalmente os digests correntes e força revalidação quando a definição de tarefa com histórico muda; isso protege execução, mas não equivale a um estado explícito de spec proposta/aprovada nem demonstra aprovação do requisito pelo autor informado.
5. O processo atual exige campos/critério TOML, mas não documenta uma orientação de suficiência proporcional à complexidade nem teste de cobertura requisito→critério→gate/evidência.

Essas lacunas são trabalho documental/planejado; esta consolidação não as declara implementadas. O protótipo ADR e os schemas históricos permanecem preservados.

### Contratos SDD mínimos planejados

- `SpecificationRevision`: `revision_id` estável/único por projeto, sequência monotônica, schema version, snapshot integral canônico, `spec_digest`, revisão anterior e provenance Git quando conhecida; estados proposta/aceita/rejeitada/sucedida e eventos separados. Revisão aceita é imutável.
- `PlanRevision`: snapshot/digest de `tasks.toml` ligado a uma especificação aceita, schema version, DAG, `RequirementTrace`, cobertura e revisão anterior. Só é aceita com cobertura completa ou adiamentos explícitos aprovados.
- `RequirementTrace`: link explícito de ID de requisito de `spec.toml` a pares task/criterion em `tasks.toml`; campo TOML candidato `satisfies = ["requirement-id"]`. F7-01 fecha compatibilidade/versionamento; sem inferência textual.
- `ChangeImpact`: diff semântico e inventário de dependências/artefatos observados, com classificação determinística `unaffected/revalidate/replan/stale/unknown`; incerteza não é considerada ausência de impacto.
- `RevalidationDecision`/`Replan`: decisão de impacto e novo plano/versionamento, com grant autenticado, motivo, revisões e dispositions. Campo `author` autodeclarado nunca autentica aprovador.

Aceite de requisitos fica exclusivamente em plano de controle humano separado do contexto do agente. Requer principal autenticado e grant de uso único vinculado a revision/plan/impact digests, escopo, operation ID e expiração. Agente não recebe API, token ou credencial de aceite; texto, ferramenta, commit ou saída de agente não constitui autorização. Sem autenticador que comprove presença/consentimento independente, operação de aceite fica indisponível e o fluxo bloqueia.

As revisões globais e locais continuam usando os mecanismos existentes: `Criterion` e seus IDs, `Task`, digests canônicos, `task_definitions`, eventos e operações idempotentes. A evolução deve ocorrer dentro de `.agentflow/spec.toml`/`tasks.toml`; não criar terceiro arquivo versionado. Um TOML antigo sem links continua legível e pode ser operado manualmente, mas seu coverage status é `legacy_unmapped`; roteamento/coordenação que exijam prova integral deve bloquear até migração/aceite explícito.

### Regra de mudança e rastreabilidade

O fluxo planejado é: TOML candidate → parse/validação/DAG → snapshots/digests propostos → ChangeImpact determinístico → autenticação/grant/aceite humano → PlanRevision aceita e cobertura → roteamento/execução → workspace/checkpoint → gates/evidências → conclusão. TOML/SQLite/Git não formam uma transação distribuída: snapshot aceito é fonte operacional imutável; mudança posterior é drift/proposta e não altera a revisão. Toda escrita transacional usa `BEGIN IMMEDIATE`, FKs, `operation_id` e request digest; grant consumido, aceite e eventos são atômicos no DB. Falha/resultado ambíguo requer reconciliação, nunca replay cego. Histórico não é apagado. Evidência incompatível permanece stale e não satisfaz revisão posterior; não existe promoção automática.

Critérios objetivos são IDs estáveis com descrições observáveis, cobertos por evidência declarada/verificada ou quality gate independente. A spec simples permanece concisa (objetivo, restrições indispensáveis, critérios); uma spec complexa detalha pressupostos, interfaces, riscos e dependências conforme impacto. Concisão não autoriza critério ambíguo; detalhe adicional não pode criar requisito oculto fora do TOML aceito.

### Incremento pelas Fases 7–9

F7-01 fecha contratos e versionamento SDD, armazenamento imutável, links de cobertura, ChangeImpact, aceite e compatibilidade. F7 grava classificação, roteamento, budget e mudança de modelo contra a revisão exata. F8 copia identidade/trace links em cada `WorkerAssignment` e bloqueia claim após drift/revalidação requerida. F9 vincula `CoordinationPlan`, `ResultProposal`, revisão independente e integração à mesma revisão; alteração proposta cria nova revisão e novo plano, nunca edita o plano corrente. Migração SQLite após v5 será aditiva, backup-first/rollback-safe; número de schema e tabelas são decididos após inventário no gate F7-01.

## Fase 1 — Fundação

| ID | Tarefa | Dependências | Critério de aceite |
| --- | --- | --- | --- |
| F1-01 | Definir layout `src/agentflow`, metadados de pacote, Python >=3.11 e entry point `agentflow`. | Nenhuma | Instalação local reproduzível; importação e `agentflow --version` funcionam sem Codex ou Docker. O pacote ADR permanece intacto. |
| F1-02 | Criar estrutura dos dez módulos e contratos mínimos de fronteira, sem comportamento de fases futuras. | F1-01 | Imports independentes; núcleo não importa adaptadores concretos; nenhum comando de execução é exposto. |
| F1-03 | Definir esquema versionado de TOML, valores padrão e validação com erros úteis. | F1-01 | Configuração válida carrega; tipos, chaves e versões inválidas falham com mensagem e código de saída consistentes; Docker não é obrigatório. |
| F1-04 | Implementar `agentflow init` não destrutivo e idempotente. | F1-03 | Primeira chamada cria somente arquivos previstos; segunda não altera bytes; conflitos e diretório sem permissão são informados sem sobrescrita; nenhuma execução de agente. |
| F1-05 | Implementar sondagens somente leitura para Python, Git, Codex CLI e Docker. | F1-02 | Resultados estruturados distinguem disponível, ausente e erro; ausência de Docker é opcional; testes usam sondagens simuladas. |
| F1-06 | Expor `agentflow doctor` e códigos de saída documentados. | F1-05 | Mostra versões/estado; funciona sem Docker; não instala, autentica ou executa agentes; status crítico tem saída previsível. |
| F1-07 | Adicionar pytest para instalação/CLI, TOML, `init` repetido e conflitos, e diagnóstico com dependências ausentes. | F1-03, F1-04, F1-06 | Testes passam em ambiente limpo sem Codex/Docker; testes verificam efeitos, não detalhes internos. |
| F1-08 | Adicionar workflow CI para Python 3.11+ e instalação/testes. | F1-07 | CI instala pacote e dependências de teste declaradas e executa pytest em sistema sem Codex/Docker. |
| F1-09 | Atualizar README e guia de instalação/uso para os comandos realmente entregues. | F1-04, F1-06, F1-08 | Exemplos executáveis, pré-requisitos, significado de `doctor` e limites da Fase 1 documentados; remover contradições com o protótipo. |

### Gate de conclusão da Fase 1

Instalação reproduzível com Python 3.11+; `agentflow --version`, `init` e `doctor` funcionais; `init` seguro e repetível; configuração TOML validada; pytest e CI passando; operação local sem Docker; nenhum agente executado e nenhuma dependência do Vibe Coding Toolkit.

## Fases seguintes, sem implementação antecipada

| Fase | Dependência | Entregas planejadas e gate |
| --- | --- | --- |
| 2 — Planejamento e estado | Fase 1 | Especificações e tarefas versionadas, dependências, esquema SQLite, transições e migrações; testes de transação e reabertura. |
| 3 — Execução local | Fase 2 | Adaptador Codex CLI, runtime local, políticas de tentativa e timeout, validação e evidências; execução de tarefa isolada por testes de contrato. |
| 4 — Recuperação e Git | Fase 3 | Reconciliação após interrupção, prevenção de duplicidade, branches e checkpoints explícitos; testes de falha entre transições. |
| 5 — Validação, Docker e entrega | Fase 4 | Validação independente e quality gates sobre checkpoints imutáveis; runtime Docker opcional, isolamento configurável, builds rastreáveis e plano de entrega sob política explícita; sem deploy automático. |
| 6 — Ecossistema de agents e contratos de modelo | Fase 5 | Contratos separados `AgentAdapter`, `ProviderDescriptor`, `ModelDescriptor`, `ModelSelection` e `ModelRoutingPolicy`; integrações na prioridade Antigravity → Claude Code → OpenCode → Hermes Agent; sem roteamento, retry ou troca automática nesta fase; sem código/conteúdo do Vibe Coding Toolkit. |
| 7 — Intelligent Model Routing | Fase 6 | Classificação de complexidade/risco, escolha explícita sob policy versionada, qualidade/capacidade/custo/latência, budgets rígidos, escalonamento fundamentado em evidência, autorização de mudança de modelo/provider e auditoria. Sem workers concorrentes. |
| 8 — Worker Orchestration | Fase 7 | Workers persistidos e independentes, scheduler/fila, leases/locks, workspaces isolados e budgets atribuídos; primeiro execução sequencial, depois paralelismo limitado apenas após gate próprio de isolamento, concorrência e recovery. |
| 9 — Advanced Coordination | Fase 8 | Coordenação de workers especializados, resultados revisáveis, validação independente, integração segura, resolução explícita de conflitos, otimização global e auditoria; concorrência é opção governada, nunca premissa. |

## Questões e riscos a resolver antes de implementar

1. Resolvido: `.agentflow/config.toml` com `schema_version = 1`, `runtime = "local"` e `agent = "none"`.
2. Resolvido: `init` usa a raiz do repositório Git atual ou a raiz que contém `--path`.
3. Resolvido: `doctor` usa códigos 0, 1 e 2. Python e Git são obrigatórios; Codex e Docker tornam-se obrigatórios apenas quando selecionados.
4. Resolvido: `adr` permanece referência legada intacta, sem migração nesta fase.
5. Resolvido: CI em Ubuntu com Python 3.11, 3.12 e 3.13.

## Progresso da Fase 1

| Tarefa | Estado | Evidência local |
| --- | --- | --- |
| F1-01 | Validada localmente | `pip install '.[test]'` construiu e instalou wheel `agentflow-0.1.0`; entry point instalado retornou `agentflow 0.1.0`. |
| F1-02 | Validada localmente | Imports de módulos cobertos pela suíte; `compileall` passou. |
| F1-03 | Validada localmente | Casos de configuração válida e inválida passaram no pytest. |
| F1-04 | Validada localmente | Pytest e teste de integração confirmaram criação, repetição sem alteração e conflito sem sobrescrita. |
| F1-05 | Validada localmente | Pytest confirmou obrigatoriedade por perfil e ausência de dependências opcionais. |
| F1-06 | Validada localmente | Teste com CLI instalada confirmou códigos 0, 1 e 2; ausência de Codex e Docker no PATH foi simulada. |
| F1-07 | Validada localmente | `python -m pytest -q`: 17 aprovados, 0 falhos. |
| F1-08 | Validada na CI remota | Workflow Ubuntu/Python 3.11, 3.12 e 3.13 passou no GitHub Actions para push e Pull Request #1. |
| F1-09 | Validada localmente | README e guia revisados contra comandos instalados e saídas observadas. |

## Homologação local — 2026-11-04

- Interpretador: Python 3.13.17 em `.venv` dentro do projeto. Python global não foi modificado.
- Dependências instaladas apenas no ambiente virtual: AgentFlow 0.1.0, pytest 9.2.0, colorama 0.4.6, iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0 e Pygments 2.21.0; pip 26.2.2 veio no ambiente.
- Empacotamento: wheel construído pela instalação PEP 517; metadados e entry point `agentflow` confirmados; `pip check` sem requisitos quebrados.
- Integração: em repositório Git temporário, `init` criou `config.toml`; segunda chamada preservou bytes; configuração inválida foi preservada com saída 2. `doctor` retornou 0 com Codex/Docker opcionais ausentes, 1 com Codex obrigatório ausente e 2 com TOML inválido. O repositório temporário foi descartado após o teste.
- `git diff --check` passou. O aviso de normalização LF/CRLF do README não é falha de whitespace.
- CI do GitHub: os jobs Python 3.11, 3.12 e 3.13 passaram nas execuções de push e Pull Request #1. A Fase 1 está pronta para revisão e merge, sem merge automático.

Revalidação antes da publicação: a suíte passou novamente com `--basetemp .pytest-run` (17 aprovados). Dois testes foram corrigidos para usar um caminho inexistente explícito; assim não dependem da localização do diretório temporário em relação a um repositório Git pai.

## Fase 2 — plano executado

A Fase 1 foi integrada à `main` no PR #1 e a Fase 2 no PR #2. Esta seção registra as tarefas executadas para especificações, dependências e estado operacional. Os contratos detalhados, o esquema de tabelas, as transições e a recuperação estão em [architecture.md](architecture.md#fase-2--planejamento-e-estado). O progresso verificável está em [phase-2-progress.md](phase-2-progress.md).

### Ordem de implementação e critérios de aceite

| ID | Tarefa | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F2-01 | Registrar em ADR as decisões consolidadas na arquitetura: ignore seletivo, revisão imutável, evidências, reconciliação, WAL/durabilidade/backup e `operation_id`; fixar parâmetros operacionais e exemplos. | Fase 1 | ADR aprovado especifica `busy_timeout`, identidade de reserva, autoria de evidência e formato canônico de digest; mudanças restantes são risco explícito, não escolha implícita no código. |
| F2-02 | Definir `spec.toml` e `tasks.toml` v1, IDs de critérios, modelos imutáveis, normalização e evolução de versão. | F2-01 | Arquivos válidos produzem o mesmo plano/digest independentemente da ordem de leitura; versão futura/chave/tipo/ID/critério inválido falham com local preciso; originais não são editados. |
| F2-03 | Validar referências, unicidade e DAG de dependências; produzir ordem topológica estável. | F2-02 | Referência ausente, ID duplicado, autodependência e ciclos simples ou longos são rejeitados com ciclo identificável; permutação de entrada não muda a ordem canônica. |
| F2-04 | Definir protocolo do repositório, comandos de transição/evidência/revalidação e erros de domínio sem importar SQLite no núcleo. | F2-02, F2-03 | Implementação simulada e núcleo importam sem SQLite; guardas, revisão esperada e idempotência por ID+digest têm testes de contrato. |
| F2-05 | Criar esquema SQLite v1, FKs, índices, WAL, `busy_timeout`, `synchronous=FULL`, backup consistente e migração inicial. | F2-01, F2-04 | Banco novo abre com versão correta; FKs ativas por conexão; falha de backup impede migração; falha transacional preserva versão anterior; versão futura é recusada; `foreign_key_check` e `integrity_check` passam. |
| F2-06 | Persistir plano validado, revisões imutáveis e digests, com importação idempotente e revalidação explícita de definições alteradas. | F2-03, F2-05 | Duas importações iguais não duplicam registros; inserção parcial reverte tudo; plano inválido não toca banco; reabertura com tarefa iniciada alterada bloqueia transição até decisão auditável; histórico não é apagado. |
| F2-07 | Implementar máquina de estados, evidências estruturadas, operações idempotentes, eventos append-only, checkpoints lógicos e liberação transacional de dependentes. | F2-04, F2-06 | Status, evidência, operação, evento e checkpoint são atômicos; evidência declarada não satisfaz conclusão; ID repetido com mesmo digest retorna resultado anterior, digest divergente falha; concorrência não reserva tarefa duas vezes. |
| F2-08 | Implementar abertura/reabertura, integridade e reconciliação determinística de reservas sem efeitos externos. | F2-07 | Falha antes do commit não persiste efeito; após commit, estado/evento/checkpoint reaparecem; reserva órfã fica `blocked` com evento único; duas recuperações não duplicam eventos; corrupção/divergência bloqueia mutação e preserva dados. |
| F2-09 | Integrar validação e inspeção seguras na CLI, documentar versionamento seletivo e operação local. | F2-08 | CLI expõe somente leitura/inspeção aprovadas e erros estáveis; `init` e `doctor` da Fase 1 mantêm comportamento; Git versiona só `spec.toml` e `tasks.toml`; nenhum comando executa agentes, Docker ou Git mutável. |
| F2-10 | Completar testes unitários, de contrato, integração, migração, concorrência, reabertura e documentação de limite. | F2-02 a F2-09 | Suíte passa em Python 3.11, 3.12 e 3.13 sem Codex/Docker; cobre disputa entre processos, FKs, ciclos, mudanças de definição, evidência, idempotência, falha de migração/backup, interrupção e preservação do ADR. |

### Matriz mínima de verificação

| Camada | Cenários obrigatórios |
| --- | --- |
| Contratos TOML | Versão desconhecida/futura, encoding, chave/tipo incorretos, critérios vazios, IDs estáveis, digest canônico, arquivo malformado, evolução sem edição implícita. |
| Grafo | Referência ausente, duplicata, ciclo direto/indireto, ordem topológica determinística, desbloqueio com múltiplos pré-requisitos. |
| Estado | Transições permitidas/proibidas, revisão esperada, evidência declarada/verificada por critério, ID+digest divergente, eventos/checkpoints atômicos, FKs e rollback. |
| Migração | Banco vazio, reabertura, versão futura, falha de migração e backup, WAL ativo, timeout e durabilidade explícitos, integridade após interrupção. |
| Recuperação | Interrupção antes/depois do commit, reserva órfã bloqueada uma vez, reabertura repetida, definição alterada, banco corrompido em modo somente leitura. |
| Integração | CLI somente leitura onde prometido, apenas dois TOML versionáveis, dados operacionais fora do Git, ausência de Codex/Docker, protótipo ADR intacto. |

### Gate e riscos

Gate da Fase 2, aprovado no PR #2: contratos v1 documentados e testados; DAG válido; SQLite transacional com migrações reversíveis por falha, FKs e auditoria; recuperação determinística e idempotente; CI verde nas três versões; nenhuma execução de agentes. A implementação da Fase 3 depende de revisão e autorização separadas.

Decisões consolidadas: somente `spec.toml` e `tasks.toml` versionáveis; revisões iniciadas imutáveis com revalidação explícita; evidência estruturada declarada/verificada por critério; reserva órfã bloqueada para reconciliação; SQLite em WAL com FKs, timeout, `synchronous=FULL`, migrações transacionais e backup consistente; `operation_id` global com digest de solicitação. F2-01 registra parâmetros concretos em ADR antes do código.

Riscos residuais: identidade confiável do proprietário de reserva e autoria de verificação; custo de I/O de durabilidade e backup; conflito quando TOML muda entre leitura e commit; restauração manual após corrupção física; compatibilidade de futuros esquemas TOML/SQLite. Migração do JSON do ADR, checkpoints Git, isolamento Docker e validação automática ficam fora da Fase 2.

## Fase 3 — implementação local

Escopo autorizado: executor local Codex CLI para uma tarefa solicitada explicitamente, runtime de processos, validação independente, limites, logs, cancelamento e recuperação conservadora. Os [contratos detalhados](architecture.md#fase-3--executor-codex-cli-local) foram implementados e integrados à `main` pelo PR #3 (merge `edb7c4f7f0e672d8d1432d2d8948c3b49b7f4b91`); CI Python 3.11, 3.12 e 3.13 aprovada. Nenhum agente real foi executado.

| ID | Tarefa planejada | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F3-01 | Revisar e aprovar ADR 0003; validar valores, capacidades concretas do Codex, cancelamento por plataforma e configuração efetiva. | Fase 2 | ADR aprovado com tabela de versão/capacidade testada e erros de pré-checagem; nenhum sandbox ou aprovação é ativado automaticamente. |
| F3-02 | Definir `ExecutionRequest`, `ExecutionResult`, `AgentAdapter`, `ProcessRuntime`, políticas operacionais e erros de domínio. | F3-01 | Núcleo importa apenas protocolos; adaptadores simulados substituem Codex; pedido inclui revisão, ID idempotente e limites operacionais. Configuração nativa de sandbox não é parte da política AgentFlow nem é transmitida pelo contrato comum. |
| F3-03 | Implementar migração SQLite v2 de tentativas/eventos, índice de uma tentativa ativa e backup consistente. | F3-02 | Reabertura v1→v2 preserva tarefas/evidências; falha de backup/migração mantém v1; tentativas referenciam revisão correta; versão futura é recusada. |
| F3-04 | Implementar pré-checagem, reserva e tentativa `prepared` atômicas, com teto de três tentativas. | F3-03 | Disputa entre processos cria uma tentativa ativa; pedido idêntico retorna a mesma; divergente falha; pré-checagem falha sem spawn; `prepared` consome uma tentativa. |
| F3-05 | Implementar runtime local independente do Codex, com identidade verificável, limites, logs incrementais e cancelamento de árvore Windows/Linux. | F3-02, F3-04 | Processos falsos cobrem timeout de 1800 s configurável, limite de log, SIGTERM/SIGKILL no Linux e Job Object no Windows; PID reutilizado não é aceito como identidade. |
| F3-06 | Implementar adaptador Codex CLI com detecção de versão/capacidades e configuração efetiva, sem sobrescrever configuração do usuário. | F3-01, F3-05 | Flags são usadas somente após verificação; modos explícitos de sandbox, inclusive acesso completo, e aprovações não interativas são preservados; formato inesperado falha; testes usam executável falso. |
| F3-07 | Persistir logs, eventos, resultado do processo e ligação à revisão; limitar retenção e crescimento. | F3-03, F3-05, F3-06 | Logs até 10 MiB por fluxo por padrão, truncamento/falha controlados, retenção padrão de 30 dias com limpeza explícita, payload SQLite até 4 KiB; evento/checkpoint atômicos e metadados sem segredos. |
| F3-08 | Integrar validação e evidências verificadas; distinguir falha do processo, da validação e resultado desconhecido. | F3-07 | Saída `process_succeeded` não conclui tarefa; somente critérios verificados da revisão vigente justificam `completed`; saída parcial e falhas deixam estado auditável. |
| F3-09 | Implementar recuperação e reconciliação de `prepared`/`running`, sem repetir efeitos externos. | F3-07, F3-08 | Queda antes/depois de spawn/identidade/commit bloqueia tentativa ambígua uma vez; processo possivelmente vivo impede nova execução; reabertura repetida não duplica eventos; decisão humana precede retentativa. |
| F3-10 | Expor execução/cancelamento explícitos na CLI, documentação, testes de contrato e CI. | F3-04 a F3-09 | CLI não cria fila autônoma nem altera Git; testes Windows/Linux simulados e CI Python 3.11/3.12/3.13 passam sem Codex/Docker real; `init`, `doctor`, `plan`, `state` preservados. |

### Matriz de testes proposta

- **Contratos/adaptador:** versões e flags de protocolo reais/incompatíveis, configuração nativa herdada sem override de sandbox, aprovação não interativa, argumentos/stdin seguros e saída estruturada válida/inválida; Codex falso por padrão.
- **Persistência e concorrência:** migração v1→v2, backup, FKs, rollback, índice de tentativa ativa, três tentativas totais, idempotência por `operation_id`, parâmetros divergentes e disputa entre processos.
- **Runtime e logs:** timeout padrão 1800 s/configuração, inatividade opcional, limite de 10 MiB por fluxo, drenagem, truncamento, retenção/limpeza explícita, falha de escrita, SIGTERM/SIGKILL Linux e Job Object Windows, pai encerrado e filhos órfãos.
- **Recuperação/repetição:** interrupção em cada fronteira SQLite/spawn, PID reutilizado, tentativa `prepared`/`running`, processo possivelmente ativo, duas reaberturas, resultado desconhecido sem nova execução e decisão humana auditada antes de retentar.
- **Validação:** falha do processo versus falha de validação versus desconhecido, declaração versus verificação, critérios da revisão correta, agente insuficiente para conclusão e preservação de alterações parciais.
- **Regressão:** fases 1 e 2, configuração Codex existente intocada, `.agentflow/` operacional e protótipo ADR fora do pacote/Git.

### Gate e limites

Gate de homologação: uma única tarefa explícita pode ser executada pelo adaptador local Codex dentro da política aprovada, com estado e logs auditáveis, limites aplicados, cancelamento e recuperação determinística; conclusão depende de validação independente; testes de contrato e CI passam em Python 3.11, 3.12 e 3.13. A Fase 3 foi publicada e integrada após a homologação; nenhum agente real foi executado.

Decisões consolidadas: timeout padrão 1800 s (máximo configurável 7200 s), três tentativas totais, sem repetição automática, logs incrementais limitados e retenção de 30 dias, identidade de processo além de PID e cancelamento de árvore por plataforma. O sandbox do Codex pertence à configuração nativa do Codex; AgentFlow não o configura, seleciona ou substitui. Aprovação não interativa é controlada separadamente. Docker não é o sandbox do Codex.

Riscos residuais: Codex CLI/flags e introspecção de configuração podem evoluir; limites rígidos de CPU/memória variam por sistema; matar processo não reverte arquivos; SQLite e spawn não são atômicos; logs podem conter segredos emitidos pelo agente; no Linux, processos podem sobreviver à morte do pai. A implementação deve falhar de modo seguro quando uma capacidade exigida não puder ser verificada. Git branches/checkpoints, Docker, múltiplos adaptadores e execução autônoma de fila permanecem fora da Fase 3.

## Fase 4 — plano detalhado: recuperação e Git

Estado: **concluída e integrada** pelo PR #4. Head revisado `709e77ad46dfa3283922856fd078b6baca1b6029`, merge `8ec623813709d71b98fbd0be7e8fba87ebb9d565`, seis checks Python aprovados. Ver [progresso da Fase 4](phase-4-progress.md), [arquitetura](architecture.md#fase-4--recuperação-e-integração-explícita-com-git-fechamento-arquitetural) e [ADR 0004](decisions/0004-phase-4-recovery-git.md).

### Ordem definitiva e critérios de aceite F4-01 a F4-10

| ID | Tarefa | Dependências | Critério verificável |
| --- | --- | --- | --- |
| F4-01 | Fixar identidade (UUIDv4 `ws-<32hex>`, branch `agentflow/ws/<hex>`, path/lock persistentes), digests SHA-256 canônicos, limites 128 paths/64 MiB/30 s, locks flock Linux e msvcrt Windows sem expiração, owner identity e recuperação como `unknown`, retenção indefinida/limpeza explícita, trailer/OID/tree/pathset de checkpoint e política de hooks/filtros. | Fase 3, ADR 0004 | Valores documentados em ADR/arquitetura; testes de parse/cálculo dos limites e owner records; nenhuma configuração executável não auditada aceita. F4-01 validada antes de interfaces/código. |
| F4-02 | Definir modelos imutáveis e protocolos `WorkspaceProvider`/`GitProvider`, erros e doubles de contrato; evoluir `tasks.toml` para schema v2 com `versioned_paths` por tarefa. | F4-01 | `core` depende só de protocolos; contratos carregam identidade, revisão, base, branch, pathset e operation ID; não há método remoto/destrutivo nem import concreto no núcleo. |
| F4-03 | Implementar migração SQLite v2→v3: workspaces, operações, checkpoints Git e eventos. | F4-02 | Backup consistente antes da migração; FKs/índices/constraints; falha injetada reverte tudo e mantém v2; preservar registros F2/F3 e recusar versão futura sem alteração. |
| F4-04 | Implementar reserva/lock interprocesso por workspace e preflight read-only de repo/dirty state/identidade. | F4-03 | Dois processos não mutam simultaneamente; PID reutilizado não adota lock; raiz comum, cwd, branch e HEAD conferidos; alterações preexistentes preservadas byte a byte e conflito não muta Git. |
| F4-05 | Criar explicitamente worktree obrigatório e branch própria para tarefa que pode alterar arquivos versionados; registrar identidade/lifecycle. | F4-02, F4-04 | Worktree começa na base persistida e limpo; checkout original permanece intacto; repetição idêntica reutiliza identidade; colisão/divergência falha; nenhuma remoção automática em estado sujo/ambíguo. |
| F4-06 | Capturar alterações e derivar/persistir allowlist de paths da autorização/revisão; validação robusta de path e submódulo. | F4-05 | Espaços/Unicode/caracteres especiais são round-tripped sem shell; traversal, symlink escape, case collision não representável, arquivo operacional e submódulo são recusados; escopo não é ampliado pelo agente. |
| F4-07 | Implementar checkpoint explícito com index privado, pathset allowlisted e verificação independente. | F4-03, F4-06 | Revalidar cada path imediatamente antes de staging e commit; nunca `add -A`/`.`; staging exato; verificar OID, parent, tree, branch/ref e paths; idempotência não duplica commit. |
| F4-08 | Implementar estados intermediários e reconciliação crash-safe entre SQLite/Git. | F4-03 a F4-07 | **Concluída:** injeção de crash nas fronteiras; `unknown` bloqueia repetição e mutações; recuperação idempotente baseada em fatos; resolução ambígua explícita. |
| F4-09 | Integrar fluxo da tarefa e CLI explícita/read-only onde aplicável, sem colapsar execução, validação e conclusão. | F4-05 a F4-08 | **Concluída:** tarefa modificadora exige execução resolvida, validação, evidências da revisão e checkpoint verificado; sem alterações exige verificação explícita de workspace limpo/HEAD; CLI expõe somente operações locais autorizadas. |
| F4-10 | Testes de regressão, compatibilidade Linux/Windows, docs, pacote e homologação completa. | F4-01 a F4-09 | **Concluída:** 121 testes locais, package smoke e seis checks Python 3.11/3.12/3.13 aprovados; PR #4 integrado pelo merge `8ec623813709d71b98fbd0be7e8fba87ebb9d565`. |

### Dependências

- F4-01 é gate obrigatório antes de contratos/esquema; F4-02 precede persistência e implementação de adaptadores.
- F4-03 precede reserva durável; F4-04 habilita qualquer mutação Git; F4-05 só cria workspace após lock/preflight.
- F4-06 depende de workspace isolado; F4-07 só consome allowlist validada; F4-08 integra e testa todas as fronteiras externas antes da CLI.
- F4-09 só expõe operações depois de recovery confiável; conclusão consome resultados de F3, evidências F2/F3 e checkpoint F4 em transação coerente.
- F4-10 é gate de homologação e não pode ser declarado aprovado antes dos três jobs da CI.

### Matriz de testes e aceite

1. **Persistência:** migração v2→v3, backup em WAL, FKs, rollback, versão futura, IDs únicos, checkpoint/task/revision coerentes e preservação de estado histórico.
2. **Workspace e preexistência:** repo temporário sem remote; worktree/branch persistente; original dirty; arquivos staged, unstaged e untracked; alteração externa de HEAD/ref; workspace sujo; nenhum dado movido, limpo ou descartado.
3. **Paths:** espaços, Unicode, caracteres de pathspec, newline/leading dash quando suportados; NUL-safe; symlink/traversal/UNC/drive/case collision; allowlist muda somente via revisão explícita; alterações entre duas validações impedem commit.
4. **Segurança Git:** spy garante sem shell, sem comando remoto/destrutivo, sem aliases/config mutável; submódulo recusado; somente index privado e lista exata; credenciais, `.agentflow/`, logs, DB/WAL, cache e ADR legado fora do checkpoint.
5. **Concorrência/idempotência:** processos simultâneos no mesmo workspace, lock ativo/órfão/PID reciclado, operação idempotente, parâmetros divergentes conflitantes, reserva refs comuns.
6. **Falhas/recuperação:** falha/crash em intenção→comando, durante comando, commit→leitura, leitura→SQLite; operação `unknown` não repete; recuperação repetida sem eventos duplicados; ausência de commit duplicado, reset ou exclusão.
7. **Fluxo:** processo falho/sucesso/desconhecido; validação falha; evidência apenas declarada; checkpoint ausente/tampered; conclusão negada até processo resolvido + validação/evidências verificadas + checkpoint, quando houver alterações versionadas.
8. **Plataforma/regressão:** Windows/Linux, Python 3.11/3.12/3.13, F1–F3, pacote instalado; Codex/Docker ausentes e nenhum agente real; protótipo e dados locais preservados.

### Riscos residuais e gate

Worktree isola arquivos de trabalho, mas compartilha repositório Git comum e não é sandbox contra processo com as permissões do usuário; race com processo externo, hooks/filters e particularidades de case/Unicode podem impedir garantia absoluta. Recuperação de commit efetuado sem confirmação SQLite pode requerer resolução humana. Locks dependem de identidade confiável por sistema operacional. Esses riscos exigem detecção e falha segura, sem prometer atomicidade distribuída.

Estado: **Fase 4 concluída e integrada** pelo PR #4, com head `709e77ad46dfa3283922856fd078b6baca1b6029`, merge `8ec623813709d71b98fbd0be7e8fba87ebb9d565` e seis checks CI aprovados. Consulte [progresso da Fase 4](phase-4-progress.md).

## Fase 5 — validação independente, quality gates e entrega

Estado: **Fase 5 homologada e integrada** pelo PR #5. Head `51db15a06672c9ffa1b3f4ac2914410ee4d10b08`, merge sem reescrita `bf558211a80038efddde36332d8c09a8157cd903`; seis jobs CI Python 3.11/3.12/3.13 (push e PR) passaram. Ver [progresso da Fase 5](phase-5-progress.md).

### Ordem definitiva e critérios F5-01 a F5-10

| ID | Tarefa | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F5-01 | Fixar limites operacionais por plataforma, perfis/capacidades, rede, segredos, quotas, retenção e limpeza. | F4 | **Concluída antes do código:** stdout+stderr 8 MiB/gate e 64 MiB/run; artefato 256 MiB cada/1 GiB run/10 GiB projeto; snapshot 2 GiB cada/10 GiB projeto; temp 4 GiB/run; total operacional 20 GiB/projeto; CPU 2 default/8 max; memória 4 GiB default/16 GiB max; processos 64 default/256 max; timeout 600/3600 s; logs 30 dias, snapshots 7 dias, artifacts 30 dias. Capacidades não verificadas reportadas `unavailable`; bloqueio quando requeridas. Testes de F5-01 registrados no progresso. |
| F5-02 | Definir contratos imutáveis e protocolos: `ValidationRequest`, snapshots, `GateDefinition/Result`, `Validator`, `ValidationRunner`, `Runtime`, `BuildRequest/Result`, `DeliveryRequest/Result`. | F5-01 | Contratos substituíveis; cada request/result carrega IDs/digests e identidade de política/runtime definidos no ADR; core não importa adaptadores concretos. |
| F5-03 | Evoluir TOML: `spec.toml` v2 para allowlist de comandos por projeto; `tasks.toml` v3 para gates estáveis, critérios, DAG, política e limites. | F5-02 | v1/v2 continuam legíveis; allowlist vazia em spec v1 e zero gates em tasks v1/v2; rejeitar IDs reutilizados/duplicados, comandos livres, referências inválidas, ciclos e limites fora de faixa; snapshot canônico estável. |
| F5-04 | Migrar SQLite v3→v4 para validações, snapshots, resultados, builds, entregas, operações e eventos. | F5-03 | Backup consistente; transação/rollback integral; FKs e `integrity_check`; v3 intocado após falha; versão futura recusada; histórico F2–F4 preservado; uma validação ativa por projeto e idempotência pelo digest da solicitação. |
| F5-05 | Materializar snapshot imutável do commit/tree de checkpoint verificado. | F5-03, F5-04 | Manifesto de paths/modos/blob OIDs e digest coincide com Git tree; sem filtros/hooks; symlink/submodule/path não suportado é rejeitado; workspace de execução/original não é lido como fonte nem modificado. |
| F5-06 | Implementar scheduler sequencial, estados, preflight, limites, cancelamento e recovery de gate/run. | F5-02 a F5-05 | Uma validação ativa por projeto; ordem determinística; default timeout 600s/max 3600s; dependentes de qualquer resultado não `passed` viram `not_run`; crash/timeout/cancelamento nunca passa nem repete automaticamente. |
| F5-07 | Implementar runtimes local e Docker opcionais, validadores iniciais, capabilities, sanitização e logs. | F5-01, F5-05, F5-06 | Só executáveis allowlisted e request explícito; Docker exige imagem digest + executable do container e verifica HostConfig/mounts; source read-only/output isolado; capabilities `supported` só liberam preflight, gate exige `enforced`; indisponível bloqueia política obrigatória; sem credenciais herdadas; Docker não é requisito. |
| F5-08 | Integrar evidências com revisão/checkpoint/gate/validator e ação separada de conclusão. | F5-04 a F5-07 | Somente evidência verificada vigente satisfaz critérios; qualquer alteração de revisão/checkpoint/config/gate/validator invalida; código de saída ou artefato isolado não conclui. |
| F5-09 | Implementar build e proveniência; delivery provider e autorização específica por operação/destino, com recovery conservador. | F5-04 a F5-08 | Build registra source/toolchain/config/dependencies/artifact SHA-256; artifact adulterado recusado; delivery desabilitada por default; autorização, gates e destino exatos são necessários; `unknown` bloqueia retry. |
| F5-10 | CLI explícita, documentação, testes de regressão, pacote, CI e homologação. | F5-01 a F5-09 | CI Linux/Windows Python 3.11/3.12/3.13; smoke package; testes de contrato/migração/recovery/isolamento/paridade; nenhuma validação/build/delivery implícita, agente real ou deploy automático. |

### Matriz de testes obrigatória

1. **TOML e compatibilidade:** schemas v1/v2 mantidos, evolução v2/v3, allowlist vazia, ID estável e não reutilizável, comando não autorizado, placeholders/injection, DAG, ciclo e referência ausente.
2. **Migração SQLite:** backup por API consistente em WAL, falha injetada em cada DDL/copy/version update, rollback v3 íntegro, FK, integrity check, versão futura, reabertura e dados/histórico anteriores preservados.
3. **Concorrência/idempotência:** processos simultâneos no mesmo projeto; somente um run ativo; operação repetida idêntica reutiliza resultado; digest divergente conflita; nenhum lock longo durante subprocesso.
4. **Snapshot:** commit/tree e manifesto iguais; blob OID/digest; mutação pós-criação detectada; mudança de branch/workspace original irrelevante; hooks, filters, autocrlf, symlink, hardlink, traversal, submodule e path Unicode/caractere especial.
5. **Estados e recovery:** todos os edges válidos/inválidos; dependentes `not_run` após fail/cancel/inconclusive; falha de spawn, timeout, cancelamento, crash pré/pós-intenção/pós-processo/pré-SQLite; reconciliação idempotente e sem retry de resultado desconhecido.
6. **Allowlist/toolchain:** argv separado sem shell; executable realpath/hash/version; PATH alterado, wrapper, alias, Git hooks/filters/config executável; gate não iniciado só por existir no TOML.
7. **Runtime e segurança:** local e Docker com capabilities reportadas; rede off por default onde controlável; exceção sem autorização negada; CPU/memória aplicadas ou `unavailable`; imagem por digest; read-only source, env mínimo, nenhum segredo herdado.
8. **Mutação/artefatos:** gates que tentam alterar/deletar source, symlink/escape no output, arquivo operacional/credencial, quota, truncagem, hash adulterado; fonte e checkpoint persistem iguais; mutação resulta failed/inconclusive, nunca pass.
9. **Logs/evidências:** sanitização de tokens conhecidos, truncagem bounded, redaction failure, retenção/expiração; evidência stale por revisão/checkpoint/gate/validator/policy não autoriza conclusão.
10. **Build/delivery:** proveniência completa e digest; builder/config divergente; destino ou autorização divergente; ausência de autorização bloqueia; falha após efeito vira unknown, sem repetição automática.
11. **Plataforma:** Windows/Linux; recursos não suportados reportados; Docker ausente; paths Unicode/case; CI Python 3.11–3.13; wheel instalada e smoke; nenhum Codex/agente ou deploy real nos testes.

### Riscos residuais e critérios de prontidão

Runtime local sob o mesmo usuário não é sandbox: não oferece contenção forte de filesystem/rede quando o OS não expõe controle confiável. Docker amplia isolamento, mas depende de daemon, kernel, image supply-chain, configuração e mounts. ACL, symlinks, permissões, locks, limites de recursos, signals e identidade de processo diferem entre Linux e Windows; capacidade indisponível deve ser explícita e bloqueada conforme política. Redaction não identifica todos os segredos. Builds/tests podem ser não determinísticos; proveniência e digests provam entradas, não comportamento correto. Entrega remota pode ter efeitos irreversíveis e resposta perdida; reconciliação pode exigir operador humano. Migração e retenção podem exigir recuperação manual.

Pronto para implementação após aprovação deste fechamento, desde que F5-01 fixe e teste valores/limites mensuráveis de recursos, logs, artefatos e retenção para cada backend/plataforma antes de F5-02/F5-03 alterarem interfaces e dados. Nenhum risco de runtime local pode ser descrito como isolamento garantido.

## Fase 6 — extensibilidade de agentes e contratos de modelo

### Escopo e limites

O roadmap reserva F6 para extensibilidade de agentes. A prioridade de integração é **Antigravity → Claude Code → OpenCode → Hermes Agent**, nesta ordem organizacional. Codex segue compatível. F6 prepara os contratos de seleção/modelo e integra as ferramentas nessa prioridade conforme cada superfície técnica for confirmada.

F6 não implementa classificador, seleção automática, escalonamento, retry automático, troca automática de modelo/provider nem decisão de orçamento. Seleção manual é por tentativa e só é registrada como efetiva quando o adapter/provider conseguir confirmá-la. Erros de ambiente, autenticação, quota ou infraestrutura nunca são classificados nesta fase como insuficiência do modelo. Docker, quality gates, builds, entrega e conclusão ficam fora do escopo.

O Toolkit permanece opcional. Qualquer integração exige avaliação de uma revisão/fonte e arquivos exatos, licença, proveniência, dependências e conflitos com políticas AgentFlow; a decisão de não integrar/deferir é válida. Não buscar, instalar, executar prompt nem alterar projeto automaticamente.

### Contratos separados

- `AgentAdapter`: integração com a ferramenta (Antigravity, Claude Code, OpenCode, Hermes Agent ou Codex); prepara a execução, verifica capabilities efetivas e interpreta o processo. Não escolhe modelos automaticamente, não repete e não conclui tarefa.
- `ProviderDescriptor`: identidade/API/version do provider de modelos, capabilities, limites, autenticação por referência e disponibilidade verificável. Nunca inclui credenciais.
- `ModelDescriptor`: identidade/revisão, provider relacionado, version, capabilities, context/modalidades/tools, disponibilidade observada com instante/fonte e metadados de preço com unidade/moeda/fonte quando verificáveis. Aliases mutáveis e preço desconhecido são tratados como tais.
- `ModelSelection`: escolha imutável por `attempt_id`, com agent, provider/model ID e versions, digests dos descriptors, modo manual/default, motivo/autor/tempo e identidade efetiva reportada. Divergência/inobservabilidade não troca modelo; registra `unknown`/`inconclusive` conforme contrato fechado em F6-01.
- `ModelRoutingPolicy`: contrato/schema versionado para fase futura: features/classificação, requisitos de capacidade, pesos e limites, budgets, gatilhos de escalation, falhas excluídas, autorização e auditoria. Em F6 é descritivo, snapshot/digest testado, proibido de ser executado.

O catálogo pode combinar registros declarados/versionados e disponibilidade consultada; resultado de disponibilidade deve ter timestamp/fonte e validade limitada. Não se presume acesso a catálogo remoto nem que ferramenta exponha provider/model. Snapshot de catálogo distribuído deve identificar versão e digest. Overrides/policies de projeto, se aprovados, são embutidos em `.agentflow/spec.toml`; ADR 0002 não permite adicionar outro arquivo versionado nesta fase. Disponibilidade e `ModelSelection` são estado operacional SQLite, não alterações de TOML. F6-04 verificou a compatibilidade com SQLite v4 e adicionou schema v5 apenas ao perfil `AgentExecutionStore`, preservando perfis legados. TOMLs antigos seguem legíveis; seleção/credenciais não são gravadas em arquivos operacionais versionados.

### Fechamento do gate F6-01

F6-01 foi concluída satisfatoriamente por auditoria documental oficial em 2026-11-05. O CLI Antigravity `agy` tem modo headless destinado a automação, protocolo stream-json, seleção manual via `--model`, status/erros estruturados, versão consultável (`agy --version`) e controles documentados de permissão/sandbox. O alvo F6-06 fica restrito a CLI headless e às versões/plataformas efetivamente homologadas. A documentação lista CLI v1.2+; isso é piso para preflight, não certificação de versões futuras.

O preflight verifica executável, versão, flags e capabilities sem iniciar agente. Seleção explícita só é confirmada pelo campo `model` do evento `init`; campo ausente/divergente fica `unknown` e produz resultado inconclusivo sem retry/fallback. `permission_mode` pode ser capturado; sandbox/rede sem atestação de enforcement ficam `unknown` e bloqueiam perfis que os exijam. Headless pode soft-deny ações de ferramenta e ainda retornar código zero, então status, stderr, eventos e exit code são interpretados em conjunto. O adapter não altera configurações do usuário; `--dangerously-skip-permissions` exige autorização específica registrada. Nenhum agente foi executado na pesquisa.

  Decisões fechadas: somente adapters built-in explicitamente registrados; nenhuma descoberta dinâmica/plugins; Toolkit fora do runtime e sem conteúdo incorporado; `ModelRoutingPolicy` é schema versionado não executável; sem router, retry, fallback, escalation ou troca automática. F6-04 inspecionou o schema v4 e adicionou migração v5 isolada ao perfil de execution store. Claude Code e OpenCode tiveram suas superfícies documentadas oficialmente verificadas antes da integração. As versões concretas disponíveis no host/provider e o suporte por plataforma ainda não foram homologados com execução real.

### Ordem proposta F6-01 a F6-10

| ID | Tarefa | Dependências | Critérios verificáveis |
| --- | --- | --- | --- |
| F6-01 | Fechar ADR 0006 e validar adequação técnica do Antigravity | F1–F5 | **Concluída:** documentação oficial comprova CLI headless adequado; versão/capabilities preflight sem agente; modelo manual verificável; controle não observável = unknown/fail-closed; decisões de plugins, Toolkit, persistência condicional e policy futura fechadas. Router/retry/fallback excluídos. |
| F6-02 | Formalizar `AgentAdapter`, registry e capabilities da ferramenta | F6-01 | API versionada, registry determinístico, sem import automático; adapter simulado substitui cada implementação; não altera execução/validação/conclusão. |
| F6-03 | Formalizar `ProviderDescriptor` e `ModelDescriptor` | F6-01, F6-02 | Identidade/versão/capabilities/availability/preço com fonte/unidades explícitos; provider/model ocultos ficam unknown; disponibilidade stale detectável. |
| F6-04 | Formalizar e persistir `ModelSelection` manual por tentativa | F6-02, F6-03 | Seleção vinculada a attempt/request digest, imutável e auditável; conflito entre selected/effective não causa fallback; v4 compatível ou migration v5 rollback-safe. |
| F6-05 | Definir schema versionado de `ModelRoutingPolicy` para uso futuro | F6-03, F6-04 | Schema valida budget/rules/authorization snapshots e digest; testes comprovam que F6 não avalia/executa a policy nem seleciona/escalona modelo. |
| F6-06 | Integrar Antigravity (prioridade 1) | F6-02 a F6-04 | **Implementada localmente:** documentação oficial confirma superfície `agy` headless; versão/flags/modelos preflight; modelo só confirmado pelo `init`; controles sem atestação = unknown e políticas incompatíveis bloqueiam; testes usam fake, nenhum agente real. Versão instalada/plataforma homologada pendente. |
| F6-07 | Integrar Claude Code (prioridade 2) | F6-06 | **Implementada localmente:** preflight `--version`/`auth status`, seleção `--model`, stream JSON e modelo confirmado por `system/init`; sem alterar settings, sem permissão ampliada. Sandbox restrita a shell e unknown fora dessa capacidade; API retry interno não repete tentativa AgentFlow. Homologação de versão/plataforma real pendente. |
| F6-08 | Integrar OpenCode (prioridade 3) | F6-07 | **Implementada localmente:** `run --format json`, provider/model explícitos, catálogo configurado sem refresh, `--pure` desabilita plugins externos no subprocesso; sem `--auto`. Campos de identidade efetiva ausentes/divergentes resultam unknown/mismatch; sandbox/rede unknown. Versão/plataforma real pendente. |
| F6-09 | Avaliar integração opcional Vibe Coding Toolkit e realizar testes cross-adapter | F6-01 a F6-08 | Decisão: não incorporar código, prompts ou dependência; nenhuma licença/proveniência upstream será presumida. Conformance, idempotência, reabertura, cancelamento e unknown devem passar com simuladores. |
| F6-10 | Docs, package, CI e homologação F6 | F6-01 a F6-09 | **Reavaliada com F6-11:** suíte completa, wheel/venv limpo, CLI smoke e `git diff --check` serão repetidos; CI deve executar Python 3.11–3.13 em Windows e Linux. Nenhum agente real, router ou retry automático. |
| F6-11 | Integrar Hermes Agent (prioridade 4) | F6-02 a F6-04, F6-06 a F6-10 | Adapter usa apenas CLI oficial; preflight verifica versão/flags; provider/model manuais; JSONL processado linha a linha, resultado terminal e exit code consistentes; seleção sem provider observável fica unknown; toolset restrito a `file`, sem memória/skills/delegação/terminal; timeout/cancelamento do runtime preservados; testes simulados e regressão dos adapters anteriores aprovados. |

### Matriz de testes F6

1. Contratos separados, versão de API, compatibilidade Codex e IDs/versions estáveis.
2. Provider/model catalog: duplicate IDs, aliases mutáveis, indisponibilidade/stale, preço/currency unknown e capability divergente.
3. Seleção manual: model/provider selecionado e efetivo coincidem; divergência/inobservabilidade não troca provider nem cria retry; operação idempotente preserva snapshot.
4. ModelRoutingPolicy: schema, budget/authorization snapshot e digest; policy never executed in F6; auto router code path absent.
5. Adapters Antigravity→Claude Code→OpenCode→Hermes: fakes de CLI/API/protocol, argumentos, versão, cancelamento, saída estruturada, toolset mínimo e plataformas; nenhum agente real.
6. Segurança: credenciais só por referências; logs/SQLite sem segredo; adapter que não prova sandbox/aprovação/rede falha fechado; plugin não autorizado não importa.
7. SQLite/TOML/workspaces: compatibilidade v1–v3, migração condicional v5/rollback, reabertura, operação idempotente, checkpoint e evidências preservados.
8. Recovery: failure categories distinguem insuficiência do modelo de ambiente/auth/infra; nenhuma falha ambígua se converte em escalation ou replay.
9. Toolkit: revisão upstream fixa, lista de arquivos, licenças/proveniência e hashes; integração ausente não acessa rede nem modifica conteúdo.
10. Homologação: pytest, package smoke, diff check e CI Python 3.11–3.13 em plataformas aprovadas.

## Fase 7 — Intelligent Model Routing (implementação local parcial; aprovação real bloqueada)

### Objetivo e fronteira

F7 decide qual `ModelSelection` será proposta para uma tentativa de tarefa executada pelo fluxo F3–F6. Usa `AgentAdapter`, `ProviderDescriptor`, `ModelDescriptor`, `ModelSelection` e o schema inerte de `ModelRoutingPolicy` existentes; não cria workers, filas concorrentes ou execução paralela. `ModelRoutingPolicy` deixa de ser inerte somente nesta fase e somente após receber avaliação versionada e autorização explícita. A decisão de roteamento não executa, valida nem conclui a tarefa.

O classificador avalia complexidade e risco a partir de sinais documentados da tarefa, seus critérios e histórico verificável. A saída é um registro versionado com classe, confiança, fatores, evidências, versão/digest do classificador e eventual override humano. Baixa confiança, dados insuficientes ou sinais conflitantes levam a seleção manual/pausa, nunca a escolha otimista. Modelos econômicos são candidatos para tarefas simples; intermediários para implementação convencional; avançados para planejamento, arquitetura, revisão crítica e tarefas complexas/de alto risco. A seleção ainda precisa satisfazer capabilities obrigatórias e compatibilidade do adapter.

A ordenação de candidatos pode considerar qualidade observada em avaliações independentes e comparáveis, latência histórica recente, preço com moeda/unidade/fonte e risco operacional. Catálogo e métricas têm timestamp, proveniência, validade e digest; preço, capacidade ou disponibilidade ausente/stale não é inventado. Catálogo-base pode ser distribuído com o pacote AgentFlow; overrides do projeto devem respeitar ADR 0002 e ficar em `.agentflow/spec.toml`, enquanto observações e seleções operacionais ficam em SQLite. Migração v5→v6 é esperada para os novos snapshots/ledger, aditiva, backup-first e rollback-safe; F7-01 confirma a versão real após inventário.

### Budgets, autorização e escalonamento

Budgets por tarefa, tentativa e execução registram custo/moeda, tokens, tempo e tentativas. Cada dimensão é `applied_verified` (mecanismo efetivo mais evidência), `estimated` (projeção sem garantia) ou `unavailable` (sem dado/controle confiável). Reservas transacionais impedem AgentFlow de admitir mais que o saldo contabilizado, mas não limitam por si só a fatura do provider. Policy de hard monetary cap exige upper bound verificável e capacidade de containment; caso contrário bloqueia spawn. Estimativa só pode ser usada com autorização explícita para cenário indicativo e nunca descrita como hard cap. Usage/fatura final incerto bloqueia gasto adicional que possa exceder o restante reconciliado. Workers futuros herdam budgets alocados (F8).

Seleção inicial pode ser automática apenas dentro de policy ativa, catálogo fixado e budgets autorizados. A escolha manual continua disponível. Qualquer alteração de modelo ou provider entre tentativas exige autorização explícita vinculada à tarefa/revisão, modelo/provider de destino, motivo, teto de custo/tempo/tokens, policy/catalog digest e expiração. Autorização genérica não autoriza fornecedor diferente. Sem autorização, a tentativa não muda. Escalonamento exige evidência verificada de que critérios objetivos não foram satisfeitos por insuficiência do modelo; falha de ambiente, autenticação, quota, infraestrutura, processo, timeout ou resultado desconhecido exclui escalonamento automático.

Escalonar significa propor e autorizar uma nova tentativa, não repetir a tentativa anterior. Tentativa desconhecida, interrupção, efeito externo incerto ou workspace não reconciliado bloqueiam toda nova execução até reconciliação humana e determinística. O checkpoint Git F4 e snapshot/resultado de validação F5 permanecem vinculados à revisão/tentativa original. Evidências de outro checkpoint, definição, modelo ou validador não podem ser reutilizadas. Resultado de modelo, código de saída e autoclassificação não substituem validação independente nem conclusão.

### Ordem planejada F7-01 a F7-10

| ID | Tarefa | Dependências | Entregáveis | Critérios de aceite e testes |
| --- | --- | --- | --- | --- |
| F7-01 | Fechar contratos SDD, autoridade humana e gate de implementação | F1–F6 | ADR 0008; contrato WebAuthn/FIDO2, enrollment, broker/UI/IPC trust boundary, armazenamento público, threat model; inventário SQLite v5/TOML e plano de migração v6. | `userVerification=required`, challenge/origin/RP/flags verificados; UI mostra resumo confiável de toda a transação; grant scoped/one-use/revocable/short-lived; agente não alcança credenciais, UI, broker ou store; ausência de qualquer capability bloqueia. **Gate atual não satisfeito:** runtime same-user não comprova separação; registrar bloqueio e não iniciar F7-02 a F7-10 antes de homologar broker isolado em Windows/Linux. Migração v6 só é aplicada depois desse gate. |
| F7-02 | Persistir `SpecificationRevision`, `PlanRevision`, ChangeImpact, decisão e eventos | F7-01 | Migração SQLite v6 aditiva, backup-first e rollback-safe; snapshots imutáveis; idempotência por `operation_id`. | Import/reopen cria proposta, nunca aceita; mesma operação/digest retorna registro; digest divergente conflita; crash antes/depois do commit recupera determinística; FK/integrity e abertura v5 preservam bytes/dados/histórico; testes transacionais, concorrência multi-processo e migration rollback. |
| F7-03 | Evoluir schemas TOML com RequirementTrace, cobertura e replan | F7-01, F7-02 | Schema compatível para vínculo requisito→task criterion; relatório de cobertura; diff semântico e `ChangeImpact`; fluxo explícito `RevalidationDecision`/`Replan`. | Referências, ciclos e critérios ambíguos falham; critérios sem cobertura bloqueiam aceitação salvo adiamento explícito; mudanças computam descendentes e artefatos afetados deterministamente; arquivos legados abrem como `legacy_unmapped` e não ganham cobertura presumida; regressões de leitura/migração/round-trip TOML. |
| F7-04 | Criar catálogo versionado e prova de capabilities | F7-01, F6 | `ModelCatalogSnapshot`, `ProviderCapabilityEvidence`, provenance, expiração e catálogo base; diferenciar advertised/observed/enforced. | Identidade provider/model/version e source/validity/digest fixos; capability é `supported/unsupported/unknown`; dado stale/ausente vira unknown; teste de aliases, mudanças de catálogo, versões não homologadas, capacidade anunciada sem prova e provider indisponível. |
| F7-05 | Implementar classificador e evaluator determinístico de seleção | F7-03, F7-04, F5 | `TaskClassification`, política de tier/capability, critérios independentes de qualidade/custo/latência/risco e `ModelRoutingDecision`. | Mesmas revisões/snapshots geram mesma saída; baixo confidence/conflicto/informação insuficiente exige manual/bloqueia; tarefas simples podem escolher econômico, implementação intermediário, planejamento/revisão crítica exigem tier/capabilities mínimos; métricas vêm de validação F5 comparável; desconhecido nunca pontua como favorável; testes property/golden e sem executar agentes. |
| F7-06 | Implementar budgets por tarefa/tentativa/execução e ledger | F7-02, F7-04 | `BudgetEnvelope`, reserva, consumo, reconciliação, dimensão/capacidade de enforcement e provenance. | Atomicidade impede alocação AgentFlow além do saldo sob concorrência; cada limite marca `applied_verified`, `estimated` ou `unavailable`; hard financial cap bloqueia quando worst-case não puder ser provado/contido; estimativa não é reportada como enforcement; limites de tempo/tentativa testados; billing final unknown bloqueia gasto adicional; testes crash/reopen/concorrência e arredondamento/moeda. |
| F7-07 | Implementar grants de roteamento e autorização de troca de modelo/provider | F7-01, F7-05, F7-06 | `RoutingAuthorization` e `ModelChangeAuthorization`; snapshot de seleção manual/automática por tentativa. | Grant autenticado liga de/para, revisão/requisitos, impact/evidence, budget, expiração e operation ID; agente não tem grant/API/secret; mudança sem autorização bloqueia; consumo de grant é atômico e não repetível; testes de adulteração, replay, identidade e revogação. |
| F7-08 | Integrar escalation por evidência e recovery conservador | F7-05 a F7-07, F3, F5 | Taxonomia causal, `EscalationEvidence`, proposta de próxima tentativa e reconciliação. | Só falha objetiva F5 atribuível a insuficiência pode propor escalonamento; ambiente/auth/quota/infra/process/timeout/unknown não escalonam; execução anterior precisa terminal/reconciled; novo attempt ID, checkpoint e budget cumulativo; aprovação explícita; simulações de crash/resultado externo desconhecido nunca fazem replay. |
| F7-09 | Completar auditoria, inspeção, compatibilidade e regressões | F7-02 a F7-08 | `agentflow audit --format human|json` read-only para revisões/digests, cobertura, impacto, catálogo/capabilities, decisões, budgets/ledger, propostas, evidências e pendências. | JSON/humano; `mode=ro&immutable=1`; recusa WAL; nunca migra/escreve, executa provider ou toca grants; omite snapshots textuais, payloads e logs; verifica digests/links e sinaliza divergências; testes de banco byte-identical, segredo sentinela, WAL, esquema antigo e CLI. |
| F7-10 | Homologar matriz de segurança, plataformas, pacote e CI | F7-01 a F7-09 | Release candidate documental, suíte integral, build/instalação e CI. | Todos os contratos/estados/budgets/autorização testados em Windows e Linux, Python 3.11, 3.12 e 3.13; nenhum agente real necessário na CI; Codex/adapters anteriores regressivos; sem workers paralelos; falha em qualquer plataforma impede aprovação. |

### Entrada e gate de saída F7

Entrada: F6 integrada e homologada; SQLite v5 e TOML inventariados; F5 oferece evidências verificadas; broker/authenticator comprova ação independente e a interface/control plane não pode ser controlada pelo runtime agente. **Estado atual: bloqueada em F7-01; tarefas F7-02–F7-10 não iniciadas.** Após o gate, saída exige revisions/coverage SDD aceitos, seleção auditável, capabilities comprovadas, budgets sem alegar enforcement falso e escalation somente por evidência/grant. Sem limite financeiro verificável, hard-cap policy recusa o provider. F7 permanece serial, sem workers paralelos.

### Atualização de execução controlada

A autorização local subsequente permite somente um facade de autoridade sempre indisponível e os componentes independentes de grant. Isso **não satisfaz a condição de entrada** acima: F7-01 permanece bloqueada quanto à fronteira confiável, enrollment, IPC e consentimento real. O trabalho local parcial implementa schema TOML tasks v4, migração SQLite v6 aditiva, propostas imutáveis/idempotentes, catálogo com capabilities verificadas separadas de claims, classificação determinística e avaliação somente preview. Não há aceite de revisão, troca de provider/modelo, escalonamento, retry, dispatch por policy ou execução de agentes.

| Tarefa | Estado local após autorização controlada |
| --- | --- |
| F7-01 | Contratos e facade fail-closed presentes; homologação de broker/WebAuthn/UI/IPC e isolamento ainda bloqueada. |
| F7-02 | Propostas SDD/ChangeImpact e migração v6 implementadas; aceite por grant permanece desabilitado. |
| F7-03 | Schema tasks v4, `satisfies`, adiamentos explícitos e compatibilidade legada implementados. Replan/aceite ainda não executáveis. |
| F7-04 | Snapshot versionado e proveniência de capability implementados; verificação real dos providers permanece externa/desconhecida. |
| F7-05 | Classificador e evaluator determinísticos em modo preview; sem dispatch ou fallback. |
| F7-06 | Apenas avaliação estimada/unavailable de budgets no preview; ledger/reserva e hard enforcement não implementados. |
| F7-07 | Não implementada além do contrato `ApprovalAuthority`; grants reais bloqueados. |
| F7-08 | Não implementada; sem escalonamento/retry. |
| F7-09 | Resumo de auditoria read-only no store; CLI completa ainda pendente. |
| F7-10 | Pendente da suíte local final, instalação e matriz remota Windows/Linux. |

### Continuação local sem autoridade real — atualização

Uma autorização posterior permite completar somente os componentes que não dependem de concessão real. O gate de grants permanece bloqueado; essa exceção não habilita aceitação de revisões, troca de provider/modelo, despacho ou retry.

| Tarefa | Estado após continuação controlada | Evidência/limite |
| --- | --- | --- |
| F7-01 | Parcial; **gate de confiança bloqueado** | Facade segue `approval_unavailable`. ADR 0008 agora define plano de broker remoto/domínio separado; sem serviço, UI, WebAuthn ou prova de isolamento real. |
| F7-02 | Implementada localmente | Revisões/propostas SDD persistidas e imutáveis, idempotentes; schema v6 preservado. Aceite continua bloqueado. |
| F7-03 | Implementada no subconjunto versionável | TOML tasks v4, cobertura/defer explícitos; compatibilidade v1–v3. Fluxo de aceite/revalidation que exija consentimento não está ativo. |
| F7-04 | Implementada como catálogo versionado | Snapshots/capabilities e proveniência; capability não verificada permanece desconhecida. |
| F7-05 | Implementada como evaluator preview | Classificação determinística e decisão auditável persistida com snapshots exatos da especificação, policy, catálogo, budget e alternativas; `dispatch_allowed=false` invariável. |
| F7-06 | Implementada como ledger local transacional | Migração aditiva v7; contas execution/task/attempt, reserva idempotente, ledger append-only, reconciliação parcial/conhecida/estimada/desconhecida, concorrência e recovery conservador. `applied_verified` descreve somente o limite que o ledger local consegue aplicar; enforcement de fatura/provider permanece unavailable. |
| F7-07 | Bloqueada | Nenhum grant real; autoridade continua fail-closed. |
| F7-08 | Proposta persistente, execução bloqueada | Proposta requer falha F5 verificável, checkpoint/revisão/model identity/capability/custo válidos, traz evidências, delta estimado, expiração e idempotência; não chama runtime e requer grant independente. |
| F7-09 | Implementada localmente no escopo sem autoridade | CLI `agentflow audit` humana/JSON cobre revisões/digests, cobertura, impactos, catálogo, preview, budgets/ledger, escalonamento, evidências e pendências; somente leitura, sem secrets e sem adapters. |
| F7-10 | Pendente de homologação | Aguarda suíte completa, smoke, diff-check e, em etapa remota autorizada, CI Windows/Linux e Python 3.11–3.13. |

SQLite permanece compatível com as fases anteriores: v6 SDD/catalog fica inalterado; v7 é migração aditiva para snapshots completos de roteamento, ledger/reservas e propostas de escalonamento. Migração e rollback são cobertos por bancos temporários/testes; nenhum banco operacional existente foi aberto ou modificado nesta continuação. O despacho automático continua ausente.

F7-09 foi concluída localmente para inspeção. Se houver WAL pendente, o comando recusa leitura: o modo immutable omitiría commits ainda no WAL, e uma leitura WAL normal poderia criar sidecars. A CLI pede que escritores sejam encerrados e o checkpoint operacional seja feito por fluxo próprio; não o executa.

## Fase 8 — Worker Orchestration (planejada; não implementada)

### Modelo e contratos dos workers

Worker é uma identidade de execução persistida; não é agente, provider, modelo, tarefa ou processo. Entidades separadas se relacionam por snapshots e FKs: `WorkerDefinition` (ID/versão/capabilities/role), `WorkerIdentity` (ID estável e geração/fencing token), `WorkerAssignment` (task ID/revisão, `attempt_id`, adapter/agent, `ModelSelection`, workspace, budget e operation IDs), `WorkerState`/`WorkerEvent` (estado e auditoria append-only), `WorkerLease` (owner, heartbeat, expiração e fencing token), `WorkerScope` (escopo de paths/recursos autorizado) e `WorkerResult` (execução, validação, evidências e checkpoint referenciados). Snapshots de assignment e policy são imutáveis; segredo, credencial ou conteúdo de log integral não entra no registro.

O ciclo de vida do worker (`queued`, `claimed`, `provisioning`, `ready`, `active`, `cancelling`, `recovering`, `quiescent`, terminal) não substitui estados independentes de processo, validação, checkpoint e tarefa. Scheduler persiste a intenção/assignment antes de provisionar e avança por transações curtas; subprocessos nunca rodam dentro da transação SQLite. `operation_id` e digest vinculam enfileirar, claim, heartbeat, cancelamento e recuperação; mesmo ID com parâmetros diferentes conflita. Repetição de chamada idempotente não reexecuta spawn/Git.

Cada assignment recebe workspace/worktree Git própria, branch própria e escopo autorizado da revisão segundo F4. Checkout original permanece intacto. Worker não altera branch/ref de outro workspace; checkpoint é feito somente com pathset explícito e revalidado, index isolado e integridade verificada. Worker sem mudança versionada ainda precisa deixar resultado/evidência auditáveis. Conclusão exige evidências F5 vinculadas à mesma revisão/checkpoint, nunca sucesso do worker/processo/commit isoladamente.

### Scheduler, filas, locks e recuperação

A primeira etapa suporta workers independentes em execução sequencial: fila persistida, claims transacionais, ordenação determinística por prioridade declarada/dependências e um worker ativo por projeto. Prioridade não muda dependências nem pode causar starvation sem política explícita. O scheduler revalida tarefa `ready`, plano/revisão, autorização, budget global/local, capabilities, escopo e workspace antes de cada claim/spawn. Nenhuma tarefa é executada apenas por estar enfileirada; início requer solicitação/policy explícita aprovada.

Heartbeat e lease indicam liveness, não prova de término. Lease inclui geração monotônica/fencing token para que dono obsoleto não grave resultados após reassignment; timeout de lease nunca autoriza matar processo por PID isolado nem assumir que worker morreu. Reclaim exige verificar identidade do processo (F3), lock/owner (F4), workspace, tentativa e eventos. Se estado ou efeito for ambíguo, assignment fica bloqueado para reconciliação; nunca se reexecuta. Pai encerrado, rede local indisponível ou heartbeat atrasado não convertem resultado em sucesso/falha.

Paralelismo controlado é uma segunda etapa separada e permanece desativado até homologação do isolamento, locks interprocesso, fencing, limites, cancelamento, recovery e concorrência em Windows/Linux. O scheduler pode ativá-lo apenas para scopes versionados explicitamente disjuntos; pathsets desconhecidos ou sobrepostos colidem e são serializados/bloqueados. Dependências sempre prevalecem. Recursos e custo de cada worker são envelopes reservados atomicamente do budget global. Limite de concorrência por projeto/provider/host é explícito; default 1. Git continua somente local; integração de resultados não pode executar operação remota ou fora do workspace.

### Etapas e tarefas planejadas F8-01 a F8-10

**Etapa A — workers sequenciais independentes (F8-01 a F8-07).**

| ID | Tarefa | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F8-01 | Fechar ADR F8: identidade, estados, prioridade, liveness, locks, quotas, scopes e recuperação | F1–F7 | Parâmetros numéricos, owner/fencing, expiração, retenção e critérios de ambiguidade definidos por plataforma; nenhum lease expirado implica retry ou kill automático. |
| F8-02 | Versionar contratos e snapshots Worker/Assignment/Scope/Budget | F8-01 | Contratos distinguem worker, tarefa, agent, provider/model, workspace e processo; cada assignment referencia `SpecificationRevision`, `PlanRevision` e requirement/criterion IDs; snapshots/digests imutáveis e compatibilidade legada testáveis. |
| F8-03 | Definir persistência e migração SQLite para fila, eventos, lease e budget | F8-02 | Migração posterior ao schema existente é aditiva, backup-first, transacional/rollback-safe, FKs íntegras, append-only auditável e abertura de bancos anteriores não reinterpreta histórico. |
| F8-04 | Provisionar workspace/branch própria via `GitProvider` F4 | F8-02, F8-03 | Um assignment tem raiz/branch/base isoladas verificadas; nenhuma escrita no checkout original ou fora do workspace; IDs e operation_id idempotentes. |
| F8-05 | Implementar fila e scheduler sequencial determinístico | F8-03, F8-04 | Dependências/status/budgets/capabilities são revalidados no claim; só uma execução ativa por projeto; concorrência multi-processo não duplica worker nem tentativa. |
| F8-06 | Implementar heartbeat, lease, fencing, cancelamento e recovery | F8-05, F3, F4 | Dono obsoleto não grava; PID reutilizado não é adotado/morto; crash nas janelas SQLite/spawn/Git torna assignment inconclusivo e reconciliação repetida é idempotente sem replay. |
| F8-07 | Vincular worker a resultado, validação, evidência e checkpoint | F8-04 a F8-06, F5 | Execução, validação e conclusão permanecem estados distintos; resultado rastreia assignment, revisão/plano/requisitos, checkpoint e gates; evidências têm revisão/checkpoint correto; tarefa sem evidência verificada ou checkpoint exigido não conclui. |

**Etapa B — paralelismo controlado (F8-08 a F8-10).**

| ID | Tarefa | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F8-08 | Homologar isolamento, scopes, locks, budgets e recovery sob concorrência | F8-01 a F8-07 | Testes interprocesso Windows/Linux injetam crash/atraso/PID reuse; sem corrupção de DB, escrita fora de worktree, duplicação, orçamento excedido ou conclusão prematura. Falha mantém paralelismo desativado. |
| F8-09 | Implementar execução paralela opt-in com limites por projeto/provider/host | F8-08 | Default continua 1; apenas scopes explicitamente disjuntos simultâneos; conflito/escopo desconhecido serializa; quotas globais reservadas atomicamente; cancelamento não mata workers alheios. |
| F8-10 | Homologar pacote, regressões, CI e rollout gradual | F8-01 a F8-09 | Sequencial permanece funcional e padrão; paralelismo ativável somente com gate F8-08 aprovado e configuração explícita; matriz, migração, recovery e métricas comprovam todos os limites. |

Entrada: F7 homologada; F3/F4/F5 preservadas e cobertas por regressões; políticas de custo do F7 transacionáveis. Saída da Etapa A não habilita paralelismo. Paralelismo é liberado apenas depois do gate F8-08; se não houver prova de isolamento/scope ou suporte de plataforma, aquele runtime permanece limitado à execução sequencial.

## Fase 9 — Advanced Coordination (planejada; não implementada)

F9 coordena resultados de workers especializados e mantém explícita a distinção entre decomposição, execução, revisão, integração, validação e conclusão. Pode executar especialistas em sequência. Paralelismo é herdado apenas se F8-08/09 tiverem sido homologados e habilitados; F9 não exige simultaneidade.

Contratos planejados: `WorkerRoleDescriptor` (função, versão, capabilities e limites, sem conflar com agent/model), `CoordinationPlan` (DAG versionado de etapas/atribuições e dependencies), `ResultProposal` (checkpoint/pathset/evidências/snapshot e worker de origem), `ReviewDecision` (revisor independente, criteria/digest/resultado), `IntegrationPlan` (base, checkpoints de entrada, escopos, autorização/operation_id) e `ConflictRecord` (paths/blobs conflitantes, causa, opções e decisão auditável). Propostas não alteram plano/TOML ou Git até aprovação e operação local gerenciada F4. Um agente não pode aprovar sua própria alteração; critérios de qualidade continuam em F5.

Planejamento pode decompor tarefas e recomendar dependências/prioridades, mas mutações à spec/tasks necessitam revisão/revalidação segundo F2. Workers de implementação ficam em worktrees/branches separados e recebem `WorkerScope`; worker de revisão lê checkpoint imutável e não escreve nele. Integração ocorre somente por operação AgentFlow autorizada, em workspace destino próprio, usando commits/checkpoints verificados, pathset explícito e validação independente no resultado integrado. Conflito é estado explícito; o sistema apresenta alternativas/evidências e aguarda decisão autorizada, sem escolher “vencedor”, rebase ou descarte silenciosamente. Nenhuma operação Git remota é parte de F9.

O otimizador global compara custo, qualidade, latência, risco e budgets de forma auditável. Prioriza qualidade dos critérios e limites duros; não pode trocar provider/modelo sem autorização F7, aumentar concorrência sem F8, ampliar permissões ou consumir orçamento não alocado. Métricas agregadas preservam proveniência e minimizam conteúdo sensível. Resultado incerto mantém coordenação bloqueada até reconciliação; nenhum resultado parcial vira conclusão de projeto.

| ID | Tarefa | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F9-01 | Fechar ADR F9: papéis, ownership, aprovações, integração e política de conflito | F1–F8 | Decide autoridade por transição, limite de autoaprovação, versão dos contratos e casos de paralelismo; nenhuma etapa ambígua pode editar ou integrar automaticamente. |
| F9-02 | Definir roles especializadas e capability/permission profiles | F9-01, F6 | Roles são independentes de agent/provider/model; capabilities/permissões mínimas exigidas, incompatibilidades e versões verificadas; sem ampliação de acesso. |
| F9-03 | Criar `CoordinationPlan` DAG e dependências entre propostas de trabalho | F9-01, F8-02 | IDs estáveis, `SpecificationRevision`/`PlanRevision`, requisito(s) e snapshots/digest rastreados; ciclos/ref inválida recusados; plano só agenda tarefas aprovadas e respeita DAG original. |
| F9-04 | Implementar interface de planejamento/decomposição como propostas | F9-02, F9-03 | Propostas incluem rationale/criteria/risco; não modificam TOML, estado de tarefa ou autorização sem revalidação explícita. |
| F9-05 | Definir envelopes de resultados e checkpoints por worker | F9-02, F8-07 | Cada output referencia specification/plan/task revisions, requirement/criterion IDs, worker/workspace/checkpoint/pathset, evidências e digests; material ausente ou stale é rejeitado. |
| F9-06 | Implementar review independente e quality gates do resultado | F9-05, F5, F7 | Worker autor não valida sua própria alteração; gate aponta para snapshot/checkpoint exato; `failed/inconclusive/not_run` bloqueiam integração/conclusão. |
| F9-07 | Definir e detectar conflitos entre escopos e alterações | F9-03, F9-05, F4 | Detecção determinística por base/tree/path e locks; sobreposição desconhecida conflita; dados originais preservados até decisão. |
| F9-08 | Implementar `IntegrationPlan`, resolução autorizada e reconciliação | F9-06, F9-07 | Integração local somente em workspace separado, por pathset e operation_id explícitos; conflito exige escolha auditada; crash não duplica ou descarta commits. |
| F9-09 | Implementar orçamento/otimização global e observabilidade distribuída | F7, F8, F9-03 a F9-08 | Alocação respeita saldo transacional e concorrência habilitada; métricas atribuem custo/qualidade/latência/risco por execução; nenhum limite é excedido. |
| F9-10 | Homologar coordenação, regressões, compatibilidade e rollout | F9-01 a F9-09 | Cenários sequenciais e paralelos autorizados passam em crash/reopen/ambiguidade; requirement-to-result trace e invalidação stale passam; migrations/old projects e Codex/adapters legados continuam compatíveis; CI aprovada. |

Entrada: F7 homologada; F8 Etapa A concluída e, para qualquer paralelismo, F8-08/09 aprovadas; F4 continua impedindo operações Git remotas e alterações fora de workspaces; F5 mantém validação independente. Saída: coordenação auditável e reversível somente mediante novas operações explícitas, com integração de resultados verificados e conflitos resolvidos por decisão autorizada.

### Riscos e decisões arquiteturais ainda pendentes para F7–F9

- Qualidade comparável entre modelos depende de tarefas/suites representativas, versões fixas, validação independente e política de privacidade; avaliações históricas podem ter viés e pouca amostra.
- Providers podem ocultar preço, tokens efetivos, retries internos, limites, latência ou identidade; um hard budget não é garantível sem teto de consumo controlável. Política deve recusar a opção quando o teto não puder ser reservado/forçado.
- Classificador pode subestimar risco/complexidade ou expor dados; features, limiar de confiança, explicabilidade e possibilidade de classificação assistida são decisões do ADR F7.
- F7-01/F7-06 precisam fixar moeda de contabilização, fontes/timestamp de câmbio, credits/prepaid e reconciliação; estimativas nunca satisfazem hard caps.
- F8 precisa fixar números de workers por host/projeto/provider, duração/heartbeat de leases, fairness/starvation, escalonamento de prioridade, fencing e quotas por plataforma. Expiração nunca prova término.
- Worktree Git isola checkout/branch, mas não o filesystem/processo sob o mesmo usuário. Scope pathset não é sandbox; perfis que exigem isolamento forte precisam runtime capaz de provar enforcement ou permanecem indisponíveis.
- Paralelismo depende de bloquear escopos sobrepostos; imports/builds podem alterar arquivos globais ou compartilhados além de pathsets. Se writes indiretas não forem confináveis, tarefas devem ser serializadas.
- F9 precisa decidir estratégia de integração local (commits escolhidos, patch/bundle ou cherry-pick gerenciado) sem contrariar o ADR 0004; não presumir que Git merge/rebase fica permitido.
- Autenticação independente de aprovador depende de provider/UI por plataforma; se prova de identidade e consentimento fora do agente não estiver disponível, aceite fica bloqueado, sem fallback para `author` autodeclarado.
- Migrações futuras precisam manter leitura de bancos F1–F6, triggers append-only, backups e recovery; não se fixa antecipadamente o próximo schema version até inventário técnico e ADR da fase.

Este roadmap não autoriza implementação de F7, F8 ou F9. Cada fase exige revisão arquitetural/ADR e autorização próprias; F9 depende dos gates reais de F7/F8, não de suposição de paralelismo.

### Matriz de testes futura F7–F9

| Fase | Camada | Cenários mínimos de aceite |
| --- | --- | --- |
| F7 | Classificação/catálogo | Mesmo snapshot gera mesma decisão; confiança baixa, capability ausente, catálogo stale e dados de preço/latência desconhecidos bloqueiam ou pedem override explícito; digest/version mismatch invalida decisão. |
| F7 | Budget/autorização | Reservas concorrentes não excedem teto; falha pré-spawn reverte reserva conforme operação; custo posterior é reconciliado; uso/preço desconhecido bloqueia próxima tentativa; provider/modelo diferente sem consentimento exato falha. |
| F7 | Escalonamento/recuperação | Evidência F5 de critério falho por insuficiência pode propor nova tentativa autorizada; erro de ambiente/auth/quota/infra, timeout ou resultado desconhecido não escala nem repete; checkpoint antigo permanece associado à tentativa antiga. |
| F7 | Regressão/qualidade | Suites independentes e versionadas comparam modelos sem usar autoclassificação como aprovação; Codex e seleção manual continuam funcionando; policies/TOMLs anteriores permanecem legíveis. |
| F8 | Identidade/queue/lease | Assignment e `operation_id` idempotentes; parâmetros divergentes conflitam; DAG/priority determinística; lease expiry, heartbeat atrasado, PID reuse, dono antigo e crash não provocam claim/spawn duplicado. |
| F8 | Workspaces/scopes | Cada worker usa workspace/branch própria; checkout original inalterado; traversal, symlink escape, scope vazio/unknown, paths sobrepostos e submódulos são bloqueados/serializados; staging continua allowlist F4. |
| F8 | Concorrência/recovery | Etapa sequencial garante um ativo por projeto; testes interprocesso Linux/Windows exercitam fencing, budget agregado, cancelamento de um worker sem atingir outro, falha entre SQLite/spawn/Git e reabertura repetida. Qualquer violação mantém paralelismo desativado. |
| F8 | Limites/plataforma | CPU/memória/processos/tempo/logs/armazenamento reportados por capacidade efetiva; recurso não aplicável fica indisponível e policy obrigatória recusa; matriz Python/plataforma e smoke de pacote passam. |
| F9 | Coordenação/revisão | DAG sem ciclo; worker não atualiza spec/tasks sem revalidação; implementação e revisão são identidades/assignments diferentes; autor não aprova o próprio checkpoint; evidência stale ou validator inativo não libera integração. |
| F9 | Conflito/integração | Propostas baseadas em checkpoints verificados; conflito de path/blob e conflito semântico exercitados; integração usa destino isolado/pathset explícito; operação ambígua não repete; decisão humana/auditoria preservadas; nenhuma operação remota. |
| F9 | Orçamento/auditoria/regressão | Limite global respeitado sequencialmente e em paralelismo homologado; custos atribuídos por worker/model/attempt; logs sanitizados; reabertura/recovery preservam events, checkpoints, autorização e dados F1–F8. |
| F7 | Autorização Windows/Linux | Grant ligado a digest/impact/escopo, identidade verificada e uso único; ausência de provider que comprove a ação humana fora do agente bloqueia aceite/troca. |
| F7 | Matriz de versões | Suíte, migração, pacote e CLI passam em Windows/Linux com Python 3.11, 3.12 e 3.13; falha em qualquer combinação bloqueia homologação. |
| F7 | Capacidade e limites de provider | Fixtures separam capability anunciada/observada/enforced; unknown não satisfaz policy; hard cap sem worst-case controlado bloqueia execução; estimates permanecem não vinculantes. |

## Trilha transversal MVP-20/MVP-21 — Intelligent Failure Recovery

Esta trilha é um adendo ao roadmap, não renumera fases homologadas nem declara dispatch pronto. A decisão está em [ADR 0009 — IFR](decisions/0009-intelligent-failure-recovery.md). A base IFR é pré-requisito de recuperação para a execução operacional; AQA completo fica condicionado à homologação de execução real.

| Marco | Entrega | Critério de saída |
| --- | --- | --- |
| MVP-20-IFR-01 | Contratos versionados para resultado de qualidade (`PASS`/`FAIL`/`INCONCLUSIVE`), estados de execução/autorização separados, incidente, tentativa, diagnóstico, estratégia, decisão IFR e intervenção humana. | Decodificação estrita; estado de qualidade não altera estado de processo/grant; agente não pode autorizar; compatibilidade de snapshots legados definida sem reinterpretar histórico. |
| MVP-20-IFR-02 | Integração com controlador grant-scoped, `CodexAdapter`/`LocalRuntime`, State Store e journal append-only. | Cada tentativa registra operation ID, estratégia, ações, diff/checkpoint/evidência, falha, hipótese, diagnóstico e limites; workspace, grant, digest, validade e revogação validados antes do efeito. |
| MVP-21-IFR-03 | Policy de diagnóstico e retry, assinatura normalizada com contexto, detecção de equivalência/loop e budgets configuráveis. | Três tentativas totais iniciais por incidente (incluindo a primeira), limites configuráveis de equivalência/duração/orçamento; encerramento antecipado se inútil/inseguro; determinística não repete cegamente. |
| MVP-21-IFR-04 | Reconciliação, escalonamento humano e retomada persistente. | `unknown` nunca é repetido sem prova de ausência de efeito ou reconciliação segura; intervenção autenticada ligada à tarefa e escopo não expande; restart/replay/concurrency idempotentes. |
| MVP-21-IFR-05 | Homologação IFR sintética e, somente sob autorização específica, operacional em workspace temporário. | Todos os dez cenários da ADR 0009 passam; recuperação e limites comprovados; real executor distinguido de mocks; dispatch continua fechado fora do grant. |
| AQA-IFR-06 (posterior) | Avaliação adversarial independente, Critic orientado por evidência e Quality Gates. | Só inicia após homologação da execução real; avaliador independente, evidência vinculada ao snapshot exato; `FAIL`/`INCONCLUSIVE`/`not_run` bloqueiam integração/conclusão; AQA não emite permissões. |

**Checkpoint MVP-20.1:** contratos de qualidade/diagnóstico e policy pura implementados em `agentflow.apl.failure_recovery`; controlador contínuo ligado ao journal SQLite existente; `operation_id` estável, `attempt_id` por tentativa, binding imutável dos limites IFR, retry restrito a efeito comprovadamente seguro e estratégia coberta pelo grant, detecção contextual de repetição e resumo de escalonamento persistido. Testes Windows: 47 focados e 622 na suíte (1 skip POSIX). A fatia está funcionalmente implementada, mas não homologa execução real; resposta autenticada à intervenção, reconciliação operacional de efeitos e retomada pós-intervenção seguem pendentes. Os estados PASS/FAIL/INCONCLUSIVE foram exercitados com validadores sintéticos. Ver [relatório MVP-20](mvp-20-controlled-codex-execution.md).

### Adendo obrigatório — grant operacional e quotas

Este adendo refina os critérios de saída MVP-20/MVP-21; não libera dispatch. Um contrato operacional persistido deve cobrir todas as tarefas previstas em um plano imutável específico (digest e revisão), workspace canônico e sua identidade, executor/versão, operações, limites e validade. Uma solicitação explícita inicia o contrato sem senha nem confirmação por tarefa; não é uma autorização ilimitada de sessão. Qualquer mudança material em plano, workspace, executor, operações ou limites exige novo contrato. Iniciadores agent/service requerem allowlist/configuração prévia. Revisão do plano e início de execução são ações distintas.

Antes de qualquer piloto, implementar um ledger transacional no State Store/journal existente, sem banco paralelo. Em uma transação serializada e protegida contra concorrência entre processos, revalidar estado/expiração/revogação e binding do contrato, reservar a tarefa e suas quotas antes de iniciar efeito; persistir separadamente valores **reservados**, **consumidos** e **liberados**. Liberação só é permitida com evidência confiável de ausência de efeitos. Oversubscription deve ser impossível entre workers/processos. O último ponto controlado antes de criar o processo executor deve verificar a autorização e consumir/reservar atomicamente a permissão; uma checagem seguida de uma janela independente até o despacho não satisfaz este gate.

Expiração da proposta de consentimento, expiração do grant e estado histórico da decisão são dimensões distintas. Retomada, recuperação ou reinício não renova prazo. Revogação impede novas reservas imediatamente e solicita cancelamento das tarefas ativas; o cancelamento pode não ser instantâneo e não reverte efeitos já produzidos. Processos órfãos e resultados ambíguos devem permanecer `unknown`/bloqueados até reconciliação segura, sem duplicar efeitos.

IFR pode escolher retry somente dentro do contrato vigente: retries não ampliam escopo, não alteram plano, não renovam contrato e consomem quotas/limites da mesma autorização. Falhas `unknown` não liberam quotas nem repetem operação sem prova/reconciliação. Registrar custo estimado, observado e qualquer teto efetivamente imposto em campos separados. Estimativas não são teto financeiro; não declarar hard cap sem enforcement comprovado pelo provider.

`cwd`/diretório de trabalho identifica contexto, mas não é fronteira suficiente de isolamento do filesystem. Registrar controles de isolamento por plataforma como `PASS`, `FAIL`, `INCONCLUSIVE` ou `BLOCKED`, com evidência concreta e justificativa; não inferir isolamento de uma configuração de processo.

Antes de atualizar allowlist, verificar Codex CLI 0.167.2 por testes comportamentais, versão/argumentos observados e evidências de timeout, cancelamento, captura e saída. Não usar override do gate. O piloto futuro terá pelo menos duas tarefas dependentes em workspace temporário descartável (por exemplo, criar arquivo sintético e depois validá-lo), com limites e plano/digest apresentados ao operador. Não iniciar dispatch global nem executar essas tarefas antes de aprovação autenticada específica desse plano.

| Gate MVP-20.2+ | Estado atual | Critério de saída e evidência exigida |
| --- | --- | --- |
| Contrato de execução local completo | **PENDENTE DE IMPLEMENTAÇÃO** | Contrato persistido vinculado a digest/revisão, workspace, executor/versão, operações, quotas e validade; resolução, revogação e journal. Início explícito não exige senha nem consentimento por tarefa. |
| Reserva/consumo de duração e tentativas | **PASS — sintético** | SQLite `BEGIN IMMEDIATE` reserva tarefa/tentativa/segundos, consome duração medida, libera saldo após retorno ou prova no-effect, retém saldo para `unknown`; concorrência de reserva exercitada. Sem atomicidade com processo externo. |
| Custo estimado/observado | **BLOCKED** | `ExecutionOutcome.cost` fica como estimativa não verificada; uso/custo observado e teto financeiro do provider não disponíveis. Sem hard cap. |
| Último ponto controlado antes do spawn | **PASS — sintético / INCONCLUSIVE operacional** | Callback APL no `LocalRuntime` revalida binding/intenção imediatamente antes de `Popen`; teste de negativa impede a criação simulada. A transação SQLite não é atômica com criação externa. |
| Recuperação/revogação | **PASS — sintético / INCONCLUSIVE operacional** | Revogação/unavailability persiste cancelamento e token cancela cooperativamente; unknown retém reserva e não repete. Falta teste com processo Codex real e fluxo de reconciliação/liberação. |
| IFR sob grant | **PASS — sintético** | Estratégia autorizada é revalidada, retry exige no-effect/reconciled-safe e consome duração; unknown fica bloqueado. Nenhum grant operacional real foi emitido. |
| Compatibilidade Codex 0.167.2 | **INCONCLUSIVE** | `--version` e ajudas confirmam flags documentadas; não houve prompt/inferência, logo parsing real, JSONL, timeout e cancelamento em execução permanecem não testados. Allowlist não alterada. |
| Isolamento de filesystem | **BLOCKED** | `cwd` não conta como isolamento. Exigir prova por controle OS homologado ou declarar explicitamente o limite do perfil local; nenhum resultado de um SO extrapola ao outro. |
| Piloto dependente (duas tarefas) | **BLOCKED** | Workspace em `%TEMP%` e manifesto com tarefas dependentes preparados; falta contrato local, integração operacional e prova de limites/recuperação. Execução requer evidência de persistência, cancelamento e ausência de tarefas fora do plano. |

Cada registro de gate deve conter um dos estados `PASS`, `FAIL`, `INCONCLUSIVE` ou `BLOCKED`, evidências reproduzíveis e justificativa. `PASS` de testes sintéticos não equivale a `PASS` operacional. Uma decisão histórica concluída deve continuar consultável após expiração, mas não autoriza nova aprovação ou uso de grant expirado/revogado.

### Sequência IFR de diagnóstico

1. Registrar outcome de execução e resultado de qualidade em campos/eixos separados.
2. Diagnosticar natureza (`quality_failure`, `deterministic`, `transient`, `environment`, `evaluation_inconclusive`, `operational_unknown`, `security_or_authorization_block`) e recuperabilidade (`potentially_recoverable`/`not_recoverable`).
3. Conferir efeitos/estado anterior; se `unknown`, parar e reconciliar. Sem prova suficiente, escalar.
4. Avaliar necessidade, utilidade, autorização e segurança do retry; computar assinatura contextualizada e limites restantes.
5. Persistir estratégia e justificativa antes da execução. Repetição equivalente sem progresso, limite ou alteração de grant/escopo bloqueia.
6. Executar apenas dentro da autorização atual; validar, persistir evidência/checkpoint e atualizar o incidente. Escalonamento mantém resumo, alternativas e resposta autenticada ligadas à tarefa.

### Testes de aceite IFR

Além dos testes listados em ADR 0009, executar crash entre cada transição persistente e spawn/resultado, restart, corrupção/inconsistência, concorrência, revogação/expiração, retry equivalente com e sem progresso, respostas humanas duplicadas/stale e proposta de estratégia que tente exceder escopo. Regressões para fases anteriores são obrigatórias.

| SDD/F7 | Propor, comparar e aceitar revisão de spec/plano; falha de persistência durante aceite | Snapshot/digest e eventos são atômicos; revisão aceita é append-only; falha não ativa a proposta parcialmente. |
| SDD/F7 | Abrir TOML legado sem `satisfies`, adicionar referência inválida ou critério sem cobertura | Legado carrega como `legacy_unmapped`; referência inválida falha; plano estrito não é aceito sem cobertura ou deferred rationale auditável. |
| SDD/F7 | Alterar requisito, critério, tarefa ou dependência e calcular impacto transitivo | Mesmo input/revisões gera ChangeImpact idêntico; dependentes e artefatos afetados aparecem sem apagar histórico. |
| SDD/F7 | Reabrir projeto com arquivos TOML diferentes da revisão aceita | Estado vira proposta/drift pendente; não há aceite, execução ou roteamento silencioso; histórico anterior permanece íntegro. |
| SDD/F7 | Reutilizar evidência de revisão/checkpoint/gate diferente | Evidência não satisfaz critério atual; decisão de revalidação exige evento explícito e não muda o resultado histórico. |
| SDD/F7–F8 | Criar routing decision ou WorkerAssignment a partir de revisão não aceita ou stale | Operação é recusada; assignment persistido contém os IDs exatos de revisão/plano/requisitos. |
| SDD/F8–F9 | Propagar requisito até tentativa, worker, workspace, checkpoint, gate e resultado | Consulta de rastreabilidade reconstrói a cadeia por IDs/digests e identifica elo ausente sem inferir por texto. |
| SDD/F9 | Propor alteração de spec/tasks em decomposição ou integração | Proposta não altera definição ativa; requer ChangeImpact, replanejamento e aceite antes de nova execução. |

### ADR 0010 — início local sem senha para jobs comuns

O proprietário decidiu que `LOCAL_SUPERVISED` não exige cadastro de senha, autenticação local adicional ou consentimento por tarefa para iniciar jobs. Uma chamada explícita do operador autoriza o plano inteiro por meio de contrato persistido. Agentes/serviços só iniciam contratos de trabalhos previamente configurados. O vínculo imutável ao plano, workspace, executor, operações, quotas/validade, revogação, cancelamento, journal e recuperação conservadora continuam obrigatórios. Esta decisão não remove nem altera `INDEPENDENT_AUTHORITY`, H-01–H-10 ou os controles da ADR 0008 independente.

**Estado de implementação:** decisão registrada em [ADR 0010](decisions/0010-passwordless-local-job-execution.md). O controlador e o IFR existentes ainda são grant-resolver scoped; a API de contrato local, sua CLI e sua homologação são pendências concretas desta fatia. `authorize-plan` e `local-auth` ainda existem para compatibilidade, mas não devem ser tratados como caminho requerido para jobs comuns. Dispatch não foi iniciado nesta refatoração.
