# AgentFlow APL MVP-08 — Restricted Inference Harness

**Estado:** implementação parcial; inferência real indisponível.
**Parecer Windows:** **PARTIAL**.
**Parecer Linux:** **BLOCKED / não homologado**.
**Parecer global:** **PARTIAL**; não elegível para piloto de worker restrito.

## Fronteira de confiança

O APL supervisionado é o chamador confiável; entrada de resultados/jobs e toda resposta do worker são dados não confiáveis. O worker futuro teria de executar sob identidade/ACL distintas e receber apenas JSON limitado por IPC. A saída seria decodificada para `PlanProposal`, validada por `validate_proposal` e submetida ao aceite supervisionado. Nenhuma proposta executaria job automaticamente.

O harness desta entrega não inicia subprocessos. Não aceita executável, `cwd`, argumentos, environment, callback de lançamento ou evidência fornecida pelo chamador para abrir uma exceção. A ausência de backend retorna `WorkerStatus.BLOCKED`. Isso é fail-closed no dispatch do APL, não prova de que um processo confinado já possa ser criado.

## Interfaces implementadas

Em `src/agentflow/apl/restricted_worker.py`, exportadas por `agentflow.apl`:

- `RestrictedWorkerSpec`: ID, JSON de entrada, timeout e limites de memória/saída; não contém campos de processo.
- `IsolationPolicy`: conjunto obrigatório imutável de controles; rejeita remoção de controles ou habilitação de rede, workspace, credenciais, subprocessos ou extensões.
- `WorkerLaunchResult` e `WorkerTerminationResult`: estados tipados; resultado de término só pode ser `terminated` quando descendentes estão confirmados encerrados.
- `IsolationEvidence` e `CapabilityProbe`: evidência por controle e relatório observado. Só evidência marcada simultaneamente `enforced` e `verified` satisfaz o relatório; o harness não aceita probe injetado como autorização de lançamento.
- `RestrictedInferenceHarness`: probe atual identifica backend `unavailable`, todos os controles ficam sem evidência efetiva e `launch` bloqueia. `terminate` informa que não há worker iniciado.
- `decode_worker_proposal`: limita JSON, rejeita chaves duplicadas/constantes inválidas, exige envelope versionado exato e usa `proposal_from_mapping`. A proposta ainda deve passar por `validate_proposal`.

Limites atuais do contrato: entrada 1 MiB, saída 2 MiB, timeout até 300 s e memória solicitada entre 16 MiB e 2 GiB. Esses são limites validados pelo contrato, **não quotas OS aplicadas**. `memory_limit_bytes` não pode ser tratado como enforcement enquanto backend não aplicar e verificar a quota.

O módulo não implementa o protocolo `Executor`, não altera CLI, autorização, Broker/TEG ou execução do APL. Não há modelo/adaptador semântico.

## Controles e evidências por SO

| Sistema | Evidência local | Controles aplicados pelo novo harness | Resultado |
|---|---|---|---|
| Windows | Código existente contém Job Object configurado com `KILL_ON_JOB_CLOSE`, associação do processo suspenso e encerramento de árvore. O runtime existente também detecta timeout/cancelamento em execução geral. Inspeção de código, sem worker/harness iniciado. | Nenhum controle de isolamento é aplicado por este harness. Job Object não restringe identidade, filesystem, credenciais, rede ou extensões; a implementação existente não configura quotas de memória/CPU/PIDs para este uso. AppContainer, token restrito, ACL efetiva e firewall de egresso não foram implementados/verificados. | **PARTIAL**: contratos e bloqueio fail-closed existem; falta fronteira OS completa e testes de negação efetiva. Lançamento sempre bloqueado. |
| Linux | Host da tarefa é Windows. `wsl.exe --list --quiet` informou que o Subsistema Windows para Linux não está instalado. Nenhum kernel/distro Linux foi iniciado ou sondado. | Nenhum namespace de usuário/mount/rede, cgroup, seccomp, filesystem read-only ou processo Linux foi criado/inspecionado. | **BLOCKED / não homologado**: sem ambiente Linux de teste; nenhum resultado Windows é extrapolado. |

Também não se alteraram permissões globais, sandbox, Codex, serviços, firewall ou configuração de sistema. Não foram instalados modelos, providers ou dependências.

## Testes executados e alcance

