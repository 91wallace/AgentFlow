# RB-09C.1-R5 — remediação documental dos achados R4

**Status:** proposta de remediação para revisão humana. Nada neste documento aprova os contratos, parâmetros ou serviços propostos.
**Decisão já aprovada e preservada:** Alternativa B — Broker e TEG em domínios administrativos independentes — conforme ADR 0008, emenda R2. A independência continua não implementada e não homologada.
**Escopo:** responder a F1–F9 do parecer R4, propor contratos verificáveis e critérios de revisão. Não autoriza RB-09C.2, operação, credenciais, grants ou execução.

Este adendo complementa, sem substituir, ADR 0008, R1 e R3. Em caso de conflito, o ADR vigente prevalece; uma proposta R5 somente se torna normativa após decisão humana explícita e atualização documental autorizada. Os contratos Python atuais permanecem contratos locais de dados/digests, não envelopes assinados.

## 1. Matriz de remediação F1–F9

`RESOLVED_PROPOSED` significa que esta proposta fornece uma resolução revisável; não significa aprovação nem implementação. `APPROVED` exige aprovação humana explícita. A Alternativa B aprovada não é reaberta nesta matriz.

| Achado | Problema e causa arquitetural | Documentos / contratos envolvidos | Dependências | Correção proposta | Critério objetivo de encerramento | Evidência documental | Decisão humana necessária | Estado |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | Serialização e assinatura não definem uma representação interoperável byte a byte; Approval rejeita floats, TEG os aceita. JSON customizado deixa casos Unicode/número subespecificados. | ADR 0008 §§R2; R3 §§3–4; `approval.protocol` (`AcceptedSnapshot`, `ExecutionAuthorization`, `ExecutionReceipt`); `teg.contracts`, `teg.protocol` (`ExecutionBinding`, `GatewayExecutionReceipt`). | H-01; precede F2, F3, F5, F7, F8. | Propor COSE_Sign1 + deterministic CBOR, algoritmo fechado Ed25519, regras de tipos/limites e vetores na §2; um perfil compartilhado para Approval e TEG. Não reaproveitar silenciosamente os digests JSON v1. | Especificação identifica bytes e rejeições sem ambiguidade; segunda implementação independente reproduz todos os vetores; testes negativos cobrem cada classe e os dois lados. Aprovação do perfil e revisão criptográfica ainda requeridas. | §2 e apêndice de vetores requeridos. | H-01: formato, algoritmo, limites, biblioteca/KMS e política de migração. | RESOLVED_PROPOSED |
| F2 | Assinar manifestos não basta se a raiz ou o canal de distribuição da raiz puder ser substituído ou sofrer rollback. Recuperação de comprometimento não tem autoridade definida. | R1 §§3, 7; R3 §3.3; ADR 0008 §§R2; trust registry, COSE key manifest. | H-01 antes; H-03; H-06 para epochs externos. | Bootstrap de fingerprint por canal administrativo independente com verificação por duas pessoas; epochs monotônicos ancorados; rotação assinada pela raiz atual e confirmada fora de banda; comprometimento causa halt/quarentena e novo bootstrap explícito. | Teste de troca de raiz, rollback de epoch, key revoked e restore antigo falha fechado; trilha externa identifica aprovadores e epochs; procedimento de recuperação aprovado por operadores separados. | §3 e cenários C-13/C-20. | H-03: custódia, KMS/HSM, control planes, recuperação, papéis e canais. | RESOLVED_PROPOSED |
| F3 | `StartPermit` não tem schema implementado/normativo completo; confirmação, revogação, expiração e chamada física ao efeito deixam corridas abertas. O Gateway atual chama handler após confirmação sem permit. | R3 §§5, 8–9; `ConsumptionConfirmation`; `gateway.py` após confirmação; ADR 0008 §R2. | H-01, H-03, H-05; antes de H-06 e da execução RB-09C.2. | Contrato one-use assinado pelo Broker e máquina de estados/linearização na §4. Revogação bloqueia novos permits; não promete cancelar um permit emitido nem efeito iniciado. Inclui corridas de consume/revoke e expiry/confirmation/permit. | Cada operação tem um único ponto de linearização definido; barreiras multiprocesso observam ordem serializável; qualquer resposta perdida permanece query/unknown; nenhuma chamada sem permit e ACK de âncora definido na §6. | §§4–5; C-05, C-17, C-21. | H-04/H-05: semântica de revogação, relógio, TTL, tolerância e aceitação da janela in-flight. | RESOLVED_PROPOSED |
| F4 | O fluxo não prova que a intenção ancorada é exatamente a que recebeu consumo/permit nem que o ACK ocorre antes do efeito. O checkpoint de R3 omite digests de consumo e permit. | R1 §1.5; R3 §6; `ConsumptionReservation`, `ConsumptionConfirmation`, `GatewayExecutionReceipt`. | F1–F3; H-06; testemunhas e provider identificáveis. | Um checkpoint pré-efeito inclui authorization, binding, reservation, confirmation e StartPermit; ACK durável de inclusão/continuidade é obrigatório antes de marcar `effect_possible`/chamar provider. Outro checkpoint pós-efeito ancora receipt/evidência. | Logs externos demonstram checkpoint contendo os mesmos digests antes da primeira requisição do provider; resposta perdida da âncora não causa efeito; mutação de qualquer elo é detectada; pós-efeito ausente resulta em unknown. | §6 e cenários C-06, C-07, C-15, C-18, C-20. | H-06: âncora, prova, testemunhas/quórum, disponibilidade, retenção, custo e latência. | RESOLVED_PROPOSED |
| F5 | Dois receipts têm digests próprios, sem fonte de verdade, projeção determinística ou regra de divergência. Receipt local pode ser confundido com prova de efeito externo. | ADR 0008 §R2; R3 §§3.2, 7–8; `ExecutionReceipt`; `GatewayExecutionReceipt`. | F1, F3, F4; H-02. | Tornar `GatewayExecutionReceipt` a atestação autoritativa do TEG; definir `ExecutionReceipt` como projeção determinística assinada que referencia o digest do primeiro. Divergência => unknown/quarentena. Evidência do provider é independente e classificada separadamente. | Testes de projeção reproduzem exatamente os campos; qualquer divergência impede sucesso; sucesso externo exige evidência verificável do provider quando disponível, e sem ela receipt local só prova relato/invocação do TEG. | §5; C-19 e C-18. | H-02: autoridade, campos, emissores, cardinalidade e significado de sucesso. | RESOLVED_PROPOSED |
| F6 | H-01–H-10 não têm DAG e as quantidades/coberturas das campanhas R1/R3 divergem, com lacunas em consumo/revogação, expiração/confirmação e split view. | R1 §§1.6–1.8; R3 §§9–10; ADR 0008 §R2. | F1–F5; recursos e avaliador H-08/H-09. | DAG explícito na §7; matriz consolidada C-01–C-21 na §8. Corridas têm barreiras, quantidades, injeção, evidência independente e regra pass/fail fixas. | Um protocolo de teste versionado executa cada linha com IDs e relatório; nenhum cenário recebe pass sem evidência externa designada; avaliador independente aprova o plano antes da campanha. | §§7–8. | H-09: aprovar campanha, quantidades, ambiente, evidências e critérios. | RESOLVED_PROPOSED |
| F7 | Digest do snapshot não autentica a aceitação, o principal que aceitou, a linhagem ou a revogação; TEG não tem fonte verificável definida para esses fatos. | `AcceptedSnapshot` em `approval.protocol`; `ExecutionBinding`; R3 §§3.2, 4; ADR 0008 §§R2. | F1, F2; autoridade de snapshots; H-01/H-03. | Assinatura `SnapshotAcceptanceAttestation` pelo serviço de autoridade SDD/Broker designado; incluir snapshot digest, revision/schema, parent, provenance digest, decisão/principal, policy epoch e event ID. Authorization e binding carregam o digest da atestação; TEG resolve/revalida status e linhagem online. | Troca de snapshot, parent, principal, decisão ou epoch falha; revogação/revisão posterior invalida uso novo; atestação válida offline não substitui status online para efeito. | §3.4 e C-08. | H-03 e dono de produto/SDD: autoridade de aceite, fonte, política de revalidação/revogação. | RESOLVED_PROPOSED |
| F8 | ACK assinado de uma âncora não detecta sozinho split view, rollback administrativo ou apagamento; formato de witness e continuidade insuficientes. | R1 §§1.5, 6; R3 §6; ADR 0008 §§R2. | F1, F2, F4, H-06/H-07. | Log de transparência com inclusion/consistency proofs, roots pinadas fora dos domínios, testemunhas independentes que assinam checkpoints e comparam heads; falha de continuidade coloca ambos os lados afetados em quarentena. | Ensaio de duas raízes conflitantes é detectado por witness independente; restauração de root menor é recusada; confirmação durável exige política de witness/quórum aprovada e verificável. | §6 e C-20. | H-06/H-07: serviço, independência, quórum, retenção e custos. | RESOLVED_PROPOSED |
| F9 | R1 mantém B como recomendação pendente, podendo parecer contradizer a aprovação R2. | R1 §§1–2; ADR 0008 emenda R2. | Nenhuma dependência técnica. | Inserir nota histórica no R1: B aprovada por R2; A não é arquitetura-alvo; demais propostas continuam abertas. Histórico original preservado. | R1 distingue claramente decisão vigente, texto histórico e propostas ainda pendentes; ADR 0008 continua a fonte normativa. | Nota de status no início do R1 e esta matriz. | Nenhuma decisão nova; Alternativa B já aprovada. | RESOLVED_PROPOSED |

