# MVP-20 — Execução contínua sob autorização inicial

## Estado

**PARTIAL — mecanismo persistente exercitado com executor sintético; dispatch real não homologado nem exposto pela CLI.**

Esta fatia implementa a máquina de execução grant-scoped em `agentflow.apl.authorized_execution`. Não altera `UnavailableLocalAuthorizationAuthority`, não habilita dispatch global e não fornece uma implementação padrão para o executor ou para a resolução do grant.

## O que foi implementado

- `VerifiedExecutionGrant` representa uma resolução feita por autoridade confiável. Inclui grant e request IDs, digest/revisão da proposta, digest/revisão do plano aceito, identidade e caminho do workspace, digest da identidade do workspace, identidade do operador, executor/versão vinculados, operações, escopo, validade e limites globais/por tarefa.
- `SQLiteAuthorizedExecutionStore` usa o mesmo banco operacional do APL. Vincula um grant a uma única sessão, persiste plano e tarefas, gera `operation_id` estável por tarefa e mantém journal append-only com triggers contra atualização/remoção.
- `AuthorizedExecutionController` valida o grant antes da sessão e imediatamente antes de cada despacho, exige cobertura exata de todas as tarefas, executa somente jobs elegíveis, valida evidências, persiste checkpoints e encerra automaticamente ao concluir.
- A fundação IFR classifica qualidade independentemente do estado do processo e grava tentativa, `attempt_id`, estratégia, diagnóstico, decisão e evidências no mesmo journal. O `operation_id` permanece estável para o job. Retry só é aceito após `NoEffectExecutionError` com efeitos comprovadamente ausentes/reconciliados, estratégia expressamente coberta pelo grant, executor capaz de aplicá-la e policy favorável. Não há repetição cega da mesma estratégia.
- `unknown`, `INCONCLUSIVE`, efeito possível, falha determinística sem estratégia diferente, falha genérica, revogação, expiração e limites atingidos bloqueiam a execução e registram escalonamento. Não existe ainda interface autenticada para responder ao escalonamento e retomar; o estado fica bloqueado.
- A reserva da tarefa, tentativa e duração é persistida em transação SQLite antes do executor; a intenção inclui operation ID, attempt ID e digests. Duração medida é consumida após retorno, saldo é liberado após retorno verificado ou prova de ausência de efeito; `unknown` retém o saldo restante. Depois de crash, PID e marcador de criação são verificados: processo vivo mantém a reserva; processo ausente converte tarefa em `unknown`. `unknown` nunca é redispatchado automaticamente.
- A transação reserva a tarefa e a tentativa, valida o binding persistido do grant e aplica limites observados no ponto de reserva. Ela ainda **não reserva saldo de tempo/orçamento por tarefa**, nem separa quota reservada, consumida e liberada. A atualização de consumo acontece depois do resultado; portanto não se declara prevenção de oversubscription de orçamento/duração entre processos nem hard cap financeiro do provider.
- Cancelamento persistido impede o próximo job; revogação/expiração é consultada antes de cada despacho e durante o processo por token cooperativo. O `LocalRuntime` faz uma última revalidação da intenção imediatamente antes de `Popen`. A criação do processo não é atômica com SQLite; cancelamento pode não ser instantâneo e não desfaz efeitos já iniciados.
- A resolução autoritativa de consentimento agora apresenta e vincula as operações de **todo o plano resultante**, em vez de somente a lista de alterações. Mudanças do plano aceito continuam exigindo grant compatível com o digest/revisão novos.

## Contrato de integração

Uma aplicação deve injetar `ExecutionGrantResolver`, `AuthorizedJobExecutor` e `ResultEvidenceValidator`. Não há fallback permissivo. O resolver é responsável por validar assinatura, estado `approved`, expiração, revogação, request digest, identidade do Broker/autoridade e por revalidar o caminho canônico e a identidade atual do workspace contra a vinculação persistida. O controlador compara a resposta com o snapshot imutável do plano e rejeita grant que não cubra literalmente `job:<id>` para cada tarefa.

`start(cycle_id, grant_id)` cria/reutiliza uma única sessão idempotente para esse grant e executa sequencialmente. `resume(run_id)` retoma somente sessão segura. `SQLiteAuthorizedExecutionStore.request_cancel(run_id)` registra cancelamento; a operação ativa depende da interface do executor para cancelamento e pode exigir reconciliação.

Este contrato ainda não está ligado ao comando `agentflow apl` nem ao dispatcher local de `ExecutionStore`/`LocalRuntime`. Não se deve configurar um resolver sintético para trabalho real. O adaptador existente de execução precisa oferecer identity binding, autorização TEG imediatamente antes do efeito, timeout/cancelamento, coleta de evidência verificável e reconciliação de processos sob as políticas por sistema operacional.

## Limites de segurança

