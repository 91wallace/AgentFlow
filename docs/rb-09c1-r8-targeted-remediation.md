# RB-09C.1-R8 — remediação dirigida dos achados R7-B

**Estado: proposta para revisão independente.** Este documento não aprova H-01–H-10, não altera ADR 0008 nem contratos Python e não autoriza RB-09C.2. A arquitetura-alvo B continua aprovada pelo ADR 0008 R2. A correção de `kid` para CBOR `bstr` está preservada no escopo estreito confirmado por R7-A.2.

## 1. Baseline e limites

Baseline observado antes da edição: branch `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`, índice vazio. Já havia alterações rastreadas em documentos de arquitetura, ADRs 0002/0004/0005/0006/0008, plano e progressos das fases 2–7, além de `autonomous-development-runner/` e documentos R1/R3/R5/R6/R7-A/R7-A.1/R7-A.2/R7-B não rastreados. Todos foram preservados. Somente este documento foi criado.

Documentos lidos para esta remediação: ADR 0008; plano R1; R3; R5; R6; R7-A, R7-A.1, R7-A.2 e auditoria R7-B. Os contratos atuais foram considerados apenas para a diferença de estado: Approval/TEG continuam contratos JSON/digest v1; não implementam envelopes COSE, StartPermit, coordinator/fencing nem a projeção proposta. A autoridade TEG padrão segue indisponível. O fluxo local atual não constitui evidência das garantias propostas e não deve ser habilitado operacionalmente antes dos gates futuros.

Este R8 trata **todos** os problemas e limitações registrados na R7-B: B-01, M-01–M-05, F1–F9, os sete cenários C parciais e os critérios transversais. “RESOLVED_PROPOSED” abaixo quer dizer que há texto suficientemente específico para nova deliberação; não quer dizer aprovado, implementado ou homologado.

## 2. Matriz completa dos achados R7-B