`tests/test_apl_restricted_worker.py` cobre validação de JSON de entrada, tamanho/versão/limites de contrato, tentativas de relaxar policy, cálculo de evidência completa/incompleta, bloqueio de lançamento sem backend, ausência de worker no cancelamento, validação do contrato de encerramento e parsing estrito da resposta APL. Cenários marcados como leitura/escrita de arquivo protegido, rede, subprocesso, memória, deadline e descendente confirmam apenas que **não há despacho** sem backend.

Esses testes não executam um worker hostil e não demonstram que Windows/Linux negaram chamadas ao filesystem, rede ou criação de processo. Não são homologação adversarial OS. Testes de timeout, cancelamento e encerramento de árvore de worker real, consumo excessivo, falha parcial ao instalar política, concorrência e recuperação do worker real permanecem **não executados**, porque não existe processo restrito para exercitá-los com segurança.

Testes executados nesta etapa:

- `.venv\Scripts\python.exe -m pytest -q tests/test_apl_restricted_worker.py tests/test_apl.py` — **54 passed** após os ajustes finais do módulo.
- `pytest -q` (Python global 3.13) — **435 passed** em 260,68 s.
- `.venv\Scripts\python.exe -m pytest -q` não coletou a suíte: 9 erros de importação por `ModuleNotFoundError: cryptography`; essa dependência está declarada no `pyproject.toml`, mas ausente da `.venv`. Nenhuma dependência foi instalada. A execução pelo pytest global completou a suíte.

## Requisitos para homologação futura

### Windows

Selecionar mecanismo OS aplicável (por exemplo, AppContainer ou usuário/token restrito), provar identidade efetiva, ACL negando workspace, `.agentflow`, perfis e credenciais do executor, diretório temporário privado, rede negada por regra efetiva, plugins/extensions ausentes, limite de memória/processos/CPU/I/O, limite de entrada/saída, timeout e término de toda a árvore. A política deve ser aplicada antes de iniciar o worker e falha/instalação parcial deve resultar em não lançamento. Verificar acesso por tentativas com arquivos sentinela e destinos de rede/processo controlados, sem tocar permissões globais.

### Linux

Em ambiente Linux dedicado, verificar suporte e privilégios sem pressupor user/mount/network namespaces, cgroups e controllers, seccomp, mount read-only e quotas. Criar namespace/identidade por worker, retirar mounts e credenciais, negar rede, instalar seccomp e limites antes do `exec`, comprovar o estado efetivo e testar ataques negativos. Se qualquer recurso não estiver disponível, bloquear. Nenhuma dessas propriedades foi testada nesta etapa.

### Ambos

Adicionar backend privado revisado que não permita bypass por `CapabilityProbe` fabricado; salvar request digest/estado antes do spawn; identificar PID/árvore de forma inequívoca; persistir resultado depois; manter `unknown` após crash/timeout ambíguo sem retry; impor idempotência e concorrência; testar chaves repetidas e recuperação. Para inferência remota futura, rede deve permanecer negada por padrão e qualquer endpoint requer decisão/isolamento de dados separado.

## Recuperação e riscos residuais

Como nenhum worker pode iniciar, não existe identidade de processo, estado em execução ou resultado para recuperar. O cancelamento retorna `NOT_RUNNING`; não afirma encerramento de processo externo. Não há retenção ou log de inferência nesta entrega. Em implementação futura, resposta incompleta/timeout/crash após dispatch deve resultar em `unknown`; não repetir automaticamente uma chamada possivelmente faturada ou processada.

`IsolationEvidence` é um contrato de relato, não prova criptográfica nem mecanismo de enforcement. O único caminho de lançamento atual é permanentemente bloqueado pelo backend `unavailable`. A aprovação de um futuro backend requer evidência do SO e testes adversariais no sistema operacional exato, não somente a configuração pedida ou a afirmação do adapter.

## Próximos passos

1. Homologar isoladamente uma implementação Windows de identidade/ACL/rede/quota/cancelamento sem alterar políticas globais; até isso ocorrer, manter lançamento bloqueado.
2. Planejar uma homologação Linux em host Linux dedicado, após disponibilidade e autorização explícita desse ambiente.
3. Reexecutar toda suíte e os testes OS adversariais em cada plataforma antes de classificar como `READY_FOR_RESTRICTED_WORKER_PILOT`.
4. Só então criar um `PlannerAdapter` que use o transporte restrito; não tornar planner real padrão e não conectar o worker a jobs.

**Conclusão:** MVP-08 entrega contratos importáveis e um gate que falha fechado. Windows permanece parcial e Linux não homologado. Não habilitar inferência semântica real.
