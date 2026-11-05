# Arquitetura do AgentFlow

## Estado e objetivo

O repositório versiona `README.md`, `LICENSE` Apache 2.0 e `autonomous-development-runner-v0.1.zip`. O protótipo extraído em `autonomous-development-runner/` está não versionado: pacote `adr`, CLI monolítica, estado em JSON e execução direta de `codex exec`. A Fase 1 entrega o pacote `agentflow`, uma suíte pytest e um workflow de CI para Python 3.11, 3.12 e 3.13, sem alterar esse protótipo nem ativar execução autônoma.

O processo `agentflow` roda localmente. No produto completo, o núcleo escolhe tarefas e aplica políticas; adaptadores executam agentes em ambiente local ou Docker; validadores produzem evidências; SQLite guarda estado operacional e Git guarda alterações de código e checkpoints. Docker nunca é requisito para o modo local.

```text
CLI -> core -> planning, state, agents, runtime, validation, git
           \-> environment, delivery
agents -> interface de runtime; adaptadores concretos ficam nas bordas
```

## Limites dos módulos

| Módulo | Responsabilidade e contrato planejado |
| --- | --- |
| `cli` | Analisa argumentos, apresenta resultados e traduz erros em códigos de saída; chama serviços por interfaces. |
| `core` | Coordena transições e políticas; não conhece subprocessos, SQLite ou Docker diretamente. |
| `planning` | Lê especificações, tarefas, dependências e critérios; entrega plano validado e imutável ao núcleo. |
| `state` | Repositório de estados, tentativas, eventos e retomada; implementação SQLite com transações em fase posterior. |
| `agents` | Protocolo de execução de tarefa e resultados; primeiro adaptador Codex CLI em fase posterior. |
| `runtime` | Protocolo de processos isolados; implementações local e Docker em fases posteriores. |
| `validation` | Executa verificações declaradas e devolve evidência estruturada; não decide sozinho a conclusão. |
| `git` | Consulta repositório e cria branches/checkpoints apenas quando uma política futura autorizar. |
| `environment` | Inspeciona Python, Git, Codex CLI e Docker sem alterar o sistema. |
| `delivery` | Contratos futuros para build e implantação; sem implementação funcional na Fase 1. |

Na Fase 1, só `cli`, configuração e `environment` têm comportamento de produto. Os demais módulos recebem estrutura e contratos mínimos onde necessários para impedir acoplamento, sem implementar seus fluxos futuros.

## Fluxos e dados planejados

1. `init` identifica o diretório alvo e cria somente arquivos ausentes de configuração e instruções iniciais. Arquivos existentes permanecem byte a byte iguais. Repetir o comando deve produzir o mesmo estado e saída clara.
2. `doctor` coleta disponibilidade e versões de Python, Git, Codex CLI e Docker. Ausência de Docker é aviso opcional; ausência de Codex limita execução futura, mas não impede `init` ou uso local básico. O comando não instala nem executa agentes.
3. Em fases futuras, um plano validado define tarefas e dependências; `core` reserva uma tarefa por transação SQLite, invoca agente via `runtime`, valida, registra evidências e solicita checkpoint Git. Uma execução interrompida reabre estado persistido e reconcilia o trabalho antes de qualquer repetição.

Configuração TOML terá versão de esquema, modo de execução `local` por padrão, seleção de agente e opções de diagnóstico. Valores desconhecidos, tipos inválidos e versões de esquema incompatíveis devem gerar erros claros. Segredos não entram no arquivo gerado por `init`.

## Limitações e riscos conhecidos

- O ADR 0.1 não implementa a arquitetura alvo: usa JSON, Python 3.10+, CLI `adr` e não oferece isolamento ou reconciliação transacional. Ele não deve ser promovido silenciosamente a `agentflow`.
- Estado SQLite, migração de dados ADR, execução, recuperação, checkpoints e Docker não pertencem à Fase 1.
- A forma exata dos modelos de tarefa e dos contratos de execução será fixada quando esses fluxos forem implementados; evitar interfaces especulativas profundas agora.
- `doctor` mede disponibilidade, não autenticação efetiva nem segurança de um agente ou Docker.
- Dependências do sistema variam por plataforma; testes devem simular sondagens sem exigir Codex CLI ou Docker instalados.

## Fase 2 — planejamento e estado

Esta seção registra os contratos aprovados para a implementação local da Fase 2. A Fase 2 não invoca agentes, validadores de projetos, Docker ou operações Git que alterem histórico. `planning` interpreta arquivos versionáveis; `state` persiste somente estado operacional local; `core` recebe planos imutáveis e solicita transições por interfaces. Nenhuma implementação concreta de SQLite entra no núcleo.

### Arquivos e contratos versionados