| ID R7-B / referência | Lacuna remediada e proposta mínima R8 | Aceitação objetiva proposta | H / estado documental |
|---|---|---|---|
| **B-01**, R7-B §3, R6 §§9–10, C-20 | Quorum 3/4 e quatro witnesses são mantidos. C-20 é substituído por subcasos isolados descritos na §3; uma indisponibilidade não é ausência de quorum. | Campanha registra quatro identidades, respostas assinadas, tree size/root/epoch e receipt durável por tentativa. Um down permite três votos consistentes; dois down não permitem ACK; conflitos/rollback/timeout ficam sem ACK e em quarentena. | H-06/H-09 OPEN; **RESOLVED_PROPOSED** |
| **M-01**, R7-B §3, R3 §3.2/§4.1, R6 §§3.1/4.2/6 | Separar audience criptográfica (destinatário do envelope) da identidade do TEG autorizado (claim do payload). Binding TEG→Broker é endereçado ao Broker; autorização Broker→TEG service e StartPermit é endereçado à instância/lease exata. Ver §4. | Vetores positivos para destino correto e negativos para Broker, serviço, instância e ambiente divergentes; receptor rejeita mesmo com assinatura válida. | H-01/H-03/H-08 OPEN; **RESOLVED_PROPOSED** |
| **M-02**, R7-B §3, R6 §§6–8/10, C-04/C-09 | Introduzir proposta de Execution Coordination/Fencing Authority (ECA), estado linearizável por operação/slot, epoch monotônico e consumo único do StartPermit. Clone/restore não prova ausência de efeito; sem fencing provider ou prova conclusiva, a operação fica `unknown` e inelegível para nova execução. Ver §5. | Duas cópias concorrentes não obtêm fencing válido; epoch antigo é recusado; timeout depois de claim não cria nova geração nem chamada; provider sem idempotência/fencing permanece bloqueado em unknown. | H-03/H-04/H-08/H-09 OPEN; **PARTIAL** até escolha e autoridade efetiva |
| **M-03**, R7-B §3, R3 §§5.2–5.4, R6 §6/H-04 | Diferenciar pedido de revogação, barreira de novos starts e quiescência de operações já in-flight. Recomenda-se não retornar `revocation_confirmed_quiescent` enquanto existir start/efeito unresolved; sem prova, manter `revocation_pending/unknown`. Não prometer cancelamento retroativo. Ver §6. | Barreiras entre cada estado e revogação provam a ordem; ACK quiescente só após todos os slots encerrados ou cancelamento confirmado pelo provider; perda de comunicação não produz ACK falso. | H-04 OPEN; **RESOLVED_PROPOSED** como semântica conservadora, sujeita a decisão |
| **M-04**, R7-B §3, R6 §§4.3–4.4/8 | Fechar campos e projeção GER→ER campo a campo, distinguindo estado do handler e efeito externo. Acrescentar provider status/evidence class como campos explícitos ao schema proposto; vetores R6 ficam históricos, não schema completo. Ver §7. | Quatro estados de provider, nulls e divergências reproduzem projeção deterministicamente; receipt local sem evidência externa nunca produz confirmação externa. | H-02 OPEN; **RESOLVED_PROPOSED** |
| **M-05**, R7-B §3, ADR 0008 R2, R1/R3/R5/R6/R7-A.2 | Fixar precedência e status documental na §8. R6 não se torna norma só por ser mais recente; R8 continua proposta. | Revisor identifica uma única base para código atual e uma única proposta em deliberação por contrato, sem reescrever decisão aprovada. | H-01–H-10 OPEN conforme domínio; **RESOLVED_PROPOSED** |
| **F1**, R7-B §§2, 6–7; R6 §§3–4; R7-A/A.1/A.2 | Preservar `kid=bstr`; não alegar backend Ed25519 independente. A verificação `pycose` é independente para COSE/CBOR, mas usa `cryptography`, também usado por R7-A. Completar schema byte-a-byte e vetores negativos antes de H-01. | Auditor independente reproduz versão de envelope aprovada; backend compartilhado é claramente registrado ou outra implementação/backend independente é selecionado por decisão H-01. | H-01 OPEN; **PARTIAL** |
| **F2**, R7-B §§2,6; R6 §5/C-22 | Manter bootstrap externo, epochs e recuperação 3/4 como proposta; congelar o domínio comprometido; recuperar por trustees/registro independentemente provisionados, nunca por chave comprometida. Especificar a autoridade externa e recuperar a confiança por canal fora do registro suspeito. | Ensaios de raiz substituída, epoch rollback, recovery key comprometida e falha antes/depois da publicação deixam serviços em quarantine até continuidade verificável. | H-03/H-08/H-09 OPEN; **PARTIAL** |
| **F3**, R7-B §§2,6; R6 §§6–7 | Binding/permit referenciam serviço, ambiente, operação, instância e generation. Permit só é resgatado por claim ECA one-use; corrida de revogação segue §6. | Nenhuma audiência, instância, epoch, operation ou provider diferente recebe claim; caso sem fencing termina bloqueado/unknown. | H-01/H-03/H-04/H-05 OPEN; **PARTIAL** |
| **F4**, R7-B §§2,4; R6 §7 | Preservar um checkpoint pré-efeito contendo o permit já emitido e todos os digests de snapshot, intent, autorização, binding, reserva, consumo, lease/fence e destino; confirmar durable anchor antes do efeito; checkpoint pós-efeito separado. | Injeção de falha antes do ACK dá zero chamada; perda de resposta consulta mesma sequência/digest; depois de efeito possível sem prova mantém unknown. | H-06/H-08 OPEN; **PARTIAL** |
| **F5**, R7-B §§2–4; R6 §8 | Aplicar schema/projeção da §7; status provider e receipt TEG permanecem provas distintas. | Projeção inválida, cardinalidade diversa ou campo externo sem evidência resulta unknown/quarantine. | H-02/H-06 OPEN; **RESOLVED_PROPOSED** |
| **F6**, R7-B §§2,5,7; R6 §9 | Substituir critérios contraditórios C-20; detalhar C-01/C-06/C-08/C-12/C-13/C-15/C-22 na §9. Demais cenários mantêm os limites R6 e continuam planos, não resultados. | Matriz fechada com contagem, failpoints, fontes independentes e pass/fail; H-09 aprova a campanha antes de executar. | H-09 OPEN; **PARTIAL** até revisão independente da matriz |
| **F7**, R7-B §§2,4; ADR 0008 fronteira/fonte autoritativa, R6 §11 | Broker permanece autoridade inicial dos snapshots conforme ADR. Atestação proposta vincula conteúdo/revisão/linhagem a evento de aceite humano; TEG valida assinatura e status online. | Snapshot, parent, decisão, epoch ou status divergente impede autorização/claim; aceite do cliente não estabelece autoridade. | H-03/H-08 OPEN; **PARTIAL** |
| **F8**, R7-B §§2–3,5; R6 §10/C-20 | Manter 4 witnesses e 3/4 como modelo proposto; especificar falhas simples, Byzantine/equivocation, roots divergentes, heads e ACK durável em C-20. Independência administrativa continua não comprovada. | Quorum apenas para três assinaturas válidas distintas, mesma root/size/epoch e continuidade válida; qualquer split/rollback detectável leva a quarantine. | H-06/H-08/H-09 OPEN; **PARTIAL** |
| **F9**, R7-B §§2,6; R1.7/R1.8 e ADR 0008 R2 | R1.7/R1.8 continuam marcadas como supersedidas somente quanto à escolha A/B; B aprovada, demais propostas abertas. R8 não altera R1. | Comparação R1 com R2 mostra B vigente e histórico preservado. | Não demanda nova escolha A/B; **PASS documental mantido** |
| **C-01**, R7-B §5; R6 C-01 | Fixar no manifesto de teste todas as interfaces/rotas: filesystem/ACL, IPC, APIs, DNS/IP, proxy/egress, credenciais alternativas, endpoints e processos. “Cada rota” não pode ser definido no momento do teste. | Inventário assinado antes da campanha; 10 processos, 100 intents conforme R6; tentativa em cada interface enumerada e logs externos. Item fora do inventário invalida o teste. | H-08/H-09 OPEN; **RESOLVED_PROPOSED** |
| **C-06**, R7-B §5; R6 C-06 | Definir 5 boundaries: reserva Broker; consumo Broker; permit no Broker; confirmação anchor pré-efeito; receipt/anchor pós-efeito. Em cada boundary, 10 operações, comprovando commit remoto antes de descartar resposta e identificando qual serviço reinicia. | 50 injeções; 5/5 grupos registram IDs de commit, remetente/receptor e query da mesma operation ID; sem novo efeito. | H-09 OPEN; **RESOLVED_PROPOSED** |
| **C-08**, R7-B §5; R6 C-08 | Definir 8 mutações: conteúdo/revision/schema/parent/attestation/intent/binding/policy-status. Fazer 10 fixtures independentes por mutação (80), incluindo tentativa de recalcular digest/assinatura sob chave não autorizada. | 80/80 são bloqueadas pelo Broker/TEG antes do handler; logs do source, verifier, anchor e provider comprovam recusa/zero efeito. | H-01/H-03/H-09 OPEN; **RESOLVED_PROPOSED** |
| **C-12**, R7-B §5; R6 C-12 | Manter 8 campos ×10 =80; fixar viewport, escala, idioma, versão da UI, dispositivo, orientação, acessibilidade e limites de conteúdo. Digest mostrado deve ser inspecionável integralmente ou a operação é negada; captura acompanha o digest exato. | Avaliador independente verifica em dispositivo aprovado que os oito campos são completos, identificáveis e não truncados; 80 mutações invalidam consentimento. Captura não é tratada como prova de compreensão. | H-08/H-09 OPEN; **RESOLVED_PROPOSED** com limite humano explícito |
| **C-13**, R7-B §5; R6 C-13 | Preservar cinco categorias de domínio, mas congelar inventário de todos os principals, grupos, superadmins, control planes, break-glass, recovery, witnesses e operadores. Base: 200 tentativas (5 categorias ×4 categorias-destino distintas ×10 por par dirigido); adicionar 10 por par de principal privilegiado extra; qualquer não enumerado invalida o resultado. | Manifesto IAM lista todos os pares; cada tentativa tem evidência administrada fora do domínio atacado; tabletop não substitui teste negativo real. | H-08/H-09 OPEN; **PARTIAL** até inventário real |
| **C-15**, R7-B §5; R6 C-15 | Ensaio A: anchor indisponível antes do pre-ACK, 30 min/30 tentativas; zero efeitos. Ensaio B: 10 casos divididos em 5 ACK post-effect durável com resposta descartada e 5 indisponibilidades reais antes do post-ACK; apenas query/reconcile. | A: 0 effect IDs. B: 5 queries recuperam o mesmo checkpoint/receipt; 5 operações permanecem unknown; zero retries. Confirmar pelo ledger provider independente. | H-06/H-08/H-09 OPEN; **RESOLVED_PROPOSED** |
| **C-22**, R7-B §5; R6 C-22 | Preservar 30 rejeições (10 manifestos antigos, 10 rollbacks de epoch, 10 certificados inválidos/replay) e 1 recovery válida 3/4; acrescentar 3 failpoints ×5: após revogar raiz suspeita, antes da publicação no registry externo, e após publicação/antes dos pins do verificador. | 46 casos total. Cada failpoint deixa efeito bloqueado; recovery válida só é aceita após prova externa monotônica por canal não dependente da raiz/registry comprometidos. | H-03/H-08/H-09 OPEN; **PARTIAL** até canal/registry independente escolhido |