- A sessão não emite, renova, amplia ou altera grants.
- O resolver é reconsultado antes de cada job. Um grant inválido bloqueia a sessão.
- Uma sessão única por `grant_id` impede reutilização da mesma autorização em uma nova sessão por este controlador.
- O orçamento é estimado e conferido com o custo reportado; não é teto financeiro do provider.
- `ExecutionOutcome.cost` fica registrado como estimativa não verificada; o adaptador atual não expõe custo observado autenticado. Não há reserva financeira nem hard cap comprovado.
- A validação do resolver e a reserva da tarefa são chamadas sequenciais. O último ponto controlado antes de criar o processo ainda não compartilha uma transação/lease com revogação e quota; essa janela permanece um gate bloqueado.
- O executor recebe apenas escopo e operação já autorizados; o mecanismo genérico não prova isolamento do processo.
- A recuperação usa identidade PID + marcador de criação para não confundir PID reutilizado. Ela não prova que um processo externo terminou efeitos; resultados ambíguos ficam `unknown`.
- A aprovação local continua confiando no host/OS; isso não satisfaz `INDEPENDENT_AUTHORITY` nem os gates de alta garantia.

## Bloqueios para dispatch real

1. A revisão ADR 0008-R separa implementação local de liberação operacional; seu gate L7 requer autorização separada do proprietário para o primeiro dispatch sintético.
2. A solicitação atual não identifica um ciclo, proposta, digest, workspace e tarefa concretos para esse primeiro piloto; nenhum grant válido atual foi selecionado para execução.
3. O fluxo `handoff-review` atual aprova a proposta de replanejamento, enquanto a ativação da revisão e a identidade do plano aceito ainda não estão ligadas atomicamente ao grant. A fatia nova recusa despacho sem resolver esse vínculo.
4. O executor APL existente ainda recebe resultados previamente validados; `LocalRuntime` pertence ao fluxo de execução de tarefas do State Store. Falta o adaptador autorizado entre ambos, com TEG local homologado e `operation_id` transferido até o efeito.
5. A autoridade local e o consentimento por senha não foram homologados como fronteira de dispatch e não provam isolamento contra processos maliciosos da mesma conta ou administrador local.

Por esses motivos, não houve chamada a provider, execução real de job, aceite de plano ou dispatch. Um plano real não pode ser escolhido implicitamente pelo agente: o proprietário precisa autorizar um plano específico depois que o adaptador e os gates acima estiverem prontos.

## Testes

`tests/test_apl_authorized_execution.py`, `tests/test_apl_codex_execution.py` e `tests/test_apl_failure_recovery.py` usam SQLite real e adaptadores sintéticos. Cobrem execução sequencial, dependências, grants inválidos, quality `PASS`/`FAIL`/`INCONCLUSIVE`, resultado `unknown`, no-retry de efeito ambíguo, troca de estratégia dentro do grant, limite e assinatura sem progresso, replay, falha transacional, reabertura do journal, revogação antes de retry, concorrência, cancelamento e integridade do journal.

Validação atual após a fundação IFR MVP-20.1: testes focados IFR/execução **46 passaram**; suíte completa **621 passaram, 1 skip em 303,29 s**; `git diff --check` passou. O skip POSIX é condicionado a sistema POSIX e não executou no Windows.

Os testes sintéticos provam a máquina de estados e a persistência local. Eles não provam autenticação humana operacional, assinatura de grant real, integração ao Broker/TEG, isolamento do executor, efeitos de provider ou conclusão do MVP. Não houve chamada de provider nem job real.

## Próxima fatia

