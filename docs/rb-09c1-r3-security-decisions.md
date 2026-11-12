# RB-09C.1-R3 — propostas finais de decisões de segurança

**Estado:** documento de decisão para revisão humana. Nenhuma proposta deste documento, exceto a Alternativa B administrativa já aprovada na emenda R2 do ADR 0008, é decisão aprovada.

**Escopo:** contratos criptográficos Broker/TEG, revogação/tempo e ancoragem independente. Isto não inicia RB-09C.2, não altera o ADR 0008, não muda contratos Python, não implementa serviços e não habilita Broker, TEG, grants, providers ou efeitos.

## 1. Contexto e estado observado

- ADR 0008 R2 aprova Broker e TEG em domínios administrativos independentes como arquitetura-alvo. A implementação e o isolamento efetivo não foram comprovados. O host agente continua fora dos domínios confiáveis; a interface do aprovador deve ser independente dele; a âncora deve ser administrativamente independente dos journals que verifica.
- `approval.protocol` define envelopes de dados JSON estritos, canonicalização e SHA-256; `ExecutionAuthorization` e `ExecutionReceipt` têm digest próprio. `teg.protocol` define `ExecutionBinding` e `GatewayExecutionReceipt`, também com digests próprios. Digest próprio detecta corrupção acidental, mas não autentica emissor: quem altera o objeto pode recalcular o digest.
- O comentário de `teg.contracts` afirma que digest não é assinatura nem autentica resposta Broker. O `ApprovalBroker` padrão permanece indisponível; a autoridade TEG padrão falha fechada. Não há chave operacional, emissão operacional, listener, provider nem execução protegida.
- O plano RB-09C.1-R1 recomenda Ed25519, tolerância temporal de 2 s e confirmação de âncora pré-efeito, entre outras propostas. A emenda R2 aprovou apenas a Alternativa B administrativa. Esses parâmetros permanecem pendentes até aprovação própria.

Este documento apresenta propostas normativas futuras. “Normativo proposto” abaixo significa que, se aprovado, o contrato deve obedecer ao requisito; não significa comportamento existente ou aprovado hoje.

## 2. Princípios de segurança e separação de provas

1. **Autenticação técnica:** mTLS autentica o peer/canal conforme certificado e protege a conexão. Não comprova consentimento, autorização por operação, exclusividade administrativa ou execução. A identidade mTLS deve corresponder ao `issuer_id`/`audience` esperado, mas policy continua sendo uma decisão separada.
2. **Consentimento humano:** o Broker registra que uma cerimônia WebAuthn foi validada e que a decisão foi associada ao digest exato apresentado por uma UI no domínio confiável. A assinatura WebAuthn prova uma resposta do autenticador para um challenge sob as verificações aplicadas; não prova que a pessoa compreendeu o conteúdo, que a UI era clara ou que não houve coerção.
3. **Autorização:** uma asserção assinada do Broker comprova que a chave Broker emitiu uma autorização com campos determinados. O receptor ainda deve verificar policy, audiência, snapshot, validade, revogação e uso único. Ela não é bearer token para o agente.
4. **Execução:** assinatura de receipt comprova que a chave do TEG atestou determinados dados. Não prova sozinha que provider externo comitou o efeito. Prova de efeito requer resposta verificável/query autoritativa do provider, identificador de efeito ou outra evidência externa identificada pelo handler.
5. Falha de prova, serviço indisponível, versão desconhecida, divergência, relógio inválido ou estado ambíguo bloqueia novo efeito. `unknown` não é sucesso nem falha conhecida e nunca aciona retry automático.

## 3. Perfil criptográfico e serialização propostos

### 3.1 Perfil `AF-SIG-1`

