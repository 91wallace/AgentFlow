# Plano de implementação

## Resumo e regras de fase

Fase 1 estabelece um pacote `agentflow` instalável, comandos seguros `--version`, `init` e `doctor`, configuração TOML, testes pytest, CI e instruções de instalação. O protótipo `autonomous-development-runner/` permanece intacto. Nenhuma tarefa desta fase inicia agentes, cria contêineres, grava SQLite ou faz commits. Cada tarefa abaixo deve ser concluída e verificada antes das dependentes.

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
| 6 — Ecossistema | Fase 5 | Adaptadores de outros agentes e eventual integração opcional com Vibe Coding Toolkit após revisão legal e técnica. |

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
| F3-02 | Definir `ExecutionRequest`, `ExecutionResult`, `AgentAdapter`, `ProcessRuntime`, políticas efetivas e erros de domínio. | F3-01 | Núcleo importa apenas protocolos; adaptadores simulados substituem Codex; pedido inclui revisão, ID idempotente e configuração efetiva, inclusive `danger-full-access` quando escolhida. |
| F3-03 | Implementar migração SQLite v2 de tentativas/eventos, índice de uma tentativa ativa e backup consistente. | F3-02 | Reabertura v1→v2 preserva tarefas/evidências; falha de backup/migração mantém v1; tentativas referenciam revisão correta; versão futura é recusada. |
| F3-04 | Implementar pré-checagem, reserva e tentativa `prepared` atômicas, com teto de três tentativas. | F3-03 | Disputa entre processos cria uma tentativa ativa; pedido idêntico retorna a mesma; divergente falha; pré-checagem falha sem spawn; `prepared` consome uma tentativa. |
| F3-05 | Implementar runtime local independente do Codex, com identidade verificável, limites, logs incrementais e cancelamento de árvore Windows/Linux. | F3-02, F3-04 | Processos falsos cobrem timeout de 1800 s configurável, limite de log, SIGTERM/SIGKILL no Linux e Job Object no Windows; PID reutilizado não é aceito como identidade. |
| F3-06 | Implementar adaptador Codex CLI com detecção de versão/capacidades e configuração efetiva, sem sobrescrever configuração do usuário. | F3-01, F3-05 | Flags são usadas somente após verificação; modos explícitos de sandbox, inclusive acesso completo, e aprovações não interativas são preservados; formato inesperado falha; testes usam executável falso. |
| F3-07 | Persistir logs, eventos, resultado do processo e ligação à revisão; limitar retenção e crescimento. | F3-03, F3-05, F3-06 | Logs até 10 MiB por fluxo por padrão, truncamento/falha controlados, retenção padrão de 30 dias com limpeza explícita, payload SQLite até 4 KiB; evento/checkpoint atômicos e metadados sem segredos. |
| F3-08 | Integrar validação e evidências verificadas; distinguir falha do processo, da validação e resultado desconhecido. | F3-07 | Saída `process_succeeded` não conclui tarefa; somente critérios verificados da revisão vigente justificam `completed`; saída parcial e falhas deixam estado auditável. |
| F3-09 | Implementar recuperação e reconciliação de `prepared`/`running`, sem repetir efeitos externos. | F3-07, F3-08 | Queda antes/depois de spawn/identidade/commit bloqueia tentativa ambígua uma vez; processo possivelmente vivo impede nova execução; reabertura repetida não duplica eventos; decisão humana precede retentativa. |
| F3-10 | Expor execução/cancelamento explícitos na CLI, documentação, testes de contrato e CI. | F3-04 a F3-09 | CLI não cria fila autônoma nem altera Git; testes Windows/Linux simulados e CI Python 3.11/3.12/3.13 passam sem Codex/Docker real; `init`, `doctor`, `plan`, `state` preservados. |

### Matriz de testes proposta

- **Contratos/adaptador:** versões e flags reais/incompatíveis, configuração herdada versus explícita, `danger-full-access`, aprovação não interativa, ausência de sandbox automático, argumentos/stdin seguros e saída estruturada válida/inválida; Codex falso por padrão.
- **Persistência e concorrência:** migração v1→v2, backup, FKs, rollback, índice de tentativa ativa, três tentativas totais, idempotência por `operation_id`, parâmetros divergentes e disputa entre processos.
- **Runtime e logs:** timeout padrão 1800 s/configuração, inatividade opcional, limite de 10 MiB por fluxo, drenagem, truncamento, retenção/limpeza explícita, falha de escrita, SIGTERM/SIGKILL Linux e Job Object Windows, pai encerrado e filhos órfãos.
- **Recuperação/repetição:** interrupção em cada fronteira SQLite/spawn, PID reutilizado, tentativa `prepared`/`running`, processo possivelmente ativo, duas reaberturas, resultado desconhecido sem nova execução e decisão humana auditada antes de retentar.
- **Validação:** falha do processo versus falha de validação versus desconhecido, declaração versus verificação, critérios da revisão correta, agente insuficiente para conclusão e preservação de alterações parciais.
- **Regressão:** fases 1 e 2, configuração Codex existente intocada, `.agentflow/` operacional e protótipo ADR fora do pacote/Git.

