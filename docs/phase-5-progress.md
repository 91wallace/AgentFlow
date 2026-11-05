# Progresso da Fase 5

Branch local de implementação/publicação: `feat/validation-quality-gates-phase-5`. Após autorização específica, `git fetch origin main` confirmou `origin/main` em `8ec623813709d71b98fbd0be7e8fba87ebb9d565`; `git merge --ff-only origin/main` avançou a branch sem conflitos ou sobrescrita das alterações locais. `.agentflow/`, `autonomous-development-runner/` e alterações locais preexistentes foram preservados.

Base reconciliada: `HEAD` e `git merge-base HEAD origin/main` são ambos `8ec623813709d71b98fbd0be7e8fba87ebb9d565`; a Fase 4 integrada é ancestral da implementação.

## Estado das tarefas

| Tarefa | Estado | Evidência |
|---|---|---|
| F5-01 | Concluída antes de qualquer código | Arquitetura e ADR fixam quotas, retenção, capacidades obrigatórias/opcionais, comportamento `unavailable`, segredo, rede, cancelamento/crash e limpeza. Checagem de parâmetros documentados passou; `git diff --check` passou. |
| F5-02 | Concluída | Contratos imutáveis para requests/results, gates, runtime/capabilities, validator/runner, build/artifact e entrega por autorização específica; protocolos não importam adaptadores concretos. `pytest`: 8 passaram. |
| F5-03 | Concluída | Loader mantém spec v1/v2 e tasks v1/v2/v3; schema v2 valida allowlist fixa sem shell; schema v3 valida gates, critérios, referências, DAG e limites. Digest legado vazio preservado. `pytest`: 39 passaram em `test_planning`, `test_execution_schema`, `test_phase5_contracts` e `test_phase5_planning`. |
| F5-04 | Concluída | Migração v3→v4 transacional com backup consistente, FK/integrity checks e rollback; store v4, tabelas de runs/gates/snapshots/artifacts/delivery/eventos e registry persistente de IDs não reutilizáveis. Regressões cobrem migração, rollback injetado, versão futura, integridade, operação ID transversal e reutilização de IDs. Suíte relacionada final: 38 passaram em 46,39 s (`test_phase5_schema`, planejamento/contratos, schema de execução e Git workspace). |
| F5-05 | Concluída | Snapshot materializado somente de blobs do tree de checkpoint F4 verificado; manifest canônico cobre paths/modos/OIDs/SHA-256/tamanhos; streaming limitado, sem checkout/filtros; integridade e conjunto exato verificáveis antes/depois. Symlinks/submódulos rejeitados. Testes de snapshot + Git: 19 passaram em 70,89 s. |
| F5-06 | Concluída | Scheduler sequencial com seleção explícita/fechamento de DAG, preflight de capacidades, run/gate states, timeout bounds, processo-intenção/identidade callback, idempotência e recuperação inconclusiva sem retry. Scheduler simulado: 11 passaram em 90,16 s. |
| F5-07 | Concluída localmente | Runtime local shell-free valida SHA-256/versão, env mínima, redige e limita logs; timeout/cancelamento preservam temporário se término da árvore não é comprovado. Docker opt-in exige imagem local por digest, inspeciona HostConfig/mounts e fornece capabilities `supported` em preflight, `enforced` após inspeção da execução. StructuredGateValidator exige relatório `declared` vinculado a gate/revisão/snapshot e cobertura. 13 testes de runtime/validator passaram (inclui 3 testes com engine Docker simulado); CLI Docker não instalado, sem alegação de validação contra daemon real. |
| F5-08 | Concluída localmente | `record_verified_gate_evidence` só grava quando run+gate `passed`, checkpoint e snapshot verificados, revisão vigente e critério associado. Evidência append-only contém os digests da definição/gate/validator/snapshot; idempotência testada e falhas/inconclusões não promovidas. 2 testes de evidência passaram; conclusão continua chamada separada F2. |
| F5-09 | Concluída localmente | `ArtifactDeliveryService` valida proveniência do run/checkpoint/snapshot, builder, artefato único, SHA-256 e limites; reserva operação antes do builder/provider; autorização persistida por operação/destino/digest; delivery desligada por padrão; divergência ou resultado `unknown` não repete provider. 1 teste integrado cobriu proveniência, repetição idempotente, resultado unknown e artefato adulterado. |
| F5-10 | Concluída localmente; CI remota pendente | CLI explícita `validate`, inspeção `validation-state` e limpeza explícita `validation-cleanup`; progresso, testes de retenção e documentação incluídos. Instalação editável isolada, smoke do CLI/import, suíte pytest completa (181 testes) e `git diff --check` aprovados. CI remota aguarda autorização posterior para publicar a branch. |