Nenhum F1–F9 está `APPROVED`. A conclusão documental desta remediação não equivale a encerramento de RB-09C.1 enquanto os itens `RESOLVED_PROPOSED` não receberem revisão e decisão cabíveis.

## 2. Proposta de perfil criptográfico e representação

### 2.1 Perfil proposto AF-COSE-1

Usar **COSE_Sign1** conforme RFC 9052, com **EdDSA/Ed25519** (COSE alg `-8`, Ed25519 conforme RFC 8032) e payload **deterministic CBOR** conforme RFC 8949 §4.2.1. Esta proposta substitui o envelope JSON customizado AF-SIG-1 de R3; não se recomenda criar framing criptográfico próprio. O protocolo wire proposto é v2; `AF-COSE-1` identifica a revisão do perfil criptográfico, não a versão do schema. SHA-256 identifica payloads/digests, mas não é uma assinatura. Não há fallback de algoritmo, codec ou versão.

O objeto assinado é COSE_Sign1 tag 18, payload embutido, protected headers contendo exatamente `alg=-8`, `kid` ASCII, `content-type`, `profile-id=AF-COSE-1` e `crit=[profile-id]`; unprotected headers devem estar vazios. O content type é específico do contrato e da nova versão (`application/vnd.agentflow.<contract>.v2+cbor`). External AAD é a sequência ASCII `agentflow:<contract>:v2`; `Sig_structure` segue COSE. `kid`, algoritmo, audience, versão e tipo ficam protegidos pela assinatura. Campo desconhecido, header duplicado/desprotegido, tag não permitida, critical header desconhecido, versão ou content type desconhecidos são rejeitados antes de uso.

### 2.2 Regras byte a byte

- Aplicar deterministic encoding RFC 8949 §4.2.1: comprimento mínimo de argumento, mapa ordenado por comprimento da codificação da chave e depois comparação lexicográfica dos bytes codificados; sem indefinite-length. Decoder rejeita forma não determinística, chave duplicada e chave de tipo inesperado.
- Strings são UTF-8 válido, sem surrogate não pareado e obrigatoriamente NFC. Entrada não NFC é rejeitada, nunca normalizada silenciosamente. IDs de protocolo continuam ASCII conforme as restrições atuais. Bytes de payload textual são o UTF-8 exato resultante.
- Permitidos: null, boolean, bytes, texto, arrays, mapas com chaves textuais e inteiros sem sinal de 0 a `2^64-1`. Floats, NaN/Infinity, negative integers, bignum tags, simple values não definidos, tags não explicitamente listadas e tipos específicos de linguagem são proibidos. Valores monetários usam decimal ASCII validado pelo schema, nunca float CBOR.
- Timestamps são inteiro Unix UTC em milissegundos, uint64; regras de precisão/subsegundo e `not_before` são comuns a todos os contratos. Converter strings RFC 3339 legadas só em migração explicitamente versionada; não aceitar ambos formatos no mesmo envelope.
- Limite proposto: envelope COSE completo até 64 KiB; payload até 60 KiB; profundidade máxima 16; até 4096 itens por array/mapa; chave até 128 bytes UTF-8 e string até 16 KiB. Decoder rejeita antes de alocar além dos limites. A aprovação dos valores é H-01.
- Cada contrato possui schema fechado por `contract_version`; presença de campo opcional ausente difere de campo presente com null. Nenhum campo extra é ignorado. Schema version, audience e contrato estão em payload protegido e em content type.
- Digest: `SHA-256(ASCII("agentflow:<contract>:v2:digest\0") || deterministic_cbor(payload))`, apresentado como `sha256:` mais 64 dígitos hex minúsculos. A assinatura COSE protege o envelope; um digest sozinho não autentica origem.
- Approval e TEG devem chamar um codec/profile único versionado ou duas implementações independentes que passem os mesmos vetores. Não permitir canonicalizadores próprios divergentes. Contratos JSON/digest v1 permanecem legados e somente para leitura/auditoria; não podem autorizar efeito nem ser reinterpretados como AF-COSE-1. Cutover para v2 exige versão de endpoint/schema explícita, preservação dos eventos antigos sem reescrita, backup/rollback testados e rejeição de mistura v1/v2 na mesma operação. Nenhuma migração ou compatibilidade dual é automática.

