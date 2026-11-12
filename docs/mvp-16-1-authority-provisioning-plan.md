# MVP-16.1 — Preparação de provisionamento Broker/TEG

## Resultado

**Classificação: `AUTHORITY_PREPARATION_IMPLEMENTED` — preparação local concluída; autoridade operacional ainda bloqueada.**

Este MVP implementa validadores de preparação não privilegiados e um procedimento de provisionamento. Não instala serviços, cria identidades ou credenciais, emite grants, aprova propostas ou libera dispatch. `UnavailableApprovalAuthority` e `UnavailableGatewayAuthority` continuam sendo os defaults.

## Base normativa e limites

- ADR 0008 R2 aprova a arquitetura-alvo B: Broker e TEG em domínios administrativos independentes. Não atesta que essa independência exista nem autoriza RB-09C.2.
- R6 é uma proposta de fechamento; `RESOLVED` nela significa especificado para deliberação, não aprovado ou homologado.
- R8 permanece proposta e a revisão independente R8-V a rejeitou como fechamento completo. Em particular, H-01, H-03, H-08 e H-10 continuam sem resolução aprovada. Propostas R3/R5/R6/R8 não alteram os contratos Python v1.
- RB-09C não foi alterada. O protocolo operacional local permanece JSON/digest v1; não implementa COSE/CBOR v2, assinatura de grant, ECA, StartPermit ou testemunhas independentes.

## Componentes preparados

`agentflow.approval.preparation` fornece:

- `ServiceDomainConfig`, `ApproverDeviceConfig` e `AuthorityProvisioningManifest`: configuração tipada, sem campos para segredos, com rejeição de endpoints locais/não HTTPS, fingerprints malformados e sobreposição declarada de domínios, serviços, operadores, raízes, certificados, custódia, recovery, aprovador e owners.
- `AuthorityProvisioningManifest.from_toml`: leitura estrita de schema v1 e rejeição de chaves desconhecidas. O manifesto é uma declaração de preparação, não evidência de IAM, custódia ou independência implantada.
- `verify_tls_peer_pin`: exige socket TLS com validação de cadeia e hostname e compara o certificado público peer com o pin declarado. O resultado afirma apenas identidade TLS observada naquela conexão; `administrative_independence_verified` e `accepted_for_authority` são sempre falsos.
- `inspect_grant_binding`: compara request, decision e authorization v1 quanto a estado, principal distinto, operation ID, digests, audiência, destino e janela temporal. O relatório é somente estrutural: autenticidade criptográfica, estado atual de revogação e consumo anti-replay são sempre não verificados; `authorized` é sempre falso.

Esses componentes não são conectados a um caminho de aprovação, consumo de grants, TEG ou dispatch. Digests de contratos v1 detectam inconsistência acidental, mas não autenticam o emissor.

## Matriz H-01 a H-10

