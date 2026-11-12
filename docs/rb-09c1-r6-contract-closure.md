# RB-09C.1-R6 — fechamento proposto dos contratos de confiança

**Estado:** proposta documental para revisão independente e deliberação humana. Não autoriza implementação, implantação, operação, grants, enrollment ou RB-09C.2.

**Arquitetura já aprovada:** a Alternativa B — Approval Broker e Trusted Execution Gateway (TEG) em domínios administrativos independentes — permanece aprovada pelo ADR 0008 R2. Este documento não reabre essa escolha.

**Escopo:** remediar documentalmente F1–F9 do parecer R4 e da proposta R5. RESOLVED significa apenas que o problema foi especificado suficientemente para deliberação; nunca significa APPROVED, implementado, testado ou homologado. H-01–H-10 continuam decisões humanas independentes e pendentes, salvo registro explícito posterior.

## 1. Estado recuperado e limites

O repositório foi inspecionado antes da edição. Branch feat/intelligent-model-routing-phase-7, HEAD b9f7ebeab3fe2a7019ac98fffb79dee6c1225138; índice vazio. O working tree continha modificações preexistentes nos documentos de arquitetura, ADRs 0002/0004/0005/0006/0008, plano e progressos das Fases 2–7, além de arquivos não rastreados em autonomous-development-runner/ e nos planos R1/R3/R5. Essas alterações foram preservadas. Nenhum código Python, teste, workflow, ADR 0008, documento R3 ou R5 foi alterado.

Os contratos atuais approval.protocol e teg.protocol usam JSON canônico e digests de conteúdo, não envelopes assinados. A canonicalização Approval rejeita floats; a canonicalização TEG admite floats finitos. ExecutionAuthorization, ExecutionBinding, ExecutionReceipt e GatewayExecutionReceipt não são atualmente autenticados por assinatura. O TEG atual não implementa StartPermit; sua autoridade padrão continua indisponível/fail-closed. As definições abaixo são propostas futuras, não capacidades presentes.

## 2. Matriz de remediação F1–F9

| Achado | Causa original | Correção especificada neste R6 | Critério documental de encerramento | Estado |
|---|---|---|---|---|
| F1 — wire criptográfico | Serialização JSON divergente, floats e representações sem regra byte a byte; faltavam vetores completos e migração. | §3 fixa perfil proposto COSE_Sign1/Ed25519/CBOR determinístico, versões separadas, limites, schemas fechados, vetores reproduzíveis e corte v1→v2. | Duas implementações independentes reproduzem bytes/digests/assinaturas dos quatro vetores e rejeitam representações negativas antes de uso. | RESOLVED |
| F2 — raiz de confiança | Substituição/rollback da raiz e recuperação dependiam da raiz possivelmente comprometida. | §5 define bootstrap por registro externo independente, epochs monotônicos, revogação e recuperação 3-de-4 sem assinatura da raiz comprometida. | Verificador aceita somente bootstrap autenticado fora do domínio; testes de troca, rollback e certificado inválido bloqueiam. | RESOLVED |
| F3 — StartPermit | Semântica de uso único, réplicas, revogação, expiração e início físico do efeito ficava aberta. | §§6–7 definem identidade por instância TEG, consumo único, linearização, ACK de revogação e corridas separadas; unknown permanece bloqueado. | Campanha prova ordenação em barreiras e zero segunda invocação para o mesmo slot; resultado ambíguo não é repetido. | RESOLVED |
| F4 — ancoragem | Checkpoint pré-efeito não identificava precisamente o objeto confirmado; referência pós-efeito estava mal definida. | §7 fixa checkpoint de intenção pré-efeito, digests vinculados, receipt de âncora e pre_effect_anchor_receipt_digest no checkpoint pós-efeito. | Evidência externa atesta confirmação durável do checkpoint exato antes da primeira chamada provider; ausência bloqueia o efeito. | RESOLVED |
| F5 — receipts | Dois receipts podiam divergir e receipt local podia ser confundido com prova do efeito. | §8 define autoridade, projeção campo a campo, cardinalidade, nulls e separação entre observação TEG e prova provider. | Qualquer divergência gera unknown/quarentena; somente confirmação externa independente pode provar efeito externo. | RESOLVED |
| F6 — campanha | C-01–C-21 continham contagens e subcasos ambíguos; faltava comprometimento/recuperação da raiz. | §9 fixa quantidades, subcasos, barreiras, evidências e pass/fail; adiciona C-22. | Cada execução registra IDs, contagem, evidência independente e resultado; falta de evidência é inconclusiva. | RESOLVED |
| F7 — snapshots | R5 deixou autoridade de aceite ambígua frente à autoridade inicial Broker definida no ADR 0008. | §11 preserva Broker como autoridade inicial e define atestação vinculada ao evento humano, linhagem e consulta de status pelo TEG. | Atestação e status são verificáveis; snapshot, pai, decisão ou epoch divergente bloqueiam. Mudança de autoridade exige aprovação separada. | RESOLVED |
| F8 — witnesses | ACK/hash chain isolados não protegiam contra rollback, split view ou administrador da âncora. | §10 propõe quatro domínios witness independentes, quorum 3-de-4, continuidade, troca de heads e fail-closed sem quorum. | Sob no máximo um witness bizantino, quorums conflitantes não são ambos aceitos; split view/rollback entram em quarentena. | RESOLVED |
| F9 — histórico R1 | R1.7/R1.8 ainda pareciam solicitar escolha A/B após decisão R2. | Notas históricas inseridas diretamente sob R1.7 e R1.8; tabela e critérios originais preservados. | Leitor distingue decisão B vigente, histórico e pendências remanescentes. | RESOLVED |

RESOLVED avalia suficiência documental para deliberação. Todos os contratos criptográficos, de revogação, ancoragem, receipts e witnesses permanecem propostos, não aprovados. F9 é uma correção histórica. Nenhum teste operacional foi executado.

