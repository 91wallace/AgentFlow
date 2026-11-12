# RB-09C.1-R7-A — reprodução criptográfica independente

**Parecer: PARTIAL.** Os quatro vetores R6 foram reproduzidos com sucesso por uma implementação Python separada da implementação Node usada para gerá-los. A verificação encontrou consistência nos envelopes, assinaturas, digests e vínculos entre contratos. A independência é parcial: não havia biblioteca consolidada de COSE/CBOR instalada e a rota Python usou um decoder/encoder CBOR escrito para esta verificação; além disso, `cryptography` e Node usam OpenSSL, embora com versões diferentes. Portanto, este resultado não satisfaz plenamente o critério R6 de duas implementações independentes para aprovação de H-01.

Este relatório documenta uma verificação de vetores, não aprova contratos, não homologa serviços e não autoriza RB-09C.2. Nenhum vetor ou documento normativo anterior foi alterado.

## 1. Escopo e preservação

Foram lidos os quatro documentos solicitados: R6 (`docs/rb-09c1-r6-contract-closure.md`), R5 (`docs/rb-09c1-r5-remediation.md`), R3 (`docs/rb-09c1-r3-security-decisions.md`) e ADR 0008 (`docs/decisions/0008-phase-7-routing-and-approval.md`). Os envelopes e digests abaixo são os valores registrados em R6.

No início da verificação, a branch era `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138` e o índice estava vazio. Já existiam modificações locais em documentos de arquitetura, ADRs e registros de progresso, além dos itens não rastreados `autonomous-development-runner/` e documentos R1/R3/R5/R6. Todos foram preservados. Nenhuma sincronização, operação de escrita Git, teste operacional ou alteração de código foi realizada. Este relatório é o único arquivo novo desta atividade.

## 2. Ambiente e método

- Ambiente: Windows, PowerShell, Python 3.13 (`py -3.13`).
- Verificação Ed25519: pacote instalado `cryptography 50.0.2`, sem instalação ou alteração do ambiente.
- Backend informado por `cryptography`: OpenSSL 4.0.3, 29 Sep 2026.
- Implementação geradora R6: Node `v26.13.0`, OpenSSL 3.5.9.
- Não estavam instalados `cbor2`, `cose`, `pycose` ou outra biblioteca COSE/CBOR consolidada. Nenhum pacote foi instalado.
- O verificador foi um script Python transitório executado em memória/terminal: decoder CBOR estrito e encoder determinístico separados do código Node de R6, mais a API Ed25519 de `cryptography`. O script não foi salvo no repositório; não há artefato executável permanente desta execução.

O método decodificou tag 18 e a estrutura COSE_Sign1, conferiu que o mapa unprotected estava vazio, interpretou os cabeçalhos protegidos, reconstruiu `Sig_structure` conforme o perfil R6, verificou Ed25519 usando a chave pública de teste publicada no RFC 8032, recalculou os digests e aplicou verificações semânticas do perfil aos payloads e às referências cruzadas. Não foi usado material de chave privada operacional. A semente de teste divulgada pelo RFC não foi necessária para a verificação positiva.

Essa rota não copiou o parser/verificador Node. Contudo, o parser CBOR foi específico e transitório, não uma segunda biblioteca consolidada independente. Também não é correto chamar a pilha criptográfica completamente independente: ambas as rotas reportaram OpenSSL, ainda que versões distintas. O resultado prova reprodução cruzada entre implementações de linguagem/API, não independência de todos os componentes criptográficos subjacentes.

## 3. Verificações positivas

Todos os quatro envelopes passaram nas verificações registradas abaixo. Para cada um, o verificador confirmou tag/forma COSE, cabeçalhos protegidos e política de algoritmo, assinatura Ed25519, payload digest, envelope digest, versão e semântica básica de issuer/audience/referências.