- `.agentflow/spec.toml`: `schema_version = 1`, `project_id` estável, `title`, `objective` não vazio e `acceptance_criteria` como lista não vazia de critérios com `id` e `description` observável. O identificador do projeto usa letras ASCII minúsculas, dígitos e hífen, sem depender do caminho local.
- `.agentflow/tasks.toml`: `schema_version = 1` e `[[tasks]]`, cada uma com `id`, `title`, `description`, `acceptance_criteria` (lista não vazia de critérios com `id` e `description`) e `depends_on` (lista de IDs, vazia por padrão). IDs de tarefas e critérios seguem a mesma regra do projeto, são únicos em seu escopo e não são renumerados quando a ordem do arquivo muda. Um ID removido não pode ser reutilizado para outra tarefa ou critério no mesmo projeto.
- Os dois arquivos são UTF-8/TOML, revisáveis em Git e independentes de SQLite. Chaves desconhecidas, tipos errados, versões incompatíveis, IDs duplicados, referências ausentes, autodependência e ciclos geram erros com caminho e campo. A carga valida tudo antes de produzir um `Plan` imutável; não grava estado em caso de erro.
- Critérios de aceite descrevem resultados verificáveis por uma pessoa ou por validadores futuros. A Fase 2 armazena texto e IDs estáveis, sem interpretá-los como comandos executáveis. Cada revisão imutável da definição contém digest canônico da tarefa, critérios, dependências e revisão da especificação; uma tarefa iniciada permanece vinculada a essa revisão. Mudança relevante exige revalidação explícita antes de nova transição ou evidência, sem reescrever histórico nem apagar conclusão silenciosamente.
- Somente `.agentflow/spec.toml` e `.agentflow/tasks.toml` serão versionados. A regra de ignore da Fase 1 será substituída por exceções estritas para esses dois caminhos; `config.toml`, `state.sqlite3`, arquivos WAL/SHM, backups e logs permanecem ignorados. `init` da Fase 1 não sobrescreve nenhum arquivo existente. O formato v1 dos TOML permanece legível; novas versões exigem migração explícita, nunca interpretação silenciosa de chaves novas.

### Modelo operacional SQLite proposto

Banco local `.agentflow/state.sqlite3`, ignorado pelo Git. Cada conexão ativa `PRAGMA foreign_keys = ON`, `journal_mode = WAL`, `busy_timeout` explícito e `synchronous = FULL` para durabilidade após encerramento inesperado. Alterações usam `BEGIN IMMEDIATE`; um único escritor por vez e revisões condicionais impedem reservas duplicadas entre processos. Operações que mudam estado, revisão, evidência, evento e checkpoint confirmam juntas ou revertem juntas. Leitores veem snapshots confirmados. Datas em UTC ISO 8601; sequência de eventos inteira monotônica é a ordem auditável, sem depender de precisão do relógio.

| Tabela | Chave e campos essenciais | Integridade |
| --- | --- | --- |
| `schema_migrations` | `version` PK, `applied_at` | Migrações sequenciais, imutáveis e transacionais. |
| `projects` | `project_id` PK, `spec_digest`, `tasks_digest`, `plan_revision`, `created_at` | Um projeto por banco nesta fase; hashes canônicos do plano validado. |
| `task_definitions` | `(project_id, task_id, definition_revision)` PK, `definition_digest`, `spec_digest`, `definition_snapshot` | Revisões imutáveis; FK para projeto; snapshot canônico preserva critérios e dependências usados. |
| `tasks` | `(project_id, task_id)` PK, `definition_revision` FK, `status`, `attempt_count`, `updated_at`, `revision` | FK para revisão imutável; status fechado; revisão para escrita condicional. |
| `task_dependencies` | `(project_id, task_id, depends_on_id)` PK | Duas FKs compostas para `tasks`; sem autodependência; DAG validado antes da inserção. |
| `task_events` | `event_id` PK crescente, `(project_id, task_id)` FK, `definition_revision`, `from_status`, `to_status`, `reason`, `operation_id`, `occurred_at` | Somente append; mesma transação da mudança; FK para revisão usada. |
| `operations` | `operation_id` PK, `request_digest`, `result_event_id`, `created_at` | Escopo global do banco; mesmo ID e digest retorna resultado anterior, digest divergente é conflito. |
| `evidence` | `evidence_id` PK, FK para tarefa/revisão/critério, `kind`, `payload`, `source`, `recorded_at`, `operation_id` | `kind` distingue `declared` e `verified`; verificação referencia declaração e método/resultado, sem transformar declaração em verificação. |
| `checkpoints` | `checkpoint_id` PK, `project_id`, `event_id` FK, `plan_revision`, `created_at`, `kind` | Marco lógico do estado persistido, associado a evento confirmado; sem commit Git na Fase 2. |

Índices apoiam busca por status, dependências inversas e eventos por tarefa. FKs impedem apagar histórico; nenhuma cascata remove revisões, evidências ou eventos. Importação repetida do mesmo digest é idempotente. Ao reabrir com definições alteradas, o plano novo é validado antes de qualquer escrita; revisões anteriores permanecem imutáveis. Mudanças em tarefas `reserved`, `completed`, `failed` ou `blocked` são marcadas para revalidação explícita e bloqueiam transições dependentes; mudanças em tarefas ainda não iniciadas podem ser aplicadas transacionalmente após verificar dependências. A operação de revalidação registra decisão, autor, revisão antiga/nova e evento auditável. Remoção ou reutilização de ID com histórico é recusada.

### Máquina de estados e invariantes

