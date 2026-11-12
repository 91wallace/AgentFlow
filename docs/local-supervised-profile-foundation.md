# LOCAL_SUPERVISED — primeira fatia

## Estado

**`LOCAL_PROFILE_FOUNDATION_IMPLEMENTED` como fundação de configuração e contratos; perfil não homologado.** A seleção não habilita Broker de autoridade, UI de consentimento, grant, provider ou dispatch. Execução real continua fora desta fatia.

O registro da aprovação arquitetural está em [ADR 0008-R](decisions/0008r-local-self-sufficient-and-executable-mvp.md). O perfil `INDEPENDENT_AUTHORITY` continua distinto e indisponível sem homologação própria; H-01–H-10 permanecem pendentes para ele.

## Configurar explicitamente

Em um repositório AgentFlow já inicializado, escolha uma única vez:

```powershell
agentflow profile select --profile LOCAL_SUPERVISED --confirm
```

Ou, quando suportado pelo CLI Linux:

```sh
agentflow profile select --profile LOCAL_SUPERVISED --confirm
```

O comando cria `.agentflow/security-profile.toml` sem sobrescrever seleção existente. O caminho pode ser informado com `--path <repo>`. Um estado existente exige revisão manual; a CLI não oferece troca in-place nesta fatia.

Conteúdo persistido:

```toml
schema_version = 1
profile = "LOCAL_SUPERVISED"
```

Para consultar:

```sh
agentflow profile status --path <repo>
```

O resultado JSON inclui perfil, estado da configuração e flags `authority_available`, `grant_issuance_enabled` e `dispatch_enabled`, todas `false`. `agentflow doctor` também inclui o perfil em seu diagnóstico. Um arquivo ausente, inválido, ambíguo, com versão/campo desconhecido ou perfil não reconhecido não seleciona default: o status é `invalid_or_missing`, a operação `profile status` retorna erro e o dispatch permanece indisponível.

Selecionar `INDEPENDENT_AUTHORITY` somente registra a seleção pretendida; o diagnóstico permanece `configured_but_authority_unavailable`. Não há fallback entre perfis.

## Contratos públicos

Em `agentflow.security`:

- `SecurityProfile` e `ProfileConfiguration`: seleção tipada/versionada.
- `load_profile_configuration`, `save_profile_configuration`, `profile_diagnostic`: persistência create-only e diagnóstico sem segredos.
- `LocalConsentRequest`: binding imutável de ID/digest/revisão da proposta, workspace, operações allowlisted, limites, validade e identidade reivindicada do operador. Seu `request_digest` cobre todos esses campos.
- `LocalConsentReceipt` e `inspect_consent_receipt`: representação e inspeção estrutural de uma resposta explícita. O resultado diferencia binding/ação explícita de identidade autenticada e nunca autoriza execução.
- `LocalConsentInterface` e `UnavailableLocalConsent`: interface de extensão com implementação padrão que retorna indisponível.
- `ProfileDiagnostic`: autoridade, emissão de grant e dispatch permanecem falsos. Os diagnósticos também são enviados ao logger `agentflow.security.profile` sem dados sensíveis.

Os contratos de consentimento são entrada/registro não confiável. O campo `operator_identity` ainda não é autenticado; um receipt sintético ou criado pelo APL não é consentimento válido. O código não contém chave, verificador de assinatura, persistência de grants ou conexão ao executor.

## Separação e falha segura

`LOCAL_SUPERVISED` é uma configuração de perfil, não prova de separação do processo. O host local e o SO são pressupostos confiáveis; administrador/root e comprometimento do host estão fora da garantia. `INDEPENDENT_AUTHORITY` exige domínios administrativos independentes e evidência H-01–H-10, sem redução.

Essa fatia não altera `UnavailableApprovalAuthority`, `UnavailableGatewayAuthority`, Broker, TEG, RB-09C, Production Mode ou `RestrictedInferenceHarness`. Nenhum consentimento é criado por prompt, planner, CLI de perfil ou executor. Nenhum job é disparado.

## Validação

Os testes `tests/test_security_profile.py` verificam seleção explícita, rejeição de ausência/ambiguidade, persistência/reabertura, bloqueio de sobrescrita, diagnóstico/log, contratos de consentimento sem autenticação/autorização e CLI. `tests/test_authority_preparation.py` mantém a checagem estrutural preparatória distinta. Validação: `.venv\Scripts\python.exe -m pytest -q tests/test_security_profile.py tests/test_foundation.py tests/test_authority_preparation.py` — **46 passed**; `.venv\Scripts\python.exe -m pytest -q` — **501 passed**; `py_compile` dos arquivos Python da fatia passou; `git diff --check` passou com avisos LF/CRLF em arquivos locais preexistentes.

## Próximas dependências

1. Especificação aprovada do contrato local de consentimento/grant/revogação e sua relação precisa com F7-01/RB-09C.
2. Broker local com identidade de serviço/armazenamento separados e UI que apresente integralmente o digest e limites ao operador.
3. Autenticação comprovável do operador, store de autoridade, journal e recuperação por plataforma.
4. TEG local com validação do issuer, uso único, revogação, limites e falha `unknown` sem retry automático.
5. Homologação adversarial separada em Windows e Linux; PRoot não é presumido como fronteira.
6. Autorização operacional específica antes do primeiro dispatch sintético supervisionado.

Nenhuma dessas etapas é iniciada por este documento.
