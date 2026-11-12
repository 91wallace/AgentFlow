# Autonomous Planning Loop (APL)

## Estado

O APL é um módulo interno importável em `agentflow.apl`, implementado para coordenação determinística com adaptadores injetados. Ele não é um aplicativo/daemon separado e não ativa execução real. A implementação local é experimental e não está homologada para produção.

## Interfaces públicas

- `APLController`: recebe `Planner`, `Executor`, `Evaluator`, `Policy`, `CycleStore`, limites e relógio injetáveis; `start()` inicia um ciclo, `run()` retoma estado seguro e `recover()` bloqueia estados com execução ambígua.
- `APLState`, `APLLimits`, `JobProposal`, `ExecutionOutcome`, `Evaluation`, `CycleSnapshot`: contratos dataclass/enum versionados (`version=1`).
- Protocolos `Planner`, `Executor`, `Evaluator`, `Policy`, `CycleStore` mantêm integrações substituíveis. O planejador propõe jobs, enquanto somente a policy decide `authorized`, `approval_required` ou recusa.
- `SQLiteCycleStore`: persiste snapshot e eventos namespaced no arquivo SQLite operacional indicado pela aplicação; não cria um banco duplicado automaticamente fora do caminho explicitamente fornecido.

## Fluxo

`CREATED → PLANNING → READY → EXECUTING → EVALUATING → REPLANNING` repete sem copiar prompts manualmente: o histórico estruturado é passado ao planejador. Jobs elegíveis respeitam dependências. `COMPLETED`, `FAILED`, `BLOCKED`, `WAITING_APPROVAL` e `CANCELLED` são estados terminais nesta versão. `unknown` e exceção durante execução bloqueiam para reconciliação e nunca reexecutam automaticamente. A recuperação de snapshot `EXECUTING`/`EVALUATING` bloqueia conservadoramente.

O escopo é capturado imutavelmente no início. Propostas que declaram escopo fora dele bloqueiam. A policy é consultada antes de encaminhar; o APL não concede permissão nem implementa Approval Broker/TEG. O executor é um protocolo injetado e os testes usam apenas simuladores.

## Persistência, limites e evidências

`apl_cycles` guarda snapshot corrente e revisão; `apl_events` registra digest e estado por revisão, com atualização transacional `BEGIN IMMEDIATE` e compare-and-swap de iterações/revisão para concorrência/duplicação. Evidências estruturadas são persistidas dentro dos outcomes e snapshots. Limites configuráveis cobrem número de jobs, iterações, duração total, orçamento reportado pelo adaptador e progresso repetido. Valores de custo são telemetria do adaptador: sem enforcement do provider, orçamento é apenas limite observado e não hard cap externo.

Exemplo mínimo (sem provider):

```python
from agentflow.apl import APLController, APLLimits, SQLiteCycleStore

controller = APLController(planner, simulated_executor, evaluator, existing_policy,
                           SQLiteCycleStore(".agentflow/state.sqlite3"),
                           limits=APLLimits(max_jobs=5, max_iterations=8))
snapshot = controller.start("objetivo", authorized_scope=("src/",))
```

## Developer Mode supervisionado

`SupervisedCycleService` cria propostas de jobs a partir do plano TOML já validado e exige escopo explícito. `next_job` apenas retorna o primeiro job cujas dependências têm resultados associados. `pause`, `resume` e `cancel` gravam snapshots/eventos por revisão com compare-and-swap. `submit_result` aceita apenas um `attempt_id` existente em `ExecutionStore` com `process_succeeded`, `validation_passed` e IDs de evidência `verified` registrados no evento de validação para cobrir cada critério. A referência de tentativa tem unicidade global no armazenamento APL. Não recebe resumo/evidência textual como entrada.

CLI: `agentflow apl start --profile developer --objective ... --scope ...` (scope repetível), `next`, `submit-result --attempt-id ...`, `pause`, `resume`, `cancel`, `status`, `history` e `inspect`. Todos aceitam `--path`. O perfil `developer` é opt-in e não muda policy. Start/next/resume/submit-result nunca chamam executor. `resume` só aceita `PAUSED`; `BLOCKED` (incluindo outcome unknown) não retoma. Cancelamento não altera estado bloqueado/ambíguo. Evidência permanece vinculada ao job/tentativa e revisão pelo store de execução existente.

