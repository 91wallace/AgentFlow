# MVP-16 — Consentimento confiável e autoridade Broker/TEG

## Resultado

**Classificação: `BLOCKED`.** Nenhuma autoridade de aprovação, consentimento
operacional ou emissão de grant foi ativada. O fluxo MVP-15 permanece em
`awaiting_authorization`; dispatch continua bloqueado.

Este resultado decorre de dependências normativas e operacionais específicas,
não de falta de implementação de uma facade local. Transformar o Broker atual,
uma tela local ou um processo filho sob a mesma identidade em autoridade de
consentimento contrariaria a fronteira aprovada.

## Contratos e componentes encontrados

- `docs/decisions/0008-phase-7-routing-and-approval.md`, emenda RB-09C.1-R2,
  aprova a arquitetura-alvo B: Broker e TEG em domínios administrativos
  independentes. A emenda diz expressamente que essa decisão não prova que a
  separação exista nem autoriza RB-09C.2. Também exige que aprovador/dispositivo
  sejam independentes do host do agente.
- O mesmo ADR registra que autenticação mTLS identifica o peer do canal; não
  prova consentimento. A verificação WebAuthn atualmente implementada é
  autenticação de uso único: retorna `consent_granted=false` e
  `approval_created=false`.
- `docs/rb-09c1-r6-contract-closure.md` é uma proposta documental. H-01–H-10
  permanecem decisões pendentes; R6 não autoriza implantação, enrollment,
  grants ou RB-09C.2.
- `docs/rb-09c1-r8v-independent-review.md` rejeita R8 como fechamento completo.
  H-01 (wire/crypto), H-03 (raízes, custódia e autoridade/ECA), H-08
  (infraestrutura e operadores) e H-10 (go/no-go) continuam sem aprovação. O
  documento registra que não há infraestrutura nem identidades independentes
  selecionadas.
- `ApprovalBroker` e `UnavailableApprovalAuthority` são facades fail-closed.
  Elas não emitem grants. `verify_and_consume` consulta disponibilidade antes
  de examinar a referência do grant.
- `agentflow.teg.gateway.UnavailableGatewayAuthority` nega consulta,
  reservation, confirmação e reconciliação. O protocolo TEG documenta que a
  autenticação de respostas pertence ao transporte externo; esse cliente e essa
  fronteira autenticada não estão implementados.
- O Broker operacional MVP-15 aceita propostas pendentes por mTLS em loopback,
  mas responde `authority_status=unavailable` e `dispatch_allowed=false`. Seu
  protocolo não expõe consentimento ou decisão de aprovação.
- A apresentação de consentimento existente é somente leitura, marcada como
  demonstração não homologada e sem formulário/botão. O verificador WebAuthn
  não cria `approval_decisions` nem `execution_authorizations`.

## Avaliação por camada

| Camada | Evidência atual | Resultado MVP-16 |
|---|---|---|
| A. Implementação funcional | MVP-15 persiste proposta e estado `WAITING_APPROVAL`; Broker mantém `pending`. A facade de autoridade continua indisponível. | A integração até aguardar autoridade funciona; consentimento/grant não implementados. |
| B. Integração entre processos | Broker local usa loopback e mTLS, mas isso prova autenticação do canal, não domínio independente. Não foi instalado serviço separado. | Não homologada como autoridade independente. |
| C. Isolamento de credenciais e estado | Broker e APL locais estão no mesmo host; não há custodiante, identidade administrativa, armazenamento/chave de autoridade ou recovery independentes provisionados. | Não comprovado. Nenhuma ACL/permissão do sistema foi alterada. |
| D. Autenticação e consentimento humano | Há preview determinístico e testes com assertion WebAuthn sintética; assertion confirma autenticação, nunca consentimento. Não há enrollment operacional nem dispositivo/aprovador homologados. | Consentimento operacional indisponível. Fixtures sintéticas não são prova de identidade real. |
| E. Homologação independente Broker/TEG | ADR requer domínios administrativos distintos. Não existem identidades, operadores, infraestrutura, canais, raízes e recuperação selecionados; H-01–H-10 pendentes e R8 rejeitada. | Bloqueada; sem teste de independência operacional. |

## Bloqueio concreto

O ambiente local atual não fornece nem tem provisionados dois domínios
administrativos independentes, operadores distintos, credenciais/chaves sob
custódia separada, armazenamento protegido, canal de bootstrap/recovery externo
ou dispositivo confiável de aprovador. Um segundo processo, conta nominal,
certificado mTLS local, SQLite separado ou prompt não satisfaz a decisão B. A
instrução MVP-16 também não autoriza instalar serviços persistentes nem alterar
permissões do sistema; portanto, não se tentou fazê-lo.

Além disso, os wire contracts COSE/CBOR, raízes/keys, semântica completa de
consentimento, revogação/status, ancoragem e campanha operacional estão em
documentos propostos/rejeitados, não em contratos aprovados para ativação. O
`GrantReference` local não é uma credencial de autorização e não existe issuer
operacional validado. Implementar emissão/consumo com os dados atuais criaria
uma autoridade fictícia e enfraqueceria os requisitos da Fase 7.

## Testes e interpretação

Os testes focados existentes verificam que:

- a autoridade indisponível retorna `available=false`, rejeita a verificação e
  falha no consumo antes de inspecionar um grant;
- configurar variáveis locais não habilita o Broker;
- uma assertion WebAuthn válida é one-shot e autentica o principal, mas não cria
  decisão nem autorização;
- o preview não altera o SQLite e não contém ação de aprovação;
- o Broker de propostas mantém `authority_status=unavailable` e dispatch falso.

Esses testes são evidência de fail-closed local, não de separação de processos,
identidades, ACLs, custódia, consentimento humano real ou independência
administrativa. Os testes focados desta retomada passaram: `64 passed` em
`test_phase7_sdd.py`, `test_approval_presentation.py`,
`test_approval_webauthn.py` e `test_approval_transport.py`. A suíte completa será
registrada após sua execução: `473 passed` (`python -m pytest -q`).

## Próximas dependências para retomar

1. Deliberar/aprovar as decisões pendentes H-01–H-10 e encerrar os blockers da
   revisão independente; aprovação B do ADR é apenas a arquitetura-alvo.
2. Designar operadores/custodiantes e selecionar infraestrutura/control planes,
   roots/KMS e recovery independentes para Broker, TEG e aprovador; documentar
   autoridade superior compartilhada e break-glass.
3. Aprovar o perfil criptográfico, o contrato de consentimento/grant, revogação,
   validade/relógio e vínculo exato de identidade, proposta, revisão, digest,
   escopo e operações.
4. Autorizar separadamente o provisionamento do laboratório, identidades,
   credenciais sintéticas, serviço e dispositivo de aprovação; homologar testes
   negativos de acesso cruzado e recuperação com avaliador independente.
5. Só após esses gates, conectar uma autoridade real à interface existente e
   validar persistência, replay, concorrência e revogação. O dispatch permanece
   fora deste MVP e depende de autorização própria.

## Preservação

Branch observada: `feat/intelligent-model-routing-phase-7`; HEAD:
`b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`. O working tree já continha diversas
alterações locais e arquivos não rastreados antes desta etapa; foram
preservados. Nenhum serviço, provider, job, grant, chave real, instalação ou
mudança de permissão foi criado. Nenhuma operação de histórico Git foi feita.