Estados da Fase 2: `pending`, `ready`, `reserved`, `completed`, `failed`, `blocked`. `pending` aguarda dependências; `ready` tem todas concluídas; `reserved` representa apenas uma reserva lógica futura, sem executar agente; `failed` e `blocked` exigem motivo. `completed` exige evidência `verified` para cada critério da revisão vinculada, ou decisão explícita de exceção registrada com autor e justificativa. Evidência `declared` sozinha não conclui tarefa. A Fase 2 implementa transições e simulações de reserva, sem comando público de execução.

| Origem | Destino permitido | Guarda |
| --- | --- | --- |
| criação | `pending` ou `ready` | `ready` somente sem dependências pendentes. |
| `pending` | `ready`, `blocked` | Todas as dependências concluídas para `ready`; motivo para `blocked`. |
| `ready` | `reserved`, `blocked` | Reserva atômica, operação única; motivo para `blocked`. |
| `reserved` | `completed`, `failed`, `ready` | Conclusão com evidência; falha ou liberação com motivo. |
| `failed` | `ready`, `blocked` | Retentativa explícita e dependências satisfeitas; motivo para `blocked`. |
| `blocked` | `pending` ou `ready` | Desbloqueio explícito; destino recalculado pelas dependências. |
| `completed` | nenhum | Imutável nesta fase. |

Qualquer outra transição retorna erro de domínio sem escrita. `ready` nunca existe com dependência incompleta. A transição para `completed` libera dependentes na mesma transação e registra eventos próprios para cada promoção. O núcleo decide a intenção; o repositório verifica guardas, revisão da definição e revisão do estado novamente dentro da transação. `operation_id` e digest canônico da solicitação são persistidos juntos com o resultado; reenvio idêntico retorna o resultado anterior, inclusive após reabertura, e parâmetros divergentes são conflito. Timeout de `busy_timeout` retorna erro controlado e permite nova tentativa com o mesmo ID.

### Migrações, checkpoints e recuperação

Migração inicial cria o esquema v1 em banco novo. Ao abrir, `PRAGMA user_version` e `schema_migrations` devem concordar; versão maior que a suportada é recusada sem alteração. Migrações SQLite são sequenciais e transacionais, com atualização da versão na mesma transação. Antes de mudança estrutural, backup consistente pela API de backup do SQLite para arquivo temporário local, verificação de integridade e publicação atômica do backup; copiar apenas o arquivo `.sqlite3` em modo WAL é proibido. Falha de backup impede migração. Falha da migração reverte a transação e preserva backup e versão anterior. Esquemas TOML têm versão própria: arquivo de versão futura é recusado, conversão exige revisão explícita e nunca modifica os originais implicitamente. Não se migra JSON do ADR nesta fase.

Um checkpoint é o último limite transacional confirmado de uma operação, com evento e revisão de plano. Na abertura após interrupção, `PRAGMA integrity_check`, `foreign_key_check`, versão e digest do plano são verificados; eventos são comparados com status materializados. Transação incompleta é revertida pelo SQLite. Reserva lógica `reserved` sem proprietário ativo comprovado passa a `blocked` com razão `recovery_required`, evento e `operation_id` determinístico derivado da reserva; somente uma decisão explícita de reconciliação a libera para `ready` ou mantém bloqueada. Não se presume sucesso, não se gera evidência e não se repetem efeitos externos. Reabrir duas vezes não cria eventos adicionais. Erro de integridade, versão incompatível ou divergência de definição bloqueia mutações, produz diagnóstico somente leitura e preserva banco, WAL/SHM e backup para análise; não há reparo destrutivo automático.

### Fronteiras e riscos para revisão

- `planning` expõe modelos imutáveis e erros de validação; `state` expõe um protocolo de repositório e tipos de transição. O adaptador SQLite fica em `state`, injetado no serviço de `core`; CLI continua sem `run`/`resume` de execução.
- O protocolo não deve expor conexão SQL, subprocessos ou caminhos internos ao núcleo. Testes de contrato com implementação simulada e SQLite verificam a mesma semântica.
- Decisões consolidadas para F2-01: exceções estritas de ignore; revisão imutável e revalidação explícita; evidência declarada/verificada vinculada à revisão e ao critério; reconciliação conservadora de `reserved`; WAL, `synchronous=FULL`, `busy_timeout`, FKs e backup consistente; idempotência global por `operation_id` e digest. F2-01 registra valores operacionais e exemplos em ADR antes do código.
- Riscos residuais: a prova de proprietário ativo em processos concorrentes precisa de protocolo de lease/identidade; decisão humana de verificação requer autoria confiável; `synchronous=FULL` e backups custam I/O; alterações simultâneas de TOML e estado exigem comparação de digest no início e no commit; corrupção física pode exigir restauração manual do último backup íntegro.

## Fase 3 — executor Codex CLI local