| Elemento | Proposta normativa verificável | Alternativas / justificativa | Risco residual e dependências | Critério de aceitação | Aprovação humana necessária |
| --- | --- | --- | --- | --- | --- |
| Assinatura de serviço | Ed25519, assinatura detached de 64 bytes; nenhuma negociação de algoritmo por mensagem. `signature_algorithm` deve ser exatamente `Ed25519`. | ES256 oferece compatibilidade ampla com alguns KMS/FIPS; RSA-PSS pode ser exigência legada e tem chaves/assinaturas maiores. Ed25519 é compacto e reduz ambiguidades, mas não é suportado por todo HSM/FIPS. | Depende de KMS/HSM, biblioteca criptográfica e perfil regulatório escolhidos. Compromisso da chave emissora continua crítico. | Vetores golden de assinatura válida e recusas para bit flip, chave errada, algoritmo alternativo, assinatura curta/longa e key ID desconhecido; execução nos runtimes homologados. | Algoritmo, exigência FIPS, fornecedor/uso de KMS/HSM e responsável por chaves. |
| Digest | SHA-256 sobre bytes canônicos do payload; formato textual `sha256:` + 64 hex minúsculos. | SHA-384 não oferece benefício proporcional para este perfil; hashes legados não entram em novos contratos. | Colisão criptográfica é risco residual teórico; digest não autentica sem assinatura. | Vetores fixos cross-language e rejeição de uppercase, comprimento inválido ou digest divergente. | Aceitação do algoritmo e política de migração pós-compromisso. |
| Envelope | `SignedEnvelopeV1` fechado: `envelope_version`, `message_type`, `protocol_version`, `issuer_id`, `key_id`, `audience`, `issued_at`, `not_before`, `expires_at`, `message_id`, `operation_id`, `payload_digest`, `signature_algorithm`, `payload`, `signature`. Proibidos campos extras e valores nulos fora dos schemas. | Assinatura de objeto sem envelope dificulta domain separation, audiência e rotação. JWS/COSE padronizados são alternativas; AF-SIG-1 propõe formato próprio mínimo, sujeito a revisão criptográfica antes de código. | Formato próprio aumenta risco de implementação. Idealmente substituir por envelope padrão se revisão independente apontar risco. | JSON Schema fechado, vectors byte-a-byte e parser que recusa campos desconhecidos, duplicados, payload incompleto, tipos ambíguos e versão futura. | Envelope próprio versus COSE/JWS; formato de codificação e limite máximo de mensagem. |
| Canonicalização | Payload e header são JSON UTF-8: chaves únicas em ordem lexicográfica por pontos de código Unicode, strings emitidas sem escape ASCII desnecessário, escapes JSON mínimos, separadores `,` e `:`, sem whitespace, sem floats/NaN/Infinity, inteiros decimais sem zeros iniciais, boolean/null JSON. Nenhuma normalização Unicode implícita; code points exatos são significativos. | Alinha com o canonicalizer estrito de Approval e remove números de ponto flutuante ambíguos. RFC 8785/JCS é alternativa interoperável, mas diverge de regras legadas e precisa vetores/migração próprios. | Implementações em linguagens diferentes podem tratar Unicode/ordenação de forma distinta. TEG canonicalizer atual não é automaticamente equivalente. | Test vectors para Unicode composto/decomposto, escaping, ordenação, inteiros, duplicatas, campos extras, floats e payloads de limite; todos os clientes produzem bytes idênticos. | Perfil próprio versus JCS; normalização Unicode (proposta: nenhuma) e compatibilidade com versões existentes. |
| Bytes assinados | `M = ASCII("AgentFlow-SignedEnvelope\0AF-SIG-1\0") || uint64be(len(H)) || H || uint64be(len(P)) || P`, onde `H` é o JSON canônico do header sem `signature` e sem campo `payload`, e `P` é o JSON canônico integral do payload. Assinar `M`; digest cobre `P`. `signature` é base64url sem padding. | Comprimentos explícitos impedem concatenações ambíguas; prefixo separa uso/versão. | Erro de enquadramento invalida interoperabilidade. | Vectors publicados com `H`, `P`, `M`, digest, chave pública e assinatura esperada; validação independente por segunda implementação. | Prefixo, lengths e formato base64url fixos. |
| Identidade/audiência | `issuer_id` é URI-like estável do domínio + serviço + ambiente; `audience` identifica uma instância/role exata (por exemplo, `teg:<id>`), nunca wildcard. Receiver exige igualdade literal com registro confiável e binding mTLS esperado. | Nome de produto amplo seria mais simples, porém permite cross-service replay. | Registro e ciclo de vida de identidades dependem da arquitetura independente B e de IAM ainda não escolhido. | Mensagem válida para outro TEG, ambiente ou serviço é recusada mesmo com assinatura criptográfica correta. | Formato de identificador e autoridade de provisionamento. |

### 3.2 Campos mínimos por contrato

Todos os campos abaixo integram o payload assinado; campos de envelope nunca substituem os vínculos dentro do payload. IDs/digests usam schemas fechados. Cada contrato inclui `contract_version=1` e `operation_id` quando associado a uma operação.

