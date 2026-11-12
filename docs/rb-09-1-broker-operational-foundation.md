# RB-09.1 — Fundação operacional local do Broker

## Resultado

**PARTIAL — fundação de serviço implementada; autoridade e dispatch seguem bloqueados.** Esta fatia inicial coloca um serviço Broker executável em `agentflow.approval.service`, usando o envelope e o armazenamento existentes. Ela permite somente consultar status. Não cria aprovação, grant, autorização consumível ou efeito.

## Arquivos desta fatia

- `src/agentflow/approval/service.py` — serviço single-instance de desenvolvimento, listener TCP TLS restrito a `127.0.0.1`, identidade por instância, leitura de uma mensagem por conexão, validação e encerramento.
- `src/agentflow/approval/persistence.py` — eventos de serviço validados e append-only no journal/hash-chain existente; consulta da última transição de ciclo de vida; verificação de integridade reconhece o novo conjunto fechado de eventos.
- `src/agentflow/approval/__init__.py` — exports públicos `LocalBrokerService` e `BrokerServiceError`.
- `tests/test_approval_transport.py` — integração TLS local e teste de recuperação após término abrupto de processo.

Não houve migração de schema: os eventos de serviço usam a tabela `broker_events` existente e suas verificações de cadeia. Nenhuma alteração foi feita no APL, `ApprovalBroker`, `UnavailableGatewayAuthority`, TEG, Production Mode ou RB-09C.

## Protocolo e operação demonstrados

- O listener fixa o bind em IPv4 loopback; host externo não é configurável. Cada conexão exige TLS 1.2+ mTLS, ALPN `agentflow-broker/1` e um certificado ativo registrado no `BrokerStore`.
- As mensagens usam `BrokerEnvelope` e protocolo v1 existentes, framing limitado a 64 KiB, canonicalização/digest e correlação de `message_id`, `operation_id` e nonce.
- A única ação exposta é `service_status`. A resposta inclui o `service_instance_id`, `authority_status=unavailable` e `dispatch_allowed=false`.
- Operação `approve`/dispatch não exposta e schema inválido retornam erro versionado e geram evento de rejeição. Versão incompatível é registrada e a conexão é encerrada sem interpretar a mensagem como operação válida.
- Eventos fechados e sem payload de usuário: `service.started`, `service.recovered`, `service.status_queried`, `service.request_rejected`, `service.failed` e `service.stopped`. Rejeições distinguem frame inválido, schema inválido, versão incompatível, operação não exposta, identidade TLS recusada e timeout de requisição. A integridade do journal é revalidada ao abrir o store.
- O estado persistido `started` sem evento `stopped` é tratado como interrupção; o próximo processo registra `service.recovered` com a identidade anterior e inicia com uma nova identidade. O encerramento normal persiste `stopped`.

## Evidências operacionais

Em testes, o serviço foi iniciado em processo local, aceitou conexão TLS/mTLS com certificado sintético temporário, respondeu a `service_status` e devolveu sua identidade; a resposta confirmou autoridade indisponível e dispatch desabilitado. As mensagens de aprovação e schema inválido foram recusadas e seus códigos apareceram no journal. Uma mensagem v99 foi recusada por incompatibilidade sem resposta de status.

O teste de recuperação iniciou o Broker em processo filho, aguardou a identidade/porta, encerrou o processo sem `close`, iniciou uma nova instância contra o mesmo SQLite, consultou status e verificou o evento de recovery referenciando a instância interrompida. O store passou por `verify_integrity()` depois do reinício. Certificados, chaves e bancos eram sintéticos e temporários; nenhuma credencial operacional foi usada.

Falha após shutdown também foi exercitada: o cliente recebeu `BrokerTransportError`. A interface existente classifica timeout de transporte como resultado desconhecido; a chamada de status não tem efeito externo nem autorização a repetir.

## Testes

Bateria focada executada:

```text
.venv\Scripts\python.exe -m pytest tests/test_approval_identity.py tests/test_approval_transport.py tests/test_approval_protocol.py tests/test_approval_persistence.py tests/test_approval_presentation.py tests/test_approval_webauthn.py tests/test_phase7_budgets.py tests/test_phase7_escalation.py tests/test_phase7_sdd.py tests/test_phase7_audit_cli.py tests/test_execution_reservation.py tests/test_execution_cli.py tests/test_local_runtime.py tests/test_apl.py -q
162 passed in 38.56s
```

Cobertura do ciclo de vida também injeta falha na criação da thread e na persistência do evento de encerramento, verificando liberação de recursos/store e recuperação subsequente; a bateria de transporte passou: **15 passed in 8.44s**.

Suíte completa após as alterações:

```text
.venv\Scripts\python.exe -m pytest -q
456 passed in 277.03s (0:04:37)
```

Falhas: 0. Skips: 0. Esses resultados homologam comportamento local de código/teste no Windows/Python selecionado; não são revisão de segurança independente nem homologação Linux.

## Limites de segurança

O certificado mTLS identifica tecnicamente um cliente cadastrado no store de desenvolvimento; não prova consentimento humano. As chaves de teste não representam PKI de produção. O processo e o banco continuam no host local e no mesmo domínio administrativo, portanto **não** satisfazem a fronteira independente Broker–TEG decidida em RB-09C.1-R2. Loopback reduz exposição de rede, mas não prova isolamento contra processos locais do mesmo usuário.

O serviço não implementa endpoint de snapshots, emissão/consumo de grants, decisão humana, enrollment, revogação operacional, handler TEG ou execução. A authority real permanece indisponível. O APL não está conectado ao serviço e nenhum job/provider foi executado.

## Próxima fatia

A próxima unidade técnica dentro da implementação Broker é definir e implementar somente endpoints tipados de proposta/consulta de snapshots sob identidade de cliente e limites de protocolo, mantendo estado `pending` e sem aceite. Antes de qualquer decisão/grant, ainda são obrigatórios serviço/identidade/store/operadores em domínio independente, UI confiável e ceremony de consentimento homologadas; essas propriedades não podem ser obtidas por este serviço local.
