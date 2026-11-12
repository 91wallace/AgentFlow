# MVP-19.2 — Autenticação local e consentimento verificável

## Resultado

**PARTIAL.** O APL agora vincula cada revisão de consentimento a um workspace persistido e à identidade do diretório observada pelo sistema de arquivos. O fluxo também emite desafios únicos, limitados no tempo, persistidos e de uso único, e exige que a autoridade receba o desafio ao validar o autenticador.

A autenticação humana operacional continua **indisponível**. Este host expõe a API Windows WebAuthn, mas o AgentFlow ainda não implementa cadastro/enrollment de uma credencial local, seleção da credencial, chamada nativa de assertion, verificação de UP/UV nessa ceremony e associação da identidade verificada ao operador permitido. A presença da DLL não prova Windows Hello configurado nem autentica uma pessoa. A aprovação continua bloqueada sem um autenticador operacional. Nenhum dispatch foi habilitado.

## Componentes

- `agentflow.security.authoritative_consent.AuthoritativeConsentResolver` recebe `workspace_root` explícito. Antes de revisar uma proposta, o operador da aplicação precisa chamar `register_workspace(cycle_id)`; não há fallback para o diretório corrente.
- A tabela `apl_consent_workspace_bindings`, no SQLite do State Store já usado pelo APL, persiste projeto, caminho canônico, identidade de filesystem (`st_dev`/`st_ino`), digest de identidade, digest do escopo, ID derivado e data de registro. Triggers impedem update/delete.
- A tabela `apl_consent_request_bindings` fixa por proposta digest, revisão, identidade do workspace, digest do pedido, operações, limites, validade e operador configurado. Também é imutável. Repetição idêntica é estável; alteração de limites, caminho, revisão ou conteúdo gera bloqueio.
- O resolver verifica novamente o caminho real e a identidade de filesystem em cada resolução, valida que referências de caminho no escopo não escapem da raiz (inclusive por symlink existente), e cruza os fatos persistidos APL/Broker. A UI terminal apresenta o caminho canônico persistido junto do ID e re-resolve antes da exibição.
- `SQLiteLocalAuthenticationChallengeStore` guarda no mesmo State Store a vinculação imutável do desafio e um journal append-only de emissão, consumo, expiração ou falha. A nonce aleatória só é retornada ao chamador em memória; o SQLite contém seu hash. A nonce vincula request digest, digest da decisão explícita, ID aleatório do desafio e validade.
- `LocalConsentReviewService` exige um store de desafios persistentes para decisões explícitas. O autenticador recebe a nonce; `LocalAuthorizationAuthority` valida a vinculação persistida e consome o desafio depois de uma autenticação aceita. Falhas encerram o desafio e permanecem negadas.
- `inspect_local_authentication()` é uma sondagem somente leitura de capacidade. Não abre UI, não autentica, não lê credenciais e não invoca providers.

## Workspace e limites de evidência

O caminho canônico e a identidade física do diretório não são substituídos pelo `workspace_id`: ambos ficam persistidos separadamente e são revalidados. O `workspace_id` é somente um identificador derivado que inclui caminho, identidade de filesystem, projeto e escopo. A proposta vincula digest/revisão/escopo APL e Broker ao pedido de consentimento; os limites vêm da configuração local injetada, nunca do planner.

O vínculo identifica a raiz usada durante a revisão e detecta mudança de caminho, troca do diretório e mudança de escopo/configuração. `st_dev`/`st_ino` dependem das garantias do filesystem e não impedem um administrador ou processo sob a mesma identidade de adulterar o SQLite. Não constituem isolamento nem autoridade independente. A execução não foi conectada nesta etapa; antes de qualquer dispatch futuro, o executor terá de revalidar o mesmo binding imediatamente antes de iniciar efeitos e comparar o digest de identidade com o grant. Isso ainda não está homologado.

## Autenticação por plataforma

### Windows

Verificação local de leitura: `webauthn.dll` carregou e expôs `WebAuthNGetApiVersionNumber`, `WebAuthNAuthenticatorGetAssertion` e `WebAuthNFreeAssertion`; a versão reportada pela API neste host foi **9**. Isso é evidência de disponibilidade da API nativa, não de enrollment, dispositivo, PIN/biometria configurados, presença do operador ou verificação de usuário.

