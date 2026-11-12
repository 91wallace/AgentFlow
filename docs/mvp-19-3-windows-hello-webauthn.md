# MVP-19.3 — Windows platform WebAuthn para LOCAL_SUPERVISED

**Resultado: PARTIAL.** Foi implementada a integração nativa e fail-closed para enrollment interativo e assertion Windows WebAuthn, reutilizando o verificador criptográfico existente. O host consultado tem WebAuthn API v9, mas informou que não há autenticador de plataforma com UV disponível. Nenhum enrollment ou assertion real foi iniciado; portanto autenticação operacional e LOCAL_SUPERVISED continuam não homologados. Nenhum grant foi emitido nem job executado.

## Componentes

- `agentflow.security.windows_webauthn.WindowsWebAuthnApi`: binding `ctypes` da API Windows, limitado a autenticador de plataforma e UV obrigatório; fornece o `clientDataJSON` real retornado pela API e libera os buffers nativos.
- `WindowsHelloCredentialEnrollment`: bootstrap manual para a conta Windows atual. Exige terminal interativo, frase de confirmação e cerimônia do SO. Usa RP `localhost`, origin `http://localhost`, algoritmo ES256, attestation `none`, UP+UV e credencial não discoverable. Extrai apenas ID e chave pública do COSE no attestation object; não armazena chave privada nem PIN/biometria.
- `WindowsWebAuthnProofProvider`: após uma decisão explícita da revisão existente, pede assertion vinculada ao nonce persistido, request e receipt; o próprio Windows exibe a UI.
- `WindowsWebAuthnOperatorAuthenticator`: associa a credencial ao principal opaco derivado do SID do token Windows corrente e usa `verify_assertion_signature` existente para verificar challenge, tipo, origin/RP hash, credencial, assinatura, UP/UV e validade. Atualiza contadores monotônicos por transação/journal do Broker Store.
- `BrokerStore.list_active_webauthn_credentials` e `advance_webauthn_credential_counter`: APIs tipadas para consultar credenciais públicas ativas e atualizar contador com journal append-only; credencial revogada não é elegível.
- CLI: `agentflow webauthn-enroll --broker-db <caminho>`. A opção não cria grants, decisões de consentimento ou dispatch. O banco precisa ser indicado explicitamente.

O fluxo de autenticação pode ser composto com `LocalConsentReviewService` injetando `WindowsWebAuthnProofProvider` e a autoridade já configurada com `WindowsWebAuthnOperatorAuthenticator`. A identidade em `LocalConsentRequest.operator_identity` precisa ser o principal Windows resolvido por `windows_operator_principal()`. A ponte não é configurada como padrão e não conecta um executor. `INDEPENDENT_AUTHORITY`, H-01–H-10, Broker/TEG e RB-09C permanecem inalterados.

## Contratos e verificações

O verificador APL existente persiste nonce aleatório de 32 bytes, digest da solicitação e digest do receipt, com expiração e consumo de uso único. A ceremony WebAuthn assina o nonce; a autoridade ainda valida o challenge persistido contra request/receipt antes de consumir. Falhas de UI/prova não produzem identidade autenticada. O verificador existente exige UP e UV e só suporta ES256, EdDSA e RS256; o enrollment Windows solicita ES256 para manter o conjunto mínimo compatível.

`localhost`/`http://localhost` é a origem local permitida pelo contrato atual; a API desktop recebe `clientDataJSON` fornecido pelo aplicativo. A opção `PLATFORM` restringe a seleção a autenticadores de plataforma, mas não prova por si só que Windows Hello foi o componente escolhido: plugins de autenticador também podem participar da API. UV exigido e flags verificados são necessários, mas não provam identidade civil do operador; a associação é à conta Windows (SID) e à credencial previamente cadastrada.

O algoritmo WebAuthn não foi reimplementado. Foi adicionado apenas parsing CBOR limitado e de comprimentos definidos para extrair a COSE public key do registro e rejeitar formatos/tipos não suportados; assinaturas são verificadas pelo `cryptography` e função existente do AgentFlow. O banco persiste somente chave pública, ID, principal, RP/origin e metadados/journal. O banco local continua acessível à mesma conta e a administradores locais; `LOCAL_SUPERVISED` não resiste a administrador/root comprometido e não é domínio administrativo independente.

## Uso e bootstrap