Para F2/F7/F8 e C-13/C-22, uma especificação não demonstra independência. Para F1, R7-A.2 não demonstra independência criptográfica Ed25519. Os limites ficam abertos e visíveis até decisões e ensaios reais.

## 3. C-20 corrigido — quorum, respostas e heads

### 3.1 Modelo preservado

Modelo proposto permanece em **quatro witnesses**, quorum mínimo **3 assinaturas válidas de 3 identidades distintas (3/4)**. Nenhum ajuste de quorum ou quantidade de witnesses é feito nesta remediação. O argumento de safety é limitado: dois quorums de 3/4 intersectam em pelo menos duas identidades; sob no máximo um witness Byzantine, há ao menos um witness honesto na interseção, que deve persistir monotonicamente e não assinar heads incompatíveis. Isso não prova independência administrativa nem liveness sob duas falhas simultâneas.

Cada assinatura válida cobre `log_id`, `stream_id`, `tree_size`, `tree_root`, `log_epoch`, `previous_root/tree_size`, checkpoint digest, sequence e a decisão durável. Para formar quorum, as três assinaturas devem concordar byte a byte nesses campos, usar chaves/status vigentes, incluir provas de continuidade válidas e referir o mesmo checkpoint persistido duravelmente pelo log. Contar replies sem assinatura/sem confirmação durable não conta como ACK.

### 3.2 Estados de resposta

- **Witness indisponível:** não responde até deadline. Uma indisponibilidade deixa três witnesses; se todos os três válidos concordarem e a âncora provar durable commit, quorum pode ser atingido. Duas indisponibilidades deixam no máximo dois; quorum é impossível e o efeito não inicia.
- **Witness comprometido/Byzantine:** não é modelado como simples indisponível. Uma assinatura pode ser criptograficamente válida e semanticamente conflitante. Ela conta somente para a root exata que assinou; não pode ser tratada como prova de honestidade. Prova de equivocation (duas assinaturas válidas incompatíveis do mesmo witness para mesmo log/size/epoch) causa quarantine do stream e revisão independente, mesmo que uma das roots tenha três assinaturas. Uma resposta inválida é excluída, registrada como falha e não conta para quorum.
- **Resposta inválida:** assinatura, chave/epoch, identidade, log, size, root, sequence, predecessor, inclusion/consistency proof ou checkpoint digest inválido é descartado; só as respostas restantes válidas contam. Três válidas consistentes ainda podem atingir quorum; duas ou menos não.
- **Timeout/ausência de ACK durable:** sem receipt assinado do log, indicação verificável de persistência e quorum confirmando exatamente o mesmo checkpoint, não há permissão para efeito. Se o log pode ter persistido mas a resposta se perdeu, consultar/repetir somente `(log_id, stream_id, sequence, checkpoint_digest)`; jamais alocar sequência nova para a mesma intenção. Até recuperar a confirmação, a execução permanece bloqueada.
- **Split view/head inconsistente:** roots diferentes para mesmo `(log_id, tree_size, epoch)`, predecessor divergente, head menor que o pin confiável ou proof de continuidade inválida causam `quarantined`; não se escolhe automaticamente a maioria nem a resposta “mais recente”. Uma assinatura conflitante é preservada como evidência. A recuperação requer reconciliação por testemunha/auditor independente e novo evento append-only.
- **Limite combinado:** um witness indisponível mais um inválido/Byzantine que se recusa a cooperar pode deixar somente dois votos válidos; safety é mantida por ausência de efeito, mas liveness não é garantida. Sem quorum, nunca se reduz threshold nem se usa cache local como substituto.

### 3.3 Matriz de C-20 e contagem

Para tornar independentes os casos antes agregados, propõe-se ampliar C-20 de 30 para **65 tentativas determinísticas**. Mantêm-se os grupos originais de 15 split-view e 15 rollback; os 35 casos adicionais isolam indisponibilidade, resposta inválida, Byzantine e confirmação durable. A alteração só muda quantidade de ensaios, não o quorum nem o conjunto de quatro witnesses; H-09 deve aprovar o plano quantitativo antes da campanha.