## Parâmetros normativos F5-01

- Logs: 8 MiB combinados por gate, 64 MiB por run, até 5 GiB armazenados por projeto; retenção de 30 dias, truncagem prefixo/sufixo e digest do stream.
- Snapshots: 2 GiB cada, até 10 GiB por projeto, retenção de 7 dias após run terminal.
- Artefatos: 256 MiB cada, 1 GiB por run, até 10 GiB por projeto; retenção 30 dias e pin enquanto entrega estiver pendente.
- Temporários: 4 GiB por run; total de payload operacional (logs, snapshots, artefatos e temporários) 20 GiB por projeto.
- Runtime por gate/build: 2 CPUs, 4 GiB de memória e 64 processos como defaults; máximos configuráveis de 8 CPUs, 16 GiB e 256 processos. Timeout de 600 s default e 3600 s máximo.
- Local: solicitação explícita, argv allowlisted sem shell, ambiente sem credenciais herdadas, timeout/identidade/saída limitados, temporário privado e digest de snapshot conferido. Rede, read-only forte e quotas OS opcionais; capacidade não provada vira `unavailable`, política que a exigir bloqueia, rede não confinada exige autorização explícita.
- Docker: opt-in; imagem por digest, sem privilégios/host socket/host network, source read-only, output limitado, rede `none`, CPU/memória/PIDs, timeout e encerramento verificáveis; capability obrigatória não comprovada bloqueia e não há fallback local silencioso.
- Segredos: ambiente mínimo, nenhuma credencial herdada; referências por nome/escopo somente com autorização. Valores não são persistidos/logados; redator falho descarta output bruto.
- Cancelamento/timeout: saída graciosa por 5 s, terminar e confirmar árvore/container antes de limpar workspace gravável. Em crash, validar identidade PID+início; se processo/término não puder ser provado, quarentenar e bloquear cleanup/reexecução até reconciliação. Temporários de preflight são removidos imediatamente.

## Validação após F5-01

- PowerShell verificou presença dos limites normativos em arquitetura e ADR: passou.
- `git diff --check`: passou.
- Nenhum teste de código executado nesta etapa documental. Nenhum código F5-02+ foi escrito antes do gate.

### F5-03 — contratos TOML e DAG

- Schema/contratos implementados em `src/agentflow/planning/loader.py`, `models.py` e testes de regressão TOML.
- `pytest`: 39 passaram; testes cobrem compatibilidade e digest legado, allowlist, referências inválidas, ciclos e DAG determinístico.
- Defeito corrigido: interpretação de caminho absoluto Linux em Windows foi trocada para `PurePosixPath`, mantendo validação cruzada por plataforma.

### F5-04 — migração SQLite v4

- Implementação em `src/agentflow/state/validation_schema.py`, `sqlite.py`, `validation.py` e exports.
- `pytest tests/test_phase5_schema.py`: 6 passaram; suíte relacionada mais ampla: 38 passaram em 46,39 s.
- Evidência: dados de tarefa v3 sobrevivem à migração, backup v3 existe, `integrity_check=ok`, `foreign_key_check=[]`; DDL inválido injetado deixa versão 3 e remove integralmente tabelas v4; reutilização de gate removido e colisão transversal de `operation_id` são rejeitadas.
- Defeito corrigido: o store base precisa reabrir bancos em schema suportado mais recente sem tentar downgrade; ajustado para aceitar versões v2/v3 já migradas e recusar apenas versões acima do máximo conhecido pelo perfil.