## 3. Wire protocol, schema e perfil criptográfico

### 3.1 Identificadores de versão e envelope

| Campo | Valor proposto | Significado |
|---|---|---|
| wire_protocol_version | 2 | Regras de transporte/interação ponta a ponta; não inferido do schema. |
| schema_version | 1 em cada contrato | Forma fechada do payload do tipo. Evolução incompatível incrementa schema desse contrato. |
| profile_id | AF-COSE-1 | Regras de assinatura, CBOR, digest e limites. Mudança incompatível requer novo perfil. |
| contract_type | Nome fechado na §4 | Contexto de assinatura/discriminator, não texto livre. |

Envelope COSE_Sign1 conforme RFC 9052, tag 18, payload embutido, cabeçalhos não protegidos vazios. Protected header determinístico contém exatamente: label 1 alg=-8 (EdDSA/Ed25519 conforme RFC 8032); label 2 crit=["profile-id"]; label 3 content-type=application/vnd.agentflow.<contract-type>.v2+cbor; label 4 kid como bstr; quando a identidade lógica for texto, codificá-la explicitamente em UTF-8 e comparar os bytes resultantes exatamente. Não normalizar Unicode implicitamente e não converter tstr para bstr; label textual profile-id=AF-COSE-1. profile-id é a única extensão critical. Campo adicional, duplicado, desprotegido ou critical desconhecido é rejeitado.

Sig_structure é deterministic-CBOR(["Signature1", protected_bstr, external_aad, payload_bstr]); external_aad são bytes ASCII agentflow:<contract_type>:v2. Validação confere versão, contrato/content-type, algoritmo, audiência, emissor, chave e revogação antes de confiar no payload. Assinatura prova que a chave assinou bytes específicos, não consentimento informado nem verdade de efeito externo.

### 3.2 CBOR, digest e limites

Usar deterministic encoding RFC 8949 §4.2.1: argumentos em comprimento mínimo; mapas ordenados por tamanho da codificação da chave e depois ordem lexicográfica dos bytes; sem indefinite-length; tags proibidas salvo tag 18 do envelope. Decoder rejeita chaves duplicadas, codificação não determinística, trailing bytes, tipos incorretos e inteiros não mínimos.

- Texto: UTF-8 válido e NFC. Rejeitar texto não NFC, surrogate, UTF-8 inválido e nunca normalizar implicitamente. IDs definidos ASCII continuam ASCII.
- Payload permite null, boolean, bytes, texto, array, mapa com labels especificados e uint 0..2^64-1. Float, NaN/Infinity, inteiro negativo, bignum, simple value indefinido e tags de payload são proibidos. Dinheiro usa unidade inteira ou decimal ASCII fechado pelo schema, não float.
- Timestamps são uint64 em milissegundos Unix UTC; v2 não aceita timestamps string. Limites propostos: envelope ≤64 KiB, payload ≤60 KiB, profundidade ≤16, até 4096 itens por array/mapa, chave textual ≤128 bytes e string ≤16 KiB. Decoder limita antes de alocar. H-01 ainda precisa aprovar valores.
- Schema fechado por contrato: campo extra ou obrigatório ausente falha. Null permitido só onde declarado; campo ausente difere de null.
- Para tipo C, payload_digest = SHA-256(ASCII("agentflow:" || C || ":v2:digest\0") || payload_bstr), escrito sha256: + 64 hex minúsculos. envelope_digest = SHA-256(ASCII("agentflow:" || C || ":v2:envelope\0") || bytes COSE completos). Toda referência a outro contrato assinado — authorization_digest, binding_digest, start_permit_digest e source_gateway_receipt_digest — usa seu envelope_digest, incluindo assinatura; referências a intent/payload/snapshot content usam digest de conteúdo com domínio próprio. Digest não autentica emissor.
- Names/labels abaixo são proposta R6. Adoção depende de H-01 e revisão independente.

### 3.3 Migração v1→v2

JSON v1 atual permanece legível para histórico/auditoria, mas não autoriza efeitos no endpoint v2, não é convertido silenciosamente em COSE e não pode ser misturado numa operação. V2 rejeita versão desconhecida sem fallback/downgrade. Implantação futura exige backup verificado, migração transacional e versão explícita por registro.

Registros v1 finalizados e íntegros podem ser preservados read-only e referenciados pelo digest de origem. Pending, reservado sem resposta, consumido sem receipt, executing e unknown ficam em quarentena como unknown_v1. Migração não promove nem repete. Só reconciliador v1 pode fechar por consulta sem efeito; sem prova conclusiva permanece unknown. Nova operação v2 exige intent/snapshot e autorização explícitas novas. Rollback de schema só antes de qualquer uso v2; após primeiro envelope/efeito v2, não reserializar nem rebaixar operação.

## 4. Vetores COSE_Sign1 reproduzíveis

Fixture pública RFC 8032 §7.1, vector 1: chave pública d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a. Seed de teste 9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60 é divulgado pelo próprio vetor RFC apenas para reprodução e jamais é credencial/chave de homologação operacional. Uma só fixture assina os quatro exemplos; isso não modela segregação de emissores.

Cada envelope inclui a tag COSE 18 (byte d2). Decodificar fornece protected_bstr, mapa unprotected vazio, payload e assinatura. Reconstruir Sig_structure conforme §3.1, conferir payload digest e verificar Ed25519 pela chave pública. Os vetores originais desta seção usavam kid como tstr e foram rejeitados pela biblioteca pycose; R7-A.2 regenerou os quatro envelopes com kid bstr usando pycose 1.1.0 e cbor2 5.6.5. As referências cruzadas foram recalculadas sobre os envelopes completos. Esta reprodução valida o caminho COSE com uma biblioteca consolidada; pycose usa cryptography para Ed25519, e isso não aprova H-01 integralmente nem remove as limitações de proveniência e backend registradas em R7-A.2.

