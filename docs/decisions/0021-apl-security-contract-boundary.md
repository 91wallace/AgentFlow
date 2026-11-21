# ADR 0021 — M2.3: remover o ciclo APL/security

Status: terceira fatia de M2 autorizada pelo proprietário em 2026-11-21,
após [M2.2](0020-approval-security-contract-boundary.md). Alterações anteriores
permanecem preservadas no working tree; esta etapa não cria commit ou push.

## Problema e direção

O resolver de grants de APL importava `LocalGrant`, `SecurityProfile` e
`ExecutionPlanConsentBinding` de security. Os serviços de consentimento de
security importavam serviços/modelos de APL. Os tipos de aprovação já têm
implementação pura em approval desde M2.2; consumir os aliases de security
mantinha a dependência reversa sem necessidade.

O resolver passa a importar esses três tipos de `approval.contracts`. Security
continua dependendo de APL em seus serviços de consentimento; APL não importa
security. Não há relocação do resolver para security com reexport tardio, nem
imports dinâmicos que escondam o ciclo.

## Contratos de limites e orçamento

`apl.execution_contracts` contém somente `ExecutionLimits`, `TaskExecutionLimits`,
`FinancialBudgetMode` e `FinancialBudgetPolicy`, extraídos sem mudar seus corpos.
O módulo usa biblioteca padrão, sem SQLite, processos, serviços ou criptografia.
O teste arquitetural o protege como contrato puro.

`security.local_consent_review` consome esses valores diretamente, sem importar
o módulo de execução por causa de quatro tipos. Os nomes antigos em
`apl.authorized_execution` permanecem aliases das mesmas classes. A fachada APL
resolve esses quatro exports diretamente no módulo puro, preservando `__all__`
e identidade, sem carregar o store. Módulos de definição mudam; referências
pickle antigas continuam resolvendo os aliases históricos.

`approval.contracts` passa também a reexportar binding e enum de perfil já
existentes em `approval.consent_contracts`. Isso não acrescenta implementação
de segurança ou outro tipo de autorização ao fluxo local.

## Remapeamento explícito do baseline

Conforme a regra de extrações autorizadas da ADR 0017, este é o mapeamento
revisto, sem regenerar o baseline nem relaxar o auditor:

| Arestas anteriores | Aresta substituta |
| --- | --- |
| `local-to-advanced\|agentflow.apl.authorized_execution\|agentflow.security.authorization` | `local-to-advanced\|agentflow.apl.authorized_execution\|agentflow.approval.contracts` |
| `local-to-advanced\|agentflow.apl.authorized_execution\|agentflow.security.profile` | A mesma aresta de contrato acima, contada uma vez. |

A regra M0 classifica todo o pacote approval como avançado, inclusive contratos
puros. Essa classificação continua: a dependência opcional do resolver fica
visível, agora com um único destino inferior em vez de dois módulos de security.
Não se cria allowlist geral para imports puros nem se usa a alteração para
liberar imports de implementações. A nova entrada é exclusivamente essa
substituição revisada; as duas entradas anteriores são removidas.

As seis exceções `package-cycle` restantes também são removidas, pois não há
mais componentes fortemente conexos entre pacotes. As regras permanecem e novas
violações continuam falhando. Total: **17 → 10 exceções**, com **zero ciclos**.
As outras dívidas (agente/store concretos, seleção/planning e pontes avançadas)
continuam explícitas; M2 não afirma que toda a arquitetura está desacoplada.

## Invariantes e critérios de saída

- Resolução ainda exige grant v2, consentimento persistido, estado aprovado,
  assinatura válida, validade temporal, revisão/digest/objetivo, identidade do
  workspace, executor e quotas exatos. Não substitui esses gates por contexto.
- Modos financeiros e mensagens são preservados: estimativa continua sem provar
  hard cap; hard cap indisponível impede dispatch. Limites/retomada mantêm valores.
- Os corpos do resolver, controller e store não mudam, exceto destinos de import.
  Sem alteração de schemas, transações, journal, digests, fencing ou recovery.
- AST encontra zero ciclos entre pacotes e zero imports de security em APL,
  inclusive para tipagem. Regressões de M2.1/M2.2 continuam bloqueadas.
- Processo novo carrega os limites pela fachada APL com SQLite, approval,
  security e criptografia bloqueados. Outro exercita a rejeição de assinatura
  inválida no resolver, com security/crypto bloqueados, sem chegar a efeito externo.
- Aliases e referências pickle antigas dos quatro tipos permanecem; inventário
  M1 dos 213 exports e testes existentes de grants, limites, gates e recovery passam.
- Suíte completa permanece verde. Sem instalação de dependências ou inferência real.

M2 encerra a remoção dos três ciclos identificados. Próximo marco: M3, com
extrações verticais de serviços e módulos grandes, sob escopo próprio. A separação
de cryptography como dependência opcional continua em M4.

## Falha de baseline encontrada na validação

A primeira rodada completa expôs uma falha anterior de sondagem SQLite após
crash, reproduzida com os fontes anteriores a M2.3. A correção fica documentada
separadamente na [ADR 0022](0022-readonly-broker-wal-identity-probe.md). Ela muda
somente o tratamento excepcional da sondagem de identidade do Broker; os corpos
de execução/controller/store de APL seguem mecanicamente preservados como acima.

## Resultado final

Windows/Python 3.13: 59 testes focados de execução/grants/exports e 17 casos da
nova fronteira/arquitetura passaram. A comparação AST confirmou os quatro tipos
extraídos e o restante da execução APL idênticos, exceto imports. Referências
antigas e os 213 exports de M1 permanecem cobertos.

Após a correção isolada da ADR 0022, a suíte completa passou com
**813 aprovados e 1 ignorado**, em 361,85 segundos. `git diff --check` passou.
O auditor encontra **zero ciclos entre pacotes**, nenhuma exceção `package-cycle`
e 10 exceções restantes, incluindo o remapeamento explícito descrito nesta ADR.

M2.1, M2.2, M2.3 e a correção de recuperação permanecem no working tree, sem
commit/push nesta etapa. M2 está concluído; M3/M4 continuam fora desta autorização.
