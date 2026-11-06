# ADR 0008: Intelligent Model Routing e autoridade de aprovação — Fase 7

Status: **planejamento F7 aprovado; gate F7-01 bloqueado para grants até existir fronteira de aprovação independente e verificável**.

## Contexto e inventário F7-01

F6 fornece `AgentAdapter`, descriptors, seleção manual imutável por tentativa, registry explícito e `ModelRoutingPolicy` inerte. Não há classificador, evaluator, ledger de budget ou autenticador no código. O `SQLiteStore` possui migrações até `user_version=5`; contratos SDD first-class e ledger exigem migração posterior (v6, salvo inventário de implementação demonstrar alternativa equivalente). O projeto não tem dependência WebAuthn.

ADR 0003 permite runtime local sem isolamento OS garantido e inclusive configuração explicitamente `danger-full-access`; ADR 0004 deixa claro que worktree não é sandbox; ADR 0005 declara que runtime local não é uma fronteira de segurança universal. O runtime local e os dados operacionais podem compartilhar o mesmo usuário/filesystem do processo agente. Não existe serviço/processo de aprovação separado, ACL para banco/chaves de aprovação ou mecanismo que impeça um agente com aquelas permissões de ler/alterar estado ou controlar a sessão gráfica.

O PR #6 (F6) foi integrado por merge normal `2b8546d7d71fec160900df40ab22c35099cd8cfe`. O commit aprovado `3742e39ab7b540906fe59a8aea6262b7027ce969` é seu segundo pai e ancestral. A branch F7 foi atualizada por fast-forward, sem alterar a árvore de trabalho.

## Decisão WebAuthn/FIDO2

WebAuthn/FIDO2 com verificação de usuário obrigatória é o mecanismo criptográfico inicial, não uma interface de consentimento autossuficiente. A aprovação também requer uma interface confiável e uma fronteira que o processo agente não controle. A assinatura isolada **não** prova que o usuário viu ou entendeu os requisitos, diff, destino, custo e efeitos que está aprovando.

### Enrollment

1. Enrollment é iniciado explicitamente por operador no Approval Broker confiável e associado a um `principal_id` provisionado por autoridade fora do agente. O primeiro enrollment requer bootstrap do broker/OS; não há auto-enrollment ao importar projeto ou iniciar agente.
2. O broker cria challenge aleatório de ao menos 32 bytes com `purpose=enrollment`, RP ID/origin fixos, principal, nonce e expiração curta (máximo 5 minutos). `userVerification=required`; requer flags UP e UV na resposta.
3. Verificar `clientDataJSON` (`type`, challenge e origin exato), RP ID hash, algoritmos/attestation aceitos, credential ID único e chave pública. Attestation de fabricante não é presumida nem necessária para provar consentimento.
4. Persistir no armazenamento protegido do broker somente credential ID, chave pública COSE/algoritmo, RP ID, principal, transporte/metadados não secretos, sign counter quando aplicável, timestamps e estado de revogação. A chave privada permanece no autenticador; nunca guardar segredo, cookie, session ou token WebAuthn no `.agentflow/`, banco do projeto, log ou ambiente do agente.
5. Revogação é ação autenticada do broker e append-only; chave revogada não pode aprovar. Sign counters são defesa auxiliar: valores zero ou não monotônicos não substituem challenge único/replay protection, pois passkeys sincronizadas podem não expor contador monotônico.

### Aprovação e grant