### Gate e limites

Gate de homologação: uma única tarefa explícita pode ser executada pelo adaptador local Codex dentro da política aprovada, com estado e logs auditáveis, limites aplicados, cancelamento e recuperação determinística; conclusão depende de validação independente; testes de contrato e CI passam em Python 3.11, 3.12 e 3.13. A Fase 3 foi publicada e integrada após a homologação; nenhum agente real foi executado.

Decisões consolidadas: timeout padrão 1800 s (máximo configurável 7200 s), três tentativas totais, sem repetição automática, logs incrementais limitados e retenção de 30 dias, identidade de processo além de PID, cancelamento de árvore por plataforma, sandbox e aprovações somente conforme configuração explícita/efetiva do usuário. Docker não é o sandbox do Codex.

Riscos residuais: Codex CLI/flags e introspecção de configuração podem evoluir; limites rígidos de CPU/memória variam por sistema; matar processo não reverte arquivos; SQLite e spawn não são atômicos; logs podem conter segredos emitidos pelo agente; no Linux, processos podem sobreviver à morte do pai. A implementação deve falhar de modo seguro quando uma capacidade exigida não puder ser verificada. Git branches/checkpoints, Docker, múltiplos adaptadores e execução autônoma de fila permanecem fora da Fase 3.

## Fase 4 — plano detalhado: recuperação e Git

Estado: fechamento arquitetural consolidado, aguardando revisão antes de código. A Fase 3 foi integrada pelo PR #3 (merge `edb7c4f7f0e672d8d1432d2d8948c3b49b7f4b91`). Ver [arquitetura da Fase 4](architecture.md#fase-4--recuperação-e-integração-explícita-com-git-fechamento-arquitetural) e [ADR 0004](decisions/0004-phase-4-recovery-git.md). Não executar agentes reais nem autonomamente; sem operações remotas, push, pull/fetch, merge/rebase, deploy ou submódulos.

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
| F4-08 | Implementar estados intermediários e reconciliação crash-safe entre SQLite/Git. | F4-03 a F4-07 | Injeção de crash em toda fronteira; `unknown` bloqueia repetição e mutações; recuperação compara fatos e é idempotente; resultado não único exige decisão explícita; nenhuma alteração removida/revertida. |
| F4-09 | Integrar fluxo da tarefa e CLI explícita/read-only onde aplicável, sem colapsar execução, validação e conclusão. | F4-05 a F4-08 | Tarefa modificadora só conclui com execução resolvida, validação e evidências verificadas da revisão e checkpoint verificado; sem alterações, exige verificação explícita de workspace limpo e HEAD registrado. Operação local confinada; CLI não oferece ações remotas/destrutivas. Implementado: `workspaces`, `git-recover`, `checkpoint`, `verify-no-changes` e `complete`. |
| F4-10 | Testes de regressão, compatibilidade Linux/Windows, docs, pacote e homologação completa. | F4-01 a F4-09 | Suíte completa, reabertura, concorrência/idempotência, migração, package smoke e CI Python 3.11/3.12/3.13 aprovados; nenhum agente real; revisão de staging/arquivos exclui dados operacionais e ADR legado. Suíte local passou; pacote instalado, Linux e CI remota permanecem pendentes pelas limitações anotadas no progresso. |

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

Estado: **F4-01 a F4-09 implementadas localmente; F4-10 em homologação local**. A CI remota nas versões Python 3.11, 3.12 e 3.13 depende da publicação autorizada da branch. Não foram feitos commit, push ou merge nesta etapa.

## Fase 5 — validação independente, quality gates e entrega

Estado: **F5-01 concluída antes de qualquer código**: limites, perfis/capacidades, retenção, limpeza e política de segredos fixados em arquitetura/ADR. Ver [parâmetros operacionais](architecture.md#f5-01--parâmetros-operacionais-fixados), [arquitetura](architecture.md#fase-5--validação-independente-quality-gates-e-entrega) e [ADR 0005](decisions/0005-phase-5-validation-delivery.md). Nenhum outro componente foi implementado antes deste gate.

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