| Contrato proposto | Emissor → audiência | Campos assinados mínimos | Papel / não prova |
| --- | --- | --- | --- |
| **ExecutionAuthorization** | Broker → um `gateway_id` exato | `authorization_id`, `approval_request_id`, `approval_decision_digest`, pseudonymous `requester_id`/`approver_id`, `operation_id`, `action`, `intent_digest`, `execution_binding_digest`, `target_id`, `provider_id`, `model_id`, `scope_digest`, `budget_digest` e enforcement-status, `policy_digest`, `catalog_digest`, snapshot map `{kind, snapshot_id, revision_id, schema_version, digest}` para specification, plan, checkpoint, payload e demais snapshots exigidos, WebAuthn ceremony/presentation digest (sem assertion), `gateway_id`, `issued_at`, `not_before`, `expires_at`, `jti`, authorization epoch/revocation reference. | Prova que Broker autorizou os bytes e destinatário identificados sob a chave Broker. Não é consentimento em si, não prova compreensão e não executa o efeito. |
| **ExecutionBinding** | TEG → Broker para autorização; TEG o recalcula localmente antes do start | `binding_id`, `operation_id`, `gateway_id` + identity digest, `intent_digest`, `action`, `target_id`, provider/model, payload bytes digest, `snapshot_set_digest` e snapshot map completo, specification/plan revisions, checkpoint commit/tree/authorized-path-list digest, `scope_digest`, `budget_digest`, `policy_digest`, handler ID/version/schema digest, destination/audience, `created_at`, `binding_nonce`, binding digest. | Define exatamente o efeito que pode ser considerado. Assinatura TEG autentica a solicitação do TEG, não dá ao TEG poder de alterar a decisão Broker nem comprova que snapshot foi aceito; Broker resolve contra fonte confiável. |
| **GatewayExecutionReceipt** | TEG → Broker e anchor; cópia minimizada para AgentFlow read-only | `gateway_receipt_id`, `operation_id`, authorization/binding/intent digests, snapshot set e checkpoint digests, reservation/consumption/start-permit IDs/digests, gateway identity/version, handler/version, target/provider/model, provider idempotency mode (`applied_verified`, `estimated`, `unavailable`) e key digest/ID sem segredo, effect reference, result/evidence digest, status (`succeeded`, `failed_before_effect`, `failed_verified`, `unknown`), start/completion times, journal sequence/head, pre/post anchor receipt digests, recovery/reconciliation references. | Registro primário detalhado do TEG. “Succeeded” só se handler/provider produzir evidência segundo policy; caso contrário `unknown`. Assinatura prova emissão TEG, não a verdade externa do provider. |
| **ExecutionReceipt** | TEG → Broker e verificador autorizado; projeção pública minimizada | `receipt_id`, `gateway_receipt_digest`, `operation_id`, authorization/binding/snapshot digests, `target_id`, executor/gateway ID+version, status, started/completed, effect reference digest, result digest, evidence class/source ID, sequence, post-anchor receipt digest. | Projeção estável do GatewayExecutionReceipt para consumidores. Não é receipt de autorização do Broker. Deve referenciar exatamente um GatewayExecutionReceipt; é proibido manter campos divergentes como outra fonte de verdade. |

**Decisão de modelagem proposta:** `GatewayExecutionReceipt` é a fonte primária; `ExecutionReceipt` é sua projeção reduzida, assinada pelo mesmo TEG e ligada pelo digest. Alternativas: (1) manter somente o receipt TEG detalhado; (2) fazer `ExecutionReceipt` ser receipt Broker de reserva/consumo; (3) usar a projeção proposta. Recomendo (3) para compatibilidade de consumidores sem duplicar a fonte da verdade. O receipt de reserva/consumo, se necessário, deve ter um tipo separado (`AuthorizationConsumptionReceipt`) emitido pelo Broker. Esta distinção requer aprovação humana antes de contrato de wire.

### 3.3 Chaves, rotação, revogação e migração

- Cada domínio mantém chaves distintas por finalidade e `issuer_id`; chave Broker não assina receipts TEG e vice-versa. Chave de âncora também é distinta. Chaves privadas ficam em KMS/HSM/keystore protegido do domínio e nunca em código, `.agentflow/`, arquivos de tarefa, ambiente do agente ou logs.
- `key_id` aponta para manifesto assinado de chave: `issuer_id`, algoritmo, usos permitidos, public key, validade, status, epoch e predecessor. Verificador fixa uma trust root provisionada por canal administrativo independente; chave recebida da própria mensagem não é trust root.
- Rotação normal: publicar/ancorar manifesto da chave nova antes do primeiro uso, período de sobreposição limitado, ativar por `not_before`, retirar chave velha após expiração máxima de suas mensagens + skew e manter chave pública/status históricos para auditoria. Compromisso: revogação imediata com effective time/epoch, bloquear novas mensagens dessa key ID e consultar online o estado no instante do efeito; a chave comprometida não pode revogar a si própria unilateralmente.
- Assinaturas antigas continuam verificáveis para auditoria com trust manifest e evidência de status válida na época. Verificação histórica offline pode validar matemática e validade temporal registrada; não atesta revogação posterior, unicidade atual, ausência de fork ou frescor da âncora.
- Migração é aditiva e explicitamente versionada: contratos atuais `protocol_version=1` permanecem digests sem assinatura. Novo perfil `AF-SIG-1` exige versão nova do envelope/protocolo; `v1` não pode ser aceito no endpoint de efeito novo. Compatibilidade de leitura para auditoria pode existir em modo histórico/readonly, rotulado `unauthenticated_legacy`, nunca para autorização/efeito. Sem downgrade, fallback ou interpretação automática. Rollout exige dual-read em modo não operacional, vectors, cutover aprovado e plano de rollback que mantém efeitos desabilitados.

## 4. Verificação, consentimento, replay e duplicação

### 4.1 Regras do verificador