### 4.1 ExecutionAuthorization

Payload labels: 1 wire=2; 2 schema=1; 3 issuer=approval.example; 4 audience=teg:instance-7; 5 issued=1700000000000; 6 expiry=1700000000120; 7 auth-1; 8 op-1; 9 intent digest; 10 snapshot-set digest; 11 target-1; 12 payload digest; 13 scope digest; 14 budget digest; 15 nonce bytes 00..1f; 16 policy epoch 4; 17 human-decision event digest. A autorização não contém binding_digest, evitando ciclo; binding referencia authorization digest.

- content-type: application/vnd.agentflow.executionauthorization.v2+cbor
- protected_bstr: a5012702816a70726f66696c652d69640378386170706c69636174696f6e2f766e642e6167656e74666c6f772e657865637574696f6e617574686f72697a6174696f6e2e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31
- payload_digest: sha256:a6fb02d00384bcc106a620e9db5ff47fde69a71c3334ccfef108ebc1f51762c1
- envelope_digest: sha256:5eb44229553185376d1bb4f12ec20dadec95c4a595a29f4880e3e4b1587d87d0
- envelope_hex: d2845871a5012702816a70726f66696c652d69640378386170706c69636174696f6e2f766e642e6167656e74666c6f772e657865637574696f6e617574686f72697a6174696f6e2e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31a0590234b1010202010370617070726f76616c2e6578616d706c65046e7465673a696e7374616e63652d37051b0000018bcfe56800061b0000018bcfe568780766617574682d3108646f702d310978477368613235363a323832626362633366306133346138613461633666303063323736666366363663663337353761333333326538336439323230386535303739616634363932320a78477368613235363a326233366136653636333135386666643934326331373464653734646265313633626664623162313866366430666663363437653030363437616263613962620b687461726765742d310c78477368613235363a323339663539656435356537333763373731343763663535616430633162303330623664376565373438613734323639353266396238353264356139333565350d78477368613235363a356631363163393134393838326530653130313234626335646435633131663066626538656334353265646435326263656337366230316539323532636233330e78477368613235363a306166393661386564363232613339346538623261323339323834656534366539613761376230656333383139316262643038353731623137313131386464360f5820000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f10041178477368613235363a346634326434653838336535303262616630363766633962616663646630613565393533353035353131306164373964363830613661316334316632666234665840cd588e694c994fde812df40a52177d4daf545dce7ebf60e9ac615d8708a3f6bf75e1be95d8a35766f2e13333240895bae10077d540b3f2688079c25030e09500

### 4.2 ExecutionBinding

Payload labels 1–6: wire=2, schema=1, issuer=teg.example, audience=teg:instance-7, timestamps as above; 7 op-1; 8 teg-instance-7; 9 gateway identity digest; 10 intent digest; 11 snapshot-set digest; 12 target-1; 13 provider-1; 14 model-1; 15 scope digest; 16 budget digest; 17 envelope_digest da autorização de §4.1.

- content-type: application/vnd.agentflow.executionbinding.v2+cbor
- protected_bstr: a5012702816a70726f66696c652d69640378326170706c69636174696f6e2f766e642e6167656e74666c6f772e657865637574696f6e62696e64696e672e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31
- payload_digest: sha256:f3c34b5bf80809f7eb45199d2eeafec62707b30178a77c5f4a77b780e70ebffb
- envelope_digest: sha256:f66cde589d6b3281f7ba5bdc4f1152bf4d7098e89416adff64b85e77f5615012
- envelope_hex: d284586ba5012702816a70726f66696c652d69640378326170706c69636174696f6e2f766e642e6167656e74666c6f772e657865637574696f6e62696e64696e672e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31a0590227b101020201036b7465672e6578616d706c65046e7465673a696e7374616e63652d37051b0000018bcfe56800061b0000018bcfe5687807646f702d31086e7465672d696e7374616e63652d370978477368613235363a323264373931663135646333363566333033633766373366353739633336393433656438633630383539353761366563663163396239663739363662653535610a78477368613235363a323832626362633366306133346138613461633666303063323736666366363663663337353761333333326538336439323230386535303739616634363932320b78477368613235363a326233366136653636333135386666643934326331373464653734646265313633626664623162313866366430666663363437653030363437616263613962620c687461726765742d310d6a70726f76696465722d310e676d6f64656c2d310f78477368613235363a356631363163393134393838326530653130313234626335646435633131663066626538656334353265646435326263656337366230316539323532636233331078477368613235363a306166393661386564363232613339346538623261323339323834656534366539613761376230656333383139316262643038353731623137313131386464361178477368613235363a3565623434323239353533313835333736643162623466313265633230646164656339356334613539356132396634383830653365346231353837643837643058407bc743a06de26d880ac14422b7bb61eca75e7dbfeb4736b72ee0f24bee82524ba85328b5439a4f181c56ceb0475de9baff179291b4d3d80e5339e5dff4dfc80a

### 4.3 GatewayExecutionReceipt

Payload labels 1–6: wire=2, schema=1, issuer=teg.example, audience=audit:stream-1, timestamps as above; 7 ger-1; 8 op-1; 9 envelope_digest de ExecutionAuthorization; 10 envelope_digest de ExecutionBinding; 11 StartPermit digest; 12 status unknown; 13 start time 1700000000001; 14 completion null; 15 effect state possibly_started; 16 effect reference null; 17 provider evidence null; 18 pre-effect anchor digest; 19 post-effect anchor null.