### 2.3 Vetores de interoperabilidade requeridos

Vetores públicos iniciais propostos (hexadecimal; payload do digest conforme §2.2):

| Caso | Valor/lógica | Bytes determinísticos esperados |
| --- | --- | --- |
| CBOR uint 0 | `0` | `00` |
| CBOR uint 23 | `23` | `17` |
| CBOR uint 24 | `24` | `1818` |
| CBOR mapa | `{"a":1}` | `a1616101` |
| CBOR mapa vazio | `{}` | `a0` |
| Digest `test-vector` | prefixo ASCII `agentflow:test-vector:v2:digest\0` + payload bytes `a1616101` | `sha256:e9c51f605aabedb90fc3047a43bde0a1f968fb52dd34a5319e445a0acca2903f` |
| Ed25519/RFC 8032 §7.1, vector 1 | mensagem vazia; public key `d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a` | assinatura `e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155` `5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b` |

O vetor RFC valida a primitiva, não substitui vetor do envelope COSE. Antes de H-01, publicar também envelopes COSE_Sign1 completos e assinados para os quatro contratos e snapshot attestation, com chave de teste pública não operacional, bytes protected headers/payload/Sig_structure, `kid`, audience, digest e assinaturas esperadas; usar duas implementações independentes.

O conjunto negativo deve incluir: payload vazio válido/inválido por schema; chaves de mapa em ordem correta e incorreta; integers nas fronteiras `2^32-1`, `2^64-1` e rejeição fora delas; Unicode NFC, forma decomposta, UTF-8 inválido e surrogate; float, NaN, tag e indefinite-length; chaves duplicadas; cada limite no valor e um byte acima; digest para cada contrato; assinatura válida, bit flip, `kid` alterado, audience/versão/content type alterados, assinatura com chave errada e downgrade. Vetores incluem bytes hex, payload legível e resultado esperado.

## 3. Raiz de confiança, chaves e snapshots

### 3.1 Bootstrap/rotação propostos

Cada domínio (Broker, TEG, autoridade SDD e âncora/witnesses) tem raiz e chaves de workload distintas. Trust store inicial contém fingerprint da raiz, domínio, finalidade, epoch inicial e algoritmo; provisionamento ocorre por canal administrativo independente, autenticado fora do host agente e conferido por duas pessoas em canais distintos. O método concreto de canal e custódia permanece decisão humana.

Manifesto de chaves é COSE_Sign1 pela raiz do domínio, com `domain_id`, `epoch`, `previous_manifest_digest`, chaves (`kid`, algoritmo único, finalidade, validade), audiences autorizadas e lista de revogações. Epoch deve crescer exatamente; verificador persiste maior epoch/root externa observada e rejeita rollback, fork, reutilização de `kid` com material diferente e chave fora da finalidade. Troca ordinária: manifesto assinado pela raiz vigente, publicação fora de banda e checkpoint em âncora independente antes do uso. Revogação de chave usada para novos efeitos exige status online; offline serve apenas para análise histórica com estado de revogação explicitamente limitado ao último checkpoint conhecido.

Comprometimento de raiz: suspender o domínio e efeitos, não confiar em manifesto assinado pela raiz suspeita; congelar evidências externas; revogar identidade no domínio independente; recovery por cerimônia administrativa dupla que verifica âncoras e emite nova raiz/epoch. Restore sem prova de monotonicidade fica em quarentena. A independência e a disponibilidade de tais meios não estão demonstradas.

### 3.2 Snapshot aceito e autoridade

Proposta: conteúdo canônico não é “aceito” por `AcceptedSnapshot` apenas conter `accepted_by`/`accepted_at`. O domínio de produto ainda deve designar a **autoridade de aceitação SDD**; recomendação é serviço Broker após consentimento humano, mas isso não está aprovado. Essa autoridade emite `SnapshotAcceptanceAttestation` COSE assinado, com `attestation_id`, `snapshot_id`, `kind`, `project_id`, `revision_id`, `schema_version`, `content_digest`, `provenance_digest`, `parent_attestation_digest` ou marcador genesis, `accepted_by` (principal estável, não texto livre), `decision_event_digest`, `policy_epoch`, `issued_at`, `issuer` e `audience`. A atestação é imutável e declara somente aceitação no instante assinado; revogação/suspensão é evento assinado separado, append-only, com `status_sequence`, `previous_status_digest` e consulta online.

`ExecutionAuthorization` inclui digest da atestação de cada snapshot e conjunto ordenado de snapshot digests; `ExecutionBinding` inclui os mesmos digests e `snapshot_set_digest`. O Broker verifica que a decisão de consentimento referencia os mesmos bytes/snapshot. O TEG resolve a atestação, verifica assinatura/cadeia/parent e status/revogação online imediatamente antes do permit. Nova revisão não reescreve a anterior: marca dependências/evidências incompatíveis para revalidação explícita. Aceitação de uma nova revisão não atualiza automaticamente grants ou permits antigos.

## 4. Contrato proposto de StartPermit e estados

### 4.1 `StartPermitV1`

Mensagem COSE_Sign1 do Broker, audience exata do TEG (`teg_id` e identidade de workload), com schema fechado:

