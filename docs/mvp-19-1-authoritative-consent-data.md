# MVP-19.1 — Dados autoritativos para revisão de consentimento

## Resultado

**AUTHORITATIVE_CONSENT_DATA_IMPLEMENTED**, com limite explícito: a apresentação e a resolução agora consomem os registros APL/Broker persistidos e revalidam o binding antes de cada revisão; o autenticador humano operacional continua indisponível. Portanto, nenhuma aprovação positiva, grant operacional ou dispatch foi habilitado. O perfil `LOCAL_SUPERVISED` não está homologado.

## Componentes

- `agentflow.security.authoritative_consent.AuthoritativeConsentResolver`: resolve uma proposta persistida e valida os registros APL, handoff, snapshot Broker e journal.
- `AuthoritativeConsentProposal`: resultado tipado com ciclo, proposta, snapshot, sequência do journal, estado APL/Broker, objetivo, scope, digest, revisão, workspace, operações, tarefas, limites, validade e pedido de consentimento.
- `TerminalConsentReview`: pode receber `resolver.resolve_for_request` como `authoritative_context_resolver`. Reconsulta os stores antes de exibir, exige igualdade integral do pedido e se recusa a mostrar ação aprovável quando o estado não é pending.
- `BrokerHandoffService.cancel` e `agentflow apl handoff-cancel`: cancelamento idempotente por meio do cliente Broker já autenticado mTLS. Não usa mutação local para fingir identidade do owner e bloqueia o ciclo APL após confirmação do Broker.

Os tipos são reexportados por `agentflow.security`. A UI mostra objetivo, scope persistido, resumo das tarefas, digest do pedido APL, revisão proposta, workspace derivado, operações, limites configurados, prazo e conta local apenas como rótulo não autenticado.

## Cadeia de resolução e validação

1. Reabre o ciclo e a proposta por `SQLiteCycleStore`; o store valida o digest armazenado da proposta.
2. Confirma que a revisão de plano de origem é a revisão atual.
3. Lê, somente para leitura, o binding imutável `apl_broker_handoffs`.
4. Decodifica o `SnapshotProposal`, valida digest/conteúdo canônico e confere snapshot ID, operação, projeto, principal proponente e provenance contra APL persistido.
5. Lê o estado do Broker por `get_proposal_lifecycle_for_identity`, que confere owner, projeto, projeção e evento do journal.
6. Compara o snapshot Broker completo ao binding imutável do handoff.
7. Obtém a hora de recebimento do evento `snapshot.proposed`; a validade considera o TTL Broker de sete dias e um prazo local configurado, estável entre reaberturas e reinícios.
8. Deriva operações dos jobs persistidos, revisão da sequência de plano seguinte, workspace ID de projeto+scope, principal de operador da configuração local e limites da política local.
9. A UI re-resolve por proposal ID e compara o `LocalConsentRequest` completo. Mudança concorrente, corrupção, ausência de registros, estados terminais, expiração ou divergência bloqueiam a ação.

O planner não fornece valores para autorizar durante essa resolução. Instruções/justificativas geradas pelo planner aparecem como conteúdo da proposta persistida e são apresentadas como conteúdo proposto, nunca como política. Consultar uma proposta antiga em estado terminal permite diagnóstico, mas não permite ação `approve`.

## Campos que não vêm da proposta do agente

`ConsentLimits` e `operator_identity` são injetados pelo chamador confiável da resolver e devem vir da política/allowlist local. O modelo APL atual não persiste limites de ciclo detalhados nem um mapeamento autenticado de operador; o resolver não inventa esses dados nem aceita-os do planner. O `workspace_id` é um identificador estável derivado do ID de projeto Broker e dos paths autorizados, não um caminho de filesystem; o scope persistido também é exibido separadamente.

## Estados, consulta e cancelamento

Pending só é revisável quando o APL está `WAITING_APPROVAL`, a proposal segue `proposed`, a revisão de origem ainda é corrente e o snapshot Broker permanece `pending`. `expired`, `cancelled`, `obsolete`, `accepted` e `rejected` não são aprováveis. O sweeper TTL existente do Broker é invocado antes da leitura para que uma expiração vencida se torne estado journaled.

O cancelamento de proposal usa `snapshot_cancel` via mTLS com a identidade owner já registrada. `handoff-cancel` exige as mesmas credenciais/certificado pinado que o handoff. O comando não emite consentimento, grant ou dispatch. Cancelar a revisão de consentimento no terminal permanece a trilha `LocalAuthorizationAuthority.record_cancelled` do MVP-19.

## Segurança e limites restantes

- O autenticador operacional de operador ainda não existe. O serviço WebAuthn atual verifica credentials/assertions de outro protocolo, sem enrollment e sem ceremony ligada ao challenge local. A ação do terminal continua não autenticada.
- Não há CLI de aprovação positiva: a aplicação não possui configuração homologada para construir `operator_identity`/limits e abrir os stores APL/Broker como autoridade de leitura. A resolução tipada e a interface suportam integração por injeção; uma CLI futura deve montar ambas a partir de configuração validada, não de flags de identidade.
- `LOCAL_SUPERVISED` não demonstra separação administrativa. `INDEPENDENT_AUTHORITY` e H-01–H-10 não foram alterados.
- Nenhuma concessão, grant, autorização de execução ou job foi criada.

## Testes

`tests/test_authoritative_consent.py` usa os dois SQLite reais em diretório temporário, proposta APL persistida, handoff imutável e snapshot/journal Broker. Verifica resolução e apresentação, corrupção de digest, divergência de revisão/scope, estados cancelado e expirado, ausência de handoff, falha SQLite, concorrência, reinício, idempotência e ausência de grants/dispatch. `tests/test_approval_transport.py::test_apl_handoff_cancel_uses_real_loopback_mtls_and_persists_terminal_state` demonstra cancelamento do handoff por transporte mTLS real em loopback.

Comandos executados nesta etapa:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_authoritative_consent.py tests/test_local_consent_ui.py tests/test_local_authorization.py -q
.\.venv\Scripts\python.exe -m pytest
git diff --check
```

Também foram verificados `agentflow apl --help` e `agentflow apl handoff-cancel --help`; ambos listam os argumentos de cancelamento mTLS.

Resultado nesta etapa: testes focados **39 passed**; suíte completa **544 passed**; `compileall` e `git diff --check` aprovados.