| Subcaso | N | Pré-condição e injeção | Pass | Fail |
|---|---:|---|---|---|
| Controle saudável 4/4 | 5 | Quatro respostas válidas e mesma root/size/epoch/sequence. | Quorum 3/4 confirmado após durable ACK. | ACK sem durable commit, campos divergentes ou menos de três assinaturas verificadas. |
| Um witness indisponível | 5 | Um timeout; três restantes honestos e consistentes. | Três votos válidos formam quorum; log persiste checkpoint antes de ACK. | Rejeitar só porque um witness está offline, ou aceitar sem os três votos/durabilidade. |
| Dois witnesses indisponíveis | 5 | Dois timeouts; dois restantes respondem. | Nenhum quorum; fail closed, zero efeito e estado pendente. | Qualquer ACK/efeito com duas assinaturas ou fallback para cache. |
| Um response inválido | 5 | Uma resposta tem assinatura, key epoch, audience/log ou proof inválida; três restantes concordam. | Resposta inválida é excluída; três válidas + durable ACK podem formar quorum; falha fica auditada. | Contar a resposta inválida, aceitar root divergente ou negar por contagem bruta sem avaliar três válidas. |
| Um witness Byzantine/equivocando | 5 | Uma identidade assina duas roots incompatíveis para mesmo log/tree size/epoch, ou propõe rollback assinado. | Detectar equivocation, preservar prova e pôr stream em quarantine; nenhum efeito durante adjudicação. | Tratar assinatura válida como prova de honestidade, descartar fork ou executar sob root não reconciliada. |
| Split view do log | 15 | Cinco roots conflitantes no mesmo size; cinco sequences/leafs conflitantes; cinco witnesses com heads incompatíveis. | 15/15 detectados; sem ACK pré-efeito para a view conflitante; evidência preservada externamente. | Escolher root por relógio/ordem de chegada ou emitir ACK de view inconsistente. |
| Rollback/continuidade | 15 | Cinco tree size/epoch menores; cinco predecessor mismatch; cinco restore de head antigo. | 15/15 rejeitados/quarentena contra pin externo; recuperação somente append-only após reconcile. | Rollback aceito, sequência reiniciada ou histórico externo sobrescrito. |
| Timeout sem durable confirmation | 5 | Três votos podem existir, mas receipt/flag durable do log é perdido, falso ou resposta cai após commit. | Zero efeito antes da confirmação verificável; query do mesmo ID recupera resultado ou mantém unknown; nenhum novo sequence. | Efeito com timeout/ACK TLS apenas, ou nova submissão que possa duplicar o leaf/efeito. |
| Um offline + uma resposta inválida | 5 | Restam duas respostas válidas, sem quorum. | No ACK nem efeito; falha é distinta de 2 offline, mas resultado fail-closed igual. | Aceitar duas assinaturas ou mascarar o déficit de quorum. |

**Aritmética:** `5+5+5+5+5+15+15+5+5 = 65`. Quórum é sempre `3 de 4`; indisponibilidade de 1 deixa 3 potenciais votos; indisponibilidade de 2 deixa 2, portanto `2 < 3`. Byzantine não é contabilizado como offline: respostas são classificadas por validade e consistência. Sem prova de ACK durável não há efeito, ainda que tenham sido coletadas três mensagens.

## 4. Contrato proposto de `ExecutionBinding` e audiência

Este contrato é proposta wire futura. mTLS autentica o peer do canal; COSE autentica bytes/emissor sob a chave aprovada; audiência e policy continuam verificações separadas. Nenhum deles sozinho prova consentimento.

### 4.1 Envelope do binding (TEG → Broker)

- `issuer_id`: identidade criptográfica do workload TEG registrado, vinculada ao domínio TEG e à chave de assinatura vigente.
- `audience`: identidade exata do endpoint lógico **Approval Broker + ambiente** que recebe o binding, por exemplo `approval-broker:<environment_id>`. Não é `teg:<instance>`; esse valor no vetor R6 §4.2 é histórico de exemplo e não pode ser aceito como audience Broker.
- `gateway_service_id`: serviço TEG autorizado no payload.
- `gateway_instance_id`: instância concreta autenticada pelo canal, com certificado/identidade própria; não é substituto da audience.
- `environment_id`: test/prod/etc. explícito e igual ao peer, key registry, snapshot/policy e endpoint; cross-environment é rejeitado.
- `operation_id`, `effect_slot`, `execution_generation`, `intent_digest`, snapshot-set/attestation digests, `action`, `target_id`, `provider_id`, `model_id`, payload digest, scope/budget/policy digests, checkpoint/path-scope digest, handler/schema/version, destino e expiração compõem o binding exato.
- **Ordem sem ciclo proposta:** TEG cria e autentica um binding sem campo de digest de Authorization; Broker resolve intent/snapshots/policy e emite ExecutionAuthorization contendo `binding_digest`, a audience lógica `gateway_service_id` + ambiente e os limites exatos. StartPermit inclui o digest do envelope completo de Authorization e do binding, além da instância/lease escolhidas. Assim Authorization não é parte do binding de entrada e não há ciclo. Esta ordem substitui, como proposta de deliberação, o vetor R6 Authentication sem binding; não modifica o vetor histórico nem se torna normativa sem H-01.
- Uma Authorization destinada ao serviço TEG não pode ser usada diretamente como permit por qualquer réplica: o coordinator associa grant/operation/slot a uma única instância e epoch. O StartPermit, assinado pelo Broker, tem `audience` exata `teg:<environment>:<service>:<instance>` e vincula `gateway_instance_id`, service id, epoch, generation e fencing token.

### 4.2 Escopo e recusas