1. Confirme a disponibilidade de autenticador de plataforma com UV no sistema e use uma janela de terminal interativa.
2. Escolha explicitamente o SQLite Broker local existente. Não use banco de produção nem crie uma credencial por automação/teste.
3. Execute `agentflow webauthn-enroll --broker-db <caminho-do-broker.sqlite>`; revise o principal impresso e confirme `ENROLL WINDOWS HELLO`.
4. Complete a janela de segurança do Windows. O AgentFlow não lê nem solicita PIN, senha ou biometria. Cancelar a janela encerra o bootstrap sem gravar credencial.
5. Confira a evidência de sucesso (principal, credential ID/digest, estado e `private_key_persisted=false`). Revogação continua pelo método autenticado/administrativo existente do Broker Store; substituir requer revogar a credencial ativa primeiro.
6. Para consentimento, o chamador do fluxo local deve carregar a proposta autoritativa, coletar a decisão explícita na UI, emitir o desafio persistido e injetar os adaptadores WebAuthn acima. A asserção só autentica o operador; não é por si só consentimento nem dispatch.

O comando recusa execução sem TTY, exige frase de bootstrap, recusa segunda credencial ativa para o mesmo principal/RP e faz preflight de disponibilidade UV antes de abrir/criar o SQLite. Não foi executado neste trabalho porque o preflight do host retornou UV indisponível.

## Testes

`tests/test_windows_webauthn.py` usa chaves efêmeras e API simulada apenas em fixtures. Cobriu extração/validação da credencial pública, parser CBOR estrito, rejeição de registro sem UV ou com RP divergente, recusa de enrollment não interativo sem side effects, verificação de assertion via Broker SQLite/journal e desafio APL persistido, atualização de counter, replay, credencial desconhecida, UV ausente, assinatura inválida, expiração, mapeamento de cancelamento e probe somente leitura da API nativa.

Os testes com fixtures **não** são ceremony Windows Hello operacional. O fluxo nativo exige autenticação humana e não foi aberto neste checkpoint.

## Limitações e estado operacional

- Probe nesta máquina: `WebAuthNGetApiVersionNumber() = 9`; `WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable()` não retornou disponibilidade. O SDK documenta a API para aplicações Win32 em comunicação com Windows Hello e autenticadores FIDO2; a opção de attachment filtra plataforma e UV é requerida, mas disponibilidade e seleção reais dependem do sistema/autenticador.
- Revalidação após retomada em 2026-11-12: API `9`; `platform_uv_available=false`. O CLI de enrollment não foi invocado e nenhum SQLite operacional foi aberto/criado durante esta verificação.
- Sem credencial cadastrada, sem assertion real, sem principal autenticado operacionalmente e sem prova criptográfica real produzida por Windows Hello.
- Timeout de 120 s é informado à API; a documentação da Microsoft define-o como orientação que a plataforma pode sobrescrever. A chamada nativa é síncrona; cancelamento disponível é a ação do usuário na UI, sem cancelamento programático assíncrono integrado.
- A consulta de capability não homologa o diálogo do usuário, Windows Hello, vínculo do operador ou uma origem confiável. API desktop `localhost` não fornece isolamento de processo nem independência administrativa.
- Não há comando de aprovação/dispatch habilitado. O adaptador é opt-in por composição interna e o dispatch permanece desligado.
- O bootstrap ainda depende de host Windows com plataforma UV realmente habilitada. Linux continua sem autenticador operacional implementado nesta entrega.

## Homologação restante

Em uma sessão em que o probe indique UV disponível, um operador deve executar manualmente o enrollment acima e completar a autenticação Windows. Em seguida, uma proposta sintética válida deve ser exibida pela revisão autoritativa, ter desafio persistido e receber uma assertion real; confirmar no journal o consumo único e a chave pública associada ao SID, sem aceitar plano automaticamente nem despachar job. Repetir com cancelamento, challenge expirado e credencial revogada. Só classificar `WINDOWS_HELLO_OPERATIONAL` após evidência sanitizada dessa ceremony real e validação criptográfica. O resultado atual é **PARTIAL**.

## Referências da API

- [Microsoft WebAuthn API overview](https://learn.microsoft.com/en-us/windows/win32/webauthn/-webauthn-portal)
- [WebAuthNAuthenticatorMakeCredential](https://learn.microsoft.com/en-us/windows/win32/api/webauthn/nf-webauthn-webauthnauthenticatormakecredential)
- [WebAuthNAuthenticatorGetAssertion](https://learn.microsoft.com/en-us/windows/win32/api/webauthn/nf-webauthn-webauthnauthenticatorgetassertion)
- [GetAssertion options and platform/UV selection](https://learn.microsoft.com/en-us/windows/win32/api/webauthn/ns-webauthn-webauthn_authenticator_get_assertion_options)
- [Credential attestation output structure](https://learn.microsoft.com/en-us/windows/win32/api/webauthn/ns-webauthn-webauthn_credential_attestation)
- [API version probe](https://learn.microsoft.com/en-us/windows/win32/api/webauthn/nf-webauthn-webauthngetapiversionnumber)
