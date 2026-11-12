# MVP-15 — Integração supervisionada ponta a ponta

## Escopo entregue

O handoff supervisionado liga a proposta imutável validada pelo APL ao contrato
`SnapshotProposal` do Broker local. Ele encerra em `awaiting_authorization` e não
oferece ações de aceite, emissão de grant ou dispatch. O executor continua fora
deste caminho.

Componentes reutilizados:

- `SupervisedCycleService.replan` e `validate_proposal` para planejar, validar e
  persistir a revisão APL.
- `LoopbackTLSClient` e `BrokerEnvelope` para transporte mTLS local, com pin,
  ALPN, correlação e limite de mensagem já definidos pelo Broker.
- `LocalBrokerService` / `BrokerStore` para identidade mTLS, escopo de projeto,
  persistência do snapshot pendente, journal e consulta de ciclo de vida.
- O SQLite operacional já usado pelo APL guarda o vínculo de handoff e seus
  eventos; nenhum banco ou executor paralelo foi criado.

## Fluxo

1. `apl start` cria um ciclo com objetivo e escopo de paths explicitamente
   autorizados.
2. `apl handoff` invoca o Planner Adapter escolhido, valida e persiste a
   proposta APL, e monta um snapshot com objetivo, escopo, revisão, digest e
   proveniência.
3. Antes de qualquer chamada de rede, o SQLite persiste um vínculo imutável
   entre ciclo, proposta APL, `snapshot_id`, `operation_id`, bytes canônicos e
  digest, e transiciona o ciclo persistentemente para `WAITING_APPROVAL`. IDs
  derivados de ciclo + proposta + digest tornam o reenvio estável.
4. O snapshot é submetido pelo cliente mTLS existente. O Broker confere a
   identidade cadastrada, `execution:propose` e `project:<project_id>` e grava
   o snapshot em estado `pending` antes de responder.
5. A resposta é conferida contra revisão, digest e principal esperados; uma
   segunda consulta obtém o estado do journal. A saída informa
   `awaiting_authorization`, `authority_status=unavailable` e
   `dispatch_allowed=false`.
6. Se houver interrupção com resultado de rede desconhecido, `handoff-resume`
   consulta primeiro o snapshot. Se o Broker ainda não o conhece, reenvia os
   mesmos bytes e o mesmo operation ID, aproveitando a idempotência do Broker.
   Propostas terminais não são ressuscitadas.

O principal informado na CLI é apenas uma declaração de identidade: o Broker
compara-o com a identidade derivada do certificado mTLS. O cliente não escolhe
uma autoridade nem uma decisão de consentimento. Os paths de escopo APL e o ID
de projeto do Broker são domínios distintos e ambos são verificados pelos
respectivos componentes.

## Comandos

Os comandos usam o banco do projeto em `.agentflow/state.sqlite3`. Broker e
identidade mTLS devem estar provisionados e autorizados independentemente; esta
documentação não cria certificados nem registra clientes.

```powershell
agentflow apl start --profile developer --objective "Implementar soma" `
  --scope src/math.py --scope tests/test_math.py

agentflow apl handoff --cycle-id <cycle-id> --planner deterministic `
  --project-id <broker-project-id> --principal-id <mtls-principal> `
  --broker-port <loopback-port> --server-name localhost `
  --ca <ca.pem> --client-cert <client.pem> --client-key <client-key.pem> `
  --server-fingerprint sha256:<fingerprint>

agentflow apl handoff-status --cycle-id <cycle-id> --proposal-id <apl-proposal-id> `
  --project-id <broker-project-id> --principal-id <mtls-principal> `
  --broker-port <loopback-port> --ca <ca.pem> --client-cert <client.pem> `
  --client-key <client-key.pem> --server-fingerprint sha256:<fingerprint>

agentflow apl handoff-resume --cycle-id <cycle-id> --proposal-id <apl-proposal-id> `
  --project-id <broker-project-id> --principal-id <mtls-principal> `
  --broker-port <loopback-port> --ca <ca.pem> --client-cert <client.pem> `
  --client-key <client-key.pem> --server-fingerprint sha256:<fingerprint>
```

Para usar o planejador Codex, selecione `--planner codex` e forneça
`--enable-experimental-codex`. Isso apenas permite a proposta do planner; não
atribui isolamento ao processo Codex nem aceita a proposta automaticamente.
Repetições da mesma solicitação de planejamento retornam a proposta APL já
persistida. `handoff-resume` é o comando de retomada preferido após interrupção.

## Persistência e segurança

- A tabela `apl_broker_handoffs` mantém binding único por ciclo/proposta e IDs
  únicos globalmente. Triggers impedem update/delete do binding.
- `apl_broker_handoff_events` é append-only e registra preparação, confirmação,
  incerteza de transporte e estados observados no journal do Broker.
- A consulta compara snapshot ID, revision ID, digest, principal, autoridade e
  dispatch com os valores esperados. Respostas incompatíveis falham fechadas.
- Broker e APL mantêm suas próprias transações SQLite. Não há transação
  distribuída: falhas entre stores são reconciliadas por operação imutável e
  consulta, sem alegar exactly-once para efeitos externos.
- A rede do cliente é limitada a loopback pelo transporte existente. A
  identidade/certificado, projeto e política precisam ser previamente
  cadastrados no Broker. Resposta de escopo negado bloqueia o handoff.
- Estados `expired`, `cancelled`, `obsolete`, `accepted` ou `rejected` observados
  no Broker são retornados como terminais; o APL não repete submissão nesses
  estados. O ciclo correspondente transita para `BLOCKED` com motivo terminal.
  `accepted` não representa autorização de execução e nunca habilita dispatch
  por este serviço.

## Verificação reproduzível

O teste `tests/test_approval_transport.py::test_apl_broker_supervised_handoff_real_mtls_sqlite_and_resume`
usa certificados sintéticos, um listener `LocalBrokerService` real em loopback,
SQLite real e um Planner Adapter determinístico. Ele simula a perda da primeira
tentativa de transporte antes do envio, retoma, submete e consulta via mTLS,
reinicia o Broker, repete a consulta e verifica um único vínculo/snapshot,
nenhuma linha em `execution_authorizations` ou `execution_receipts`, autoridade
indisponível e dispatch falso. Nenhum provider ou job é iniciado.

## Limitações

- Esta fatia valida até a fronteira de submissão de uma proposta pendente; não
  implementa nem habilita consentimento independente, grant, TEG ou dispatch.
- O Broker exige identidade/certificado e escopos já provisionados. O MVP não
  cria identidade, confiança nem autoridade por conta própria.
- O planner Codex permanece um processo local experimental executado com a
  identidade e capacidades efetivas do usuário; opt-in não constitui sandbox.
- O transporte Broker continua sendo local/loopback. Não é uma integração de
  rede remota nem uma homologação de segurança de produção.

Próxima etapa técnica: homologar o consumo de uma decisão independente somente
quando o contrato RB-09C/TEG e sua autoridade estiverem aprovados e disponíveis.
Até lá, o estado final deste fluxo é `awaiting_authorization`.