O Broker compara o binding com a intent/snapshot aceitos; o TEG compara Authorization e StartPermit ao binding recalculado imediatamente antes da preparação; o coordinator verifica lease/epoch/slot. Divergência em qualquer campo resulta `rejected` antes de efeito ou `unknown/quarantined` se um commit anterior puder ter ocorrido.

Recusar: audience Broker/TEG/ambiente incorreta; assinatura válida emitida para outro receptor; operação/slot/generation reaproveitados com digest diferente; provider/model/target fora da allowlist; binding de instância que não é lease holder; binding para outra revisão/checkpoint/payload; StartPermit repetido, expirado ou já resgatado.

Para réplicas: autorização lógica pode nomear serviço/classe de ambiente, mas cada execução específica recebe uma única geração e lease. Reatribuir para outra réplica exige fence da geração anterior e prova de que o efeito anterior não pode mais ocorrer. Se não houver fence externo confiável ou consulta provider conclusiva, a operação não é elegível a failover e permanece `unknown`.

## 5. Coordenação, fencing, clones e restores

### 5.1 Componente e autoridade

Propõe-se um serviço tipado de **Execution Coordination Authority (ECA)**, fora do domínio hostil do AgentFlow/agente, com identidade, journal e autoridade de escrita próprios. Ele não aprova consentimento, não emite Authorization e não guarda credencial provider. Sua função é serializar posse de lease/fencing por operação e confirmar o resgate único do StartPermit. A implementação, domínio administrativo, disponibilidade e relação operacional com Broker/TEG são decisões H-03/H-08; nenhum serviço já existente é presumido. A âncora de transparência não substitui automaticamente o ECA: logging append-only não é, por si só, compare-and-swap de lease. Credenciais/provider e egress do TEG devem ser confinados a um handler mediado que exija claim vigente; se uma cópia TEG puder ignorar o ECA e acessar provider diretamente, o mecanismo não oferece fencing e a operação é inelegível salvo enforcement do próprio provider.

O log autoritativo do ECA guarda, por `(operation_id,effect_slot)`: `intent_digest`, `binding_digest`, `authorization_envelope_digest`, `environment_id`, `execution_generation`, `gateway_service_id`, `gateway_instance_id`, `gateway_epoch`, `lease_epoch`, `permit_id/jti_digest`, `state`, `revocation_epoch`, idempotency key digest e sequência. `operation_id`/slot são persistentes e não reutilizáveis para parâmetros divergentes. Cada claim altera estado por transação linearizável/CAS e append-only event. Requisições idênticas com o mesmo `operation_id` retornam estado existente; digest divergente conflita. Dados locais são cache, nunca fonte de epochs.

### 5.2 Estados e resgate

```text
eligible
  -> lease_issued(generation, instance, lease_epoch)
  -> permit_issued
  -> permit_claimed (CAS one-use no ECA; já pode haver efeito)
  -> effect_observed | effect_failed_verified | unknown

eligible/lease_issued/permit_issued -- revocation/expiry antes do claim --> blocked
permit_claimed/effect_observed -- timeout, crash ou falta de prova --> unknown
unknown -- consulta independente conclusiva --> reconciled_no_effect | reconciled_effect
unknown -- sem prova conclusiva --> unknown (sem nova lease/permit/geração)
```

`permit_claimed` não significa que o provider executou; significa que o efeito pode começar e o slot está gasto. O ECA lineariza o claim com transação atômica de consumo de `(permit_id, operation_id, effect_slot, generation, lease_epoch)`. Broker persiste `revocation_requested` e bloqueia novas emissões locais; envia evento de revogação autenticado com epoch ao ECA; ECA serializa esse barrier e `claim_start` no mesmo estado autoritativo. Broker só informa `new_starts_blocked` depois do ACK durável do ECA para a mesma operação/epoch. Não há transação distribuída presumida. Uma resposta perdida consulta a mesma tupla. A segunda tentativa não recebe nova permissão. Só uma decisão explícita e evidência autoritativa podem criar nova `execution_generation`; nunca se incrementa generation como retry para escapar de estado unknown.

### 5.3 Cenários de concorrência/recuperação

| Caso | Regra proposta | Resultado conservador |
|---|---|---|
| Réplicas legítimas concorrentes | ECA emite uma lease epoch ativa por effect slot e só uma CAS `permit_issued→permit_claimed` vence. | Vencedor pode prosseguir; demais recebem conflito/read-only; zero segunda chamada. |
| Clone de storage/VM | Clone não obtém novo epoch pela cópia local; lease/fence deve validar no ECA. | Se clone concorre com titular e provider não valida fencing, bloquear/quarantine; não contar com file lock local. |
| Restore de backup antigo | Reconciliar store com epoch/head ECA/anchor; head menor ou claim remoto sem receipt força unknown. | Restore não reabre slot; sem prova externa, não executa. |
| Perda de resposta do lease/claim | Query/replay idempotente da mesma request key/digest. | Estado remoto prevalece; indisponibilidade => unknown/pending, sem novo permit. |
| StartPermit reapresentado | ECA consome jti/slot uma vez; igualdade retorna estado, digest diferente conflita. | `permit_claimed`/unknown não executa novamente. |
| Operação iniciada por estado antigo | Início requer lease epoch atual; token antigo precisa ser recusado pelo ponto de efeito. | Sem provider fencing, não se atribui nova lease nem se afirma exclusão do antigo; operação fica inelegível/unknown. |

Fencing só fecha a corrida com o efeito remoto se o provider validar o token/epoch atomicamente no commit do efeito, ou oferecer idempotency key estável + query consistente que impeça duplicação. Se o provider não fornecer capacidade verificável, o ECA pode impedir concorrência honesta dentro do domínio TEG, mas não provar exactly-once contra restauração/execução remota ambígua; policy deve classificar a operação como **inelegível para execução protegida/failover**. Não se afirma exatamente uma vez para efeitos externos.