A Fase 3 foi homologada e integrada pelo PR #3. O commit do PR `8f97cd7c6f657153c5f92934ad885d8e1659bf1c` foi integrado à `main` pelo merge commit `edb7c4f7f0e672d8d1432d2d8948c3b49b7f4b91`; os jobs Python 3.11, 3.12 e 3.13 passaram. A Fase 3 adiciona uma execução **explícita de uma tarefa por solicitação**, sem fila autônoma nem avanço automático para outra tarefa. A CLI não cria branch, commit, push, contêiner ou implantação. O protótipo `adr` continua intocado. O adaptador Codex CLI é substituível e não é importado pelo núcleo.

### Contratos entre módulos

| Fronteira | Entrada | Saída e regra |
| --- | --- | --- |
| `core` → `state` | Projeto, tarefa, revisão imutável, `operation_id`, política e dono da reserva | Reserva transacional exclusiva ou conflito. A intenção e a tentativa são persistidas antes de iniciar processo. |
| `core` → `agents.AgentAdapter` | `ExecutionRequest` imutável: `attempt_id`, digest da revisão, critérios, diretório permitido, instrução, limites, política efetiva de sandbox/aprovação e identificador de cancelamento | `ExecutionResult`: estado terminal, código de saída, tempos, digest e caminhos de logs, artefatos declarados, metadados do adaptador. Sem decisão de conclusão. |
| `agents` → `runtime.ProcessRuntime` | Executável resolvido, argumentos em lista, diretório de trabalho, ambiente mínimo, stdin, limites | Identidade de processo/grupo, fluxos de saída limitados, sinalização de término/cancelamento e motivo. Nenhum shell. |
| `core` → `validation` | Revisão da tarefa, critérios, alterações observadas e resultado do agente | Evidências estruturadas `declared` e `verified` ou falha de validação. Saída textual do Codex não equivale a evidência verificada. |

O primeiro adaptador usa o modo não interativo `codex exec`. A [documentação oficial da OpenAI](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex) demonstra invocação por subprocesso, entrada por stdin, saída estruturada e sandbox configurável. O adaptador detecta versão, inspeciona ajuda/capacidades reais e valida argumentos e formato de saída antes da reserva; uma tabela de compatibilidade testada por versão evita presumir flags. Incompatibilidade é erro de pré-checagem. O núcleo nunca importa Codex nem conhece suas flags. Não se usa `shell=True` nem se interpolam prompts em comandos.

Homologação local inicial: `codex-cli 0.166.0`, com `exec --help` e `--help` confirmando JSONL, stdin, `--sandbox`, `--strict-config`, `-C` e `--ask-for-approval`; `codex login status` também é verificado. A tabela do adaptador aceita somente essa versão até novas versões passarem pelos testes. A CLI oferece `agentflow execute --task ID --operation-id ID --sandbox MODE --approval never [--timeout SEGUNDOS]`, `agentflow cancel --attempt ID`, `agentflow recover` e `agentflow cleanup --logs [--older-than-days N]`. A ausência de sandbox ou aprovação explícitos falha antes da reserva, pois a CLI não fornece consulta confiável de toda a configuração efetiva herdada. Os argumentos explícitos não editam `config.toml`; a precedência dos argumentos é verificada pela ajuda da versão instalada. `on-request` é recusado por incompatibilidade com execução não interativa.

### Política de execução e recursos

O pedido explícito identifica uma única tarefa `ready` e a revisão exata carregada do TOML. Pré-checagens validam digest do plano, dependências, Codex CLI, autenticação disponível sem expor credenciais, política de execução, diretório Git e ausência de execução concorrente para a mesma tarefa. O diretório de trabalho fica dentro da raiz aprovada; caminhos e symlinks são resolvidos antes de invocar. A execução recebe somente o ambiente necessário, sem copiar indiscriminadamente segredos do processo pai.

**Sandbox e aprovações:** AgentFlow respeita a configuração explícita do usuário e a configuração existente do Codex; não ativa sandbox nem altera arquivos do Codex automaticamente. A seleção aceita o modo configurado pelo usuário, inclusive `danger-full-access`. Opções de aprovação não interativa também são permitidas quando explicitamente selecionadas; não são impostas silenciosamente. O adaptador só passa opções explicitamente definidas e verifica precedência, versão e configuração efetiva por meios suportados pela CLI; registra valores efetivos sem segredos. Se a configuração efetiva não puder ser determinada ou exigir interação incompatível com a execução solicitada, a pré-checagem falha com diagnóstico, sem spawn. Sandbox do Codex é uma política do agente e não equivale ao isolamento Docker opcional de fase futura.

Limites validados: timeout total padrão de **1800 segundos** por tentativa, configurável de 1 a 7200 segundos; no máximo **3 tentativas totais** por tarefa, sem reset silencioso após revisão; timeout por inatividade desativado por padrão porque silêncio não prova travamento, opcional de 60 segundos até o timeout total. Stdout e stderr têm limite padrão de 10 MiB cada e máximo configurável de 50 MiB cada. Ao atingir o limite, o runtime continua drenando fluxos para não bloquear o processo, solicita cancelamento, marca truncamento e falha `log_limit`. Limite rígido de CPU/memória depende do sistema operacional e não é prometido no runtime local sem mecanismo verificável; configuração que o exija falha claramente. Docker continua opcional e fora desta fase.

### Persistência, logs e estados