### F5-05 — snapshots de checkpoint

- Implementação em `src/agentflow/validation/snapshot.py` e contratos públicos `SnapshotEntry`/`ValidationSnapshot`; `GitLocalProvider` oferece leitura da árvore e streaming de blobs sem checkout.
- `pytest tests/test_phase5_snapshot.py tests/test_git_workspace.py`: 19 passaram em 70,89 s.
- Evidência: checkpoint e tree OIDs coincidem; SHA-256 e OIDs Git são conferidos; quota aplicada no fluxo do blob; mutação/conteúdo extra invalida `verify`; symlink, submódulo, traversal, `.agentflow` operacional e estouro de quota não materializam conteúdo.
- Checkout original permaneceu no mesmo HEAD e com o mesmo arquivo `tracked.txt`; os testes operam somente em repositórios e SQLite temporários.
- Limitação fixada/documentada: symlinks são rejeitados em ambas plataformas até haver implementação no-follow validada por OS. Runtime ainda precisa verificar digest antes/depois (F5-07); o snapshot, por si só, não é sandbox.

### F5-06 — scheduler, estados e recuperação

- Implementação em `src/agentflow/validation/scheduler.py`, `state/validation.py`, contratos e triggers v4.
- `pytest tests/test_phase5_scheduler.py`: 11 passaram em 90,16 s, todos usam runtime e validator simulados; Git e SQLite são temporários.
- Evidências: gates executam em ordem sequencial; falha/timeout/cancelamento produzem estados distintos e dependentes `not_run`; capacidades necessárias indisponíveis bloqueiam antes do spawn; rede não confinada só executa com autorização do request/policy; mutação do snapshot falha e não altera workspace/checkout original.
- Concorrência entre duas conexões SQLite permite uma única reserva ativa; `operation_id` idêntico retorna resultado, digest divergente conflita; exceção simulando crash após spawn persiste identidade, reconcilia como `inconclusive` e não reexecuta. `unresolved=1` bloqueia outro run até confirmação explícita de processo parado.
- Defeitos corrigidos durante os testes: estados transitórios incompatíveis com `CHECK` de `validation_operations`; operação de output reservada por gate em vez de uma vez por run; query de transição lia `operation_id` da tabela errada.

## Registro de execução

Adicionar evidência por tarefa após cada implementação e teste relevante. Falhas, correções, contagem de testes e limitações por sistema operacional devem ser registradas antes de avançar para a tarefa seguinte.

### Evidências F5-07 a F5-09