Implementar consentimento distinto para execução do plano completo; grant não ilimitado ligado ao digest/revisão, workspace, executor/versão, operações, quotas e validade; ledger transacional de reserva/consumo/liberação sem oversubscription multiprocesso; revalidação atômica no último ponto antes do spawn; cancelamento cooperativo na revogação; reconciliação de reservas após restart; e homologação comportamental do Codex CLI 0.167.2 antes de alterar allowlist. Depois preparar um piloto descartável de duas tarefas dependentes, apresentá-lo para aprovação humana autenticada específica e somente então considerar o primeiro dispatch. O detalhamento e os estados por gate estão em [MVP-20 controlado](mvp-20-controlled-codex-execution.md) e no [plano mestre](implementation-plan.md#trilha-transversal-mvp-20mvp-21--intelligent-failure-recovery).

## Atualização — integração controlada com Codex

A ponte `CodexAuthorizedJobExecutor` foi adicionada em `agentflow.apl.codex_execution`. Ela compõe o adaptador Codex e o `LocalRuntime` com o controlador. Workspace, operação do job, timeout, cancelamento, recibo e identidade do processo são verificados/persistidos. O AgentFlow não configura sandbox; o Codex CLI usa sua configuração nativa e o modo observado é registrado como `unknown`. A evidência aceita pelo controlador ainda requer um verificador de critérios explícito.

O consentimento de revisão existente continua sem ser tratado como autorização de execução do plano completo. O grant de execução autenticado e específico, seu resolver de produção e a homologação real ainda faltam. A versão local Codex CLI 0.167.2 não está na allowlist de versões homologadas (0.166.0). Não houve inferência ou execução real. Ver [MVP-20 — Integração controlada com Codex](mvp-20-controlled-codex-execution.md) para os testes e gates atualizados.

## Decisão posterior — execução local sem senha (ADR 0010)

O MVP-20 foi originalmente escrito para execução por grant autenticado. A exigência de senha/consentimento autenticado como condição universal para jobs locais comuns foi substituída pela ADR 0010: uma solicitação explícita inicia um contrato de execução persistido para o plano inteiro, sem aprovações intermediárias. A integração atual ainda recebe `VerifiedExecutionGrant` por `ExecutionGrantResolver`; portanto, esse contrato e o caminho CLI sem senha ainda não estão implementados. Não se converte grants históricos, não se habilita dispatch global e nenhum job real foi executado nesta refatoração. Até a implementação do resolver de contrato local, o controlador permanece no caminho legado grant-scoped.

## MVP-20 implementation checkpoint — local contract foundation

The APL controller now accepts a separately resolved `local_execution_contract` authorization through `LocalExecutionContractResolver`. `LocalAuthorizedExecutionService` provides the explicit, passwordless operator API to create a plan-bound SQLite contract and pass it to the existing controller. Contract payloads are immutable and journaled; operation replay is idempotent; workspace identity, plan DAG/digest/revision, scope, executor/version, validity, budget policy and quotas are rechecked. Existing reservations, IFR, cancellation, checkpoints, and pre-spawn checks remain in the controller path. Local contracts do not enter the Broker grant resolver. Agent/service initiation remains denied until a persisted preconfiguration policy exists.

Verification added in `tests/test_apl_authorized_execution.py` exercises two dependent synthetic tasks, a no-password service start, idempotent replay, plan/workspace/executor/revocation binding, rejection of agent initiation without preconfiguration, and canonical digesting of `spec:...` plan references. No real jobs were run.

## MVP-20 completion work — public local execution and acceptance gates

`agentflow apl run` now exposes the explicit-operator, passwordless LOCAL_SUPERVISED entry point. It creates or reuses an immutable SQLite execution contract and invokes the existing APL controller, IFR, authorized Codex bridge, LocalRuntime and journal. The command requires an interactive terminal, a persisted ready cycle, explicit `operation_id`, and finite job/attempt/time/estimated-cost/validity limits. There is no password, approve, digest confirmation or `authorize-plan` step. Automated initiators remain denied because no preconfigured agent-start policy exists.

Public companion commands are `agentflow apl run-status --run-id ...`, `run-cancel --run-id ...`, and `run-resume --run-id ...`. `run` emits a flushed `run_persisted` JSONL record with the run ID before the controller starts its first dispatch, so another terminal can inspect or cancel a long synchronous invocation. Replay of a completed operation returns its stored result without redispatch. A crash after contract persistence but before creation of the run is recoverable by replaying the same start operation: the service revalidates the same unexpired contract before creating the run. Existing running/unknown task recovery remains conservative; unknown outcomes are not resent. Cancellation revokes the local contract and asks active processes to stop cooperatively; it is not instantaneous and does not reverse existing effects.

Acceptance is independent from the Codex process result. `CodexAcceptanceEvidenceValidator` requires one valid process receipt and calls `PlanGateAcceptanceVerifier`, which checks task criteria against the immutable plan, copies the authorized workspace scope to a temporary snapshot, runs only the plan's existing allowlisted validation gates through the validation runtime, validates their bounded structured reports against gate revision and snapshot digest, and persists gate receipts and `PASS`/`FAIL`/`INCONCLUSIVE` evidence in the task outcome and journal. An exit code of zero alone cannot complete a task. On this Windows runtime, network isolation is not enforced; the CLI therefore requires the cycle's explicit `network:unconfined` scope and records this residual risk. Estimated provider cost is not a financial hard cap.

Validation on Windows: the focused APL execution/Codex/CLI/adapter set passed **102 tests** before the final CLI run-ID notification change; after that change, the focused controller/CLI/Codex bridge set passed **61 tests**. `agentflow apl run --help`, `run-status --help`, `run-cancel --help` and `run-resume --help` render the public options. The installed Codex CLI probe reports **0.169.0**, `unknown / version_not_homologated`; the allowlist remains unchanged at 0.166.0. Only local `--version`/`--help` probes were made. No Codex invocation, inference, job or pilot was run. The installed version is therefore a concrete blocker for the CLI's fail-closed Codex execution path; do not use the unverified-version override or change the allowlist as part of this MVP.

The MVP is **PARTIAL**, not operationally homologated. The passwordless service executed two dependent jobs in a deterministic synthetic executor; acceptance gate evaluation and persistence were tested with synthetic runtime reports. This proves the controller/service/test path, not Codex CLI compatibility or real task effects. Final Windows validation: **658 passed, 1 skipped** in the complete suite; **102 passed** in the focused CLI/APL/Codex set; `git diff --check` passed. The skip is POSIX-only and not applicable on Windows. `INDEPENDENT_AUTHORITY`, ADR 0010, the Broker path, Production Mode and global Codex settings were not changed. No global dispatch was enabled.