- content-type: application/vnd.agentflow.gatewayexecutionreceipt.v2+cbor
- protected_bstr: a5012702816a70726f66696c652d69640378396170706c69636174696f6e2f766e642e6167656e74666c6f772e67617465776179657865637574696f6e726563656970742e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31
- payload_digest: sha256:c9d146923ef38e78a2002227eb3807e9bd8a86499121aff31511aa957195f977
- envelope_digest: sha256:543ff52cd59d1b61302c488cb37b617a6fce4f0ad1b4cf02497c395f83acca90
- envelope_hex: d2845872a5012702816a70726f66696c652d69640378396170706c69636174696f6e2f766e642e6167656e74666c6f772e67617465776179657865637574696f6e726563656970742e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31a0590198b301020201036b7465672e6578616d706c65046e61756469743a73747265616d2d31051b0000018bcfe56800061b0000018bcfe5687807656765722d3108646f702d310978477368613235363a356562343432323935353331383533373664316262346631326563323064616465633935633461353935613239663438383065336534623135383764383764300a78477368613235363a663636636465353839643662333238316637626135626463346631313532626634643730393865383934313661646666363462383565373766353631353031320b78477368613235363a633537313231643464336566353730393836653833326263363835656538643231623864353132313237333136353165306662303261336339343037636634390c67756e6b6e6f776e0d1b0000018bcfe568010ef60f70706f737369626c795f7374617274656410f611f61278477368613235363a6237396630363866313361303366326437613735386432626665303232386131656634353464666434313437643164303734313237646237363661666639346513f658402872f9cfd9fe96541b43c62116813d6123bd426783a369b724bf2242582fa39b6c0d1fa55b22f0e604a2c5a662ee0544b5b71219a09c15d1091aedee79d58706

### 4.4 ExecutionReceipt

Payload labels 1–6: wire=2, schema=1, issuer=teg.example, audience=audit:consumer-1, timestamps as above; 7 er-1; 8 envelope_digest do GER §4.3; 9 op-1; 10 envelope_digest de ExecutionAuthorization; 11 envelope_digest de ExecutionBinding; 12 snapshot-set digest; 13 target-1; 14 executor instance teg-instance-7; 15 executor build teg-build-1; 16 status unknown; 17 start time; 18 completion null; 19 effect key null; 20 result null; 21 external status unknown; 22 post-anchor null.

- content-type: application/vnd.agentflow.executionreceipt.v2+cbor
- protected_bstr: a5012702816a70726f66696c652d69640378326170706c69636174696f6e2f766e642e6167656e74666c6f772e657865637574696f6e726563656970742e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31
- payload_digest: sha256:4152f09df1a982e25d14943a43080d07293fd904d438ddb0128bb8bb56080d7c
- envelope_digest: sha256:8f97c6029a2121920175a4f01be6db72787abcb44b3495be9066deecab7fe34c
- envelope_hex: d284586ba5012702816a70726f66696c652d69640378326170706c69636174696f6e2f766e642e6167656e74666c6f772e657865637574696f6e726563656970742e76322b63626f72044f746573742d726663383033322d76316a70726f66696c652d69646941462d434f53452d31a05901b7b601020201036b7465672e6578616d706c65047061756469743a636f6e73756d65722d31051b0000018bcfe56800061b0000018bcfe56878076465722d310878477368613235363a3534336666353263643539643162363133303263343838636233376236313761366663653466306164316234636630323439376333393566383361636361393009646f702d310a78477368613235363a356562343432323935353331383533373664316262346631326563323064616465633935633461353935613239663438383065336534623135383764383764300b78477368613235363a663636636465353839643662333238316637626135626463346631313532626634643730393865383934313661646666363462383565373766353631353031320c78477368613235363a326233366136653636333135386666643934326331373464653734646265313633626664623162313866366430666663363437653030363437616263613962620d687461726765742d310e6e7465672d696e7374616e63652d370f6b7465672d6275696c642d311067756e6b6e6f776e111b0000018bcfe5680112f613f614f61567756e6b6e6f776e16f65840b7823b4377bb8d5e6fc4194c6a14b088add490279785acd2aeb5311633c50379560075d3410fbf9fad427a2a50a0586a9fe6ab8afb4f3083c5d0962036fb840f

Observação de integridade: os envelopes foram produzidos por script local e as quatro verificações Ed25519 retornaram true. Ainda falta comparação independente. Os campos content-type do vetor seguem a grafia exata do nome do contrato em minúsculas, sem separadores; este detalhe faz parte do vetor/schema.

## 5. Raiz de confiança e recuperação

Cada domínio approval-broker, teg, snapshot-authority, audit-anchor e cada witness tem raiz/chaves próprias. Trust bootstrap pinna {domain_id, root_fingerprint, profile, epoch=1, purpose} por registro administrado fora do host agente e fora dos dois domínios que valida. O pin exige dois verificadores humanos por canais distintos; o serviço não autentica sua própria raiz. Registro, canal e operador ainda precisam ser escolhidos (H-03/H-08).

Manifesto de chaves assinado pela raiz inclui epoch, digest anterior, kids/finalidades/audiências/validade/revogações e sequência. Regra ordinária: epoch = last_accepted_epoch + 1. Mesma epoch com digest diferente, regressão, salto sem recovery, kid reutilizado com chave diversa ou purpose incorreto são rejeitados. Manifesto novo deve ser publicado no registro externo/quorum antes do uso. Estado monotônico não vive somente no DB restaurável do domínio.

Comprometimento: suspender domínio e efeitos, preservar evidência externa e não confiar em manifesto da raiz suspeita. Recovery Certificate é proposta, assinada por ≥3 dos 4 trustees independentes pré-registrados. Campos: domain ID, root/epoch revogados, último manifest digest witnessed, fingerprint do novo root, new_epoch=revoked_epoch+1, event/motivo, validade. O registro externo confirma sequência e pins; clientes exigem continuidade e negam roots/epochs antigos. Sem quorum, registry ou prova monotônica, quarentena. Comprometimento de mais de um trustee ou do control plane do registry quebra a garantia e requer bootstrap humano out-of-band. Recuperação não desfaz assinaturas/efeitos anteriores; operações do período comprometido exigem reconciliação individual.

## 6. StartPermit: identidade, linearização e corridas

