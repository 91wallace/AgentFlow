# MVP-20.3A — Ponte de grant operacional autenticado

**Resultado: OPERATIONAL_GRANT_BRIDGE_IMPLEMENTED (somente validação sintética).**

## Implementação

A emissão de autorização do plano completo agora reutiliza `LOCAL_SUPERVISED`, o autenticador local por senha, o desafio de uso único, o assinador Ed25519 derivado da credencial já cadastrada, o `BrokerStore` e seu journal. A nova API pública é `agentflow.security.review_accepted_execution_plan`. Ela só aceita ciclo APL em `READY`, plano/revisão persistidos e workspace cuja localização e identidade de filesystem foram registradas e revalidadas. A UI apresenta digest, revisão, objetivo, workspace, grafo de dependências, tarefas, executor/versão, escopo, limites, estimativas e validade; o operador deve escolher `approve` e autenticar a confirmação vinculada ao digest.

Esse fluxo emite um grant schema v2 distinto dos grants de revisão anteriores. O digest do pedido inclui o binding de execução completo. Não há conversão de grants antigos: `review_persisted_handoff` rejeita explicitamente o uso de um handoff de revisão como origem de grant operacional. A autoridade verifica novamente o binding persistido antes de assinar; APL/IFR não recebem a capacidade de emitir, renovar ou ampliar grants.

`BrokerExecutionGrantResolver` é o adaptador público do grant persistido para o controlador APL. A cada resolução ele verifica assinatura, decisão autenticada ligada ao grant, estado/revogação no Broker, expiração, ciclo `READY`, ID determinístico do plano, revisão imutável, digest, grafo, escopo, workspace canônico e identidade do diretório, executor/versão, operações e quotas. Divergências falham fechadas. O controlador existente continua revalidando antes de reservar e no gate `before_spawn`.

## Limites

O modo é `LOCAL_SUPERVISED`: senha e assinatura local não criam independência administrativa e não protegem contra administrador ou processo malicioso sob a mesma conta. Quotas de tarefa, tentativas, duração e concorrência são limites locais; a implementação atual permite concorrência 1. `estimated_cost_accepted` não é teto financeiro do provider. `hard_cap_required` continua sem enforcement disponível e bloqueia execução pelo controlador.

O teste da API usa uma credencial e aprovação sintéticas, stores SQLite reais e um grafo com duas tarefas dependentes. Um teste separado valida a execução contínua dessas tarefas com executor sintético, usando um grant v2 assinado e persistido. Não houve interação do proprietário, Codex, provider, dispatch real ou execução de jobs reais nesta etapa. A implementação da ponte não homologa o perfil para uso operacional nem libera dispatch global.

## Validação

- Testes focados atuais: `tests/test_apl_plan_grant_bridge.py`, `tests/test_authoritative_consent.py`, `tests/test_local_authorization.py`, `tests/test_approval_persistence.py` e `tests/test_local_passphrase_auth.py` — **77 passaram, 1 skip**.
- Suíte completa `pytest -q` — **644 passaram, 1 skip em 310,46 s**; o skip é POSIX e não aplicável ao Windows.
- Casos comprovados: emissão v2 por API autenticada sintética; persistência/journal; assinatura; autorização única para duas tarefas dependentes sem segundo consentimento; negação para grant revogado/expirado, executor ou versão divergentes, identidade de workspace divergente e plano alterado.
- `git diff --check` passou; Git apenas avisou sobre a conversão LF/CRLF configurada para alguns arquivos modificados.

## Próximo gate

Homologar a interação humana real do consentimento local em um workspace descartável, validar a revogação e recuperação em processo separado e completar testes de adulteração/concorrência. Em seguida, realizar homologação comportamental do Codex CLI 0.167.2; a allowlist permanece em 0.166.0. Somente depois desses gates o proprietário poderá aprovar especificamente um piloto real. Nenhuma tarefa deve ser iniciada por este documento ou por testes sintéticos.