Migração SQLite v2 acrescenta `execution_attempts` com `attempt_id` imutável, tarefa/revisão, `operation_id`, política/digest do pedido, dono, estado, timestamps, versão/capacidades do Codex, identidade do processo, código de saída, motivo terminal e referências aos logs; e `execution_events` append-only para marcos da tentativa. FKs vinculam tentativa à revisão imutável e tarefa. Estados: `prepared` → `running` → um de `process_succeeded`, `process_failed`, `timed_out`, `cancelled`, `interrupted`; validação subsequente grava resultado separado `validation_passed` ou `validation_failed`, sem reinterpretar o estado do processo. `interrupted` significa resultado **desconhecido**, não falha. `process_succeeded` significa apenas término do processo, não conclusão da tarefa. A tarefa só chega a `completed` após validação e evidências verificadas da revisão correta, conforme a Fase 2. Transições, eventos e checkpoints da tentativa são transacionais. `operation_id` repetido com pedido idêntico devolve a mesma tentativa; parâmetros divergentes geram conflito e nunca iniciam outro processo. Um índice único parcial impede duas tentativas `prepared`/`running` para a mesma tarefa.

Logs ficam sob `.agentflow/runs/<attempt_id>/`, ignorados pelo Git. O runtime grava stdout/stderr incrementalmente por tentativa, com limite de bytes, digest, marca de truncamento e metadados finalizados atomicamente; não grava prompts, ambiente completo ou credenciais nos metadados. Redação de padrões sensíveis antes da persistência e permissões restritivas reduzem exposição, mas não garantem remoção de todos os segredos. Logs terminais são retidos por 30 dias por padrão; `agentflow cleanup --logs` executa limpeza explícita e preserva eventos/digests auditáveis no SQLite. Cada evento no banco tem payload máximo de 4 KiB e não contém stdout/stderr integral. A API de métricas informa tamanho do banco e WAL; compactação/arquivamento ou `VACUUM` exigem backup e operação explícita. Falha ao gravar log encerra a tentativa com erro controlado, sem apagar o que já foi confirmado.

O runtime herda somente variáveis de ambiente necessárias à descoberta do executável e dos arquivos de usuário (`PATH`, diretório de usuário, `CODEX_HOME`, temporários e variáveis básicas do Windows), mais variáveis explicitamente fornecidas pelo adaptador. Não copia indiscriminadamente o ambiente do processo pai. No Linux, o processo inicia em nova sessão e o grupo recebe SIGTERM seguido de SIGKILL após dez segundos; no Windows, inicia suspenso, entra em Job Object com `KILL_ON_JOB_CLOSE` e só então retoma. Sistemas fora de Linux/Windows não são homologados pelo runtime local.

### Falhas, cancelamento e recuperação

Falhas distintas: pré-checagem, spawn sem processo criado, timeout total, inatividade, limite de log, saída não zero, saída inválida, falha de validação, cancelamento e interrupção inesperada. Nenhuma produz conclusão automática. Erro antes de `prepared` não consome tentativa; `prepared` já consome uma das três, inclusive se o spawn falhar, e termina com evento auditável. Falha conhecida de processo e falha de validação são registradas separadamente; `interrupted` é resultado desconhecido. Nenhuma repetição automática é padrão. Nova tentativa exige solicitação explícita, novo `attempt_id`/`operation_id`, capacidade restante e reconciliação registrada do workspace. Somente falha comprovada antes de qualquer subprocesso e sem efeito no workspace admite liberação simplificada; falha após spawn, timeout, cancelamento, validação reprovada ou resultado desconhecido exige inspeção explícita das alterações e decisão humana auditável antes de repetir. Resultado desconhecido nunca é repetido sem resolver a ambiguidade.

Cancelamento é operação idempotente e auditável: no Linux, processo criado em nova sessão/grupo recebe SIGTERM no grupo, espera até 10 segundos e recebe SIGKILL se necessário; no Windows, Job Object com política de encerrar descendentes ao fechar e encerramento explícito da árvore, aguardando até 10 segundos antes da força. Se a plataforma não oferecer controle confiável da árvore, o runtime recusa iniciar. O runtime aguarda a coleta do processo e fecha fluxos; cancelamento tardio após estado terminal retorna o resultado já confirmado. Encerrar processo não desfaz alterações de arquivos, por isso a tarefa fica `blocked` para revisão humana. Não há limpeza automática de alterações do usuário.

Na reabertura, `prepared` sem prova de ausência de processo e `running` sem prova de resultado ficam `interrupted`/`blocked` para reconciliação; não são classificados automaticamente como sucesso ou falha. Identidade persistida inclui PID, horário de criação observado pelo sistema, grupo/sessão ou Job Object, `attempt_id`, token aleatório e identidade do processo pai; PID isolado jamais autoriza matar, adotar ou repetir. Se o processo possivelmente vive, bloqueia nova execução e requer inspeção/cancelamento por identidade verificada. No Windows, fechar o Job Object por morte do pai encerra a árvore quando a política estiver ativa; no Linux, morte do pai pode deixar o grupo vivo, então a reabertura verifica identidade e bloqueia até resolução. A ordem é: transação de reserva + tentativa `prepared` → spawn fora da transação → transação de identidade/`running` → coleta → transação de resultado e evidências. **SQLite e spawn não formam transação atômica**: queda no intervalo `prepared`→spawn ou spawn→identidade é ambígua e nunca causa reexecução automática. Reabertura repetida não duplica evento/tentativa. A recuperação da Fase 2 que bloqueia reservas órfãs continua válida; migração v2 a concilia com tentativas sem perder histórico.

