# MVP-19 — Interface de consentimento LOCAL_SUPERVISED

## Resultado

**PARTIAL.** Foi implementada uma interface de revisão terminal portável e um coordenador que encaminha a decisão tipada ao contrato `LocalAuthorizationAuthority` do MVP-17. Cancelamento é registrado no journal como `UNAVAILABLE`/`operator_cancelled`, com identidade e prova ausentes. A ação textual `approve` ou `reject` é apenas uma escolha explícita da interface; não autentica o operador.

Não existe autenticador local operacional integrado a esta interface. `UnavailableLocalConsentProofProvider` é o padrão. Uma autoridade com autenticador indisponível registra a tentativa como bloqueada, sem grant. Nenhum dispatch foi conectado ou executado. Portanto, esta entrega não habilita aprovação operacional.

## Localização e interfaces

- `agentflow.security.consent_ui.TerminalConsentReview`: interface terminal que apresenta os detalhes recebidos, solicita `approve`, `reject` ou `cancel` e produz recibo tipado, sem conceder autoridade.
- `format_consent_request`: apresentação dos campos de proposta, objetivo fornecido pelo resolvedor confiável, digest, revisão, workspace, operações, limites e validade. A conta do SO é exibida apenas como rótulo não autenticado.
- `LocalConsentReviewService`: liga a interface ao contrato MVP-17; deriva o desafio com `local_consent_challenge_digest` e solicita prova ao `LocalConsentProofProvider`.
- `UnavailableLocalConsentProofProvider`: default fail-closed. Não fabrica provas nem identidades.
- `LocalAuthorizationAuthority.record_cancelled`: persiste cancelamento idempotente no `BrokerStore`/journal como evento não autenticado e sem grant.

O desafio de autenticação inclui digest do pedido, decisão exata, identidade solicitada, ação explícita e datas do recibo. A interface não recebe nome de usuário por argumento como prova. O principal no pedido deve vir de configuração confiável; o rótulo de conta do terminal não o autentica.

## Fluxo atual

1. Um chamador confiável fornece `LocalConsentRequest` imutável e um resolvedor autoritativo do objetivo.
2. A interface exibe o escopo e coleta uma ação explícita. EOF/cancelamento não aprova.
3. O serviço deriva o digest de desafio ligado à ação.
4. O provedor de prova deve obter autenticação local real para esse desafio.
5. O MVP-17 valida identidade, allowlist, proposta, digest, revisão, workspace, operações, limites, validade e replay.
6. Sem prova autenticada, o MVP-17 persiste uma decisão `blocked`; não emite grant.
7. Cancelamento usa o registro append-only idempotente e não cria grant.

## Autenticação e limitações

O repositório contém verificação criptográfica WebAuthn para assertions previamente registradas, mas não contém enrollment operacional, cerimônia de browser/OS que produza assertion para o desafio deste consentimento, nem adaptador que converta o resultado verificado em `AuthenticatedOperator`. A verificação existente pertence a outro contrato de aprovação e não pode ser reutilizada como se fosse prova automática de consentimento APL. Também não há autenticação PAM/Windows Hello operacional integrada ou mapeamento homologado entre conta do SO e principal allowlisted.

Por isso, ainda não foi adicionado um comando CLI de aprovação. O APL não contém um caminho confiável que resolva conjuntamente o objetivo apresentado, os limites, o principal operador aprovado e a proposta elegível em um pedido de consentimento operacional. Aceitar um JSON/username fornecido pelo chamador e pedir `approve` em terminal permitiria apresentar dados não verificados e criaria falsa impressão de autenticação. A API de revisão está pronta para um front-end CLI que receba esses fatos de um resolvedor confiável e um autenticador instalado.

O perfil LOCAL_SUPERVISED continua distinto de `INDEPENDENT_AUTHORITY`; nada aqui satisfaz independência administrativa ou H-01–H-10. Não há afirmação de segurança contra processos sob a mesma identidade privilegiada.

## Persistência, revogação e recuperação

Decisões autenticadas, grants, revogações e expiração continuam usando o `BrokerStore` e journal transacional do MVP-17. A UI não duplica armazenamento. Cancelamento é persistido pelo método idempotente do serviço de autorização; repetição retorna o mesmo resultado, e uma decisão anterior conflitante é rejeitada. O sistema não reexecuta jobs e não despacha após qualquer decisão.

## Testes

Testes adicionados verificam apresentação completa, ações approve/reject/cancel, EOF, binding do desafio e bloqueio quando falta autenticador. Um teste integra cancelamento à autoridade MVP-17 e verifica evento journal, repetição, reinício SQLite e ausência de grant. A tentativa de aprovação com autenticador/provedor indisponíveis termina `blocked` e mantém zero grants e zero autorizações de execução. Nesta execução: testes focados **92 passed**; suíte completa **533 passed**.

Os testes de MVP-17 continuam cobrindo identidade inválida, divergência de digest/revisão/escopo, replay, expiração, revogação, concorrência, persistência e ausência de dispatch usando fixtures sintéticas. Isso não comprova autenticação humana real.

## Homologação necessária

1. Escolher autenticador local suportado em Windows e Linux e comprovar sua presença/verified-user-verification.
2. Implementar e testar enrollment/revogação/recuperação de credenciais por mecanismo operacional aprovado.
3. Ligar a ceremony de autenticação ao digest produzido depois da decisão exata.
4. Resolver objetivo, plano, workspace, permissões e limites da proposta a partir dos stores autoritativos, sem aceitar claims do APL/CLI.
5. Fornecer CLI que apresente esses dados confiáveis e só mostre resultado aprovado quando MVP-17 retornar grant válido.
6. Revisar persistência sob concorrência e falhas no fluxo completo; dispatch permanece bloqueado até homologação independente do executor e autorização específica.

## Verificação local

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_local_consent_ui.py tests/test_local_authorization.py tests/test_security_profile.py tests/test_approval_webauthn.py -q
.\.venv\Scripts\python.exe -m pytest
git diff --check
```
