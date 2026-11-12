# MVP-17 — Contratos de autorização local

## Estado

Implementação funcional de contratos e persistência para `LOCAL_SUPERVISED`, ainda sem homologação operacional. O componente não está conectado à CLI, ao APL, ao executor, ao Broker mTLS nem ao dispatch. `UnavailableLocalAuthorizationAuthority` continua sendo o padrão e informa `dispatch_enabled=false`. Nenhum job real foi executado.

Esta implementação local não satisfaz nem reduz os requisitos H-01–H-10 de `INDEPENDENT_AUTHORITY`. A decisão ADR 0008-R permite desenvolver o perfil local, mas esta etapa não o declara homologado.

## Interfaces e contratos

O módulo público está em `agentflow.security.authorization` e é reexportado por `agentflow.security`:

- `LocalAuthorizationAuthority`: serviço de domínio com dependências explícitas.
- `LocalAuthorityPolicy` / `LocalAuthorizationPolicy`: limites e allowlists confiáveis para operadores e consumidores.
- `LocalProposalResolver` e `ResolvedLocalProposal`: resolução autoritativa de plano, revisão, workspace, operações e principal proponente.
- `LocalOperatorAuthenticator` / `AuthenticatedOperator`: identidade retornada pelo autenticador, nunca aceita como identidade auto declarada pelo APL.
- `LocalConsentRequest` e `LocalConsentReceipt`: pedido imutável e ação explícita, vinculados ao perfil local.
- `LocalGrant`: contrato JSON versionado, digest canônico e assinatura Ed25519.
- `LocalRevocationRequest`: revogação explícita, autenticada e vinculada ao digest do grant.
- `LocalConsumerAuthenticator` / `AuthenticatedConsumer`: identidade e prova do consumidor para consumo único do grant.
- `LocalConsentAuditRecord`: registro imutável de decisão.
- `Ed25519LocalGrantSigner` / `Ed25519LocalGrantVerifier`: wrappers que recebem chaves fornecidas externamente; não criam nem armazenam chaves operacionais.

A assinatura autentica um grant com escopo mínimo e validade limitada. O digest liga perfil, proposta, digest do plano, revisão, workspace, operações, limites, operador e datas. O autenticador de operador recebe um desafio que também liga a decisão exata, ação explícita e validade do recibo. O proponente é resolvido pelo colaborador confiável e não pode aprovar a própria proposta.

## Persistência e recuperação

`BrokerStore` usa a migração aditiva SQLite schema v4 → v5. Decisão, grant, estado inicial e eventos do journal são gravados na mesma transação `BEGIN IMMEDIATE`. Índices únicos impedem replay de request digest, operation ID, grant e decisão. Registros de decisão e grants possuem triggers contra update/delete; transições são eventos append-only no journal encadeado existente. A abertura do store verifica integridade SQLite, cadeia do journal e projeções de estado.

Estados de grant: `approved` → `revoked`, `expired` ou `consumed`; estados terminais não retornam a `approved`. Replay de consentimento válido retorna o grant já persistido. Replay conflitante é negado. Consumo é autenticado e de uso único; aqui ele só marca o estado `consumed` e não executa nada.

## Limites e ameaças

- A autoridade é uma classe separada do APL/executor por interface, mas nesta etapa ainda pode residir no mesmo processo/identidade. Isso **não prova separação de processo ou de domínio administrativo**.
- Os autenticadores reais de operador/consumidor, a UI local de consentimento, armazenamento protegido de chaves e rotação não foram implementados. Os defaults são indisponíveis.
- Chaves efêmeras das provas existem somente em testes sintéticos; nenhuma chave operacional foi gerada.
- A autoridade não está ligada ao Broker de rede nem ao TEG; a autoridade local positiva permanece desligada na aplicação.
- O grant não é um teto financeiro garantido pelo provider. `estimated_budget` é limite declarado/estimado validado pela política, não enforcement financeiro externo.
- A assinatura do grant não transforma o host local em fronteira confiável contra outro processo privilegiado sob o mesmo usuário.
- Nenhum dispatch, execução real, approval em CLI, Production Mode ou provider foi habilitado.
- `INDEPENDENT_AUTHORITY`, seus contratos e H-01–H-10 não foram alterados.

## Testes

O módulo `tests/test_local_authorization.py` usa SQLite temporário, autenticadores simulados e chave Ed25519 efêmera de teste. Ele verifica concessão, negação, identidade, digest/revisão/escopo, autoaprovação, replay, concorrência, validação de grant, consumo único, revogação, expiração, persistência após reinício e rollback de falha de journal. Esses testes validam contratos e comportamento local; não são homologação de autenticação humana nem de isolamento operacional.

Comando focado:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_local_authorization.py tests/test_security_profile.py tests/test_approval_persistence.py tests/test_approval_webauthn.py tests/test_approval_identity.py -q
```

## Próximas dependências para homologação

1. Implementar UI de consentimento que apresente digest, revisão, workspace, operações, limites e validade, e autentique uma ação explícita contra o desafio exato.
2. Escolher e homologar autenticadores/identidades locais em Windows e Linux; não usar identidade declarada pelo APL.
3. Proteger chaves de assinatura e configuração de allowlists por mecanismo do sistema operacional.
4. Integrar resolvedor autoritativo ao Planning Store/Broker e definir revogação concorrente/reinício no serviço.
5. Obter revisão de segurança do perfil LOCAL_SUPERVISED antes de conectar qualquer validador a dispatch.
6. Manter `INDEPENDENT_AUTHORITY` bloqueado até homologação independente própria.