| Campo | Regra proposta |
| --- | --- |
| `protocol_version`, `contract_version` | Exatamente versões suportadas; desconhecida = reject. |
| `permit_id`, `jti` | IDs distintos e únicos; `jti` nonce aleatório de pelo menos 256 bits, armazenado como digest para replay. |
| `issuer`, `audience`, `key_id` | Broker autorizado e TEG único; `key_id` também protegido no header COSE. |
| `operation_id`, `action`, `target_id` | Operação/ação/destino exatos e allowlisted; nenhuma string livre autoritativa. |
| `snapshot_set_digest`, `acceptance_attestation_digests` | Digest do conjunto ordenado e atestações verificadas. |
| `authorization_digest`, `binding_digest` | Exatos envelopes verificados para esta operação e este TEG. |
| `reservation_digest`, `confirmation_digest`, `consumption_sequence` | Evidenciam reserva e confirmação únicas; consumos divergentes/repetidos recusados. |
| `scope_digest`, `budget_digest`, `payload_digest`, `provider_id`, `model_id` | Valores idênticos ao binding e à policy autorizada. |
| `revocation_epoch`, `issued_at`, `not_before`, `expires_at` | Epoch vigente; `issued_at ≤ not_before < expires_at`; prazo curto limitado pela policy aprovada. |
| `single_effect`, `effect_idempotency_key_digest` | Permite uma única invocação tipada; chave não muda em reconciliação. |

O Broker assina somente depois de transação serializável verificar autorização não expirada/revogada, confirmação de consumo, binding, audience, aceite de snapshots e ausência de permit prévio incompatível. O ponto de linearização do permit é o commit atômico do registro one-use no Broker; a resposta perdida é consultada por `operation_id` + request digest, nunca reemitida com nova identidade. O TEG aceita uma única vez, persiste o permit/digest e nonce, valida assinatura, audience, revocation epoch, janela de tempo e todos os vínculos. Campo divergente, repetido, expirado, issuer/chave desconhecida ou falha de consulta/status bloqueia.

### 4.2 Máquina de estados normativa proposta

```text
snapshot_proposed
  -> snapshot_accepted (atestação assinada)
  -> authorization_issued
  -> reservation_committed
  -> consumption_confirmed
  -> start_permit_committed
  -> permit_anchored (checkpoint pré-efeito durável)
  -> effect_possible (marcado duravelmente antes do envio)
  -> receipt_observed
  -> terminal_anchored

Qualquer timeout/perda após um commit cujo resultado não possa ser consultado:
  -> unknown -> reconcile_only -> terminal_anchored ou quarantined
Revogação/expiração antes do permit: revoked/expired; sem chamada.
Revogação após o permit: permit_in_flight; não é cancelamento.
Conflito de digest, epoch, sequência, receipt ou âncora: quarantined.
```

| Transição | Ponto de linearização proposto | Falha/recuperação |
| --- | --- | --- |
| Aceitação do snapshot | Commit append-only da atestação na autoridade designada | Resposta perdida: consultar ID/digest existente; não reapresentar decisão como nova. |
| Autorização | Commit Broker da decisão e `authorization_id` one-use | Query do mesmo operation/request; estado cliente unknown. |
| Reserva | Commit único Broker para gateway+binding | Duplicado igual retorna estado; conteúdo divergente conflita. |
| Confirmação de consumo | Commit atômico no Broker da reserva consumida e event digest | Concorrência com revogação/expiry segue ordem serializável; timeout => consulta, sem executar. |
| Revogação | Commit do epoch/evento no Broker | Bloqueia novas confirmações e permits posteriores; não apaga artefatos anteriores. |
| StartPermit | Commit de nonce/permit no Broker, em transação ordenada com revogação e expiração | Se revogação ou expiração comita antes, negar; se permit antes, estado in-flight até reconciliar. |
| Ancoragem pré-efeito | ACK de inclusão/continuidade com testemunhas exigidas, validado pelo TEG | Sem ACK completo: zero chamada; resposta perdida consulta mesma sequence+digest. |
| `effect_possible` | Commit durável local TEG após validar permit e ACK e imediatamente antes da chamada | Crash depois deste ponto é unknown, mesmo se chamada possivelmente não ocorreu. Nunca retry automático. |
| Efeito no provider | Commit definido pelo provider, se exposto | Não há linearização conjunta Broker/TEG/provider. Só evidência/query do provider pode confirmar commit remoto. |
| Receipt/terminal | Persistência TEG; sucesso observável somente após validação e checkpoint pós-efeito exigido | Divergência, falta de evidência ou anchor indisponível: unknown/quarentena. |

### 4.3 Corridas e limite de revogação

1. **Revogação vs consumo:** ambos serializam no store Broker. Revogação commit primeiro → consumo recusado. Consumo commit primeiro → consumo não equivale a permissão para iniciar; Broker ainda verifica revogação antes de emitir permit.
2. **Revogação vs permit:** revogação commit primeiro → nenhum permit. Permit commit primeiro → revogação ACK declara explicitamente `in_flight` e impede permits futuros; não declara que efeito foi cancelado.
3. **Expiração vs confirmação:** usar `valid_until` e relógio confiável do domínio Broker. Confirmação commit precisa ocorrer estritamente antes do prazo, com margem de incerteza configurada; igualdade, relógio indisponível ou incerteza fora da tolerância → deny/unknown conservador.
4. **Expiração vs permit/âncora:** permit deve ainda estar válido no ACK pré-efeito e imediatamente antes de `effect_possible`; caso expire antes, zero call. Depois de `effect_possible`, expiração não cancela o efeito.
5. **Corrida revogação depois da verificação no TEG:** há janela inevitável entre última verificação e commit remoto do provider, salvo fencing token/revogação atômica suportados pelo provider. O comportamento padrão explicitamente aceita permit linearizado antes da revogação como in-flight, ou bloqueia provider sem fencing se a policy exigir revogação instantânea. mTLS/assinatura não elimina a janela.
6. **Falha/restart em qualquer estado após permit:** recuperar por consulta read-only ao Broker, âncora e provider. Sem prova forte de ausência/presença, preservar `unknown`; sem retry, nonce novo ou nova operation ID automática. Operação manual de retry exige decisão humana e nova autorização, depois de evidência que exclua efeito duplicado.

Fonte temporal recomendada: clock monitorado do domínio Broker/TEG; não relógio do cliente. Sugestão R3 de skew ±2 s e TTL máximo 120 s não é adotada aqui como valor normativo. H-05 deve fixar fonte, limites, monotonic clock para durações, comportamento de suspensão/VM e janela conservadora. Nenhum perfil pode inferir validade a partir de timestamps locais não confiáveis.