## Replanejamento supervisionado (MVP-05)

`PlannerAdapter.propose(ReplanContext)` recebe objetivo, escopo, snapshot dos jobs/revisão, jobs concluídos, resultados validados, referências de evidência verificada, bloqueios e limites restantes. Retorna `PlanProposal` versionada com justificativa, mudanças `add`/`modify`, dependências, critérios, condição de conclusão e IDs de evidência. Resultados/instruções de job são dados não confiáveis; adapter não recebe autoridade nem executa comandos.

`validate_proposal` recusa expansão de escopo, IDs instáveis/duplicados, referências/dependências ausentes, ciclos, critérios inválidos, edição retroativa de jobs com tentativa/resultados e resolução de bloqueio sem evidência verificada referenciada. Aceite também exige que o ciclo e a revisão do plano ainda correspondam à proposta. O ciclo paused permanece paused após aceite. `replan` só grava proposta; `proposals` consulta; `accept-plan` exige `--confirm` e cria uma revisão APL append-only; `reject-plan` grava evento. Revisões, payload de propostas e eventos têm proteção SQLite contra update/delete. A mesma requisição no mesmo snapshot é idempotente.

CLI: `agentflow apl replan --cycle-id ID [--request-id ID]`, `proposals --cycle-id ID`, `accept-plan --cycle-id ID --proposal-id ID --confirm` e `reject-plan --cycle-id ID --proposal-id ID`. A CLI usa um adapter determinístico sem alterações, explicitamente rotulado; não é semântica/inteligência. `UnavailableSemanticPlanner` falha fechado porque não há planejador semântico local autorizado/configurado. Adaptadores reais ainda não estão conectados. Aceitar uma proposta só atualiza o plano do ciclo APL; não altera `.agentflow/tasks.toml`, não agenda execução e não concede permissão a jobs novos.

MVP-06 exige `agentflow apl replan --planner deterministic`. A opção `semantic` não está disponível. A inspeção local do Codex CLI encontrou um harness que executa comandos; não há garantia verificada nesta instalação que o torne apenas inferência, impeça subagentes/MCP, bloqueie efeitos externos ou isole credenciais do executor. Portanto integração semântica real está **BLOCKED**. Consulte [a revisão de capacidades MVP-06](apl-mvp06-capability-review.md). O decodificador estrito e testes de propostas simuladas protegem o contrato, não constituem isolamento do processo.

## MVP-13 — planejador Codex experimental

`CodexCLIPlannerAdapter` implementa `PlannerAdapter` usando o `CodexAdapter` e `LocalRuntime` já existentes. A ativação exige `agentflow apl replan --planner codex --enable-experimental-codex`; a seleção de planejador é sempre explícita. A chamada fornece apenas o contexto APL serializado e usa uma pasta temporária vazia, `--sandbox read-only`, `--ask-for-approval never`, limite de tempo e limite de bytes dos logs/entrada. A resposta é decodificada por `proposal_from_mapping` e passa por `validate_proposal`; a CLI apenas salva uma proposta, nunca a aceita ou executa jobs.

O Codex CLI recebe o objetivo, escopo, plano, resultados validados, evidências, bloqueios e limites. É processo sob a identidade local e herda variáveis/caminhos usados pelo runtime (incluindo ambiente do usuário/Codex). O workspace do provider é uma raiz Git temporária vazia, sem remotes; o adapter aplica `projects.<path>.trust_level="trusted"` somente como override da invocação para essa raiz e não grava configuração global. O modelo pode dispor de ferramentas no protocolo; prompt não é controle de segurança. O modo read-only é uma opção por invocação do Codex e **não** comprova isolamento do processo, credenciais, rede ou host. O adapter permite versão não homologada somente neste caminho experimental opt-in; a instalação observada foi 0.167.0, enquanto a integração de execução existente fixa 0.166.0. Não há homologação de segurança nem de compatibilidade para a versão experimental.