StartPermit é COSE_Sign1 AF-COSE-1 (schema 1/wire 2) emitido pelo Broker. Campos fechados: permit_id randômico 256-bit; operation_id; effect_slot; issuer; audience com teg_service_id, gateway_instance_id, gateway_identity_digest e gateway_epoch; snapshot-set e cada snapshot attestation digest; authorization ID/digest e human decision event digest; binding digest; reservation/confirmation IDs, digests e sequences; target/action/provider/model/handler/version; payload/scope/budget digests; policy/trust epochs; not_before/expires_at; nonce; effect idempotency key; protocolo/schema/perfil. Qualquer campo ausente, extra ou divergente bloqueia.

Uma permit autoriza exatamente um slot em uma única instância estável do TEG. Cada réplica tem identidade/certificado únicos; permit de A não transfere a B. Broker grava unicidade transacional (operation_id,effect_slot) e retorna a mesma decisão/permit só para a mesma requisição idempotente e mesma réplica. Parameters divergentes são rejeitados. Reserva/consumo entre réplicas tem registro autoritativo único no Broker. Permit perdida é recuperada por consulta da mesma identidade; não se emite nova permit para outra réplica se o efeito puder ter ocorrido/for unknown. Failover exige prova autoritativa de ausência de efeito e autorização nova explícita; caso contrário, quarentena.

| Operação | Ponto de linearização proposto | Timeout/perda de resposta |
|---|---|---|
| Reserva | Commit Broker de operation/slot/instance. | Consultar mesma chave; ausência de prova deixa unknown, sem executar. |
| Consumo | Commit atômico de consumed + evento append-only, serializado contra revoke. | Query; nenhuma repetição do efeito. |
| Revogação | Commit de revocation_epoch. ACK apenas depois de durável, com sequence/head e slots in-flight. | Sem ACK não declarar revoke concluído; incerteza bloqueia novas operações. |
| StartPermit | Commit único de permit/nonce/slot/instance e permit-issued serializado contra consume/revoke/expiry. | Consultar mesmo ID; não recriar nem transferir. |
| Preparação TEG | Commit local de intent/permit/checkpoint refs antes da âncora. | Recovery consulta, não chama handler. |
| Effect possible | Commit TEG após ACK de âncora e última consulta de validade/revogação. | Qualquer crash seguinte é unknown. Não existe atomicidade com o próximo syscall/request remoto. |
| Efeito remoto | Commit registrado pelo provider, se houver effect ID verificável. | Query idempotente permitida; mutação não é reenviada. Sem prova, unknown. |

Corridas normativas propostas:

- Revocation commit antes de consume/permit: negar, nenhum efeito.
- Consume antes de revoke: consumo histórico não basta para iniciar; revoke antes de permit bloqueia.
- Permit antes de revoke: ACK lista slot in-flight; sem promessa de cancelamento. Se TEG observar revoke antes do commit effect_possible, bloqueia. Depois do início, somente query/cancelamento suportado pelo provider; resultado incerto fica unknown.
- Revocation contra efeito: transação Broker não serializa syscall remoto. TEG consulta revocation antes de effect_possible, mas ainda há janela até provider. Fechamento requer provider fencing/epoch validado atomicamente com efeito; sem suporte, janela residual precisa de aceite H-04 e classes de operação podem ser inelegíveis.
- Expiry antes/depois do limite: now >= expiry nega; igualdade e incerteza da fonte temporal negam. Sem relógio confiável, fail-closed. Permit expirada nunca é renovada para o mesmo slot.
- Commit provider com resposta perdida: não mutar novamente; consultar effect ID. Evidência insuficiente permanece unknown.

WebAuthn UV não prova compreensão, mTLS não prova consentimento, ACK de revoke não cancela efeito irreversível. Fonte de tempo, TTL, skew e in-flight dependem de H-04/H-05.

## 7. Checkpoint e sequência consumo→efeito

Sequência proposta: snapshot attestation → ExecutionAuthorization → ExecutionBinding → reservation → consumption confirmation → StartPermit → TEG prepared durável → checkpoint pré-efeito → anchor ACK durável → revalidação online de revogação/expiry/audience → TEG effect_possible durável → única chamada provider → GatewayExecutionReceipt → ExecutionReceipt projetada → checkpoint pós-efeito → auditoria/reconciliação.

Um checkpoint pré-efeito único pode cobrir intenção e StartPermit porque o digest da permit já existe antes dele. Não há transação distribuída. Sem ACK durável antes da primeira chamada ao provider, nenhum efeito começa. Pós-efeito exige outro checkpoint, pois receipt/evidência surgem após o resultado. Falhas pós effect_possible deixam unknown e sem retry automático.

PreEffectCheckpointV1 contém checkpoint_id, stream_id, sequence, predecessor digest, wire/profile, Broker/TEG domains e epochs, operation_id/effect_slot, snapshot-set e snapshot attestation digests ordenados, human-decision event digest, authorization/binding/reservation/confirmation/StartPermit digests, revocation epoch, policy digest, target/provider/model/handler+version, payload/scope/budget/idempotency-key digests e issued/expiry.

DurableAnchorReceiptV1 contém checkpoint digest, stream/sequence, tree size/root, leaf index, inclusion proof, consistência do head anterior, anchor ID/epoch, receipt time e witness quorum/signatures/key IDs. Os signatários de Broker e TEG emitem afirmações próprias sobre os mesmos bytes/sequências. Falta de ACK/proof/quorum, resposta ambígua, head menor, fork ou prova inválida bloqueia e coloca em quarentena.

PostEffectCheckpointV1 contém checkpoint/sequence/predecessor, pre_effect_checkpoint_digest, **pre_effect_anchor_receipt_digest** (substitui a referência ambígua da R5), gateway_execution_receipt_digest, execution_receipt_digest, provider_evidence_digest/effect_id_digest quando houver, status/evidence class, time, reconciliação e domains/epochs; recebe seu próprio ACK/proof. Falta de ACK pós-efeito não apaga efeito possível: unknown, reconciliação do mesmo slot.