## 5. Protocolo normativo proposto de receipts

### 5.1 Autoridade e tipos

- `GatewayExecutionReceipt` é a atestação primária do TEG sobre o que seu handler tipado observou/retornou. É assinada pela identidade do TEG, contém `receipt_id`, operation, authorization, binding, snapshot set, reservation, confirmation, permit, provider/model/target, effect key, início/fim, status, `evidence_class`, `provider_evidence_digest` opcional, `previous_receipt_digest` e versão.
- `ExecutionReceipt` é apenas projeção de compatibilidade do Gateway receipt, não uma atestação independente nem uma segunda fonte de estado. Acrescenta `source_gateway_receipt_digest` e identifica o mesmo issuer, audience/operation e `receipt_id` de origem. Projeção determinística retém `operation_id`, intent/auth/binding digests, target, executor/version, payload digest, status/tempos, effect key, result digest. Campos sem fonte equivalente ficam ausentes conforme schema; não são inferidos.
- Evidência do provider é entidade separada: receipt/API de consulta assinado pelo provider ou registro independente verificável, vinculado a `effect_key` e operação. Se não houver suporte do provider, classificar como `provider_evidence=unavailable`; receipt TEG continua provando somente que o TEG relata uma chamada/resultado.

O modelo distingue `handler_outcome` (`succeeded`, `failed_before_effect`, `failed_verified`, `unknown`) de `provider_effect_status` (`confirmed`, `rejected`, `unknown`, `unavailable`). O receipt assinado do TEG prova o relato do Gateway e a ligação ao permit; não comprova sozinho que um efeito remoto foi comitado. UI/API de auditoria só pode rotular `external_effect_confirmed` quando `provider_effect_status=confirmed` está apoiado por evidência independente autenticada. Se o provider não oferece isso, o resultado externo fica `unknown` ou `unavailable`, mesmo que o handler tenha recebido resposta de sucesso.

### 5.2 Verificação/divergência

Verifier checa assinatura e chave vigente, audience, cadeia de autorização/binding/permit/snapshot/consumo/anchor, status temporal e replay. A projeção deve ser byte-for-byte reconstruível a partir do Gateway receipt e rejeitada se qualquer campo ou digest não corresponder. Um recibo não sobrescreve o outro nem ganha precedência por status mais favorável. Duplicidade de `receipt_id`, cardinalidade não 1:1, assinatura inválida, digest divergente, timestamps incompatíveis ou provider evidence divergente → `unknown`, quarentena e diagnóstico auditável. Correção é novo evento assinado de reconciliação, jamais edição do receipt anterior.

## 6. Ancoragem de intenção e efeito

### 6.1 Ordem de checkpoints proposta

Não emitir um permit e depois depender de checkpoint que ainda não o inclui para autorizar efeito. Um único checkpoint pré-efeito resolve o vínculo sem transação distribuída:

1. Broker registra autorização, reserva, consumo e StartPermit em commits independentes idempotentes. TEG persiste cada resposta e registra `effect_prepared`; isso não executa efeito.
2. TEG constrói **um** `PreEffectCheckpointV1` contendo os digests exatos de snapshot attestations/set, intent, authorization, binding, reservation, consumption confirmation, StartPermit, payload/destino/policy, handler version, operation ID pseudonimizado, TEG identity/epoch, sequência anterior/head local e phase. TEG assina o checkpoint.
3. Âncora aceita append-only e retorna receipt assinado com checkpoint digest, stream/sequence, tree size/root, índice e inclusion proof, consistency proof desde a última root pinada, anchor key/epoch e confirmação de durabilidade. Witnesses independentes confirmam root/quórum conforme política aprovada. TEG valida tudo e persiste o ACK.
4. Imediatamente antes de chamar, TEG verifica novamente validade temporal/permit/epoch e destino. Persiste `effect_possible` com referência ao ACK e faz uma única chamada typed-handler. Há ainda uma janela entre essa gravação e o commit no provider; queda vira unknown. Nenhuma arquitetura cliente pode eliminá-la sem idempotência/fencing/transação do provider.
5. Depois da resposta, TEG persiste `GatewayExecutionReceipt`, anexa evidência externa do provider, quando verificável, e publica `PostEffectCheckpointV1` que referencia o pré-efeito, receipt e provider evidence. Sucesso terminal só é reportável conforme policy após ACK pós-efeito/verificações requeridas. Se o efeito pode ter ocorrido mas checkpoint/resposta não confirma, unknown/quarentena.

Checkpoint único **não** é transação conjunta: intenção e permit já estão duráveis nos domínios de origem. O checkpoint prova que ambos os digests foram incluídos antes do efeito, não que os serviços fizeram commit atomizado. Um segundo checkpoint imediatamente antes do provider não é necessário se o pre-effect inclui permit e o TEG só chama depois do ACK; um segundo seria duplicado e aumentaria disponibilidade/custo. O checkpoint pós-efeito continua distinto porque ainda não existe receipt antes do efeito.

### 6.2 Campos e provas

`PreEffectCheckpointV1`: `schema_version`, `checkpoint_id`, `stream_id`, `stream_epoch`, `sequence`, `previous_anchor_receipt_digest`, `previous_tree_root/tree_size`, `journal_head_digest`, `phase=pre_effect`, pseudonymous `operation_id_digest`, `snapshot_set_digest`, ordered `snapshot_acceptance_attestation_digests`, `intent_digest`, `authorization_digest`, `binding_digest`, `reservation_digest`, `confirmation_digest`, `start_permit_digest`, `payload_digest`, `policy_digest`, `handler_id/version`, `provider_id`, `target_id`, `budget_digest`, `TEG issuer/key_id`, `created_at` e domain separator. Campos de conteúdo, prompt, segredo, assertions e credencial são proibidos.

`PostEffectCheckpointV1`: predecessor = receipt pré-efeito; mesma identidade/sequence monotônica; `phase=post_effect`; `gateway_receipt_digest`, `execution_receipt_projection_digest`, `provider_evidence_digest`/classe, outcome class (sem segredo), e timestamp. Checkpoints de recovery/key rotation possuem schema/phase próprio e não alteram os anteriores.