Pedidos de planejamento são gravados em `apl_planner_requests` e `apl_planner_events` no State Store existente. Um pedido com status `started` após interrupção não é repetido: exige novo `--request-id` após revisão humana. Propostas aceitas e rejeitadas continuam versionadas e imutáveis. Exemplo de execução controlada do planejador (pode consumir acesso/uso do Codex configurado localmente):

```powershell
agentflow apl replan --path <workspace> --cycle-id <id> --planner codex `
  --enable-experimental-codex --request-id <id-unico> `
  --planner-timeout 240 --planner-max-input-bytes 65536 --planner-max-output-bytes 65536
agentflow apl proposals --path <workspace> --cycle-id <id>
agentflow apl history --path <workspace> --cycle-id <id>
```

Este MVP não integra um dispatcher APL de jobs. O `ExecutionStore`/CLI existente continua sendo a única rota de execução; a documentação de Fase 7 mantém encadeamento automático bloqueado enquanto autorização Broker/TEG e contratos relacionados não estiverem homologados. Não há caminho de execução iniciado pelo planejador ou pelo APL. Veja [MVP-13 local](apl-mvp13-windows-local-functional-homologation.md).

## MVP-08 — Restricted Inference Harness

`agentflow.apl` exporta contratos tipados `RestrictedWorkerSpec`, `IsolationPolicy`, `WorkerLaunchResult`, `WorkerTerminationResult`, `IsolationEvidence` e `CapabilityProbe`, além de `RestrictedInferenceHarness` e `decode_worker_proposal`. O contrato da requisição não inclui executável, argumentos, diretório de trabalho ou ambiente; JSON de entrada/saída é limitado, rejeita chaves duplicadas e passa pelo decodificador existente de `PlanProposal`. A proposta ainda requer `validate_proposal` e o aceite supervisionado existente.

O harness é deliberadamente **fail-closed**: não cria processos e informa `BLOCKED` até existir implementação específica de sistema operacional que aplique e verifique todos os controles obrigatórios. Evidências declaradas/configurações solicitadas não liberam lançamento; o harness não aceita probes ou launch callbacks fornecidos pelo chamador. O módulo não implementa `Executor` e não é caminho de execução de jobs.

No host Windows inspecionado, o runtime existente tem Job Object para encerrar a árvore de um subprocesso, mas isso não restringe identidade, filesystem, credenciais ou rede. Nenhum backend AppContainer/ACL/política de rede foi implementado ou homologado. O WSL não está instalado; nenhum controle Linux foi verificado nem pode ser extrapolado do resultado Windows. Consequentemente inferência real permanece indisponível em ambos os perfis. Os testes exercitam contratos e despacho fail-closed, não alegam negação OS de operações adversariais. Ver [relatório MVP-08](apl-mvp08-restricted-inference-harness.md).

## Integração e limitações

O pacote já contém planning TOML, contratos AgentAdapter/ExecutionRequest, stores de execução/evidência/budget e políticas/gates de segurança. Developer Mode lê resultados do State Store existente, mas não cria nem autoriza tentativas. Execução exige o comando AgentFlow existente, suas próprias políticas, escopo, sandbox e aprovação. Nenhum adapter/provider concreto é importado ou iniciado pelo APL. Fase 7, Broker/TEG operacional e encadeamento autônomo permanecem bloqueados.

O store APL cria tabelas namespaced no arquivo indicado e tem testes em SQLite temporário; composição/transação com as migrações e APIs dos stores operacionais do AgentFlow ainda exige homologação. Não presume exactly-once para efeitos externos. Sem serviço de reconciliação do executor, outcomes desconhecidos permanecem bloqueados para decisão humana. Aprovação pendente não tem API de continuação nesta primeira implementação. Avaliação e validade de evidência dependem dos adaptadores; o APL não promove evidência declarada a verificada.

## Checkpoints de desenvolvimento

- APL-01: contratos tipados versionados, estados e limites.
- APL-02: controlador injetável, escopo imutável, seleção DAG elegível, policy antes do executor e loop/replanejamento automático.
- APL-03: SQLite namespaced, eventos/revisões, recuperação conservadora e testes determinísticos simulados.

Sem execução de providers reais, Docker, Broker, TEG, operações de Git ou alteração de contratos RB-09C pendentes.