| H | Requisito | Evidência local deste MVP | Estado e dependência restante |
|---|---|---|---|
| H-01 — wire/cripto | Perfil canônico, COSE/CBOR, Ed25519, limites e migração | Validação estrutural v1 apenas; teste de bindings e rejeição de divergência | **Pendente**: decisão normativa aprovada, segunda implementação independente, vetores completos, migração/rollback e revisão especializada. R8 não foi aprovada. |
| H-02 — receipts | Fonte de verdade, semântica/cardinalidade GER/ER e `unknown` | Nenhum receipt emitido; nenhuma integração com provider | **Pendente**: schema/emissores/policy aprovados e evidência independente do provider/ledger. |
| H-03 — raízes/keys/ECA | Bootstrap externo, custódia, trustees, recovery, break-glass e autoridade | Manifesto requer referências distintas e configuração de pins; nenhuma chave operacional ou root foi criada | **Pendente**: designar owners/custodians, escolher KMS/HSM/registry/canais, implementar e testar bootstrap, rotação, revogação e recovery fora do domínio do agente. |
| H-04 — revogação | Bloqueio de novos starts vs quiescência, in-flight e fencing | Nenhuma operação de revogação/grant implementada | **Pendente**: aprovar semântica e provar fencing/cancelamento no provider; `unknown` deve permanecer bloqueado. |
| H-05 — tempo/TTL | Fonte confiável, incerteza/skew e limites | Checker estrutural rejeita cadeia expirada usando relógio fornecido; não valida relógio confiável | **Pendente**: escolher fonte temporal monitorada, limites, TTL e comportamento em incerteza. Relógio cliente não pode ser fallback. |
| H-06 — anchor/witness | ACK durável, proofs, quorum e continuidade | Campo de anchor apenas declarativo no manifesto; nenhum witness/ACK | **Pendente**: aprovar protocolo e quorum, operadores/pins independentes, retenção e executar campanha de split-view/rollback. Quorum 3/4 em R8 continua proposta. |
| H-07 — retenção/custo | Retenção, quotas, privacidade, legal hold e recovery | Sem persistência de autoridade/auditoria neste MVP | **Pendente**: política e custos aprovados, modelo minimizado, restore/eliminação e exceções com owner definido. |
| H-08 — infraestrutura/operadores | Domínios, control planes, principals, dispositivo e break-glass | Manifesto rejeita sobreposições *declaradas*; teste não inspeciona IAM real | **Pendente**: selecionar infraestrutura/operadores/dispositivo e comprovar acesso cruzado, control planes, recovery e isolamento por avaliador independente. ADR B não é essa prova. |
| H-09 — campanha/evaluator | Contagens, fixtures, evidência externa e poder de parada | Testes locais determinísticos cobrem somente validadores de preparação; não são campanha de segurança | **Pendente**: aprovar manifesto/contagens (inclusive C-20=65 proposto em R8), orçamento e avaliador independente; executar campanha preregistrada. |
| H-10 — go/no-go | Decisão explícita após H-01…H-09 | Nenhum gate alterado | **Pendente**: fechar blockers e obter decisão explícita do proprietário e revisão independente antes de qualquer próxima etapa. Não autoriza deploy, enrollment ou execução por si só. |

## Procedimento de provisionamento proposto

O procedimento é um plano; nenhum comando abaixo foi executado. Placeholders devem ser preenchidos somente após as decisões de H-01…H-10 e autorização específica para provisionar.

### Recursos e papéis

1. Selecionar control planes e tenants separados para Broker e TEG, mais serviço/operadores de anchor apropriados. Registrar administradores, superadmins, break-glass, recovery, redes, backups e dependências comuns em manifesto fechado.
2. Nomear, por decisão explícita, operadores distintos por domínio, custodians/trustees, aprovador humano com dispositivo independente do host AgentFlow e ao menos dois owners de recovery que não sejam operadores/aprovador. Uma lista declarada não substitui prova IAM.
3. Dimensionar serviços, armazenamento transacional, backups, observabilidade sem segredos, fonte temporal e custos/retention conforme H-02/H-05/H-06/H-07. Nenhum provider de execução deve ser habilitado por este provisionamento.
4. Escolher roots e custódia gerenciada (KMS/HSM quando aprovado), registry de bootstrap externo e canais de rotação/revogação. Chaves privadas não devem existir no workspace, configuração TOML, SQLite do projeto ou logs.
5. Definir conectividade mínima: AgentFlow pode submeter/consultar propostas pendentes; autoridade de consentimento valida dados diretamente do Broker; TEG e ECA (se aprovado em contrato posterior) não confiam em declaração do APL. Negar rotas não inventariadas. O endpoint de serviço deve usar identidade TLS autenticada, pin/rotação e verificação de revogação aprovados.

### Sequência controlada

1. Congelar decisões aprovadas, versões de protocolo, vetores e manifesto de teste; obter aceite do avaliador independente.
2. Provisionar os domínios por administradores respectivos, cada qual usando sua identidade/tenant/control plane e armazenamento próprios. Não compartilhar credenciais administrativas, state stores, chaves ou recovery root.
3. Provisionar identidade de serviço e certificados em cada domínio por seu custodiante. Registrar somente IDs e fingerprints públicos em configuração; obter pin/bootstrap por canal externo verificado por dois verificadores, conforme decisão aprovada.
4. Provisionar dispositivo e enrollment do aprovador através de procedimento humano explícito no serviço Broker confiável. Nunca aceitar auto-enrollment pelo APL, assertion sintética como consentimento, ou chave privada exportada.
5. Instalar serviços como deployment separado e revisado, com versão/artifact digest fixos, permissões mínimas, store exclusivo e backup/recovery testados. A instalação exige autorização separada e não faz parte deste MVP.
6. Realizar validação independente antes de qualquer modo operacional: teste negativo de acesso cruzado (incluindo same-user/admin relevante), identidade de peer e rotação, UI/apresentação íntegra e não controlável pelo agente, consentimento WebAuthn real, revogação/replay/concorrência, relógios, crash/restore, logs e zero dispatch nos testes.
7. Exportar evidências assinadas pelo avaliador e pelos domínios independentes; comparar com critérios aprovados e registrar findings. Falha, timeout, divergência, resposta perdida ou `unknown` mantém `UnavailableGatewayAuthority` e bloqueio.
8. Fazer decisão go/no-go específica para próxima fase. Mesmo após eventual autoridade homologada, emissão/consumo de grant e dispatch requerem aprovação e escopo próprios; este MVP não libera nenhum deles.