Anchor receipt é assinatura do serviço de log sobre leaf digest, tree size/root e inclusion path. Consistency proof vincula root já pinada à nova root. Witnesses verificam e assinam (log ID, size, root, epoch, predecessor); quórum de testemunhas sob domínios administrativos independentes deve ser definido antes de dizer `durable quorum`. Um serviço isolado ou `durable=true` autoafirmado não prova ausência de split view. Verifiers guardam a maior tree size/root fora do host agente, com checkpoint externo de witness; root menor, duas roots para mesmo size ou proof inválida → quarantine.

### 6.3 Falhas, retenção e restore

- Âncora/witness indisponível antes do ACK pré-efeito: não executar, não enfileirar para auto-dispatch; estado preparado/blocked.
- ACK enviado e resposta perdida: consultar mesma `checkpoint_id`/sequence/digest; repost idêntico pode ser idempotente, conteúdo diferente é fork e quarentena.
- Falha após pre-effect ACK e antes de chamada: após restart tratar conservadoramente como unknown, salvo prova interna verificável de que `effect_possible` não foi persistido e permit segue válido; sem prova, query provider, nunca retry.
- Falha após possível commit remoto ou após efeito antes do post-effect ACK: unknown; apenas query/reconcile, nunca repetir efeito. Anchor offline não rebaixa receipt local para sucesso externo.
- Restore parcial ou root/epoch menor: comparação com witness independente, quarentena e recuperação append-only; nunca restaurar a âncora junto com o store a um ponto anterior para “consertar” divergência.
- Retenção deve cobrir períodos de autorização, disputa, backup, obrigação legal e continuidade; números, custo, região, legal hold e eliminação criptográfica permanecem H-07. Sem política de retenção aprovada, não iniciar armazenamento operacional.

## 7. Grafo de dependências H-01–H-10

```text
H-01 ──┬──> H-02 ──┐
       ├──> H-03 ──┼──> H-04 ──┐
       └──> H-05 ──┘           ├──> H-06 ──> H-07 ──┐
H-03 ──────────────────────────┘                    ├──> H-08 ──> H-09 ──> H-10
H-02 ───────────────────────────────> H-06          │
H-03 ───────────────────────────────────────> H-07  │
H-04/H-05 ────────────────────────────────> H-06   │
H-01...H-07 ───────────────────────────────────────> H-08
H-01...H-08 + avaliador independente ─────────────> H-09
H-01...H-09 + autoridade de risco/recursos ───────> H-10
```

| H | Conteúdo e gate de entrada | Saída/condição de dependência |
| --- | --- | --- |
| H-01 | Codec, assinatura, hashes, schemas, limites e migração | Perfil fechado + revisão independente; início do restante. |
| H-02 | Autoridade e semântica/projeção de receipts | Fonte de verdade e regras de mismatch, depende H-01. |
| H-03 | Raízes, manifests, KMS, rotação, revogação, recovery | Custódia e bootstrap demonstráveis, depende H-01. |
| H-04 | Revogação, consumo e StartPermit | Máquina serializável e fronteira in-flight, depende H-01/H-03/H-05. |
| H-05 | Relógios, tolerância, TTL, expiry | Fonte e fail-closed verificáveis, depende perfil de mensagens H-01. Pode ser decidido em paralelo ao desenho de H-03, antes de H-04. |
| H-06 | Âncora, proof, witness/quórum, continuidade | Protocolo end-to-end pré/pós-efeito, depende H-01–H-05. |
| H-07 | Retenção, privacidade, custos, backup/restore | Política quantitativa compatível com H-02/H-06. |
| H-08 | Infraestrutura, operadores, identidades e recursos | Ambiente independente aprovado; decisões de H-03–H-07 concretizadas. |
| H-09 | Campanha C-01–C-21, quantidades, evidências e evaluator | Plano aprovado e recursos do H-08 confirmados. |
| H-10 | Decisão go/no-go para solicitar RB-09C.2 | Requer H-01–H-09 resolvidos, relatório independente e autorização humana específica. Não autoriza automaticamente deploy, credentials, enrollment ou RB-09C.3+. |

Dependências adicionais: H-03 e H-05 podem avançar em paralelo após H-01; H-02 também depende do schema assinado H-01. H-04 exige ambos H-03 e H-05. H-06 deve incluir schema de receipts H-02 e estados/revogação H-04. O grafo não converte recomendação em aprovação.

## 8. Campanha adversarial consolidada C-01–C-21 (proposta)

Todos os ensaios usam ambiente descartável, dados sintéticos e sem credenciais de produção. “Pass” exige logs externos independentes e ausência de efeito não autorizado; telemetria ausente/indisponível significa inconclusivo. Para corridas, barreira reutilizável garante que processos independentes cheguem ao ponto de commit; registrar IDs de processo, transaction IDs e ordem do store autoritativo. Nenhum teste unitário/simulador prova independência operacional.

