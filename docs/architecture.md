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