1. AgentFlow cria uma proposta imutável e ChangeImpact; o broker lê o snapshot persistido e constrói uma tela própria com projeto, ação, IDs de revisão/plano, requisitos adicionados/alterados/removidos, impacto/dependentes, destinos provider/model, orçamento/enforcement, operation ID e digests completos/curtos. Strings externas são texto escapado, sem HTML/Markdown/JS do agente. Conteúdo truncado, stale ou não reconciliado não pode ser aprovado.
2. A UI exige gesto explícito de aprovação e só então inicia assertion WebAuthn com `userVerification=required`, challenge aleatório de 32 bytes e `timeout <= 120 s`. O desafio assina/binda principal, action, projeto, operação, escopo, `SpecificationRevision`/`PlanRevision`, todos os digests e expiração.
3. O verifier confere assinatura com credential pública ativa, `type=webauthn.get`, challenge, origin exato, RP ID hash, flags UP+UV, principal, escopo, expiração, revogação e estado da proposta. Divergência falha fechado.
4. Só o broker produz um `ApprovalGrant` assinado e de uso único com `grant_id`, principal/assurance, ação, revisão/plano/impact digests, escopo, operation ID, nonce, issued/expiry e assinatura. TTL máximo 120 s. Grant é ligado a um único request digest e consumido por constraint/transação `BEGIN IMMEDIATE` junto à decisão/auditoria; replay, grant alterado, expirado ou divergente conflita. Revogação do grant antes do consumo impede uso. Grant bearer não é enviado ao agente; core comunica-se com broker por IPC autenticado e de escopo mínimo.
5. DB operacional do projeto guarda apenas IDs/digests/assurance da aprovação e eventos necessários à auditoria, não a chave privada nem grant bearer. Registro de credentials/challenges/grants usados/revogações vive em banco separado do broker, com ACL/identidade OS própria e trilha append-only. O broker é autoridade no consumo único; copiar DB do projeto não cria assinatura válida.

### Fronteira confiável e bloqueio atual

O contrato exige processo broker instalado/protegido fora do workspace, serviço em principal OS separado, armazenamento com ACL que o agente não consegue ler/escrever, canal IPC autenticado, UI de aprovação sob controle do broker, autenticação WebAuthn e ausência de processo agente ativo/controlando essa UI. O runtime deve atestar que adapter/process tree não tem acesso a broker IPC, seu DB, credenciais/sessões, handles de UI ou canal de automação. Aprovação indisponível se qualquer prova faltar.

O AgentFlow atual não oferece essa separação. Um processo agente executado sob o mesmo usuário pode ter acesso ao DB/config locais e, dependendo do perfil explicitamente autorizado, à sessão de UI; não é seguro confiar que ele não iniciará uma ceremony, manipulará uma UI web local ou adulterará o estado/chaves públicas locais. Os adapters F6 não anunciam nem impõem uma capability de isolamento de UI/aprovador. Windows WebAuthn API e autenticadores FIDO não resolvem a fronteira do processo nem fazem o visor da tarefa parte do dado assinado. Linux browser/desktop/authenticator também varia por instalação e headless não fornece UI confiável.

**Resultado do gate F7-01:** grants permanecem globalmente desabilitados neste runtime. Import/inspection/proposal podem ser read-only/proposed; aceitar revisão SDD, autorizar troca/escalonamento e executar fluxo que dependa desses grants deve retornar `approval_unavailable`/blocked. Não usar prompt de terminal, confirmação CLI, assinatura WebAuthn isolada, autor autodeclarado ou fake authenticator de produção como bypass. F7-02 a F7-10 não começam até uma implementação homologável do broker/isolamento, em ambas as plataformas declaradas, ser autorizada e demonstrada. Simuladores ficam restritos a testes.

## Compatibilidade de plataforma e estado

Windows tem API WebAuthn do sistema a partir do Windows 10 1903 e Windows 11, incluindo Windows Hello e chaves FIDO2 externas, mas isso não prova que o broker/UI isolados estejam instalados nem garante detalhes de consentimento transacional. No Linux, o browser/client WebAuthn e authenticators disponíveis dependem do desktop/browser/dispositivos; headless/CI é sem UI e deve retornar `unavailable`. Fazer probe não interativo da capability; não abrir ceremony nem executar agentes para descobrir suporte. Só declarar uma combinação suportada após testes reais do broker, mas CI usa simuladores e nunca credenciais reais.

## Migração SQLite

O inventário local confirma F6 schema v5. Persistir SpecificationRevision/PlanRevision/traces/impactos e routing/budget requer migração aditiva v5→v6. Antes da migração: usar SQLite online backup consistente no mesmo diretório, verificar integridade e preservar backup. Aplicar DDL/dados/user_version/migration record em transação; rodar `integrity_check`, `foreign_key_check`, invariantes e testes de rollback/falha injetada; em falha, banco v5 permanece íntegro e sem tabelas v6 parcialmente visíveis. Reabertura/recovery nunca presume aceite ou consome grant outra vez. Nenhuma migração será executada enquanto F7-01 estiver bloqueado.