### Invariantes e riscos a fechar

Uma tarefa tem no máximo uma tentativa ativa; uma tentativa referencia exatamente uma revisão; status `completed` exige evidência verificada, não mensagem do agente; nenhuma reabertura repete efeito externo; logs e banco são locais e ignorados; falha de migração v2 mantém banco v1 íntegro e backup consistente. A versão local 0.166.0 teve capacidades e políticas verificadas; outras versões falham na pré-checagem até serem homologadas. A validação de critérios continua sendo fornecida por verificadores explícitos que registram evidência verificada; nenhum teste do projeto é iniciado automaticamente nesta etapa. A CI passou nas três versões Python e a revisão do PR #3 foi aprovada; a Fase 3 está integrada.

## Fase 4 — recuperação e integração explícita com Git (fechamento arquitetural)

**Estado:** arquitetura aprovada; F4-01 a F4-09 implementadas localmente na branch `feat/git-recovery-phase-4`. F4-10 aguarda suíte final e CI remota após autorização de publicação. Base: Fase 3 integrada à `main` no merge commit `edb7c4f7f0e672d8d1432d2d8948c3b49b7f4b91`. Nenhuma execução de agente é iniciada autonomamente; Git remoto, push, pull/fetch, merge/rebase e deploy estão fora da fase.

### Invariantes: execução, validação, conclusão

O estado do processo, o resultado da validação e a conclusão da tarefa são três fatos distintos. Saída zero, branch criada, diff limpo ou commit não são evidência de que critérios foram satisfeitos. Somente evidências `verified`, vinculadas à revisão e a cada critério, permitem `completed`. Se a tarefa modificou arquivos versionados, um checkpoint Git verificado também é precondição necessária; não substitui as evidências. Tarefa que não pode modificar arquivos versionados não exige worktree nem checkpoint. Uma execução ambígua nunca é repetida automaticamente.

### Workspace, identidade e branches

Toda tarefa autorizada a modificar arquivos versionados recebe worktree Git isolada e obrigatória. O workspace tem ID persistente, projeto/repositório, task ID e revisão, caminho absoluto canonizado, branch própria, base OID, worktree Git comum, política de caminhos, estado e datas de criação/fechamento. A branch é exclusiva desse workspace, com referência e ciclo de vida persistidos; não compartilhar branch entre workspaces. Colisão de ID, caminho, branch, base inesperada ou worktree já registrada para outra identidade falha fechada.

A criação é local e explícita. Preflight detecta estado e recursos antes de criar. O checkout original nunca é usado para escrita da tarefa e suas alterações tracked, staged ou untracked não são movidas nem descartadas. Em caso de alterações locais preexistentes no workspace registrado, interromper mutação e exigir reconciliação. Worktrees não são uma fronteira de segurança: compartilham object database, refs e configuração do repositório comum, e processos com acesso ao mesmo usuário podem alterar esses dados. AgentFlow valida repositório comum, branch, HEAD e identidade antes/depois de cada ação e assume que o agente/runtime já está limitado ao diretório aprovado; worktree por si só não impede acesso externo.

Branches/worktrees só podem ser removidas por ação explícita após estado terminal reconciliado e verificação de ausência de alterações, operações desconhecidas, checkpoints não registrados e dependências. Nunca remover automaticamente se existir dirty state, operação ambígua, lock ativo, falha de verificação ou tarefa não reconciliada. Nesta fase o padrão recomendado é preservá-las; limpeza posterior é comando separado com confirmação e registro.

### Contrato Git e segurança de caminhos

`GitProvider` é protocolo substituível usado por serviço em `core`; `planning`, `state`, `execution` e `validation` não importam o adaptador concreto. A implementação local usa subprocessos com executável resolvido e argumentos separados (sem shell), `cwd` exatamente no workspace persistido, environment limitado, prompts interativos desativados, timeout e stdout/stderr limitados. Para toda operação mutável, resolver e verificar o diretório do worktree e do repositório comum; rejeitar path ou redirecionamento que faça a operação atingir outro workspace. `git -C` deve receber somente raiz previamente validada, nunca entrada livre.

Nomes de arquivos seguem semântica Git em bytes/pathspec. Preservar Unicode e espaços sem divisão por shell; suportar nomes especiais por NUL-delimited interfaces (`-z`) e pathspec literal (`--literal-pathspecs`/`:(literal)` conforme comando). Não construir pathspecs por concatenação de strings. Validar cada caminho como relativo à raiz, normalizado e sem traversal; resolver symlinks e rejeitar escape. Em Windows, tratar separadores, drive/UNC, normalização Unicode e comparação case-insensitive sem colapsar nomes que Git distingue; conflito de case falha com diagnóstico. Submódulos são detectados e toda operação que os toque é recusada nesta fase, inclusive pathspec que selecione o gitlink.