Intenção ancorada prova declaração assinada; autorização ancorada aponta para decisão/autorização autenticadas; receipt pós-efeito prova relato do TEG. Nenhum substitui evidência independente provider (receipt assinado ou ledger/query forte) para afirmar efeito remoto.

## 8. Receipts: projeção, cardinalidade e autoridade

GatewayExecutionReceipt (GER) é o registro primário e assinado pelo TEG para audiência auditor/anchor. ExecutionReceipt (ER) é uma projeção determinística um-para-um, assinada pela mesma identidade TEG e apontando ao digest do envelope GER. receipt_id ER = gateway_receipt_id; source_gateway_receipt_digest = digest COSE completo GER. Um GER gera exatamente um ER. Uma operation tem no máximo um GER por effect_slot; novo slot requer autorização separada. Sem segunda invocação para slot unknown.

| Campo ER v2 | Origem GER | Regra determinística |
|---|---|---|
| receipt_id | gateway_receipt_id | Igualdade exata. |
| source_gateway_receipt_digest | digest envelope GER | Digest do COSE completo. |
| operation_id, authorization_digest, binding_digest, snapshot_set_digest, target_id | campos equivalentes/binding | Igualdade byte a byte. |
| executor_id, executor_version | gateway_instance_id, gateway_software_digest | Executor é TEG, não provider. |
| status, started_at, completed_at | handler_outcome/timestamps observados | Projeção exata; completed_at null só quando conclusão não foi observada. |
| effect_key, result_digest | mesmos dados | Null apenas se não observados. |
| external_effect_status | provider_effect_status | confirmed/rejected/not_observed/unknown; confirmed exige evidência provider verificada. |
| provider_evidence_digest | referência GER | Pode ser null; issuer/status/classe explícitos no GER. |
| pre/post anchor receipt digests | mesmos campos | Exatos; post null até existir. |
| opcionais | schema | Chaves sempre presentes; null só em campo nullable. Ausência invalida. |

Evidência é classificada local_observation, provider_signed, provider_ledger_query ou provider_unavailable. Só policy e prova externa válida podem afirmar external status confirmed. Exit code, digest, resposta local e assinatura TEG não provam provider effect.

Verificador recalcula a projeção e confere campo/null/issuer/audience/sequence. Divergência de GER/ER, cardinalidade, assinatura, key status, checkpoint ou prova provider resulta unknown/quarentena. Não reparar, sobrescrever ou descartar histórico. Resolução exige evento append-only de reconciliação.

## 9. Campanha adversarial harmonizada C-01–C-22

Plano, não execução. Cada linha exige IDs/fixtures, logs de processo/request, fault proxy, Broker/TEG/anchor/provider externos. Corridas usam barreiras determinísticas e processos independentes. “Sem efeito” requer ledger/telemetria externa. Evidência faltante significa INCONCLUSIVE, nunca pass.