| Vetor | Assinatura e perfil | Payload digest recalculado | Envelope digest recalculado | Resultado |
|---|---|---|---|---|
| `ExecutionAuthorization` | COSE_Sign1/tag 18, `alg=-8`, profile `AF-COSE-1`; válida | `sha256:a6fb02d00384bcc106a620e9db5ff47fde69a71c3334ccfef108ebc1f51762c1` | `sha256:7ba455c50e71cc6eccb8ba958194e735d826ab1e8cb09b8fd5080fb0eb340234` | PASS |
| `ExecutionBinding` | COSE_Sign1/tag 18, `alg=-8`, profile `AF-COSE-1`; válida | `sha256:82dabf05d4a3b19ff4b640f0d480010c17838d22f719dd7071e0aec734d7153b` | `sha256:05b4f6cc2f836e22f5d8d7bf2b1b75024dab1a654be99586b2a3d90f14ba9ba3` | PASS |
| `GatewayExecutionReceipt` | COSE_Sign1/tag 18, `alg=-8`, profile `AF-COSE-1`; válida | `sha256:6382da1e9b85f373e8cb9b9118455ac31136734fa31221580b361a4fc01a4b36` | `sha256:523e15fc10cf654bcbc4764239db06522f769d94035e972e28321833e2931010` | PASS |
| `ExecutionReceipt` | COSE_Sign1/tag 18, `alg=-8`, profile `AF-COSE-1`; válida | `sha256:42b002eed52a6c4937a1a048588fdbaa7450dfea72f93e155c22f72ea46f1873` | `sha256:47d96dad04c6db58eddefb3091da017c99a029d794c1f312581e4a7bd9358eb3` | PASS |

Os cabeçalhos protegidos corresponderam ao conteúdo/tipo de cada contrato, `kid=test-rfc8032-v1`, `profile-id=AF-COSE-1`, `crit=["profile-id"]` e `alg=-8`; o unprotected map era vazio. Os payloads declararam wire protocol 2 e schema 1. Issuer e audience foram lidos e comparados com o perfil de cada vetor. Isso valida os valores de exemplo, não autentica uma autoridade real nem torna R6 normativo.

### Vínculos entre contratos

Os seis vínculos cruzados foram verificados contra o digest do envelope completo assinado, não somente o digest do payload:

1. `ExecutionBinding.authorization_digest` → envelope `ExecutionAuthorization`.
2. `GatewayExecutionReceipt.authorization_digest` → envelope `ExecutionAuthorization`.
3. `GatewayExecutionReceipt.binding_digest` → envelope `ExecutionBinding`.
4. `ExecutionReceipt.gateway_receipt_digest` → envelope `GatewayExecutionReceipt`.
5. `ExecutionReceipt.authorization_digest` → envelope `ExecutionAuthorization`.
6. `ExecutionReceipt.binding_digest` → envelope `ExecutionBinding`.

Resultado: PASS para os seis vínculos nos vetores R6.

## 4. Verificações negativas em memória

Foram avaliadas 12 mutações/cópias em memória. Os arquivos normativos não foram modificados. “Rejeitado” significa que o verificador de ensaio classificou a falha no estágio indicado; não é alegação de que um componente AgentFlow implementado já imponha essa regra.

| Caso | Mutação | Resultado esperado/observado | Classe |
|---|---|---|---|
| N-01 | Payload alterado, assinatura original | Rejeitado | Criptográfica: assinatura inválida |
| N-02 | Bit da assinatura alterado | Rejeitado | Criptográfica: assinatura inválida |
| N-03 | Chave pública incorreta | Rejeitado | Criptográfica: assinatura inválida |
| N-04 | Audience esperada divergente | Rejeitado | Semântica do perfil |
| N-05 | Tipo de wire version inválido em espécime sintético | Rejeitado | Semântica/schema |
| N-06 | Versão não suportada (`99`) em espécime sintético | Rejeitado | Semântica/compatibilidade |
| N-07 | Algoritmo protegido inesperado | Rejeitado | Política de cabeçalhos |
| N-08 | Cabeçalho `crit` inválido | Rejeitado | Política de cabeçalhos |
| N-09 | Ordem de chaves incompatível com CBOR determinístico | Rejeitado | Estrutural/canonicalização |
| N-10 | Chave duplicada no mapa CBOR | Rejeitado | Estrutural/ambiguidade |
| N-11 | Digest declarado do envelope divergente | Rejeitado | Semântica/integridade de referência |
| N-12 | Referência cruzada adulterada no `ExecutionReceipt` | Rejeitado | Semântica/vínculo entre contratos |