### Esboço não executado de configuração

O formato aceito por `AuthorityProvisioningManifest.from_toml` contém somente identificadores, endpoints HTTPS e fingerprints públicos. Deve ser gerado a partir de inventário aprovado; não incluir PEM privado, token, senha, cookie ou segredo.

```toml
schema_version = 1
recovery_owners = ["<owner-1>", "<owner-2>"]

[broker]
domain_id = "<broker-domain>"
service_id = "<broker-service>"
operator_principal = "<broker-operator>"
endpoint = "https://<broker-host>/<path>"
trust_root_fingerprint = "sha256:<64-hex-lowercase>"
service_certificate_fingerprint = "sha256:<64-hex-lowercase>"
key_custody_reference = "<external-reference>"
recovery_reference = "<external-reference>"

# [teg] e [audit_anchor] usam os mesmos campos e domínios independentes.
# [approver] contém principal_id, device_id, rp_id e origin aprovados.
```

Não usar esta amostra para conectar a serviços ou declarar prontidão. `preparation_only` é invariavelmente true.

## Testes e limitações de evidência

Testes adicionados em `tests/test_authority_preparation.py` cobrem configuração declarativa, colisões de identidade declaradas, endpoint local/não HTTPS, vínculos request/decision/authorization, expiração, flags sempre fail-closed e facade indisponível. Um teste adicional em `tests/test_approval_transport.py` usa certificados efêmeros e servidor loopback para verificar cadeia TLS, hostname e pin do peer; ele também confirma que esse resultado não é aceito como autoridade. São fixtures sintéticas e não comprovam identidade humana, assinatura, revogação remota, anti-replay em armazenamento operacional, custódia, IAM, independência administrativa ou UI confiável.

Validação executada após a última alteração de código: `.venv\Scripts\python.exe -m pytest -q tests/test_authority_preparation.py tests/test_approval_transport.py` — **40 passed**; `.venv\Scripts\python.exe -m pytest -q` — **486 passed**; `git diff --check` — **passou**, com avisos Git de conversão LF/CRLF em arquivos locais previamente modificados. `py_compile` dos arquivos Python novos — passou. Nenhum teste habilitou uma autoridade, grant, aprovação ou dispatch.

## Decisões e recursos que exigem o proprietário

1. Deliberar H-01–H-10 individualmente e determinar a precedência/aprovação de contratos sem adotar automaticamente R6 ou R8 rejeitada. H-10 deve ser go/no-go explícito.
2. Escolher e financiar os tenants/control planes/serviços, região/retention e avaliador independente; nomear operadores Broker/TEG/anchor, custodians, trustees e recovery owners.
3. Aprovar a fonte e custódia das raízes/chaves, bootstrap externo, rotação, revogação, canal de recovery e modelo de break-glass.
4. Escolher identidade do operador e dispositivo/plataforma de aprovação; aprovar enrollment, UX de apresentação, política WebAuthn e como comprovar que o agente não controla a UI/dispositivo.
5. Aprovar semântica de grant, revogação, relógio/TTL, receipts, anchor/quorum, retenção e campanha/limites de custo, com avaliador capaz de bloquear a homologação.
6. Autorizar separadamente qualquer provisionamento que crie serviços, contas, credenciais/chaves, ACLs, custos ou tráfego externo. A autorização desta preparação não é autorização operacional nem consentimento para proposta.
7. Depois da homologação e decisão H-10, deliberar uma fase separada para implementação/ativação de autoridade e, separadamente, qualquer execução/dispatch.

## Preservação e execução

Nenhum serviço, conta, ACL, chave operacional, provider ou job foi criado/ativado. Nenhum arquivo de RB-09C foi alterado intencionalmente por esta preparação; os contratos em discussão continuam não homologados. O working tree possuía alterações locais antes desta etapa e todas foram preservadas. Commit, push, troca de branch e operações destrutivas de Git não foram executadas.