1. Verificar tamanho/JSON/schema/versão/canonical bytes antes de criptografia; rejeitar duplicatas, campos desconhecidos e números ambíguos.
2. Resolver `issuer_id`, `key_id`, algoritmo, uso e status a partir do key registry confiável; exigir assinatura válida nos bytes enquadrados e digest igual ao payload.
3. Verificar mTLS peer esperado independentemente da assinatura; conferir audience literal, ambiente, protocolo e role permitida.
4. Recalcular todo digest e snapshot lineage; buscar snapshot no source autoritativo e conferir ID, revision, schema, parent, acceptance record, digest, checkpoint e payload. Assertion/signature de cliente nunca faz snapshot ser aceito.
5. Verificar `not_before`, `expires_at`, revogação/key epoch, estado de uso e policy online. Antes de permitir o efeito, TEG consulta Broker e anchor; offline verification não autoriza efeito.
6. A decisão humana é verificada separadamente: referência à decisão Broker, digest da apresentação, principal, policy de separação solicitante/aprovador e resultado de WebAuthn UV. A assinatura Broker atesta que seus controles foram aplicados, não prova entendimento humano.
7. TEG verifica que handler, parâmetros e bytes enviados correspondem ao binding e autorização e grava intenção/start permit antes do efeito; resultado externo precisa de prova do provider de acordo com a classe da operação.

### 4.2 Chave de idempotência e replay

O Broker mantém unicidade durável por `(issuer_id, key_id, jti)`, e autorização por `(operation_id, binding_digest, audience)`. O TEG mantém unicidade por `(gateway_id, operation_id, authorization_digest, effect_slot)`. A âncora mantém `(stream_id, sequence)` e aceita repetição somente se checkpoint digest idêntico.

- Mesma chave + mesmos bytes: devolver status/receipt já persistido; não criar nova autorização, consumo ou efeito.
- Mesma chave + bytes/digest/audience diferentes: conflito, evento auditável e quarantine se indicar comprometimento.
- Mesmo operation ID com binding diferente: rejeitar; correção exige nova operação aprovada.
- Resposta perdida: consultar a mesma operação/ID. Nunca criar nonce/jti/operação nova como retry implícito.
- Cache de replay precisa sobreviver restart/restore e ser comparado com monotonicidade externa. Cache em memória não é suficiente.

## 5. Máquina de estados, revogação e linearização

### 5.1 Estados propostos

`draft → consent_pending → authorized → reserved → consumed → prepared → start_permitted → effect_possible → receipt_recorded → anchored_terminal`.

Ramos: `rejected`, `expired`, `revoked_before_start`, `failed_before_effect`, `failed_verified`, `unknown`, `quarantined`. `unknown` não pode ser promovido por timeout, ausência de erro, restart ou status local. Só consulta autoritativa/independente pode reconciliar; se prova conclusiva faltar, permanece desconhecido.

### 5.2 Pontos de linearização

| Ação | Ponto de linearização proposto | Consequência |
| --- | --- | --- |
| Consentimento/decisão | Commit serializável no Broker depois da UI confiável/WebAuthn, com decision digest, snapshots e policy atuais | Sem commit, não existe autorização. Repetição de request igual consulta a mesma decisão; digest divergente conflita. |
| Autorização | Commit Broker da `ExecutionAuthorization` + jti único + journal; assinatura é produzida a partir do registro committed | Mensagem assinada antes do commit não é válida. Falha após commit retorna query idempotente do mesmo objeto. |
| Reserva/consumo | Transação Broker que marca authorization/binding/audience consumidos para um TEG específico, append-only | Uma única reserva/consumo. Não prova que handler começou. |
| Revogação | Commit do evento de revogação/epoch no Broker; serializa contra emissão/reserva/start permit | Após ACK, nenhum novo start permit pode ser emitido. Permits previamente emitidos aparecem como in-flight; ACK não afirma cancelamento desses efeitos. |
| Start permit | Commit Broker online, verificando status/revogação/expiração; TEG persiste o permit e seu digest em journal antes de chamar handler | Este é o limite de admissão. Sem permit persistido e âncora pré-efeito, o TEG não chama provider. Não é atômico com commit remoto. |
| Efeito externo | Ponto interno do provider, se ele expuser transação/ID consultável; caso contrário não existe ponto global observável pelo Broker | TEG registra `effect_possible` antes da chamada. Crash ou timeout a partir dali pode ser unknown; exactly-once não é prometido. |
| Receipt final | Commit TEG do receipt verificado, seguido do post-effect anchor ACK | Só então estado pode ser terminal de acordo com policy. Sem ACK/valid evidence, fica unknown/quarantine. |

Revogação e start permit devem serializar no Broker; revogação não pode cancelar atomicamente uma chamada já admitida. Há uma janela entre commit do permit, persistência no TEG e chamada ao provider. Para semântica mais forte (“nenhum efeito começa após ACK de revogação”), o provider precisa aplicar fencing token/epoch na própria transação ou consultar uma autoridade de revogação no commit. Sem essa capacidade, contrato proposto define ACK de revogação como **bloqueio de novos permits** e lista os permits já emitidos; uma operação in-flight pode produzir efeito depois do ACK. UI e operadores devem receber essa informação explicitamente. Cancelamento é best-effort somente se handler/provider demonstrar API semântica de cancelamento; nunca declarar rollback de efeito irreversível.

### 5.3 Fonte de tempo e validade