Contagem: 3 rejeições criptográficas, 2 de política de cabeçalhos, 2 estruturais e 5 semânticas/schema. Os dois espécimes assinados sintéticos de versão/tipo inválidos foram apenas ensaios do validador e não alteraram os vetores publicados. A campanha não cobriu todas as classes enumeradas no pedido (por exemplo, cada limite numérico, Unicode, comprimento máximo e todas as variantes de codificação CBOR); essa cobertura não deve ser inferida.

## 5. Limitações e achados

1. **Independência incompleta (limitação que determina PARTIAL).** Não havia biblioteca COSE/CBOR independente consolidada. O decoder/encoder foi escrito para a verificação transitória e não recebeu auditoria própria. OpenSSL aparece em ambas as pilhas, embora `cryptography` reportasse 4.0.3 e Node reportasse 3.5.9.
2. **Reprodução não empacotada.** O script não foi persistido, conforme a exigência de criar somente relatório. Um terceiro pode reproduzir usando os envelopes e hashes R6 e uma biblioteca independente, mas esta execução não deixa um programa de verificação autocontido.
3. **Cobertura negativa limitada ao conjunto registrado.** Não foram varridos todos os casos-limite dos limites CBOR e schemas; aprovação de perfil exige vetores e testes mais amplos com implementação COSE/CBOR revisada.
4. **Sem defeitos observados nos quatro vetores.** Os bytes, assinaturas e digests registrados em R6 foram consistentes nesta reprodução. Nenhuma correção foi aplicada ou recomendada aos vetores neste relatório.
5. **Sem efeito sobre o estado arquitetural.** Conforme R6/ADR 0008, os contratos assinados são proposta; os contratos Python atuais não implementam esse perfil e as capacidades operacionais continuam fail-closed. Esta verificação não é validação de Broker, TEG, grants, consentimento, provider ou execução.

## 6. Repetição independente recomendada

Para elevar o resultado a PASS sem alterar os vetores:

1. Reexecutar os quatro envelopes com uma biblioteca COSE consolidada e uma biblioteca CBOR determinística, sem reaproveitar o verificador transitório.
2. Preferir uma implementação criptográfica/backend que não compartilhe a mesma biblioteca nativa da rota usada na R6; registrar versões e proveniência.
3. Publicar o comando/script de reprodução em uma etapa documental ou de homologação autorizada, incluindo hashes de entrada/saída e classificações de falha, sem incluir chave privada operacional.
4. Repetir os negativos de canonicalização, duplicatas, tipos, limites, versão e referências cruzadas; verificar que falhas criptográficas e semânticas são distinguidas.
5. Obter revisão independente dos resultados antes de deliberar H-01. Mesmo um PASS de vetores não aprova automaticamente o perfil COSE proposto nem inicia RB-09C.2.

## 7. Validação e estado do Git

`git diff --check` foi executado antes da criação deste arquivo e retornou código 0; mostrou apenas avisos preexistentes de conversão LF/CRLF nos documentos locais modificados. O índice permaneceu vazio. Após criar este relatório, o whitespace do novo arquivo deve ser conferido diretamente, pois um arquivo não rastreado não aparece no `git diff --check` padrão. Nenhum arquivo preexistente foi editado por esta atividade.

**Conclusão:** **PARTIAL** — quatro vetores reproduzidos e 12 mutações rejeitadas pelo verificador independente em Python; a independência de biblioteca COSE/CBOR e do backend criptográfico não foi plenamente comprovada. RB-09C.1 permanece sujeita a revisão independente e aprovação humana; RB-09C.2 não foi iniciada.