## 6. Revogação em voo e tempo

### 6.1 Estados e pontos de linearização

| Fase | Estado da operação | Ponto de linearização proposto | Revogação/expiração e resposta |
|---|---|---|---|
| Antes do consumo | `authorized` / `revocation_requested` | Commit da reserva/consumo no Broker; commit do revoke barrier no ECA/Broker coordinator antes de consumo | Se revoke barrier vence, consumo é recusado e fica `revoked`. Timeout no revoke não confirma revogação; novas tentativas bloqueiam até query. |
| Após consumo, antes do permit | `consumed` | Commit ECA/Broker que grava permit emitido, vinculado a binding/lease/epoch | Revogação que lineariza antes da emissão do permit bloqueia. Consumo sozinho não autoriza handler. |
| Permit emitido, não resgatado | `permit_issued` | CAS one-use no ECA para invalidar permit ou claim | Revogação primeiro invalida; claim primeiro passa a in-flight. Expiração antes do claim nega. |
| Permit resgatado, antes do início físico | `permit_claimed` / `start_pending` | CAS de claim ECA é ponto lógico de início admitido, não prova de syscall/provider commit | Se revoke chega depois, bloqueia outros claims; para declarar quiescência, aguardar prova de provider de que chamada não iniciou/foi cancelada. Sem prova, `revocation_pending` + `unknown`. |
| Efeito fisicamente iniciado | `effect_started` / `in_flight` | Provider effect ID/commit se observável; caso contrário início físico exato não é linearizável pelo AgentFlow | Não há cancelamento retroativo presumido. Cancelamento só se API provider e receipt comprovarem; senão aguardar/reconciliar. |
| Timeout/perda de comunicação | `revocation_requested`, `start_pending` ou `unknown` | Nenhum ACK baseado em timeout local; query da mesma operação/epoch | Bloquear novos claims localmente; revogação não é anunciada `confirmed_quiescent` sem confirmação; efeito existente permanece unknown. Sem retry. |

### 6.2 Semântica proposta dos acknowledgements

- `revocation_requested`: intenção registrada, ainda sem prova de barreira remota. AgentFlow não pode afirmar revogação concluída.
- `new_starts_blocked`: ECA/Broker confirmou por commit que nenhum claim posterior a essa epoch será admitido. Lista slots `permit_claimed/effect_started/unknown` como in-flight.
- `revocation_confirmed_quiescent`: estado mais forte proposto: a barreira está durável e não há operação cujo efeito ainda possa começar ou permanecer em execução segundo evidência/fencing do provider. Sem essa prova, não emitir este estado.
- Se o produto optar por chamar `new_starts_blocked` de “revogação confirmada”, a UI/API deve dizer expressamente que um efeito previamente claimado ainda pode ocorrer após o ACK. Essa alternativa exige aprovação H-04; não pode se chamar cancelamento.
- Efeito já iniciado não é revertido por revogação. O máximo é solicitar cancelamento suportado; resultado `cancelled` exige confirmação do provider. Falta de confirmação é `unknown`, não `cancelled`.

Para provar “nenhum efeito começa fisicamente após ACK”, provider deve validar fencing epoch na transação que inicia/commita o efeito, ou revocation ACK deve aguardar estado final e prova independente de todos os in-flight. Sem suporte do provider, essa garantia é indisponível e a operação não pode alegá-la. Relógio/TTL segue H-05; expiry bloqueia consumo/permit/claim antes da fronteira, não desfaz efeito admitido/iniciado.

### 6.3 Matriz de corrida

1. **Revoke vs consumo:** revoke barrier primeiro → zero consumo/permit; consume primeiro → ainda exige permit separado, sujeito à epoch atual.
2. **Revoke vs permit:** revoke barrier primeiro → permit recusado; permit emitido mas não claimado → pode ser invalidado pelo ECA; claim primeiro → slot in-flight, não se promete que a chamada física não ocorrerá.
3. **Revoke vs início físico:** fencing provider aceito na commit boundary → token antigo negado; sem fencing, ACK só informa bloqueio de futuros claims e mantém efeito possivelmente pendente, ou espera quiescência antes de `confirmed_quiescent`.
4. **Expiry vs consume/permit/claim:** vence a operação serializada no relógio autoritativo; igualdade/uncertainty/tempo indisponível nega. Expiry depois de claim não cancela efeito.
5. **Timeout durante revoke/claim:** status consulta mesma `operation_id` e epoch. Sem ACK verificável não se cria outro permit nem geração. Efeito possível mantém unknown.

## 7. Projeção GER → ER

Proposta de schema fechado; substituir campos de R6 apenas em revisão posterior, não editar R6 agora. Contratos são assinados sob profile/version aprovados futuramente.

### 7.1 Autoridade e campos

- **GatewayExecutionReceipt (GER):** atestação primária assinada pelo TEG para Broker e auditoria/anchor, com `receipt_id`, `operation_id`, `effect_slot`, `execution_generation`, authorization/binding/snapshot/payload/checkpoint digests, environment/service/instance IDs, handler/version, provider/model/target, reservation/consumption/permit/lease/fence references, `handler_status`, `provider_effect_status`, `evidence_class`, `provider_evidence_digest`, `effect_reference_digest`, `started_at`, `completed_at`, `last_observed_at`, journal sequence/head, pre/post anchor receipt digests, schema/profile/issuer/audience/key epoch.
- **ExecutionReceipt (ER):** projeção read-only, assinada pela mesma identidade lógica TEG e ligada ao digest do envelope GER completo. Sua audience é a constante versionada `agentflow-audit-readonly:<environment_id>`; o Broker/relay read-only aplica allowlist de consumidores e não repassa envelope como bearer. ER não é nova autoridade nem prova independente.
- **Provider evidence:** entidade externa separada, assinada pelo provider ou extraída de ledger/query consistente e autenticada. O digest/referência entra em GER; o conteúdo sensível permanece no domínio autorizado. GER assinado pelo TEG atesta que o TEG observou a evidência, não substitui sua verificação.