| ID | Pré-condições e quantidade fixa | Procedimento e evidência | Pass/fail e limitação |
|---|---|---|---|
| C-01 bypass provider | 1 lab, 1 credencial sandbox TEG, 10 processos, 100 tentativas diretas | Tentar cada rota/interface; fault capture na tentativa 50. Evidência: firewall/provider auth e ledger. | Pass 100 recusadas, 0 effect IDs. Sem enforcement real: inconclusivo. |
| C-02 operação desconhecida/mista | 5 classes ×10 = 50 | Handler ausente, campo extra, payload tipo shell, destinos múltiplos, swap de revisão. Broker/TEG audit + provider ledger. | 50 bloqueadas, zero efeito; não cobre efeitos locais arbitrários. |
| C-03 replay | 1 operation/slot: 10 cópias idênticas, 10 cross-instance, 10 expiradas, 8 campos ×10 alterados = 110 requests adicionais | Barreiras no commit original; repetir serial/concurrente/restart. DB, anchor e provider independentes. | Idênticas retornam mesma decisão/status persistido, zero novo consumo/efeito; outras 100 rejeitadas. Unknown não é repetido. |
| C-04 idempotência e parâmetros | 10 operation IDs ×(64 requests iguais +16 divergentes)=800; repetir após restart | 8 processos por ID com barrier; depois divergir um parâmetro. Unique constraints, events, provider ledger. | Um reserve/consume por op; iguais idempotentes; divergentes rejeitados; máximo um efeito/slot. |
| C-05 crash em fronteiras | 5 pontos fixos ×10 operações=50; pontos: (1) antes de reserva; (2) após commit reserva/antes resposta; (3) após commit consumo/antes resposta; (4) após commit permit/antes ACK pré-efeito; (5) após effect_possible durável/imediatamente antes da chamada provider. 10 operações por ponto. | Evidência: commit IDs externos, anchor e provider ledger. | Nos pontos 1–4, zero efeito; ponto 5 e depois, unknown; nunca replay. |
| C-06 perda de resposta | 5 commits ×10=50 | Perder resposta de reserve, consume, permit, anchor, receipt; reiniciar serviço. | Remote commit logs + IDs locais; query same ID, sem segundo efeito; divergência quarantine. |
| C-07 falha pós-efeito | 6 pontos fixos ×10 operações=60: (1) provider não aceitou; (2) provider commitou; (3) resposta perdida; (4) crash TEG antes de GER durável; (5) crash após GER/antes do checkpoint pós-efeito; (6) anchor commitou checkpoint pós-efeito/ACK perdido. 10 operações por ponto. | Evidência: provider ledger e fault proxy independentes. | Sem prova: unknown; com prova: mesmo effect ID; zero segunda mutação. |
| C-08 snapshot adulterado | 8 campos | Alterar content/revision/schema/parent/provenance/human event/policy epoch/status. | Assinatura, source store e trace TEG; 8 negadas. Recalcular digest não autentica. |
| C-09 restore parcial | 4 combinações | Broker antigo/TEG novo, inverso, ambos antigos, componente ausente; depois checkpoint e effect incerto. | Witness heads, SQLite checks e provider ledger; heads divergentes quarantine, zero reexecução. |
| C-10 journal | 6 alterações | Remover/editar/reordenar evento, predecessor, recalcular cadeia local, mudar projeção. | Anchor/witness logs + verifier externo; divergência detectada; sem anchor = inconclusivo. |
| C-11 WebAuthn | 8 classes ×10=80 | UV/UP faltante, origin/RP errado, challenge expired/replay, revoked credential, reject/cancel. | Broker audit e ausência permit/provider effect; todos sem aprovação positiva. UX real exige campanha separada. |
| C-12 apresentação | 8 campos ×10=80 | Mudar action/target/provider/model/scope/budget/revision/digest depois do preview; strings hostis/longas. | Device capture, decision digest, binding; divergência bloqueada; screenshot sozinho não prova entendimento. |
| C-13 domínio cruzado | 5 domínios (Broker, TEG, snapshot, anchor/witness, approver identity); 10 tentativas por cada par ordenado =200 | Comprometer uma conta/admin por vez e tentar acesso cruzado; break-glass tabletop. | IAM/audit externo, witness/provider logs; zero crossing; superadmin/control plane comum limita prova. |
| C-14 Broker outage | 1 operação, 30 min, 30 tentativas: 15 pré-reserva +15 pós-reserva/pré-consumo | Uma/minuto, restaurar sem reenvio de comando. | Broker audit, TEG, provider counter; zero effects, pós-reserva unknown/query-only, sem auto dispatch. |
| C-15 Anchor outage | A:30 tentativas/30 min antes ACK; B:10 efeitos sandbox com ACK pós-efeito perdido | A uma/minuto; B provider commit e perda de resposta; depois consulta/reconcile. | Anchor/provider/TEG; A zero effects; B unknown até reconciliar mesmos IDs, zero retry. Sem ledger = inconclusivo. |
| C-16 relógios | 7 offsets −3..+3 s ×3 fronteiras ×3 =63 | Antes not_before, igualdade/limite, expiry com time service controlado. | Time service e sequences; fora de bound/equality/incerteza nega. ±2s proposto, H-05 pendente. |
| C-17 revoke races | 100 consume-vs-revoke +100 permit-vs-revoke; 20 respostas perdidas em cada grupo | Processos independentes e barreiras simultâneas; reiniciar após perda. | Broker serialization, TEG journal, anchor/provider; revoke first nega; permit first in-flight; sem cancelamento fictício/duplicação. |
| C-18 provider commit remoto | 10 commits sandbox | Provider grava effect ID, proxy perde resposta e mata TEG antes GER; query apenas. | Provider ledger assinado/consistente e fault proxy; mesma effect ID, zero segunda mutation; sem prova unknown. |
| C-19 receipt divergence | 10 classes ×10=100 | Mutar campos/null/issuer/audience/digest/status/sequence da ER. | GER e verifier independente; 100 unknown/quarantine; sem reparo/success. |
| C-20 witnesses | 30 falhas: 15 split roots,15 rollback; exatamente 5 com witness indisponível (3 split,2 rollback) | Quatro witnesses, trocar roots/heads e restaurar head velho. | Assinaturas independentes/registry; sem quorum em cinco: quarantine; modelo simulado não prova independência admin. |
| C-21 expiry vs consume | 100 corridas: 40 confirmação antes do bound,40 expirada antes,20 equality/in uncertainty | Barreira no commit e relógio controlado; revoke excluído (C-17). | Commit order/time evidence; só 40 primeiros seguem; 60 restantes negados. |
| C-22 root recovery | 1 fixture: 10 manifests antiga root,10 rollback epoch,10 recovery cert inválidos/replay; 1 recovery válida com 3/4 trustees | Comprometer signing root; bloquear; testar registry externo e recuperação. | Registry log, trustee IDs, pins monotônicos; 30 rejeitados e recovery válida aceita só com continuidade. Sem independência real: inconclusivo. |

C-03 conta requests adicionais à operação original. Repetição idêntica pode retornar status persistido; nunca causa novo consume/efeito. C-17 separa concorrência com revogação; C-21 trata apenas expiry/confirmation. C-15 ensaios A e B são separados. C-20 fixa cinco falhas de witness. Ausência de evidência externa nunca vira aprovação.

## 10. Checkpoint de witnesses e split view

Propõe-se quatro witnesses em domínios administrativos/chaves independentes, quorum de 3/4. Modelo: até um Byzantine para safety e um indisponível para liveness. Quorums 3/4 têm interseção mínima 2; com no máximo um Byzantine, resta ao menos um honesto persistindo head monotônico e recusando roots conflitantes. Dois Byzantine anulam essa garantia; dois indisponíveis deixam sem quorum e bloqueiam checkpoint pré-efeito.

Cada witness persiste last_signed_tree_size/root/epoch fora do DB do log, verifica inclusão, predecessor, consistência Merkle e identity/epoch do anchor; testemunhas trocam heads/proofs por canal independente. Fork, regressão, ausência de prova ou head divergente suspende assinaturas e coloca stream em quarentena. Cliente usa pins de registry externo e recusa head menor/fork. Assinaturas witness não substituem inclusão nem ACK durável.

Independência exige inventário auditável de owners/tenants, IAM/control planes, chaves, operadores, recovery/break-glass, rede e backups, além de C-13 com evidência externa. Quatro processos/keys no mesmo tenant/superadmin não são independentes. Provider real, operadores, infra, custo e retenção são decisões pendentes H-03/H-06/H-07/H-08.

## 11. Snapshot Acceptance Attestation e compatibilidade ADR