| Cenário | Quantidade e procedimento/injeção fixos | Evidência externa e critério pass/fail | Limite/falso positivo |
| --- | --- | --- | --- |
| C-01 Host comprometido | 1 VM cliente, 1 identidade hostil, 10 processos, 100 intents inválidas/replay; tentar arquivos, IPC, APIs e iniciar binário cliente alterado. | IAM/ACL, logs fora do cliente, anchor e provider: 100 recusas, zero segredo/efeito acessível. Qualquer efeito ou falta de evidência = fail/inconclusivo. | Cobertura limitada às rotas inventariadas. |
| C-02 Bypass provider | 5 caminhos (DNS, IP, proxy, credencial alternativa, endpoint alternativo), 10 tentativas cada direto do cliente. | Provider/IAM/firewall: zero request autenticada/efeito do cliente; controle autorizado somente sob identidade TEG. | Endpoint não incluído pode criar falso senso de cobertura; inventário incompleto reprova. |
| C-03 Roubo/replay de autorização | 1 autorização; 10 replays same TEG, 10 cross-TEG, 10 pós-expiração e 8 mutações de campo, cada mutação repetida 10 vezes = 110 recusas. | Journals Broker/TEG, anchor e provider ledger: no máximo um permit/efeito; zero replays aceitos. | Chave TEG roubada fora do escopo precisa ser reportada. |
| C-04 Duplicação concorrente | 10 operation IDs; cada um recebe barreira de 64 requests idênticas + 16 divergentes, em processos independentes; replay após restart. | Store Broker/TEG e provider: um consumo máximo; divergências conflitam; um efeito no máximo se provider comprova idempotência, senão unknown. | Scheduler serial pode mascarar corrida; provar processos simultâneos. |
| C-05 Revogação em fronteiras | 100 operações em cada um de 5 pontos (antes da reserva, após reserva, após consumo, após preparação, corrida de emissão de permit) = 500 corridas; atraso adversarial determinístico de 0–100 ms. | Ordem serializável Broker, permit, anchor e provider: revoke antes do permit = zero efeito; permit primeiro = in-flight, sem promessa de cancelamento; zero permit novo após ACK. | Sem timestamp/sequence autoritativo, resultado inconclusivo. |
| C-06 Crash entre persistência e efeito | 5 pontos (antes de preparação persistida, após preparação, após permit, após pre-anchor ACK, após marcar `effect_possible`), 10 falhas por ponto = 50. | Provider ledger + stores/anchor: unknown onde há ambiguidade, zero retry; só falha pré-efeito com prova. | “Not found” eventual do provider não prova ausência. |
| C-07 Perda de resposta | 6 fronteiras (Broker autorização, reserva, consumo, anchor pre, provider, anchor post), 10 drops após commit por fronteira = 60. | Logs do servidor/anchor/provider: mesma identidade consultada ou unknown; nenhuma operação nova/efeito repetido. | Proxy precisa provar request chegou e commitou. |
| C-08 Divergência snapshot/store | 8 mutações: snapshot content, revision, parent, acceptance attestation, intent, binding, authorization, confirmation; uma fixture por mutação e replay de consulta. | Fonte SDD, Broker/TEG, anchor: 8/8 bloqueiam e alertam, sem correção cliente. | Comprometimento simultâneo da fonte autoritativa não é provado. |
| C-09 Restore parcial | 4 combinações (Broker antigo, TEG antigo, ambos antigos, heads distintos), em cada uma 1 query + 1 tentativa bloqueada de permit. | Anchor/witness e backup manifests: 4/4 rollback detectado, zero efeito, recovery append-only. | Restore da própria âncora compromissada demanda C-20. |
| C-10 Journal adulterado | 6 mutações (remover, reordenar, editar, duplicar, recalcular cadeia local, restaurar antigo), cada uma verificada contra âncora. | Proof externa: 6/6 detectadas/quarentenadas; janela pré-anchor é explicitamente limitada e não pode conter efeito autorizado. | Hash chain local sem referência externa é insuficiente. |
| C-11 Falha WebAuthn | 8 casos (UV/UP ausente, origin/RP errado, challenge expirado/replay, credential revogada, cancelamento), 10 vezes = 80. | Broker decision journal: zero autorização positiva; sem assertion armazenada/logada. | Simulador não comprova consentimento/UI real. |
| C-12 Substituição de apresentação | 8 campos críticos (action, target, provider, model, scope, budget, revision, digest), 10 mutações cada = 80; incluir Unicode, markup e conteúdo longo. | Digest entregue ao dispositivo/UI, challenge e binding: todas divergências bloqueadas; sem truncamento/injeção. | Screenshot não prova percepção humana; coletar versão/viewport e avaliação independente. |
| C-13 Comprometimento cruzado de admins | 3 domínios (Broker, TEG, anchor), 1 operador atacado por campanha, 10 tentativas cruzadas por domínio; simular break-glass. | IAM/control plane, vault, backups e logs administrados fora do alvo: zero acesso cruzado não autorizado; break-glass dupla custódia e expiração. | Superadmin/supply chain comum precisa estar explicitamente no threat model. |
| C-14 Broker indisponível | 30 min, 30 tentativas (1/min), falha antes de reserva e entre reserva/consumo; reconectar sem novo comando. | Métricas e provider ledger: 0 efeitos enquanto indisponível; nenhuma fila dispara na recuperação. | DNS split/partição exige topologia representativa. |
| C-15 Âncora indisponível | 30 min, 30 tentativas; ensaio A falha pre-ACK; ensaio B permite sandbox effect e perde resposta post-ACK, 10 repetições. | Anchor/provider independente: A zero efeito; B unknown até reconciliação, sem retry; logs sem lacunas. | Se serviço de evidência também indisponível, resultado inconclusivo, não pass. |
| C-16 Relógios divergentes | 7 offsets (-3,-2,-1,0,+1,+2,+3 s) × 3 pontos (antes de not_before, igualdade, expiração) × 3 repetições = 63. | Clock monitor, decisões Broker/TEG e ordem assinada: decisão determinística; fora do limite/tempo indisponível bloqueia. | NTP sozinho não prova tempo; testar suspend/VM pause no SO real. |
| C-17 Consumo e revogação concorrentes | 100 barreiras de confirmação-vs-revogação e 100 de permit-vs-revogação; incluir resposta perdida em 20 de cada subgrupo. São ensaios focados adicionais a C-05. | Transaction IDs, event journal, anchor, provider: commit order define resultado; sem segundo consumo/permit/efeito. | Fixture single-thread não serve; usar processos independentes. |
| C-18 Commit remoto do provider | 10 commits remotos identificados; em cada um derrubar response e matar TEG antes de receipt/pós-âncora; somente query/reconcile no restart. | Ledger/API fortemente consistente do provider: mesmo effect ID, sem segunda chamada; sem prova = permanece unknown. | Não inferir execução pelo receipt local ou por ausência de nova linha local. |
| C-19 Divergência de receipts | 100 fixtures: mutar por vez issuer, operation, auth, binding, permit, status, effect key, result/provider evidence, projection digest e timestamp; 10 casos por dimensão. | Verificador independente: todas divergências unknown/quarantine; nenhum receipt “mais favorável” prevalece. | Parser compartilhado pode mascarar bug; segunda implementação necessária. |
| C-20 Split view e rollback da âncora | 30 execuções: 15 apresentar roots conflitantes para mesmo tree size a dois witnesses e 15 restaurar root/epoch menor; incluir witness indisponível em 5. | Witnesses externos e root pin: todo fork/rollback detectado; sem quorum não há ACK pré-efeito. | Witnesses sob control plane comum não são independentes; auditar administração real. |
| C-21 Expiração vs confirmação | 100 corridas com barreira na transação de consumo/confirmação: 50 expiry/revoke antes do commit, 50 commit antes do limite; clock boundary inclui igualdade. | Relógio Broker monitorado e journal transacional: primeiro grupo negado; segundo só se commit comprovadamente antes do prazo e dentro da margem; demais unknown/deny. | Clock incerto ou ordem indistinguível não pode ser contado como pass. |