### 7.2 Mapeamento determinístico

| ER field | Origem GER | Regra |
|---|---|---|
| `receipt_id` | `receipt_id` | Igualdade exata; um ER por GER. |
| `source_gateway_receipt_digest` | envelope GER completo | SHA-256 com domain separator e profile aprovado; não é digest somente do payload. |
| `operation_id`, `effect_slot`, `execution_generation` | mesmos campos | Igualdade exata; divergence significa conflito/quarantine. |
| `authorization_digest`, `binding_digest`, snapshot/payload/checkpoint digests | mesmos campos | Igualdade byte a byte. |
| `gateway_service_id`, `gateway_instance_id`, `environment_id` | mesmos campos | Igualdade; ER não esconde instância/environment usados. |
| `handler_status` | `handler_status` | Copiar sem reinterpretar: `succeeded`, `failed_before_effect`, `failed_verified`, `unknown`. |
| `external_effect_status` | `provider_effect_status` | Copiar: `confirmed`, `rejected`, `not_observed`, `unknown`, `unavailable`; nunca derivar de handler status/exit code. |
| `evidence_class`, `provider_evidence_digest`, `effect_reference_digest` | mesmos campos | Exatos; `confirmed/rejected` exigem classe e digest verificáveis segundo policy; `unavailable/unknown` não se convertem em sucesso. |
| `started_at`, `completed_at`, `last_observed_at` | mesmos campos | Cópia exata da precisão/UTC wire; `completed_at=null` se handler/provider completion não foi observada; chave permanece presente. `last_observed_at` é sempre obrigatório e não prova completion. |
| `pre_anchor_receipt_digest`, `post_anchor_receipt_digest` | mesmos campos | Exatos; post pode ser `null` apenas se ainda não existe receipt, status resultante não terminal/success. |
| `issuer`, `audience`, `key_id`, `profile/schema version` | issuer/key do TEG; audience ER deriva da constante versionada + `environment_id` | ER identifica mesmo domínio/instância TEG; assinatura ER cobre projeção, audience exata e digest GER. |
| campos de resultado minimizado | mesmo campo GER | Sem inferência; campos não projetáveis não são adicionados ao ER. |

Todos os campos do schema ER são obrigatórios como chave; nullable só onde indicado pelo schema. Campo ausente difere de null. `handler_status=succeeded` não implica `external_effect_status=confirmed`. `not_observed` só pode significar “a consulta definida não observou efeito”; não comprova ausência se provider tem consistência eventual. `failed_before_effect` requer prova do TEG/provider conforme policy; se há dúvida, `unknown`.

### 7.3 Cardinalidade, divergência e recuperação

- No máximo um GER primário por `(operation_id,effect_slot,execution_generation)` e exatamente uma projeção ER para um GER que esteja publicável. Não criar novo slot/generation para repetir unknown.
- GER/ER são imutáveis. Evidência posterior produz evento/tipo de reconciliação append-only ligado ao GER original; não se sobrescreve receipt e não se fabrica uma segunda projeção terminal.
- Assinatura/chave/audience inválida, digest de fonte divergente, 0 ou >1 projeção para GER, null/enum inválido, timestamp incompatível ou discrepância de provider → `unknown`/quarantine, sem escolher o status mais favorável.
- Receipt local, resposta HTTP, exit code, hash local ou assinatura TEG não confirma provider commit. `external_effect_status=confirmed` exige evidência provider independente autenticada/consistente; quando não disponível, fica `unknown` ou `unavailable` conforme capacidade, jamais success externo.

## 8. Precedência documental e estados normativos

Esta matriz trata documentos como fontes com escopos diferentes; um relatório de validação não vira regra do produto e proposta posterior não sobrepõe decisão aprovada automaticamente.

| Artefato | Classe | Precedência/uso agora |
|---|---|---|
| ADR 0008, inclusive emenda R2 | **Decisão arquitetural aprovada** | Fonte normativa superior para F7/RB. Alternativa B aprovada; Broker/TEG inativos/fail-closed; contratos R1 e posteriores continuam propostas salvo decisão explícita. |
| R1 e seções R1.7/R1.8 | **Histórico + proposta** | Recomendação A/B pendente está supersedida exclusivamente pela aprovação B no ADR 0008 R2. Não apagar histórico nem declarar outras decisões fechadas. |
| R3 | **Proposta de contratos de segurança** | Historicamente especifica AF-SIG-1/JSON e seus campos. Não é wire vigente nem decisão aprovada; divergências técnicas devem ser resolvidas em uma proposta consolidada. |
| R5 | **Proposta de remediação R4** | Propõe AF-COSE-1 e remediações F1–F9; RESOLVED_PROPOSED não é aprovação. |
| R6 | **Proposta posterior de fechamento** | Base mais recente de deliberação antes de R8, mas não ganha autoridade por data. R8 propõe substituir/precisar pontos listados nesta remediação; os vetores R6 ficam como evidência histórica, não schema wire completo. |
| R7-A | **Evidência técnica parcial histórica** | Verificou vetores com codec transitório; PARTIAL e não prova biblioteca COSE independente. |
| R7-A.1 | **Evidência de bloqueio histórica** | pycose rejeitou `kid` tstr; descreve falha anterior à correção. |
| R7-A.2 | **Correção autorizada e verificada, escopo estreito** | `kid` COSE é `bstr`, bytes UTF-8 exatos quando ID lógico textual; vetores corrigidos verificados por pycose. Não aprova todo H-01 nem o backend Ed25519 independente. |
| R7-B | **Parecer independente** | REJECT do estado de deliberação R6 e registro dos achados. Não altera ADR nem implementações. |
| R8 | **Proposta de remediação** | Este documento responde à R7-B; não altera ADR/R1/R3/R5/R6/R7, não aprova H e não inicia RB-09C.2. |
| Contratos Python atuais | **Implementação local v1** | Continuam JSON/digest v1, sem assinatura/StartPermit/ECA/anchor. Nenhuma proposta R3–R8 é implementada por sua presença documental. |