O verificador criptográfico de assertions WebAuthn existente valida challenge, RP/origin, assinatura, UP/UV e contadores nas suas fixtures e no protocolo de aprovação já existente. Ele não cria a ceremony Windows Hello do MVP-19.2 nem associa uma credencial operacional de operador. Nenhuma assertion real foi solicitada.

### Linux

O contrato é portátil e o probe informa `linux_authentication_backend_not_configured`. Não se infere capacidade a partir de PAM, processo, variável de ambiente, desktop ou pacote instalado. Um adapter futuro precisa invocar uma ceremony local concreta, devolver identidade verificada e prova vinculada à nonce, e ser homologado em cada ambiente suportado.

## Contrato de desafio

`LocalAuthenticationChallenge` é versionado (`schema_version=1`) e inclui ID aleatório, digest do pedido, digest da decisão, digest do desafio, nonce de uso efêmero e expiração curta. O digest da decisão cobre decisão, identidade declarada, ação explícita, timestamps e request digest; a identidade só é aceita depois da verificação pelo autenticador. O nonce é entregue ao provider e o authority consome o desafio uma vez, com replay idempotente apenas para o mesmo challenge, proof digest e operation ID. Nenhum componente trata uma confirmação textual como prova de identidade.

O terminal ainda coleta `approve`/`reject` como intenção explícita; sozinho, esse input nunca autentica nem concede autorização. Sem provider operacional, a autoridade nega e registra o desafio como falho. Não há caminho para o APL ou executor autoaprovar.

## Testes e evidências

Testes sintéticos do workspace e autenticação não são testes com identidade real. O teste de integração exercita SQLite real temporário, persistência/reabertura, journal, replay, concorrência de leitura, proposta APL e snapshot/journal Broker já existentes; as provas de operador continuam fixtures.

Cobertura adicional: caminho/identidade canônicos; substituição do diretório; persistência dos limites e operações; conflito ao alterar limites; exibição do caminho; challenge aleatório; request/receipt divergentes; expiração; consumo/replay; recuperação após reabertura; probe Windows/Linux sem declarar autenticação operacional; tentativa sem challenge persistido; ausência de grants/dispatch no caminho indisponível.

Validação no Windows 11, Python 3.13.17:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_authoritative_consent.py tests/test_local_consent_ui.py tests/test_local_authorization.py -q
# 43 passed
.\.venv\Scripts\python.exe -m compileall -q src/agentflow/security
.\.venv\Scripts\python.exe -m pytest
# 550 passed
git diff --check
```

O teste da API nativa foi uma consulta somente leitura; nenhuma ceremony WebAuthn, login, provider ou execução de job ocorreu. `git diff --check` passou; Git emitiu apenas avisos conhecidos de normalização LF/CRLF em arquivos locais já modificados.

## Limitações e próximos requisitos

- Implementar enrollment seguro de credenciais (identidade/RP/origin allowlisted e revogação) e ceremony de assertion WebAuthn nativa, entregando a assertion ao verificador existente e exigindo UP+UV conforme policy.
- Resolver a identidade autenticada para o principal local permitido sem receber username do APL, argumento, variável de ambiente ou texto do terminal.
- Homologar presença/verificação do usuário com interação real e desafio exato, replay, cancelamento, expiração e falhas em Windows. Criar backend Linux próprio e homologar separadamente.
- Persistir/recuperar decisões autenticadas com política de revogação consistente; nenhum grant deve existir quando houver divergência, `unknown`, indisponibilidade ou falha de persistência.
- Integrar o binding de filesystem ao verificador de grant e ao executor somente numa etapa posterior autorizada. O MVP-19.2 não dispara jobs e não prova que o executor usará a mesma raiz.
- `LOCAL_SUPERVISED` não demonstra separação administrativa. `INDEPENDENT_AUTHORITY` e H-01–H-10 permanecem inalterados.

## Estado de segurança

- Autenticador operacional: **não disponível**.
- API WebAuthn nativa Windows: **detectada**, sem ceremony integrada.
- Linux: **adapter operacional não configurado**.
- Dados autoritativos e workspace: **persistidos e revalidados** dentro do caminho de revisão.
- Desafio: **persistido, aleatório, vinculado, expirável e de uso único**.
- Consentimento positivo autenticado: **bloqueado**.
- Dispatch: **desabilitado**.
- Homologação `LOCAL_SUPERVISED`: **não concluída**.
- `INDEPENDENT_AUTHORITY` e H-01–H-10: **inalterados**.