| Item | Proposta | Alternativa / risco / aceite |
| --- | --- | --- |
| Codificação | UTC RFC 3339 exatamente com milissegundos: `YYYY-MM-DDTHH:MM:SS.sssZ`; rejeitar timezone local, precisão variável e leap-second não suportado. Timeout decorrido mede-se com monotonic clock. | Precisão maior é possível mas aumenta divergências; aceitar somente se vectors cross-language passarem. |
| Fonte | Relógio do serviço em cada domínio sincronizado e monitorado por fonte autenticada administrada fora do host agente; gravar status/uncertainty do serviço. Cliente nunca determina validade. | NTP comum sem monitoramento; serviço autenticado/time attestation. Escolha concreta depende da infraestrutura e é pendente. |
| Tolerância | ±2 s como recomendação R1, ainda não aprovada. Para não aceitar cedo: exigir `trusted_now - uncertainty >= not_before`. Para não aceitar tarde: exigir `trusted_now + uncertainty < expires_at`. | Maior skew amplia janela de abuso; menor pode indisponibilizar. Aceitação exige medir clock offset e executar C-16. |
| Indisponibilidade | Se status de sincronização ausente, incerteza excedida ou clocks dos serviços incompatíveis, bloquear nova autorização/start permit e manter operação pendente/unknown. Não usar relógio do cliente como fallback. | Pode causar indisponibilidade prolongada; não há fail-open. |
| Expiração | Sugestão: autorização válida no máximo 120 s, e start permit precisa linearizar estritamente antes de expiry considerando uncertainty; expiry impede novos permits/reservas. | TTL e limite de clock dependem da classe de efeito e exigem aprovação humana. |
| Efeito em andamento | Expiração/revogação depois do start permit bloqueia novos efeitos; não aborta nem reverte automaticamente o atual. Reinício após possível início reabre como unknown e permite somente query/reconcile. | Cancelamento exige suporte explícito e evidência do provider. |

### 5.4 Corridas concorrentes — resultados esperados

| Corrida | Ordem 1 | Ordem 2 | Resultado normativo proposto |
| --- | --- | --- | --- |
| Revogação vs reserva | Revogação commita antes | Reserva commita antes | Ordem 1: reserva recusada. Ordem 2: a reserva pode ser consultada, mas nenhum start permit é dado depois da revogação. |
| Revogação vs start permit | Revogação commita primeiro | Permit commita primeiro | Ordem 1: zero handler calls. Ordem 2: revoke ACK marca a operação in-flight; não promete cancelamento; TEG executa no máximo a chamada associada ao permit ou reconcilia unknown. |
| Expiração vs start permit | expiry alcançada antes do commit | permit commita antes de expiry | Ordem 1: permit recusado. Ordem 2: operação admitida; expiry posterior não é cancelamento. Incerteza temporal fora do limite bloqueia ambos os casos. |
| Dois TEGs consumindo | TEG-A ganha commit único | TEG-B ganha commit único | Um audience/binding vence. Outro recebe consumed/conflict e não invoca handler. Não compartilhar authorization entre Gateways. |
| Provider commit vs queda | Provider registra commit e resposta chega | Provider registra commit, resposta perde-se | Primeiro: receipt vinculado ao effect ID. Segundo: unknown até query externa recuperar o mesmo ID; zero retry automático. Sem query/idempotency forte, permanece unknown. |
| Revogação de chave vs verificação offline | Key revoga antes da consulta online | Auditor verifica arquivo histórico sem rede | Nova operação: estado online indisponível implica bloqueio. Auditoria: assinatura histórica pode ser matemática/temporalmente válida, mas status de revogação atual não pode ser alegado offline. |

## 6. Protocolo proposto de ancoragem externa

### 6.1 Formatos

**`AnchorCheckpointV1`** (assinado pelo dono do stream):

```json
{
  "version": 1,
  "stream_id": "teg:<stable-id>",
  "stream_owner_id": "teg:<stable-id>",
  "sequence": 1042,
  "previous_anchor_receipt_digest": "sha256:<64-lower-hex>",
  "journal_head_digest": "sha256:<64-lower-hex>",
  "phase": "pre_effect",
  "operation_id_digest": "sha256:<64-lower-hex>",
  "authorization_digest": "sha256:<64-lower-hex>",
  "binding_digest": "sha256:<64-lower-hex>",
  "gateway_receipt_digest": null,
  "schema_version": 1,
  "created_at": "2030-01-02T03:04:05.006Z",
  "signing_key_id": "teg-anchor-2026-01"
}
```

`phase` enum: `pre_effect`, `post_effect`, `recovery`, `key_rotation`. No payload, prompt, credential, assertion, provider secret, raw snapshot or PII goes to anchor. The operation ID is pseudonymized by a keyed digest scoped to the anchor domain; key rotation and correlation policy are pending human decision.

**`AnchorReceiptV1`** (signed by independent anchor): anchor/log ID, checkpoint digest, stream/sequence, accepted time, durable flag, tree size/root, leaf index, inclusion path, consistency proof reference, anchor key ID/epoch, status. Proposed Merkle hashing: `leaf=SHA256(0x00 || canonical_checkpoint_bytes)`, `node=SHA256(0x01 || left_hash || right_hash)`. Root and tree size are signed. `durable=true` means the anchor’s documented storage durability policy has completed before receipt issuance; merely accepting bytes in process memory or TLS ACK is insufficient.