ADR 0008 define Broker como autoridade inicial dos snapshots aceitos e prevê decisão humana no domínio Broker. R6 preserva essa decisão; não transfere autoridade para serviço SDD externo. Qualquer transferência futura requer aprovação/alteração explícita do ADR.

Proposta SnapshotAcceptanceAttestationV1 assinada pelo Broker: attestation_id; snapshot_id/kind/project/revision/schema; content/provenance digest; parent attestation digest ou genesis; accepted principal estável; human decision event digest e ID; digest da apresentação reconstruída; referência ao challenge record (nunca assertion); policy epoch; issuer/audience; issued time e sequence. Evento humano associa principal, ação, alvo, scope, budget, payload, revision e digests exatos mostrados/decididos. Assinatura não prova compreensão humana; UI/device independentes continuam requisito.

Alterações de status são eventos assinados append-only com status sequence, predecessor, motivo codificado e effective time. TEG verifica assinatura/epoch/linhagem e status atual online antes de novo efeito. Verificação offline é somente histórica; sem status online, operação nova bloqueia. Nova revisão não reescreve a antiga: evidências incompatíveis ficam obsoletas e exigem revalidação/replanejamento explícitos.

## 12. DAG H-01–H-10 e decisões pendentes

Dependências propostas, não aprovações:

```text
H-01 (wire/cripto) ─┬─> H-02 (receipts) ─┐
                    ├─> H-03 (roots/KMS) ├─> H-06 (anchor/witness) ─> H-07 (retention/cost)
                    └─> H-05 (time/TTL) ─┘                              │
H-03 ────────────────> H-04 (revocation/in-flight) ──────────────────────┤
H-03..H-07 ──────────> H-08 (infra/lab) ─> H-09 (campaign/evaluator) ─> H-10 (go/no-go)
H-01..H-05 ──────────> H-06; H-01..H-08 ─> H-09; H-01..H-09 ─> H-10
```

| H | Decisão necessária | Proposta R6 para deliberação | Dependência |
|---|---|---|---|
| H-01 | COSE/CBOR/Ed25519, labels, limites, codec, migração v1→v2 | AF-COSE-1 §3 e vetores §4; requer segunda implementação e revisão especialista. | Pré-requisito cripto; H-03 antes de operação. |
| H-02 | Fonte de verdade e semântica/cardinalidade receipts | GER primário, ER projeção 1:1, divergência unknown. | H-01 + policy provider. |
| H-03 | Bootstrap, KMS/HSM, roots, trustees, break-glass, operadores | External registry e recuperação 3/4; escolher owner/provider/recovery. | H-01. |
| H-04 | Revocation ACK, in-flight, fencing/cancelamento | Bloquear novas; ACK enumera in-flight; provider fencing se janela zero necessária. | H-03/H-05. |
| H-05 | Fonte temporal, bounds/skew, TTL | Time source monitorado; equality/incerteza nega; valores numéricos pendentes. | H-01/H-03. |
| H-06 | Anchor, proofs, quorum/witness, pre-effect ACK | Pre-effect obrigatório e proposta 3/4; escolher operador/serviço/disponibilidade. | H-01–H-05. |
| H-07 | Retenção, minimização, custo/restore | Definir períodos e quotas sem assertions/segredos em logs. | H-02/H-06. |
| H-08 | Plataforma, rede, laboratório e credenciais sandbox | Domínios reais separados; recursos autorizados separadamente. | H-03–H-07. |
| H-09 | Quantidades, evidências, critérios, avaliador | Aprovar C-01–C-22 e evidência externa/poder de parada. | H-01–H-08. |
| H-10 | Go/no-go RB-09C.2 | Aprovação humana explícita depois da revisão, papéis e recursos definidos. Não implica deploy/enrollment/provider. | H-01–H-09. |

Permanecem sem decisão/nome: fornecedor e infraestrutura, operadores independentes, PKI/KMS/HSM, root recovery, dispositivo aprovador, autoridade de risco, provider sandbox, gastos, retenção, laboratório e avaliador. Alternativa B aprovada não seleciona nenhuma dessas escolhas.

## 13. Critérios objetivos para revisão e encerramento RB-09C.1

1. Revisão independente confirma respostas F1–F9; dois codecs independentes reproduzem vetores sem uso operacional do seed.
2. Revisor valida versionamento, schemas fechados, compatibilidade e quarentena v1 pending/unknown.
3. Revisor de protocolo aceita linearização/races de consume/revoke/permit/expiry/anchor/efeito ou classifica operação sem fencing como inelegível.
4. Revisor de auditoria valida checkpoint pré/pós, proofs, ameaça/quorum witness e limites dos receipts.
5. C-01–C-22 têm passos e critérios verificáveis; esta é a campanha harmonizada substitutiva para planejamento futuro. A referência histórica C-01–C-18 no item 7 de R1.8 permanece preservada, mas não governa a campanha R6. Campanha não executada; evidência sem independência é inconclusiva.
6. R1.7/R1.8 reconhecem Alternativa B aprovada no ADR 0008 R2 sem apagar histórico.
7. H-01–H-10 são aprovadas individualmente em registro documental autorizado; RESOLVED não altera status H.
8. Só depois de revisão independente e aprovação humana explícita RB-09C.1 pode ser encerrada. RB-09C.2 continua exigindo autorização separada.

## 14. Limitações de validação R6

Na validação original do R6 foram feitas verificações documentais/whitespace, sem segundo codec, campanha adversarial, anchor, witness ou provider; as assinaturas originais foram geradas/verificadas pelo script Node local. A etapa posterior R7-A.2 corrigiu kid e reproduziu os quatro vetores revisados com pycose 1.1.0/cbor2 5.6.5. Essa confirmação cobre os vetores COSE, não é teste do AgentFlow ou prova operacional; pycose usa cryptography, portanto o backend Ed25519 não é independente da rota R7-A. Contratos atuais não suportam COSE, StartPermit, snapshots assinados ou receipts assinados. Nenhuma garantia de produção é afirmada.