- `pytest tests/test_phase5_runtime.py`: 8 passaram; cobre argv, hash/versão, sanitização, limite/digest do stream, timeout, cancelamento, falha de spawn e capabilities locais `unavailable`.
- `pytest tests/test_phase5_docker_runtime.py`: 3 passaram com engine simulado; configuração divergente e imagem ausente são recusadas antes de start; cancelamento confirma stop e remove container; capabilities mudam de `supported` para `enforced` somente após inspeção de configuração.
- `pytest tests/test_phase5_validator.py`: 2 passaram; saída JSON duplicada, incompleta ou stale não valida; status do comando é `declared`, não evidência final.
- `pytest tests/test_phase5_evidence.py`: 2 passaram; proveniência vigente e operação idempotente aceitas, parâmetros divergentes/gate falho/critério não coberto recusados.
- `pytest tests/test_phase5_delivery.py tests/test_phase5_schema.py tests/test_phase5_scheduler.py tests/test_phase5_docker_runtime.py`: 21 passaram em 100,14 s; cobriu migração/rollback, DAG/estado, snapshots, validação sequencial, evidência, artifact, autorização/delivery e unknown sem retry. (A revisão subsequente adicionou uma asserção de adulteração do artifact; reexecução pendente.)
- Correções durante a validação: identidades de version-probe e gate são processos distintos; geração de stdout grande foi encurtada para respeitar limite de linha de comando Windows; spawn negado agora resulta em erro controlado; logs Windows incluem CRLF nos bytes contados; teste Docker distingue container de probe curto e container persistente.
- Retenção/quotas: scheduler recusa snapshots que excedam 2 GiB cada, 10 GiB/projeto e o teto agregado de 20 GiB junto com logs/artefatos; reserva logs com 64 MiB/run e até 5 GiB/projeto considerando apenas payload retido. Build aplica 256 MiB/artefato, 1 GiB/run, 10 GiB/projeto e agregado 20 GiB. `validation-cleanup` remove logs após 30 dias, snapshots após 7 dias de terminal/reconciliado, artifacts expirados apenas quando não pinned/pending; mantém digests/tamanhos/eventos. Teste da limpeza em teste de retenção simulado; reexecução idempotente adicionada.
- Segurança runtime revisada: saída secreta JSON/form-urlencoded e cabeçalhos Authorization é redigida antes de persistir; capabilities `supported` não satisfazem resultado; Docker só libera preflight de recursos em daemon local Linux com cgroup v2/controllers verificáveis, e Windows/Docker Desktop sem prova falha fechado. Source é único bind read-only; `/tmp` e `/output` são tmpfs limitados; mounts, user, network mode, caps, security-opt e quotas são inspecionados. Argumentos não podem referir host paths externos nem traversals.
- Limites de plataforma: runtime local Windows não alega rede/quotas/árvore de processos como confinadas, e ACL de temporário privado é `unavailable`; Linux local também não alega cgroup/network namespace/read-only forte sem mecanismo comprobado. Docker é simulado aqui porque CLI/daemon não estão instalados. Windows/Linux reais e CI ainda não homologados.
- Instalação: `pip install --no-deps --no-build-isolation -e .` inicialmente falhou por `setuptools.build_meta` ausente no venv (sem dependências de build instaladas). `pip install --no-deps -e .` com build isolation instalou o backend declarado no `pyproject.toml` e o editable `agentflow 0.1.0`; smoke `agentflow --version`, `agentflow validate --help` e imports `agentflow.validation`/`agentflow.delivery` passaram. Não foi alterado o conjunto de dependências do projeto.

### Fechamento de validação local F5-10

- Suíte completa final após a correção multiplataforma: `python -m pytest -q` — 181 passaram em 222,09 s; o teste focado passou (1 teste).
- `git diff --check`: passou; Git emitiu somente avisos de normalização LF/CRLF no Windows.
- Smoke do pacote editável: `agentflow --version` retornou `agentflow 0.1.0`; `agentflow validate --help` e imports de `agentflow`, `agentflow.validation` e `agentflow.delivery` passaram.
- Commit inicial publicado: `4c1b2e73edb4058aa604428aff0293665e34eda6`; PR #5 aberto contra `main`. O diretório `autonomous-development-runner/`, `.agentflow/` local e alterações preexistentes foram preservados e ficaram fora do commit.
- Nesta publicação, arquivos compartilhados de arquitetura/plano são staged apenas com hunks da Fase 5; atualizações locais de encerramento F4, progresso F2–F4 e o ADR 0004 permanecem fora do commit.
- Limitação de homologação: Docker foi exercitado apenas com engine simulado; nenhum daemon real foi usado.

### Primeira execução da CI e correção de regressão

- PR #5 executou o workflow `CI` em duplicidade (eventos `push` e `pull_request`). Os dois conjuntos falharam nos três jobs Python 3.11, 3.12 e 3.13 no teste `test_local_runtime_bounds_logs_and_hashes_the_full_stream`.
- Os logs dos jobs mostraram que o coletor contou os bytes reais no Linux (`200001`), enquanto a expectativa do teste codificava CRLF (`200002`). A diferença era somente a newline de `print`; não houve defeito no limite nem no digest do stream.
- Regressão corrigida em `tests/test_phase5_runtime.py`: expectativa agora usa `os.linesep`, mantendo validação exata de tamanho e SHA-256 sob Windows/Linux. O teste focado passou localmente (1 teste) e a suíte completa pós-correção passou (181 testes em 222,09 s). Novo commit e nova CI serão publicados antes de concluir a homologação.