Nenhuma operação remota é permitida: não executar push, pull, fetch, clone, merge, rebase ou deploy; nem buscar refs remotas. Não alterar configuração global/local de Git nem credenciais. Rejeitar config de alias/extensão que redirecione comando gerenciado e opções de protocolo/extensão não reconhecidas. Não executar `reset`, `clean`, checkout destrutivo, `gc`, `prune` ou remoção implícita.

### Lista explícita e checkpoint

O escopo autorizado da tarefa deve produzir lista explícita e persistida de caminhos relativos que podem entrar no checkpoint. A lista é aprovada antes da operação, vinculada à revisão da tarefa e não pode ser ampliada pelo executor. Alteração de escopo gera nova revisão/revalidação explícita. Imediatamente antes do staging **e novamente imediatamente antes do commit**, revalidar cada caminho: pertencimento ao workspace, ausência de escape/symlink, tipo regular permitido, ausência de submódulo, política de exclusão e estado/digest esperado. Mudança desde a validação interrompe a operação e requer novo pedido.

Não usar `git add -A`, `git add .` nem staging global. Preparar index temporário privado via `GIT_INDEX_FILE` ou mecanismo equivalente; construir staging somente com a lista autorizada, em pathspec literal/NUL-safe. Verificar `git diff --cached --name-only -z` exatamente contra allowlist antes de commit. Rejeitar arquivo não rastreado ou arquivo operacional/credencial/cache/banco/log salvo se e somente se política explícita permitir (padrão é rejeitar). Depois de commit, verificar independentemente OID, parent, tree, branch/ref esperada e paths da árvore contra a lista. Persistir OIDs e hash canônico do conjunto de paths no registro imutável do checkpoint.

Conclusão de tarefa com alterações versionadas exige: processo não ambíguo, validação aprovada, evidências verificadas para todos os critérios da revisão e checkpoint verificado da mesma revisão/workspace. Qualquer alteração posterior ao checkpoint invalida seu uso para conclusão até nova validação e novo checkpoint. Se a tarefa não produziu mudança versionada, uma operação explícita `verify-no-changes` registra intenção e confirma branch/HEAD idênticos ao estado persistido e workspace limpo; não se cria commit vazio. Essa observação também é recuperável e não substitui validação/evidências.

### Persistência, atomicidade e estados

Migração SQLite v3 é transacional e precedida por backup consistente; falha preserva v2. Tabelas propostas: `workspaces` (identidade/lifecycle/branch/base/repo); `git_operations` (intenção, `operation_id`, digest parâmetros, ator, estado e resultado verificado); `git_checkpoints` (workspace/task/revision, operação, base/commit/tree/parent, paths/hash, ator); `git_events` append-only. FKs e constraints impedem checkpoint órfão ou cruzado entre task/revision/workspace. Operação e checkpoint são conceitos distintos dos checkpoints lógicos F2/F3.

`operation_id` único globalmente na camada de operações Git. Repetição com tipo, workspace, revisão, escopo e digest idênticos retorna a operação/resultado já registrado, sem executar Git novamente; divergência em qualquer parâmetro é conflito. Uma operação `unknown` não é reexecutada mesmo com mesmos argumentos; somente a reconciliação pode concluir o registro ou produzir nova operação explicitamente autorizada.

Toda mutação segue: preflight → transação curta para reservar workspace e registrar intenção `prepared` → operação Git fora da transação → inspeção independente do estado real → transação para persistir `verified`/`failed` e evento. Estados: `prepared -> running -> verified | failed | unknown`. Queda, timeout ou sinal interrompendo a janela após efeito potencial deixa `unknown`; reconciliação verifica HEAD/ref/index/tree e parâmetros gravados, emite evento idempotente e resolve apenas se um único resultado corresponder. Caso contrário bloqueia workspace/tarefa. Nunca manter transação SQLite aberta durante subprocesso nem tentar simular atomicidade distribuída.

Checkpoint: `prepared -> created -> verified`; queda após criação e antes da confirmação DB é `unknown`, reconciliada pelo commit OID/ref/tree e pelo operation ID gravado como trailer/mensagem estruturada quando tecnicamente confiável. Se identificação inequívoca não for possível, não criar outro commit e solicitar resolução explícita. Resultado verificável é guardado antes de liberar reserva. Reabertura repetida não duplica evento.

### Concorrência e locks

Reserva transacional SQLite com índice único para operação ativa por workspace/task, combinada com lock interprocesso no próprio diretório administrativo do worktree; lock contém token aleatório, operation ID, PID, instante de criação e identidade da máquina/processo suportada. PID isolado não identifica dono. Dois processos concorrentes: apenas um consegue reservar; lock órfão ou identidade incerta bloqueia e demanda reconciliação, não expiração otimista. Revalidar lock e HEAD após cada comando Git. A serialização é por workspace e por refs compartilhadas do repositório comum onde aplicável; alteração externa de branch/ref entre leitura e escrita invalida a operação.

### Alterações preexistentes e limites de proteção