### 6.2 Protocol and order

1. Broker and TEG maintain independent stream IDs, sequences and signing keys. Stream sequence increments exactly by one; same `(stream_id, sequence)` is idempotent only for identical checkpoint digest. Sequence gap, fork, predecessor mismatch or digest change is conflict/quarantine.
2. Before every protected provider effect, TEG durably records intent, exact binding, authorization and `effect_prepared`; constructs `pre_effect` checkpoint; signs it; submits to the anchor. The anchor durably appends it and returns signed receipt + inclusion proof. TEG verifies anchor identity/key, signature, expected sequence/predecessor, inclusion/root, consistency with its last pinned root and freshness policy. **No protected handler is called without valid durable pre-effect ACK.**
3. The TEG then requests/uses the Broker `start permit` online, serialized against revocation, and persists the permit before invoking exactly one typed handler. The choice whether the anchor checkpoint must include the final start permit digest creates an ordering dependency; recommendation: pre-effect checkpoint binds authorization+binding+prepared digest, then permit receipt gets a second pre-call checkpoint, both anchored before provider call. This exact sequence requires approval because it increases latency and anchor availability dependency.
4. After known provider response, TEG durably records raw evidence reference/digest and `GatewayExecutionReceipt`; signs receipt and submits `post_effect` checkpoint. `succeeded` is exposed only after post-effect durable ACK and required provider evidence. Failed anchor after a possible effect leaves `unknown` and quarantines dependent protected operations; it does not trigger retry.
5. Recovery reads latest anchor head/root and checks inclusion/consistency. A smaller local sequence, divergent digest, missing checkpoint, invalid key epoch, unrecognized fork or rollback is quarantine. Recovery appends a `recovery` checkpoint after independent reconciliation; it never deletes or overwrites the anchor head.

### 6.3 Guarantees and limits

- Pre-effect ACK proves the anchor accepted a signed digest before the TEG proceeded, according to the anchor’s stated durability policy. It is evidence of **intent/preparation**, not authorization or effect.
- Signed `ExecutionAuthorization` + Broker journal/decision reference is evidence of **authorization**. It does not prove that the TEG invoked provider.
- TEG receipt plus provider evidence and post-effect ACK are evidence of **reported/observed effect**. The anchor only proves inclusion/order of digests; it does not prove the event is truthful or complete.
- Monotonicity is per stream and signed log tree size/root. Verifiers pin latest root/size durably outside the agent host and require consistency proofs. To detect split-view equivocation, independent witnesses/auditors must compare roots; one anchor signing two forks may fool isolated clients. Witness count/provider and gossip mechanism are pending.
- Retention must exceed maximum authorization, recovery, dispute, backup and regulatory periods; legal hold overrides routine expiry. Numeric retention, cost, region and deletion are undecided. Expired anchor receipts must not be silently treated as absent or valid; service enters reconciliation/quarantine.
- Before-effect anchor outage: no new effect. A pre-effect receipt already verified remains evidence, but outage before required second checkpoint/start permit blocks. After possible effect but before post-effect ACK: `unknown`, no auto-retry, query provider and anchor. After ACK response loss: query by same stream/sequence/digest; identical submission may be idempotent, changed content must alert and quarantine.

## 7. Decision matrix por contrato e dependência

| Contrato/decisão | Alternativas viáveis | Recomendação | Risco residual | Dependências | Aceite verificável | Aprovação humana necessária |
| --- | --- | --- | --- | --- | --- | --- |
| ExecutionAuthorization | Ed25519 envelope; JWS/COSE with EdDSA/ES256; unsigned mTLS response (rejeitar para efeito) | AF-SIG-1 proposto com Broker signature + audience TEG | Broker key compromise pode forjar; assinatura não equivale a consentimento | KMS, identity, accepted snapshot source, WebAuthn/UI, online revocation | assinatura/binding/snapshot/audience/replay/expiry vectors; receiver rejects revoked/future/unknown fields | Envelope/algoritmo; issuer/Key Registry; limite de TTL; source of consent evidence |
| ExecutionBinding | Digest apenas; TEG assinatura; Broker recalcula sem assinatura | TEG assina pedido e Broker resolve/recalcula conteúdo de fonte aceita; TEG recalcula antes do efeito | TEG comprometido pode reportar binding falso; Broker não observa bytes provider se handler malicioso | Identidade TEG, handler registry, payload store, provider semantics | mutation de cada campo rejeitada; byte-for-byte payload digest; gateway identity and handler exact | Schema, quais campos e fontes são autoritativos, se binding signature é obrigatória |
| ExecutionReceipt | Um receipt detalhado; Broker receipt de consumo; projection do Gateway receipt | GatewayExecutionReceipt primário + ExecutionReceipt signed projection pelo mesmo TEG | Projection drift; TEG sign key compromised; effect evidence provider incomplete | Schema do receipt, provider query/keys, anchor, audit export | projection digest maps one-to-one; statuses and evidence class; no success without post-anchor + provider evidence | Semântica final dos dois tipos; se remover/tornar alias um deles; definição de `succeeded` |
| GatewayExecutionReceipt | Self-digest; TEG signature; provider-signed receipt | TEG signature plus provider evidence reference; provider signature when available | TEG attestation not independent; provider may not expose verifiable receipt | TEG KMS, handler, provider features, anchor | tests of tampering/replay, evidence classes, crash after provider commit; unknown if evidence missing | Signer, audience, algorithm, provider evidence levels, retention |
| Signing algorithm | Ed25519; ES256; RSA-PSS | Ed25519 subject to crypto review and platform support | Ed25519 may be disallowed in chosen KMS/FIPS | KMS/HSM support and compliance owner | interoperability vectors and cryptographic review | Final algorithm and approved library/KMS |
| Time/revocation | Local clock; monitored trusted time; provider fencing | Trusted domain clock + proposed ±2s conservative bound; online check before permit | Distributed revoke cannot cancel already admitted effect | Time service, broker availability, provider cancellation/fencing | clock skew tests; linearizability trials; revoke ACK states in-flight precisely | Source, tolerance, TTL, revoke semantics, cancellation service-level behavior |
| Anchor | Periodic batch; per-effect pre/post ACK; WORM object store; Merkle transparency log | Per-effect pre-effect inclusion ACK and post-effect receipt, with independent witnesses | Anchor/admin compromise, outage, split-view, latency/cost | Independent anchor domain, keys, witnesses, retention and network | inclusion/consistency proof, rollback/fork, outage/recovery and commit-before-effect evidence | Provider, format, proof algorithm, ack durability, outage mode, retention/cost |