Quantidade e ambiente acima são proposta R5, não homologação executada nem autorização de infraestrutura. Resultados precisam ser `passed`, `failed` ou `inconclusive`, com commit/order IDs, versões, hashes de evidência e relatório independente. Nenhuma ausência de evento substitui prova de ausência de efeito.

## 9. Decisões humanas pendentes

| Decisão | Alternativas | Recomendação proposta | Risco residual | Dependências | Critério para decidir | Responsável |
| --- | --- | --- | --- | --- | --- | --- |
| H-01 perfil wire | COSE/deterministic CBOR vs JWS/JSON canonicalizado; Ed25519 vs algoritmo aprovado alternativo | AF-COSE-1 conforme §2, sujeito a análise criptográfica e suporte de KMS | Codec/algoritmo pode não ser suportado ou aprovado em ambiente escolhido | F1, HSM/KMS | vetores independentes, review e política de migração | Papel criptografia/plataforma a designar |
| H-02 receipts | Gateway receipt primário + projeção; consolidar em um tipo | §5, source de verdade Gateway receipt com provider evidence separada | TEG comprometido pode mentir; provider pode não oferecer prova | H-01, handler/provider | projeção determinística e semântica aprovada | Protocolo/auditor a designar |
| H-03 trust keys | KMS/HSM/TPM, raiz offline, workload identity | Raízes por domínio, epoch e bootstrap dual-control da §3 | Control plane superior compartilhado e break-glass | H-01 | recuperação, rotação e evidência de independência | Operadores independentes a designar |
| H-04 revogação | Revoga novas reservas/permits; provider fencing; best effort cancel | Serializar emissão do permit; estado in-flight explícito | Efeito pode iniciar após ACK de revogação se permit já linearizou | H-03/H-05 | cenários C-05/C-17/C-21 e aceitação explícita da janela | Dono de risco a designar |
| H-05 tempo | fonte de tempo, skew, TTL | relógio confiável do domínio + deny conservador | indisponibilidade e particionamento | H-01, operação alvo | precisão/limites e ensaios C-16/C-21 | SRE/segurança a designar |
| H-06 anchor | witness log, quórum, Merkle, ACK pre/post | PreEffectCheckpoint único com permit e receipt pós-efeito; witness independente | custo/latência, split view residual | H-01–H-05 | prova externa C-20 e política de quorum | Auditor/plataforma a designar |
| H-07 retenção/custo | retenção fixa, legal hold, eliminação | período compatível com disputa/recovery/lei, ainda sem números | dados sensíveis/pseudônimos e custos | H-02/H-06 | análise legal, custo e teste de restore | Produto/privacidade/finanças a designar |
| H-08 ambiente | tenant/contas dedicadas, provedor existente, lab isolado | domínios independentes B com device aprovador separado | indisponibilidade, dependência de provedor e operação | H-03–H-07 | owners, recursos e threat model reais | Dono do laboratório a designar |
| H-09 campanha | C-01–C-21 e evidência externa | quantidades R5 fixas para campanha, sem reduzir por falha | custo e falsos positivos | H-01–H-08 | plano reproduzível e evaluator independente | Avaliador/programa a designar |
| H-10 go/no-go | manter bloqueado ou solicitar RB-09C.2 | considerar somente com H-01–H-09 aprovados e relatório independente | aprovação arquitetural não garante segurança operacional | todos anteriores | decisão escrita, recursos e escopo específico | Autoridade de risco/produto a designar |

Nenhum nome, papel, provedor, região, chave ou recurso está escolhido por este documento. mTLS continua identidade técnica; WebAuthn verifica um autenticador/usuário segundo o protocolo, mas a tela independente é necessária para consentimento informado. Nem assinatura nem âncora tornam verdadeiro o conteúdo declarado pelo emissor.

## 10. Revisão independente e critérios para encerrar RB-09C.1

O avaliador independente deve:

1. Conferir que cada campo e digest tem produtor, autoridade, audience, validador e destino definidos; verificar o grafo sem ciclos e compatibilidade da migração v1→v2.
2. Reimplementar decode/canonicalização/digest/verificação em implementação independente e reproduzir todos os vetores; testar versão desconhecida e downgrade.
3. Revisar o modelo de keys/bootstrap/epochs/compromise e confirmar que não existe confiança em chave recebida pelo cliente sem pin externo.
4. Revisar cada ponto de linearização, condição de corrida e impossibilidade de cancelamento; validar regra de unknown e não retry.
5. Conferir projeção/receipt com divergência explícita, sem aceitar receipt local como prova de provider.
6. Demonstrar que `PreEffectCheckpointV1` inclui authorization, binding, reservation, confirmation, permit e snapshot acceptance; ACK/witness precedem chamada de provider; checkpoint pós-efeito não é confundido com pre-effect.
7. Verificar C-01–C-21: quantidade, barreira, failpoint, fonte independente, pass/fail, limites e inconclusivo. Aprovar plano antes de provisionar laboratório.
8. Identificar todas as garantias que dependem de decisão operacional e manter esses itens marcados como pendentes.

RB-09C.1 só pode ser encerrada após: R4 F1–F9 reavaliados sem achado MAJOR aberto; R1 nota histórica consistente; R5 e ADR 0008 sem contradição; contratos e DAG aprovados explicitamente pelos responsáveis; e decisão humana registrada para os H aplicáveis. Este documento sozinho não encerra a fase nem inicia RB-09C.2.

## 11. Estado e limites

- **Aprovado:** Alternativa B no ADR 0008 R2, arquitetura-alvo administrativa.
- **Proposto, não aprovado:** todas as especificações R5, inclusive COSE/CBOR/Ed25519, StartPermit, receipts, snapshot attestation, checkpoint, testemunhas, time, quantidades e DAG.
- **Não demonstrado:** independência efetiva, UI confiável, autoridade SDD, chaves, âncora, provider enforcement, fencing, commit remoto e durabilidade operacional.
- **Fail-closed:** Broker/TEG, grants e dispatch permanecem inativos; timeout, divergência ou resultado desconhecido não autoriza execução nem repetição automática.
- **Não iniciado/autorizado:** RB-09C.2–RB-09C.8 e RB-10.

Não houve alteração de código/contratos Python, execução de teste operacional, serviço, credencial, enrollment ou operação externa nesta atividade.