O worktree original pode estar dirty; isso não impede criação isolada se a base for identificada sem alterá-lo. O workspace novo deve começar limpo na base registrada. Alterações fora do allowlist nunca são incluídas e alteram o diagnóstico; não descartar nem mover arquivos automaticamente. Usuários/processos externos ainda podem escrever no worktree, em `.git` comum, refs ou banco entre as verificações; a proteção é de isolamento operacional e detecção, não sandbox contra processo malicioso com mesmo usuário. Um agente pode escrever fora do worktree se o runtime não aplicar sandbox efetivo. Essa limitação precisa ser visível e configuração de segurança não confirmada deve falhar antes do spawn conforme Fase 3.

### Integração com o fluxo e compatibilidade

`core` coordena repositório de tarefa, executor F3, validador e `GitProvider` por protocolos, sem acoplar módulos. F3 associa tentativa ao workspace persistente. Validação consome diff/artefatos observados e emite evidências; só regras explícitas as marcam verificadas. Conclusão consulta em uma transação estado de execução, revisão, evidências e checkpoint verificado. Não interpretar presença de branch/commit como sucesso de agente.

Linux e Windows são alvo. Testes usam repositórios temporários locais; subprocessos com `shell=False`, paths NUL-safe e locks interprocesso específicos por sistema. Se Windows não oferecer semântica segura para um path/ref/lock ou se Linux não conseguir reconciliar identidade, recusar operação. Submódulos recusados em ambas as plataformas. Worktrees podem deixar refs/objetos após crash e não isolam configuração/ref do repositório comum; limpeza é explícita e conservadora.

### F4-01 — parâmetros operacionais fixados

Identidade `workspace_id`: UUIDv4 minúsculo sem hífen, prefixo `ws-`; branch exclusiva `agentflow/ws/<uuid-hex>`; worktree `<git-common-dir>/agentflow/worktrees/<uuid-hex>`; lock file persistente `<git-common-dir>/agentflow/locks/<uuid-hex>.lock`. Identidade real reside no SQLite, não se deduz do path. Digests são `sha256:` de JSON canônico UTF-8 (chaves ordenadas, sem espaços/floats/NaN, strings NFC); digest de pathset usa array sem duplicatas ordenado por bytes UTF-8 NFC, cada item terminado por NUL. Operation digest abrange todos os parâmetros e ator.

Limites máximos: 128 paths por checkpoint, 1024 bytes UTF-8 por caminho relativo, 255 por componente, 64 MiB agregados de conteúdo, 4 MiB de saída/status total por comando, 1 MiB por stdout/stderr e timeout Git de 30 s. Exceder limite bloqueia antes do commit sem apagar alterações.

Reserva SQLite `BEGIN IMMEDIATE` mais lock OS não bloqueante no primeiro byte: `flock(LOCK_EX|LOCK_NB)` Linux; `msvcrt.locking(LK_NBLCK, 1)` Windows. Sem expiração temporal. Owner record usa UUID aleatório, PID, início, Linux boot ID ou Windows creation FILETIME, hostname e operation ID. Após morte, lock de kernel libera, mas arquivo permanece; nova posse detecta operação `prepared/running`, marca `unknown` em evento idempotente e bloqueia. Não repete automaticamente nem presume que Git não ocorreu.

Workspaces, branches, checkpoints, eventos e locks não expiram automaticamente; retenção de eventos/checkpoints é indefinida. Limpeza exige comando explícito, lock exclusivo, estado terminal/reconciliado, ausência de operação desconhecida, dirty/untracked/submódulo; preserva histórico persistido. Checkpoint só é reconhecido quando há commit de parent único igual à base, branch/ref esperada, HEAD/OID e tree consistentes, diferença de paths exatamente igual ao allowlist, blobs/tipos esperados e trailer único `AgentFlow-Operation: <operation_id>`. Falha ou ambiguidade deixa checkpoint não verificado.

Git é invocado em argv sem shell, com prompts desligados; sistema/global config isolados por ambiente privado, hooksPath vazio, assinatura/editor desativados e herança de variáveis `GIT_*` removida. Não alterar configuração de usuário. Rejeitar alias/redirecionamento, hooks personalizados, filtros externos `filter.*`, fsmonitor executável, atributos filter, submódulos e mecanismos não auditáveis. Ator (nome/email) deve ser explícito e persistido; committer fixo AgentFlow; mensagem <=1 KiB. Estes parâmetros operacionais mensuráveis estão fixados em F4-01 e cobertos pelos testes de implementação.

### Contrato de escopo por revisão

A Fase 4 evolui `tasks.toml` para schema v2, com `versioned_paths` obrigatório por tarefa. Uma lista vazia declara tarefa sem escrita versionada; lista não vazia é uma allowlist exata de paths relativos para a revisão. Não há glob nem diretório implícito. Schema v1 continua legível e equivale a lista vazia, preservando digests legados quando o campo está vazio; só edição/revisão explícita autoriza novo escopo. O modelo `Task` e seu snapshot persistido carregam a lista. Agente que precisa alterar arquivo versionado deve receber worktree; modo de execução gravável sem escopo aprovado é recusado.