## 8. Matriz de falhas e recuperação

| Falha/ponto | Evidência já persistida | Estado após restart | Ação permitida | Ação proibida |
| --- | --- | --- | --- | --- |
| Antes de consentimento commit | Proposal only | `consent_pending` ou `rejected` | Read/review; nova cerimônia explícita | Emit authorization |
| Broker commitou authorization, resposta perdida | Broker DB + signed message ID/jti; cliente pode não ter response | `authorized` no Broker; cliente `unknown` até query | Query mesmo operation ID e digest | Criar nova autorização/operation automaticamente |
| Reserva commitada, confirmação perdida | Broker consumption record + TEG intent | `unknown` local, query Broker | Reconciliar status | Novo consume com chave diferente ou efeito |
| Revogação commitou antes de start permit | Revocation epoch event | `revoked_before_start` | Auditoria/read | Emitir permit/handler call |
| Permit commitou e TEG persistiu, crash antes da chamada | Broker permit + TEG permit journal + pre-anchor receipt | Conservadoramente `unknown` se ausência de chamada não for atomicamente comprovável | Query provider por idempotency/effect key; marcar failed-before-effect somente com prova | Repetir chamada automaticamente |
| Provider pode ter comitado, resposta perdida | TEG `effect_possible`; talvez provider effect ID não conhecido | `unknown` | Query status idempotente; anexar evidência | Retry, nova idempotency key ou sucesso presumido |
| Efeito conhecido, anchor post falhou | Provider evidence + TEG signed receipt local, sem post-anchor | `unknown`/quarantine | Retry somente da submissão de checkpoint idêntico; reconciliar receipt | Repetir efeito ou expor conclusão terminal sem policy explícita |
| Anchor indisponível antes do efeito | Intent/preparation; sem pre-effect ACK | `prepared`/blocked | Aguardar, consulta read-only | Executar, enfileirar para auto-dispatch |
| Âncora devolve sequence/fork divergente | local checkpoint + external root conflict | `quarantined` | Evidência preservada, auditor independente | Resetar sequence/head ou sobrescrever âncora |
| Fonte de tempo indisponível/skew excessivo | Monitoria temporal ausente | blocked; possível efeito prévio mantém unknown | Recuperar time source e revalidar autorização | Usar relógio cliente ou aceitar expiração incerta |
| Revogação depois do permit | Permit durável; operation in-flight | `effect_possible`/`unknown` até prova | Cancel request best-effort só se provider suporta; reconciliar | Afirmar que efeito foi cancelado ou retry automático |

## 9. Critérios objetivos de homologação (propostos)

Uma futura aprovação técnica de contratos exigiria todos os grupos aplicáveis:

1. **Criptografia:** segunda implementação independente valida vectors de assinatura/canonicalização; tamper de cada campo, key/issuer/audience/version errados, algoritmo downgrade e envelope malformado são recusados; key rotation, key revocation e historical offline verification têm fixtures distintas.
2. **Snapshot/consent:** autorização liga snapshots imutáveis e decisão digest; troca de um byte/ID/revision/schema/checkpoint/payload/policy invalida; testes provam que assinatura do cliente não estabelece snapshot aceito nem consentimento.
3. **Replay/idempotência:** corrida multiprocesso nos estados issue/reserve/consume/start permit; mesmo request recebe mesmo estado e nunca segundo efeito; conteúdo divergente conflita; durable replay ledger sobrevive restart/restore.
4. **Revocation/time:** pelo menos 100 barreiras concorrentes em cada corrida reserve-vs-revoke e permit-vs-revoke; verificar commit order; offsets propostos -3,-2,-1,0,+1,+2,+3s e expiry boundaries; time unavailable/skew beyond configured limit sempre bloqueiam novo permit. Quantidades são proposta e dependem de aprovação de campanha.
5. **Efeito/recovery:** injetar crash em cada fronteira antes/depois do provider commit; provider ledger/query externo confirma no máximo um efeito quando suportado; ausência de prova mantém unknown; zero auto-retry; cancellation claims correspondem à semântica do provider.
6. **Anchor:** validar inclusion e consistency proofs, idempotência mesma seq/digest, rejeitar fork/rollback; provar pre-effect durable ACK existe antes do provider request no log externo; queda/outage antes do ACK dá zero efeito; post-effect outage deixa unknown; restart compara raiz/tamanho contra estado pinado fora do host agente.
7. **Independência administrativa B:** evidência de papéis efetivos/control planes, chaves/vaults/stores/backups/redes separados; teste cruzado de operador Broker e TEG; break-glass auditado; anchor/admin/aprovador fora do host cliente conforme ADR R2. Teste de unidade ou diagrama não basta.
8. **Auditoria e privacidade:** recibos se encadeiam authorization→binding→permit→gateway receipt→provider evidence→anchor refs; export read-only redigido, sem secrets/assertions; root/head e discrepâncias verificáveis por auditor independente.
9. **Classificação do resultado:** cada ambiente relata passed/failed/inconclusive e versões. Recurso ausente, log externo ausente, serviço não real ou simulador usado para comprovar isolamento resulta em `inconclusive`, não `passed`. BLOCKER/MAJOR ou dúvida de proveniência bloqueia qualquer avanço.

## 10. Decisões que precisam de aprovação humana

| ID | Decisão solicitada | Alternativas e recomendação | Papel responsável a designar | Estado |
| --- | --- | --- | --- | --- |
| H-01 | Perfil de serialização/assinatura | AF-SIG-1 próprio + Ed25519 recomendado; COSE/JWS + EdDSA/ES256 como alternativa de padrão; escolher depois de revisão criptográfica | Responsável de segurança criptográfica + plataforma | Pendente |
| H-02 | Relacionamento entre receipts | GatewayExecutionReceipt primário + ExecutionReceipt projeção assinada recomendada; alternativamente um único tipo | Dono do protocolo Broker/TEG + auditor | Pendente |
| H-03 | Key management | KMS/HSM/TPM suportado por domínio e roots offline; fornecedor/região/backup ainda sem escolha | Operadores independentes Broker/TEG/anchor | Pendente |
| H-04 | Semântica de revogação | ACK bloqueia novos permits e lista in-flight recomendado; fencing provider para garantia mais forte | Dono de risco de produto e operadores Broker/TEG | Pendente |
| H-05 | Time/TTL | Fonte autenticada monitorada, skew ±2 s e max authorization 120 s como valores iniciais propostos | SRE + segurança + owner do efeito | Pendente |
| H-06 | Protocolo de anchor | Prova Merkle inclusão/consistência, ACK pré-efeito e receipt pós-efeito recomendado; witnessed log/object lock alternativos | Auditor independente + owner de plataforma | Pendente |
| H-07 | Retenção e custos | Período definido por requisitos legais, disputas e recovery; numeric limit não fixado | Produto + privacidade + finanças | Pendente |
| H-08 | Ambiente/ensaios | Primeiro simuladores para contrato, depois sandbox independente e credenciais sintéticas/escopadas; nenhuma autorização de recursos dada | Owner do laboratório + security reviewer | Pendente |
| H-09 | Critérios C-01–C-18 | Aprovar protocolo, quantidades, evidências externas, falsos positivos e operação de provider sandbox | Avaliador independente + responsável do programa | Pendente |
| H-10 | Go/no-go e RB-09C.2 | RB-09C.2 somente após aprovação formal do conjunto de decisões, threat model e recursos; não autoriza RB-09C.3+ | Autoridade de risco/produto a designar | Pendente |

Nenhum responsável nominal ou aprovação foi presumido. A Alternativa B de domínios administrativos independentes é a única escolha considerada aprovada, conforme ADR 0008 R2. Todos os demais itens acima continuam pendentes.

## 11. Próximo gate e limites

Este R3 pode ser aceito, devolvido para revisão ou rejeitado como proposta. **Não é autorização para RB-09C.2.** Para pedir RB-09C.2, responsáveis humanos devem decidir H-01–H-10 aplicáveis, aprovar as mudanças correspondentes no ADR 0008 em solicitação própria, designar papéis/recursos e aceitar critérios de homologação. Mesmo essa aprovação não autoriza automaticamente infraestrutura, enrollment, credenciais de teste, providers ou serviços: cada etapa operacional permanece sujeita ao gate/autorização correspondente.

Nenhum código, teste, contrato wire, workflow, serviço, chave, credencial, grant, provider ou agente foi criado/executado por este documento. A autoridade padrão mantém-se fail-closed e a RB-09C.2–RB-09C.8/RB-10 permanecem não iniciadas.