## Dependências e limites

Um broker separado exige decisão de instalação/provisionamento por SO, ACL/identidade de serviço, IPC, UI não controlável pelo agente e integração WebAuthn; pode requerer privilégios/admin e testes interativos reais. A matriz Windows/Linux e CI simulada não comprovam o isolamento real. Até esse gate ser satisfeito, F7 pode evoluir somente como plano; roteamento que exija aceite/grant não é habilitado e nenhum budget é gasto.

## Referências técnicas consultadas

- [W3C Web Authentication Level 3](https://www.w3.org/TR/webauthn-3/) — challenge imprevisível, validação de origin/RP ID, UP e UV; autenticação não é consentimento transacional informado.
- [Microsoft WebAuthn API](https://learn.microsoft.com/en-us/windows/win32/webauthn/-webauthn-portal) — API do sistema para Windows Hello/FIDO2 em Windows 10 1903+ e Windows 11.
- [Microsoft WebAuthn APIs for passwordless authentication](https://learn.microsoft.com/en-us/windows/security/identity-protection/hello-for-business/webauthn-apis) — fluxo mediado pelo usuário e limites das capacidades transacionais do autenticador.
- ADRs 0001–0007 e `docs/architecture.md` / `docs/implementation-plan.md`.

## Addendum — autorização para desenvolvimento local controlado

Uma autorização posterior permite desenvolver o contrato do Approval Broker e os componentes F7 independentes de consentimento, sem ativar grants reais. Este addendum prevalece sobre a frase anterior que proibia qualquer F7-02–F7-10 antes do gate: somente o subconjunto isolado abaixo pode avançar. A condição de homologação do broker continua inalterada.

- `ApprovalAuthority` expõe somente status, verificação e consumo; não há método de emissão. O facade local está fixado em `approval_unavailable`, sem backend injetável, configuração, variável de ambiente ou flag que o ative.
- Não foram implementados listener IPC, identidade OS separada, ACL de store, WebAuthn verifier, enrollment, interface confiável ou instalação de serviço. Nenhuma dessas capacidades foi verificada em Windows ou Linux. A implementação do facade não constitui um Approval Broker homologado.
- O threat model mantém o runtime do agente como potencialmente hostil, com privilégios iguais ou superiores ao processo do AgentFlow. Conta OS separada isoladamente não é prova suficiente. O broker deverá residir em principal/dispositivo remoto ou sistema operacional efetivamente isolado, com store e UI que o agente não consiga ler/controlar. Quando a plataforma não comprovar essa fronteira, aprovação permanece bloqueada.
- Avançaram localmente apenas snapshots/propostas SDD, rastreabilidade TOML, impacto determinístico, catálogo versionado, classificação/avaliação preview e auditoria. Propostas não podem ser aceitas; nenhuma seleção de preview é enviada a adapter. Execução, escalonamento, mudança de provider e uso de budget continuam fora do alcance desta implementação.
- Migração v5→v6 adiciona tabelas append-only para revisões, impacto, catálogo e decisões. Migração testada em banco temporário com rollback por falha injetada. Dados operacionais existentes não foram abertos nem migrados nesta atividade.
- Testes simulados podem demonstrar contrato, expiração, replay e consumo único do protocolo, mas não demonstram isolamento OS, confiança de UI, autenticidade WebAuthn ou segurança IPC.

O gate F7-01 permanece **não homologado**. O trabalho local habilita somente homologação técnica dos componentes sem grants; qualquer retomada de aceite real requer implementação independente do serviço e evidência de teste de fronteira em plataforma-alvo.

## Plano específico de homologação do Approval Broker

### Fronteira e modelo de ameaças

O broker é um serviço de autoridade separado do processo AgentFlow e dos runtimes/adapters. O agente é tratado como hostil e pode controlar o workspace, os argumentos e a saída do runtime; pode também ter privilégios iguais aos do processo principal. A fronteira só é aceita se controles efetivos demonstrarem que esse processo não lê/escreve o estado/segredos do broker, não se autentica como cliente broker, não controla a sessão ou a tela confiável e não consegue alterar a apresentação do request. Contas, worktrees, containers ou processos com o mesmo principal, isoladamente, não comprovam isso. Administrador/root e comprometimento físico do dispositivo aprovador ficam fora do modelo; se o agente puder obter esses privilégios, o broker deve residir em domínio remoto/dispositivo independente.

Ameaças a testar: spoofing/replay do IPC; substituição/truncamento de operação ou digests; confusão de principal; UI redressing/injeção de conteúdo vindo do projeto; roubo/cópia do banco; corrida entre revogação e consumo; crash entre emissão e consumo; repetição após resposta perdida; relógio divergente; perda/corrupção do armazenamento; logs com secrets; processos same-user/elevated tentando acessar service, socket, UI, store ou chave.

### Implantação-alvo e protocolo

Preferência: serviço broker em host ou dispositivo de confiança separado, com interface de consentimento independente. Um serviço local só pode ser aceito se a matriz de isolamento abaixo passar sob a configuração real de execução do agente; caso contrário, o status é `approval_unavailable` e é necessário broker remoto/dispositivo independente.

O core envia ao broker uma proposta somente leitura e imutável, identificada por `operation_id`, principal solicitado, ação, escopo, revisão e digests. O canal usa autenticação mútua (certificados provisionados/pinados e rotação/revogação) e autorização por identidade de cliente; em instalação local, named pipe com ACL e verificação de peer no Windows ou Unix domain socket com permissões e `SO_PEERCRED`/credenciais de peer no Linux podem ser defesa adicional, não substitutos de isolamento. Payloads têm tamanho/versionamento estritos, nonce, request digest, limites de taxa e timeout. Falha, peer desconhecido, timeout ou resposta ambígua bloqueiam operação; consultas de reconciliação são read-only e nunca reemitem grant.

O serviço carrega a revisão/diff diretamente da fonte persistida confiável e renderiza, em UI pertencente ao broker, uma confirmação explícita e não editável do request exato: ação, projeto, revisões, requisitos/impacto, provider/model, limites e tipo de enforcement, destinos e digests. Texto do projeto é escapado como texto sem markup ativo. Conteúdo ausente, truncado, stale, inconsistente ou impossível de apresentar integralmente não pode ser aprovado. WebAuthn/FIDO2 `userVerification=required` ocorre após a pessoa revisar e acionar a UI; challenge aleatório de 32 bytes, escopo/digests/operation, origin/RP, nonce e expiração curta (até 120 s) são verificados pelo broker. A ceremony prova presença/verificação/autenticador e assinatura do challenge, mas não prova entendimento humano; o controle vem da UI independente e da exibição completa do conteúdo.

### Dados, grants, atomicidade e recuperação

Credential IDs, chaves públicas, revogações, challenges, grants consumidos e audit trail residem apenas em armazenamento do broker com ACL mínima, criptografia em repouso quando disponível e integridade/backup verificáveis. Chaves privadas ficam no autenticador; qualquer chave de assinatura do serviço deve usar keystore/TPM/HSM do domínio do broker quando disponível. O banco do projeto guarda referência, digest, resultado e assurance, nunca bearer grant nem material de autenticação. Segredos não entram nos logs.

Grant assinado pelo broker liga `principal_id`, ação, operation ID, request/scope digest, revisão/spec/plan/impact digests, destino, budget e expiração. TTL máximo de 120 s, revogável antes do consumo, um único uso. O agente não recebe o grant nem credencial. AgentFlow submete o request e a referência ao broker; o broker revalida a assinatura/escopo/revogação e faz consumo atômico junto ao registro append-only (transação serializável/SQLite `BEGIN IMMEDIATE` sob lock exclusivo ou armazenamento transacional equivalente). Restrição única em `grant_id` impede duas aceitações concorrentes. Nenhuma transação distribuída é presumida entre broker e banco do projeto: se a resposta ao consumo ou persistência local for perdida, estado `unknown` bloqueia repetição; uma consulta autenticada de status reconcilia o receipt existente sem novo consumo. Crash antes de resposta, backup restore e indisponibilidade de revogação também falham fechado.

### Requisitos por plataforma

- **Windows:** serviço com principal dedicado e ACL restrita para banco/keystore; named pipe com ACL explícita, validação de identidade/token do cliente e rejeição de impersonation insuficiente; UI em sessão/desktop de confiança não automatizável pelo processo agente. Windows Hello/WebAuthn pode autenticar o usuário, mas não fornece por si só isolamento de UI ou consentimento transacional. Processo agente no mesmo usuário, admin, serviço elevado ou automação de UI deve ser usado como adversário nos testes. Se o perfil do agente pode acessar a sessão ou elevar privilégios, mover broker/UI para outro dispositivo/domínio.
- **Linux:** service user dedicado, diretório/store mode restrito, systemd hardening e MAC (SELinux/AppArmor) quando efetivamente configurados; Unix socket com permissões e credenciais de peer, além de autenticação criptográfica mútua. Desktop/browser/authenticator são capacidades detectadas, nunca presumidas. Processos same-UID, root, sessão gráfica compartilhada e automação de X11/Wayland devem ser avaliados; containers/namespaces não são automaticamente fronteira contra host. Se separação não for demonstrada, exigir broker/dispositivo remoto.

### Plano de homologação e critérios de bloqueio

1. Revisar configuração instalada e provar identidade/ACL em Windows e Linux; testar leitura/escrita do store e conexão IPC por agente same-user, usuário não autorizado e processo elevado.
2. Testar autenticação mútua, peer spoofing, versão/payload inválidos, nonce, request/digest substitution, replay e limites de taxa.
3. Em UI real controlada pelo broker, confirmar apresentação exata, escaping/injection, sem truncamento, origem confiável e que o agente não consegue substituir/automatizar a tela; incluir avaliação humana explícita da clareza do consentimento.
4. Com autenticador de homologação dedicado, testar enrollment, UV obrigatória, origin/RP/challenge, credencial revogada, expiração e sem autenticador. CI simulada cobre protocolo, não identidade humana ou WebAuthn real.
5. Testar consumo simultâneo, grant usado/revogado/expirado, corrida de revogação, alteração de digests, desconexão após consumo, crash/restart, restauração de backup, corrupção e reconciliação sem reemissão.
6. Inspecionar audit/logs para ausência de secrets/bearers; testar backup e retenção; obter evidência de permissões efetivas e matriz de versões SO/browser/authenticator.

Qualquer falha, capability desconhecida ou impossibilidade de testar o perfil real bloqueia grants reais e operações dependentes. A homologação unitária/simulada jamais será descrita como prova da fronteira. Provisionamento de principal, certificado, autenticador, UI e host/dispositivo de confiança é decisão operacional externa ainda pendente.

## Classificação explícita das garantias

| Classe | Situação |
| --- | --- |
| **Comprovada localmente nesta árvore** | Facade de autoridade é fail-closed e não tem API de emissão nem configuração de ativação; testes verificam que flags/variáveis não habilitam grants. Protocolo de interface é exercitado com autoridade simulada em testes, sem alegar separação. CLI `agentflow audit` abre SQLite com `mode=ro&immutable=1`, não migra, recusa WAL presente, valida integridade/digests e não expõe snapshots/payloads/logs. Testes locais verificam hash idêntico do banco antes/depois. |
| **Projetada, ainda não homologada** | Serviço remoto/de domínio separado, autenticação mútua, autorização por operação, enrollment e assertion WebAuthn, apresentação confiável, grant scoped, consumo atômico, revogação, recuperação, proteções de store e deployment Windows/Linux descritos neste ADR. |
| **Não comprovada** | Identidade remota, proteção efetiva do host/keystore, resistência a administrador/root, segurança de IPC em produção, origem/RP reais, UI e consentimento informado, independência frente à automação do agente, isolamento em Windows/Linux, WebAuthn com autenticadores reais, operação de revogação, alta disponibilidade, backups/restores e qualquer enforcement financeiro do provider. |

Os testes simulados apenas validam contratos/estados. Eles não elevam qualquer item projetado/não comprovado à categoria de garantia real.

## Especificação executável do broker remoto

### Identidade e autorização de cliente

Cada instância tem `broker_id`, chave/certificado de serviço provisionado fora do projeto e versão de protocolo. Clientes são identidades de instalação AgentFlow registradas (`client_id`, public key/cert, estado e scopes); certificado identifica o cliente, mas toda requisição também exige autorização explícita para principal, operação e ação. Chaves/certificados são rotacionáveis e revogáveis; tokens bearer de longa duração não são aceitos. O broker valida que o principal solicitante pode pedir aquela classe de operação; principal solicitante não é o aprovador. `project_id`, revisão, destino e scope são dados, não autoridade.

Transporte primário: HTTPS com mTLS 1.3, CA/pin de confiança provisionada, lista de cipher suites do SO/runtime homologado, hostname e certificado verificados, proteção contra downgrade. Mensagens incluem `protocol_version`, `client_id`, `request_id`, `operation_id`, timestamp, nonce aleatório de 256 bits, request digest e assinatura da mensagem pelo cliente. Limites de tamanho, clock skew (máximo 60 s), replay cache durável e rate limit são obrigatórios. Um nonce repetido, versão desconhecida, cert expirado/revogado, escopo divergente ou timeout resulta em bloqueio. Não usar HTTP local inseguro nem fallback para socket sem autenticação.

### APIs versionadas mínimas

- `POST /v1/approval-requests`: cria/idempotentemente recupera uma proposta. Campos: `request_id`, `operation_id`, `principal_id`, `action`, `scope`, `project_id`, `specification_revision_id/digest`, `plan_revision_id/digest`, `change_impact_digest`, `source/target provider+model`, `budget` com status de enforcement, `evidence_digests`, `expires_at` e `request_digest`. O servidor compara digests e conteúdo carregado da origem confiável; o cliente não pode fornecer texto livre como verdade de apresentação.
- `GET /v1/approval-requests/{request_id}`: consulta estado somente leitura (`pending`, `approved_unconsumed`, `consumed`, `denied`, `expired`, `revoked`, `unknown`) e receipt/digests. Não retorna chave de grant utilizável.
- `POST /v1/approval-requests/{request_id}/begin-webauthn`: somente UI/identidade do broker; cria challenge single-use, origin/RP fixos, UV obrigatória e TTL ≤120 s.
- `POST /v1/approval-requests/{request_id}/complete-webauthn`: aceita assertion apenas do componente UI autenticado do broker, verifica challenge/origin/RP/UP/UV/assinatura/revogação e transiciona idempotentemente; corpo de assertion nunca é retornado a cliente nem gravado em logs.
- `POST /v1/grants/{grant_id}/consume`: autenticação mTLS do cliente + verificação exata de operação/request digest/scope/digests/expiração; marca consumo uma única vez em transação e retorna receipt assinado. Mesmo cliente/operação recebe resultado idempotente sem novo consumo; request divergente é `409` e replay por outra operação é negado.
- `POST /v1/grants/{grant_id}/revoke`: ação de principal administrativo autenticado; revogação append-only e transacional, válida até commit de consumo.
- `GET /v1/operations/{operation_id}`: reconcilia resposta perdida/estado ambíguo; read-only, autenticado e retorna estado/receipt existente. Nunca reemite ou recria grant.
- `GET /healthz`: somente liveness sem detalhes de identidade, store ou credenciais; readiness privada informa disponibilidade mínima sem retornar segredos.

Todos os endpoints usam JSON canônico versionado, `Content-Type` estrito, máximo 64 KiB por request (e limite menor para metadados de assertion), esquema fechado sem propriedades ignoradas e respostas contendo `request_digest`, `broker_id`, `protocol_version`, `server_time` e ID de auditoria. Erros não refletem assertion, secrets ou conteúdo arbitrário. `unknown`/timeout não significa negado ou aprovado; cliente persiste intenção local antes da chamada e reconcilia pelo mesmo operation ID.

### Consentimento, expiração e grant

O Broker UI obtém conteúdo diretamente de revisão/proposta persistida e gera uma visão determinística. A UI apresenta sem truncar ação, projeto, diff semântico/impacto, IDs e digests, criteria/evidências, destino, custo/dados ausentes, duração e permissões. Usuário seleciona deliberadamente “aprovar esta operação” antes da ceremony; “cancelar/recusar” é estado terminal. Markdown, ANSI, HTML, nome de arquivo e conteúdo do agente são escapados como texto. Se o resumo completo exceder limites, não há aprovação por digest oculto: operação é bloqueada.

Enrollment requer sessão administrativa confiável, prova de principal e confirmação em autenticador com UV; credenciais são ligadas ao `principal_id`, RP/origin, algoritmo, chave pública e estado. Recovery de authenticator perdido/re-enrollment requer procedimento de duas pessoas ou identidade corporativa já estabelecida, revoga credenciais anteriores e emite audit event. Não usar aprovação pelo próprio agente, secret local, variável de ambiente, TTY ou click automatizável como alternativa.

O grant é uma autorização assinada pelo broker, interna ao broker: `grant_id`, `principal_id`, assurance, `action`, `operation_id`, `request_digest`, scope, digests de spec/plan/impact/evidence, destino, orçamento, `issued_at`, `expires_at`, nonce e broker key ID. TTL ≤120 s desde a assertion, expiração verificada usando relógio confiável do serviço e estado de revogação. AgentFlow recebe somente um `grant_reference` sem autoridade bearer. Consumo exige o broker online; modo offline nunca aceita grant cacheado.

### Esquema e atomicidade

Store transacional mínimo (SQL com isolamento serializável ou SQLite local ao serviço em volume exclusivo):

- `principals(principal_id, status, authn_policy, created_at)`;
- `client_identities(client_id, public_key/cert_thumbprint, scopes, status, not_before, not_after)`;
- `credentials(credential_id, principal_id, cose_public_key, alg, rp_id, sign_count, status, enrolled_at, revoked_at)`;
- `approval_requests(request_id, operation_id, principal_id, action, scope_digest, spec_digest, plan_digest, impact_digest, request_digest, status, expires_at, created_at, updated_at)`, chave única `(operation_id, action)` e request digest imutável;
- `challenges(challenge_id, request_id, challenge_digest, origin, rp_id, expires_at, used_at)` com nonce único/uso único;
- `grants(grant_id, request_id, signed_payload_digest, issued_at, expires_at, status)`;
- `grant_consumptions(grant_id PRIMARY KEY, client_id, operation_id, request_digest, receipt_digest, consumed_at)`;
- `revocation_events(event_id, object_type, object_id, principal_id, reason_code, occurred_at, operation_id)` append-only;
- `audit_events(event_id, actor_type/id, action_code, object_id, request_digest, result_code, occurred_at, previous_event_digest)` append-only;
- `idempotency_records(client_id, operation_id, request_digest, response_digest, status, created_at)` com unique por identidade/operação.

Emissão só grava grant e evento após assertion válida em transação. Consumo é uma transação que verifica grant + request + status + expiração + revogação, grava a única linha `grant_consumptions`, avança estado e grava evento/receipt antes do commit. Revogação concorrente serializa contra consumo: exatamente uma ordem vence e é registrada. Nenhuma transação fica aberta durante WebAuthn ou interação humana. Correlação com SQLite AgentFlow é por operation/request/receipt digests, não transação distribuída.

Se o broker commitou consumo e a resposta caiu, AgentFlow persiste `approval_consumption_unknown`, consulta status da mesma operação e aceita apenas receipt já gravado. Se o broker não pode provar estado, a operação permanece bloqueada para reconciliação; não se solicita nova aprovação com operação diferente automaticamente. Se store está corrompido, restore é incompleto ou auditoria falha, serviço fica não pronto e não emite/consome. Backup cifrado, snapshot transacional, teste de restore, retenção definida por política institucional e export append-only assinado são requisitos de operação.

### Credenciais, logs e retenção

O autenticador mantém a chave privada. A tabela `credentials` guarda somente chave pública e metadata necessária. Certificados de cliente/serviço e chaves de assinatura ficam em Windows CNG/TPM/DPAPI sob service SID ou secret manager/HSM; Linux em keystore de serviço/HSM/TPM via interface suportada, com key file de fallback proibido para host multiusuário não cifrado. Chaves não podem residir em `.agentflow`, workspace do agente, environment, linha de comando, logs, dumps ou backup plaintext. Core cliente não armazena assertion ou signed grant.

Auditoria guarda códigos de ação/resultado, IDs, digests, timestamps e identidade do ator; não registra challenge raw, assertion, headers, cert privado, payload de projeto, prompts, corpo de UI ou grant completo. Retenção inicial proposta: eventos de segurança pelo período de compliance configurado (mínimo operacional a fixar na implantação); dados de challenge expiram/removidos até 24 h; grants consumidos/revogados mantêm digests/metadados, sem secrets, pelo mesmo período auditável. A decisão de prazos/região/retention legal pertence ao operador antes da implantação.

### Implementação incremental proposta (sem autorização de implantação)

| Incremento | Componentes | Dependências | Aceite objetivo |
| --- | --- | --- | --- |
| RB-01 | ADR de implantação, threat model fechado, perfil do cliente e identidade broker | Escolha externa do domínio/host e owner do serviço | Security boundary aprovada; ameaças/fora-de-escopo e principal de cada componente listados; nenhuma chave criada ainda. |
| RB-02 | Contratos OpenAPI/JSON Schema v1, canonicalização, digest e client SDK `ApprovalAuthority` remoto | RB-01 | Schemas closed/versioned, limites, exemplos golden, erro unknown; simulador de contrato em CI. |
| RB-03 | Broker API + store + migrações transacionais + idempotency/replay ledger | Banco/backup/keystore escolhidos | Testes crash-injection, rollback, FK/integridade, concorrência consumo/revogação, operation ID divergente e restore passam. |
| RB-04 | mTLS, provisioning, rotação/revogação de client identity, rate limits | PKI e processo de provisionamento | Cert inválido/revogado/fora de scope recusado; sem downgrade; auditoria sem secrets; Windows/Linux integração com credenciais de teste. |
| RB-05 | Enrolment WebAuthn/FIDO2 e verificador | RP ID, origin HTTPS, autenticadores e política de identidade aprovados; implementação WebAuthn revisada por segurança | Testes cryptographic vector de challenge/type/origin/RP/UP/UV, replay, expiry, credential revocation; sessão nenhum segredo exposto. |
| RB-06 | Broker UI e resumo transacional | RB-02/05 e origem confiável para dados/diffs | UI não recebe markup ativo; teste truncation/injection; pessoa vê exatamente operation/digests/destino/budget; automatização pelo agent host demonstravelmente fora do domínio. |
| RB-07 | Grant issuance/consume/revoke e AgentFlow remote authority client | RB-03–06 | Uma única consume concorrente; idempotent response após perda de resposta; operation/scope/digest alterado bloqueia; facade local continua fail-closed. |
| RB-08 | Recovery, backup, audit export, observabilidade e hardening | RB-03–07 | Queda antes/depois de cada commit recupera deterministicamente; unknown nunca reaprovado; restore e retenção verificados; scan de logs sem credentials/bearers. |
| RB-09 | Homologação real Windows e Linux em domínio de teste isolado | Ambiente, admin e authenticators de teste especificamente autorizados | Testes de peer spoof, same-user/elevated access, UI control, key/store ACL, revogação/corrida/crash e WebAuthn real passam na versão/SKU suportada; evidência anexada sem segredo. |
| RB-10 | Piloto controlado e avaliação de segurança externa | Aprovação explícita de implantação/enrollment, resposta a incidente e rollback | Operador aprova deployment, RP/origin, identidades e retenção; piloto sem produção inicialmente; kill switch remove autoridade e mantém fail-closed. |

Nenhuma etapa RB-01–RB-10 está implantada ou autorizada para enrollment/produção. Etapas RB-04 a RB-10 precisam de autorização operacional específica quando envolverem host remoto, PKI, credenciais reais, UI ou usuários. Os critérios acima permitem preparar código isolado e testes simulados após escopo aprovado; não convertem esta especificação em aprovação de segurança.
