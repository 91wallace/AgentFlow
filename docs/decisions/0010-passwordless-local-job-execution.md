# ADR 0010 — Execução local de jobs sem autenticação por execução

**Status: DECIDED BY OWNER — implementation partial**  
**Date:** 2026-11-13  
**Scope:** perfil `LOCAL_SUPERVISED`; não altera `INDEPENDENT_AUTHORITY`, H-01–H-10 ou ADR 0008 da Fase 7.

## Context and decision

A ferramenta AgentFlow automatiza desenvolvimento de software no computador do proprietário. A senha cadastrada no MVP-17–20, a autenticação local adicional e a confirmação humana por tarefa/digest não são requisitos universais para iniciar jobs locais. Uma solicitação explícita do operador pode iniciar um trabalho e permitir que o plano previsto prossiga sem confirmações intermediárias.

O caminho `LOCAL_SUPERVISED` deve usar um **contrato persistido de execução**, vinculado ao identificador do trabalho, workspace e identidade do diretório, plano e revisão, executor/versão, operações e tarefas, limites, validade, cancelamento e recuperação. A criação do contrato pelo comando de início explícito é a autorização operacional daquele trabalho; não é autenticação forte nem prova criptográfica da identidade humana. O contrato não é uma autorização ilimitada de sessão.

Jobs iniciados por agente ou serviço somente podem usar um contrato previamente configurado para esses iniciadores e trabalhos. O agente não pode criar permissões, autoatribuir escopo, estender validade, trocar executor ou alterar tarefas. Se essa configuração prévia não existir, o início por agente é recusado.

Uma única autorização cobre as tarefas dentro do plano exato. Alteração material de plano, workspace, executor, operações ou limites requer um novo contrato compatível. Não há aprovação por tarefa nem confirmação por digest no fluxo comum.

## Invariants

- O APL/IFR pode solicitar início/retomada, mas não ampliar ou emitir permissões além de um contrato criado pelo caminho operacional aprovado.
- Antes de cada tarefa e no último ponto controlado antes do spawn, validar binding, validade, cancelamento/revogação e quotas.
- Estado, reservas, consumo, journal e checkpoints permanecem em SQLite existente; reserva e despacho externo não são uma transação atômica.
- `unknown` bloqueia repetição até reconciliação segura; cancelamento é cooperativo, pode não ser instantâneo e não reverte efeitos já produzidos.
- Escopo fora do contrato, limites excedidos, falha de validação e incompatibilidade do plano falham fechados.
- Credenciais e consentimento do MVP-17–20 podem permanecer para funcionalidades independentes que os usem; não são pré-requisito para jobs comuns.
- O perfil local confia na conta, no sistema operacional e no host. Não oferece proteção contra administrador local comprometido ou processo malicioso com os mesmos privilégios.
- `INDEPENDENT_AUTHORITY`, ADR 0008 R2, H-01–H-10 e seus requisitos de autenticação/independência permanecem separados e inalterados.

## Consequences and migration

`authorize-plan`/`local-auth enroll` deixam de ser o caminho de iniciação de trabalhos comuns; o caminho de consentimento autenticado pode permanecer disponível para workflows de consentimento já existentes, sem controlar o dispatcher local comum. O contrato operacional deve substituir o grant autenticado para dispatch local, sem converter nem alterar grants históricos. Run records e eventos antigos permanecem consultáveis.

A implementação precisa adicionar emissão explícita do contrato persistido, resolução antes de cada tarefa, revogação/cancelamento, CLI de início/status/retomada/cancelamento e política pré-configurada para iniciadores automatizados. Até esses componentes e regressões serem implementados, o dispatcher permanece fechado; esta decisão não autoriza executar jobs reais durante a refatoração.

`INDEPENDENT_AUTHORITY` continua exigindo consentimento/autenticação e domínios administrativos independentes conforme ADR 0008 e H-01–H-10. O modo local não satisfaz nem reduz esses requisitos.

## Acceptance tests

A implementação deve demonstrar início de trabalho sem senha; múltiplas tarefas sem aprovação intermediária; persistência/retomada/cancelamento; limites e revogação; recusa de alteração de plano/escopo; início por agente apenas via trabalho previamente configurado; recusa fora do escopo; IFR sem ampliar contrato; recuperação conservadora de `unknown`; e ausência de regressões nos fluxos históricos de consentimento.

## MVP-20 implementation checkpoint (2026-11-13)

Implemented in `agentflow.apl.authorized_execution`:

- `LocalExecutionContract`, an immutable typed contract stored separately from Broker-signed grants in the existing SQLite database.
- Append-only hash-chained contract events; payload and journal integrity are checked on read.
- `LocalExecutionContractResolver`, which checks current plan/revision/DAG/scope, canonical workspace path and directory identity, executor/version, validity, state, and revocation.
- `LocalAuthorizedExecutionService`, a passwordless explicit operator API over the existing APL execution controller. It persists the contract and runs the entire plan under existing transactional reservation, IFR, checkpoints, cancellation, and before-spawn checks.
- A local canonical plan digest covers the complete objective, revision, DAG, and scope even when an APL snapshot has a non-cryptographic source reference such as `spec:...`.
- Terminal completion/cancellation is recorded on the local contract. Replaying the same operation returns the existing terminal result and does not dispatch again.

Agent/service initiation is currently rejected (`preconfigured_agent` is not accepted); a durable preconfigured initiation policy is not implemented. No public CLI command is wired to this service yet. Production acceptance-evidence integration for the Codex executor remains required before a user-facing command may dispatch safely. This checkpoint uses only synthetic executors and does not establish real Codex execution or operational homologation. `INDEPENDENT_AUTHORITY` and signed Broker grant resolution remain separate.