Para evitar ambiguidade na deliberação proposta: Binding entra primeiro e é endereçado ao Broker; Authorization referencia o digest do binding e é destinada ao serviço TEG/environment; StartPermit referencia ambos e é destinado à instância/lease. Essa cadeia substitui a proposta anterior conflitante apenas se H-01 aprovar. Não se alteram retrospectivamente decisões humanas: B e `kid=bstr` são decisões/escopo já autorizados; demais elementos permanecem pendentes.

## 9. Dependências H-01–H-10

| H | Impacto R8 | Estado/decisão ainda necessária |
|---|---|---|
| H-01 wire/crypto | M-01/M-05, schema Binding/Authorization/StartPermit, vector `kid`, backend Ed25519 | Aprovar audiência/direção, ordem sem ciclo, perfil/codec/backend/limites/migração; Ed25519 independente não verificado. |
| H-02 receipts | M-04, F5, §7 | Aprovar schema GER/ER, estados/evidence class, issuer/audience, cardinalidade e regra unknown. |
| H-03 keys/authority | M-02/F2/F7 | Designar bootstrap, trustee, key custody e autoridade operacional de ECA fora do domínio do agente. |
| H-04 revocation | M-02/M-03/F3 | Aprovar semântica de new-start block vs quiescent confirmation e requisitos de provider fencing/cancelamento. |
| H-05 time/TTL | F3, StartPermit e lease | Definir fonte, uncertainty/skew/TTL e boundary exata; sem fallback de relógio cliente. |
| H-06 anchor/witness | B-01/F4/F8/C-20 | Manter 3/4 ou deliberar alternativa; aprovar sequência/proofs, durable ACK, independent pinning, quarantine e retention. Esta R8 mantém 3/4 sem aprová-lo. |
| H-07 retention/cost | F5/F8/receipts/anchor | Fixar retenção/quotas/privacy/legal hold/custo. |
| H-08 infra/operators | F2/F7/F8, ECA, C-01/C-13/C-22 | Selecionar domínios/identidades/principals/control planes/dispositivos; comprovar separação. Nada foi escolhido. |
| H-09 campaign/evaluator | B-01 e C-01–C-22 alterados | Aprovar C-20=65 e demais quantidades/failpoints; designar avaliador independente antes da execução. |
| H-10 go/no-go | Todos os anteriores | Revisão R8 sem BLOCKER/MAJOR aberto e autorização explícita; não autoriza RB-09C.2 automaticamente. |

## 10. Critérios para revisão R8 independente

Um revisor que não produziu este R8 deve:

1. Recalcular a aritmética de C-20 (65 casos), confirmar quatro witnesses e threshold 3/4 em todas as linhas, e conferir que indisponível, inválido, Byzantine, split view, rollback e durable timeout têm resultados distintos.
2. Verificar que C-20 aceita 3 respostas válidas consistentes com um witness offline, rejeita 2 votos com dois offline e quarentena equivocation/heads divergentes; conferir que um Byzantine não é contado automaticamente como offline.
3. Confirmar que Binding TEG→Broker possui audience Broker+environment e Authorization/StartPermit possuem audiences TEG service/instance separadas; referências de operation/provider/model/revision/payload/environment são verificáveis e não cíclicas.
4. Tentar cenário de clone/restore de storage, lease antiga, resgate duplicado, resposta perdida e operation unknown; confirmar que ECA é fonte linearizável não controlável pelo host agente, ou que a operação fica inelegível. Nenhum exactly-once provider é alegado sem prova do provider.
5. Executar revisão de corridas revoke/consume/permit/claim/provider e confirmar estados `requested`, `new_starts_blocked`, `confirmed_quiescent`, `in_flight`, `unknown`; nenhum timeout ou ACK fraco é apresentado como cancelamento.
6. Recalcular GER→ER para todos os status/evidence classes, null/absent, timestamps e digests; testar divergência, duplicação, receipt local sem evidência provider e append-only reconciliation.
7. Conferir tabela de precedência, garantindo ADR 0008 B e decisão `kid=bstr` não são reabertas, propostas não se tornam normativas e R1/R3/R5/R6/R7 permanecem evidência histórica.
8. Avaliar C-01/C-06/C-08/C-12/C-13/C-15/C-22 conforme critérios novos; resultados sem inventário/IAM/evidência real recebem `inconclusive`, não pass.
9. Não converter READY_FOR_REVIEW em aprovação humana nem em autorização RB-09C.2.

## 11. Pendências explícitas e validação desta etapa

Permanecem abertos: independência do backend Ed25519; trust bootstrap/recovery operacional; domínio e operadores ECA; lease/fencing provider; semântica humana final de revogação em voo; schema wire e vetores completos; quorum/âncora/testemunhas implantados e independentes; inventário de principals e rotas; retenção/custos; execução da campanha. A proposta segura na ausência de garantias é fail-closed e inelegibilidade da operação, não fallback.

Verificações desta etapa: referências entre achados R7-B e as seções acima; contagem declarada e soma C-20; separação de decisão aprovada, proposta e evidência; whitespace do documento. Não foram executados testes de código, serviços, agentes, providers, Broker, TEG ou âncora. R8 é exclusivamente documental.

**Resultado proposto: READY_FOR_REVIEW.** Não é conclusão/aprovação da RB-09C.1, não fecha H-01–H-10 e não autoriza RB-09C.2/RB-10.
