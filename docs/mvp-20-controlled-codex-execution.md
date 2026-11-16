# MVP-20 — Integração controlada com Codex

## Estado

**PARTIAL — ponte ao adaptador/runtime e validações sintéticas implementadas; concessão de grant específico para execução completa e dispatch real permanecem bloqueados.** Nenhum job real foi iniciado nesta etapa.

## Componentes

- `agentflow.apl.codex_execution.CodexAuthorizedJobExecutor` adapta o `CodexAdapter` e o `LocalRuntime` existentes ao `AuthorizedExecutionController`. Não cria um segundo subprocess runtime.
- Cada tarefa exige um `VerifiedExecutionGrant` já resolvido, a operação literal `job:<id>`, escopo contido no grant e uma resolução autoritativa do workspace. Caminho canônico, volume/inode e digest persistido são comparados antes de preparar o processo.
- **Política atual:** AgentFlow não adiciona argumento `--sandbox`, não seleciona um modo e não injeta `--config` para alterar confiança do projeto. O Codex CLI usa integralmente a configuração nativa do usuário/projeto. AgentFlow não lê, grava ou substitui `config.toml`; o campo histórico de auditoria é `unknown`. Timeout, cancelamento, escopo, autorização, evidências e recuperação continuam sob controle do AgentFlow. A configuração nativa pode conceder capacidades além do workspace; a raiz de trabalho não é uma fronteira de isolamento.
- Timeout vem do limite aprovado e é limitado ao timeout local. O cancelamento persistido do run é consultado pelo `LocalRuntime` enquanto o processo roda.
- PID, marcador de criação, grupo e operation ID são persistidos antes do efeito e registrados no journal. Na recuperação de owner morto, o runtime tenta terminar somente o processo cuja identidade ainda confere; o job fica `unknown` e nunca é repetido automaticamente, mesmo se a tentativa de término retornar sucesso.
- `CodexAcceptanceEvidenceValidator` exige recibo do processo e um verificador explícito dos critérios da tarefa. Sair com código zero não basta para marcar a tarefa concluída.
- A CLI não instancia este executor e não há dispatch global.

## Autorização e gate

O grant consumido pelo controlador precisa vincular plano completo, digest/revisão, workspace e identidade atual, tarefas/operações, limites, validade, identidade do operador e estado de revogação. A aprovação existente em `apl handoff-review` é uma decisão de consentimento/revisão da proposta. Ela não foi reutilizada como aprovação do dispatch do plano inteiro.

Falta um fluxo operacional separado para apresentar o plano ativo completo, gerar desafio autenticado específico de execução, persistir essa intenção, emitir e revalidar o grant contra esse vínculo e integrá-lo a um comando de execução fail-closed. Até isso existir e passar homologação, o resolver de grant de produção permanece indisponível. Não use grants de demonstração, grants sintéticos ou o adaptador de teste para executar trabalho real.

## Codex instalado

O diagnóstico local encontrou Codex CLI **0.167.2**. `CodexAdapter.discover()` retornou `unknown / version_not_homologated`; a allowlist atual contém 0.166.0. Não houve `codex exec`, inferência, leitura de projeto por provider ou chamada externa. A versão não foi adicionada à allowlist nem executada com override de versão.

## Verificação

`tests/test_apl_codex_execution.py` usa adaptador e runtime sintéticos, workspace temporário e SQLite real. Verifica composição com o controlador, receipt/checkpoint, limites operacionais do processo, identidade do workspace e rejeição de operação não coberta. `tests/test_apl_authorized_execution.py` cobre a máquina de execução, grants inválidos, cancelamento, recuperação, idempotência e concorrência.

Não foi realizada homologação com Codex real nem aprovação autenticada de plano de execução. Os testes sintéticos não provam autoridade operacional, comportamento do Codex 0.167.2, critérios reais de aceitação nem isolamento do sistema operacional.

## Próximos gates

1. Definir e implementar consentimento de execução distinto de revisão de proposta, com plano inteiro e binding imutável.
2. Construir resolver de grant que valida assinatura, estado, validade, revogação, digest/revisão, workspace real e operações antes de cada job.
3. Homologar a versão do Codex CLI sem relaxar o gate e validar captura/timeout/cancelamento em workspace sintético descartável, sem tarefa com efeito.
4. Executar regressão completa e testes de recuperação de processo por Windows.
5. Apresentar plano pequeno e reversível com digest, workspace e limites; aguardar aprovação humana autenticada no AgentFlow antes do primeiro dispatch.

`LOCAL_SUPERVISED` confia no host e no sistema operacional local; não é `INDEPENDENT_AUTHORITY` nem homologação de produção.

## MVP-20.2 — Vinculação APL + IFR + Codex (Windows)

O controlador APL permanece a única rota de orquestração deste executor: revalida o grant e o vínculo do plano/workspace antes de reservar cada tentativa, reserva tarefa, tentativa e duração dentro de `BEGIN IMMEDIATE`, preserva `operation_id` por tarefa e gera `attempt_id` distinto por tentativa. O grant vincula operador, executor/versão e limites explícitos por tarefa que cobrem exatamente o plano; limites por tarefa não podem superar os globais. O store confere o binding persistido byte a byte na reserva. Um evento `task.dispatch_intent_persisted` vincula operação, tentativa, digest de plano e grant antes do executor. Durações e orçamentos não finitos são rejeitados. A duração reservada é contabilizada separadamente da duração consumida; no retorno do processo, o tempo medido é consumido e o restante liberado, enquanto um resultado `unknown` mantém a reserva restante. Retry só libera reserva restante após diagnóstico persistido de ausência/reconciliação segura. SQLite serializa reservas concorrentes do mesmo run/grant entre processos.

O controlador encaminha ao `CodexAuthorizedJobExecutor` a duração já reservada. O bridge verifica que adapter ID/versão e workspace correspondem ao grant. O `LocalRuntime` agora recebe um callback `before_spawn` executado imediatamente antes de `Popen`; o controlador revalida grant, plano, validade, revogação, intenção e reserva nesse ponto. Não se declara atomicidade entre essa verificação SQLite e a criação do processo pelo sistema operacional: existe uma janela residual inevitável após o callback. Durante a execução, o runtime consulta cancelamento e revalida autorização em intervalos limitados; revogação/indisponibilidade persiste pedido de cancelamento e interrompe cooperativamente o processo. Isso pode não ser instantâneo nem desfazer efeitos já produzidos. O bridge segue usando `CodexAdapter` e `LocalRuntime`, com recibo e identidade do processo persistidos. A recuperação IFR mantém estratégia autorizada, prova de ausência de efeito e limites; um processo órfão ou resultado `unknown` fica bloqueado.

**Limitações de homologação:** ainda não existe resolver operacional que emita e valide um grant autenticado de execução do plano completo. A autoridade de consentimento local validada até o MVP-19.5 só cobre revisão e decisão/grant de consentimento, não execução. Portanto esta ponte não pode ser ativada pelo CLI e nenhum job foi executado. A quota de duração tem reserva/consumo/liberação sintética; orçamento permanece estimado, custo observado do provider indisponível e sem teto financeiro rígido. A janela entre o callback final e o processo do SO não é transacional. O cancelamento ativo e a revogação foram exercitados apenas com adaptadores sintéticos. O executor é um componente injetável de biblioteca, não uma fronteira contra código malicioso rodando sob a mesma identidade local.

O registro histórico da inspeção 0.167.2 permanece acima: a presença da opção `--sandbox` na CLI não é uma dependência do AgentFlow. A versão observada e a allowlist precisam ser reavaliadas após os testes comportamentais atuais. Nenhum dispatch global ou job real é habilitado por essa documentação.

Testes MVP-20.2: **61 testes focados passaram**, cobrindo controller/IFR/Codex bridge/LocalRuntime sintéticos, reserva concorrente, retenção de quota em `unknown`, revogação ativa e negativa do gate antes de `Popen`. A suíte completa terminou com **630 passed, 1 skipped em 298,49 s**; skip POSIX não aplicável ao Windows. Adaptadores de processo foram sintéticos; sondagens Codex foram apenas `--version`/`--help`. Nenhuma inferência, provider ou job real ocorreu. Esses testes não constituem homologação operacional nem isolamento de segurança.

### Gates do adendo (12 Nov 2026)

| Gate | Estado | Evidência/justificativa |
| --- | --- | --- |
| Grant por plano completo e não ilimitado | **BLOCKED** | Não há consentimento/grant operacional de execução; o consentimento de revisão não serve para dispatch. |
| Digest, revisão, workspace, executor, operações e limites vinculados | **INCONCLUSIVE** | Testes sintéticos exercitam binding de plano/workspace; falta resolver e validar uma autorização operacional autenticada atual. |
| Reserva de duração e tentativas | **PASS — sintético** | Reserva transacional, rejeição de reserva duplicada em concorrência, consumo/liberação após retorno e retenção após unknown exercitados em SQLite real; sem atomicidade com dispatch externo. |
| Orçamento/custo do provider | **BLOCKED** | `ExecutionOutcome.cost` é estimativa não verificada; custo observado/teto financeiro não fornecido pelo CodexAdapter/runtime. Não há hard cap. |
| Validação no ponto final antes de spawn | **PASS** | No teste sintético, callback `before_spawn` é chamado antes de `Popen` e sua negativa impede a criação simulada. Nenhuma atomicidade entre SQLite e SO é alegada. |
| Revogação/cancelamento ativo | **PASS** | Resolver revogado gera evento e pedido de cancelamento observado pelo token sintético. Latência e efeitos de processo Codex real não testados. |
| Recuperação de processo órfão/unknown | **INCONCLUSIVE** | `unknown` retém saldo reservado e não repete em testes; fluxo para reconciliação segura e eventual liberação ainda não existe. |
| IFR sem ampliação de escopo | **PASS — sintético** | Estratégias precisam estar cobertas pelo grant; `unknown` não repete e novos attempts mantêm operação. Não equivale a dispatch autorizado real. |
| Codex CLI 0.167.2 | **INCONCLUSIVE** | `--version`/`--help` confirmaram presença de flags; não houve inferência para testar parsing, protocolo JSONL, timeout ou cancelamento. Allowlist mantida em 0.166.0. |
| Isolamento por diretório de trabalho | **BLOCKED** | `cwd` é binding contextual, não fronteira de filesystem. Sem prova de isolamento OS neste perfil. |
| Piloto com duas tarefas dependentes | **BLOCKED** | Workspace temporário e manifesto criados e inspecionados sem dados sensíveis; falta Git root, isolamento comprovado, grant e aprovação autenticada específica. Tarefas não executadas. |

Os gates seguem `PASS`, `FAIL`, `INCONCLUSIVE` e `BLOCKED`; estados sintéticos são explicitamente identificados e não podem ser promovidos a evidência operacional.

### Atualização MVP-20.3A — ponte de consentimento para grant

A limitação histórica acima sobre ausência de issuer operacional foi parcialmente resolvida por `review_accepted_execution_plan` e `BrokerExecutionGrantResolver`, descritos em [MVP-20.3A](mvp-20-3a-authenticated-operational-grant-bridge.md). A emissão agora exige decisão local autenticada sobre um plano APL aceito e persistido; o grant v2 assina o vínculo de plano, workspace, tarefas, executor e quotas. Grants de revisão existentes permanecem incompatíveis com dispatch. A implementação foi exercitada apenas com credencial/autenticação sintéticas e executor sintético; portanto o gate de grant está **PASS — sintético**, e não operacional. O fluxo continua sem execução real, sem integração comportamental do Codex 0.167.2 e sem autorização específica do piloto. O estado `PARTIAL` do MVP-20 permanece.

## MVP-20.3 — Política financeira explícita e grant de plano (12 Nov 2026)

**Classificação: PARTIAL.** O controlador agora exige `FinancialBudgetPolicy` explícita no binding do grant. `hard_cap_required` é negado antes de qualquer chamada ao executor porque não existe enforcement confiável do provider. `estimated_cost_accepted` exige estimativa positiva, finita e moeda ISO explícita; esse valor limita localmente o custo estimado reportado e não é teto financeiro garantido. A ausência da política bloqueia a execução (sem orçamento ilimitado implícito). O journal de liquidação distingue valor/moeda estimados, custo observado indisponível e `financial_hard_cap: false`. O binding inclui concorrência máxima explícita; a implementação atual aceita somente `max_concurrency=1` e rejeita grant que tente autorizar paralelismo não suportado.

O controle mantém quotas operacionais de tarefas, tentativas, tempo e duração reservada no SQLite transacional. Um custo estimado acima do valor consentido bloqueia a continuação; isso não impede o provider de incorrer em custo maior durante a tarefa já iniciada. Não há custo observado verificado nem moeda reportada pelo processo; o campo `ExecutionOutcome.cost` continua sendo uma estimativa fornecida pelo executor.

O grant autenticado de execução do plano completo permanece **BLOCKED**. A fachada `UnavailableGatewayAuthority` não emite grants operacionais, e o grant local de consentimento existente autoriza revisão da proposta, sem binding de plano completo com executor/versão, operações e quotas de execução. O controlador exige que o resolver valide o binding imutável do plano inteiro, mas não existe issuer/resolver operacional aprovado que converta a autenticação humana existente nesse contrato. Não reutilizamos grants de revisão nem habilitamos um emissor paralelo. Por consequência, aprovação única de plano, revogação operacional de grant e dispatch continuam indisponíveis fora dos testes sintéticos.

Reconciliação após reinício continua conservadora: o processo órfão é identificado quando possível, a tarefa fica `unknown`, a reserva permanece consumida/retida e não ocorre retry automático. Conclusão requer resultado e evidência persistidos; retry exige prova confiável de ausência de efeitos pelo contrato IFR existente. Não há reconciliador autenticado que prove estado externo e libere uma reserva ambígua.

| Gate MVP-20.3 | Estado | Evidência |
| --- | --- | --- |
| Autorização autenticada do plano completo, executor e quotas | **BLOCKED** | Sem issuer/resolver operacional homologado com esse vínculo; autorização de revisão não é dispatch. |
| Uma autorização abrange duas tarefas dependentes | **PASS — contrato sintético** | Teste do controlador executa duas tarefas sob um binding fixture e valida operações/tarefas exatamente; não é grant operacional. |
| Política financeira explícita | **PASS — contrato sintético** | Política ausente/invalidamente limitada e exigência de hard cap são negadas antes do executor. |
| Custo observado ou hard cap pelo provider | **BLOCKED** | Indisponível e não garantido por `CodexAdapter`/`ExecutionOutcome`. |
| Reserva concorrente, tentativa e duração | **PASS — sintético/SQLite** | Reutiliza `BEGIN IMMEDIATE`, binding imutável, operação estável e tentativa distinta. |
| Reconciliação de resultado desconhecido | **PASS — negação conservadora; recuperação positiva INCONCLUSIVE** | `unknown` retém reserva e bloqueia repetição; não há prova autenticada para liberar/resumir com segurança. |
| Dispatch, inferência e piloto | **BLOCKED / não executados** | Explicitamente fora desta etapa; sem Codex real ou jobs reais. |

Testes MVP-20.3 após validação estrita da política e concorrência: **48 testes focados passaram** (`test_apl_authorized_execution.py` e `test_apl_codex_execution.py`); a suíte completa terminou com **639 passed, 1 skipped** em **300,76 s**. O skip POSIX não se aplica ao Windows. `git diff --check` passou. Nenhum Codex/provider foi iniciado e nenhum job real executado nesta etapa. O manifesto do piloto existente não foi alterado.

## MVP-20.1 — Fundação IFR

`agentflow.apl.failure_recovery` fornece contratos tipados para `PASS`/`FAIL`/`INCONCLUSIVE`, natureza e recuperabilidade da falha, estado dos efeitos, limites e decisão pura de recuperação. A policy não executa, não concede grants e só admite estratégia presente no conjunto autorizado do grant.

O `AuthorizedExecutionController` agora registra tentativas e estratégia no mesmo journal SQLite append-only do run. O `operation_id` permanece estável por job; cada tentativa recebe `attempt_id` próprio. Diagnósticos, assinatura contextual da falha, evidências, resultado de qualidade, decisão e resumo de escalonamento são eventos encadeados por digest. Limites IFR são persistidos como binding imutável do run e validados ao reabrir.

Retry automático só ocorre quando o executor confiável lança `NoEffectExecutionError` com estado de efeito `NO_EFFECT_PROVEN` ou reconciliado, a falha é potencialmente recuperável, a policy aprova, a autorização vigente inclui a estratégia e o executor declara suporte a essa estratégia. Falhas determinísticas não repetem a mesma estratégia; `unknown`, falhas de avaliação `INCONCLUSIVE`, efeitos possíveis e bloqueios de segurança/autorização escalam sem retry. Grant é revalidado imediatamente antes de cada tentativa.

`FAIL` de qualidade é registrado separadamente de sucesso/falha operacional, e `INCONCLUSIVE` não é promovido a PASS ou FAIL. Resultado do executor sem prova suficiente não permite retry. A interface de intervenção autenticada e retomada após decisão humana ainda não existe; o escalonamento persistido permanece bloqueado até essa próxima fatia.

Validações sintéticas no Windows: os três módulos IFR/execução tiveram **47 testes aprovados**. A suíte completa teve **622 aprovados e 1 skip** em **301,04 s**; o skip POSIX é condicionado a plataforma POSIX e não executou no Windows. `git diff --check` passou após a validação final. Nenhum job real, provider, dispatch operacional ou alteração dos gates ocorreu. O AQA completo permanece posterior à homologação da execução real. Consulte [ADR 0009](decisions/0009-intelligent-failure-recovery.md) e a [trilha IFR do plano mestre](implementation-plan.md#trilha-transversal-mvp-20mvp-21--intelligent-failure-recovery).

**Limite desta fundação:** escalonamentos são eventos persistidos e o run fica bloqueado, mas ainda não há endpoint de resposta humana autenticada/reconciliação nem retomada após essa resposta. `INCONCLUSIVE`, saída `unknown`, efeitos possíveis e bloqueio de segurança nunca disparam retry. O retry automático existente exige prova `NO_EFFECT_PROVEN`/`RECONCILED_SAFE`, estratégia autorizada explícita e suporte declarado pelo executor; o mesmo `operation_id` é mantido e cada tentativa usa novo `attempt_id`.

## MVP-20 completion checkpoint — CLI and independent acceptance

The later MVP-20 implementation supersedes the earlier statements that public local execution commands and an acceptance verifier were still pending. The public LOCAL_SUPERVISED commands are now:

```powershell
agentflow apl run --path <workspace> --cycle-id <cycle-id> --operation-id <unique-operation-id> --max-jobs <N> --max-attempts-per-job <N> --max-seconds <seconds> --max-estimated-cost <finite-amount> --currency USD --validity-seconds <seconds> --task-timeout <seconds>
agentflow apl run-status --path <workspace> --run-id <run-id>
agentflow apl run-cancel --path <workspace> --run-id <run-id>
agentflow apl run-resume --path <workspace> --run-id <run-id>
```

`run` and `run-resume` require an interactive terminal. They use the explicit local contract and the existing APL execution controller; they do not ask for a password, digest confirmation or per-task approval. `run` prints the persisted `run_id` as flushed JSONL before dispatch begins, allowing status/cancel from another terminal. The controller revalidates the contract before each dispatch and at the runtime's final pre-spawn callback. Unknown effects stay blocked. A same-operation replay cannot renew or widen a contract. A cancellation revokes the contract and requests cooperative process cancellation.

Acceptance evidence is generated by plan-bound allowlisted validation gates on a temporary scoped workspace snapshot and stored with the execution outcome and journal. Process completion, command exit success, artifact/gate evidence, criteria quality, and task approval are represented separately. Only independently validated gate reports yield `PASS`; a zero exit code alone is insufficient. On Windows the current local validation runtime does not enforce network isolation, so this CLI path requires the cycle to explicitly include `network:unconfined`; this is an explicit operational limitation, not an isolation guarantee.

Synthetic verification passed for two dependent tasks, CLI status/cancel persistence, acceptance evidence, and recovery of the interruption window after contract persistence but before run creation. Full test results and current Codex probe are in [the MVP-20 completion report](mvp-20-authorized-continuous-execution.md). The installed Codex CLI is 0.169.0 and remains outside the 0.166.0 tested allowlist. No Codex invocation or real job was made; the Codex execution path remains unavailable until a version is behaviorally homologated and allowlisted. This does not change `INDEPENDENT_AUTHORITY`, dispatch defaults, global Codex settings, or the sandbox.

## MVP-20 — Codex CLI 0.169.0 sem imposição de sandbox (2026-11-14)

O adapter deixou de enviar `--sandbox` e `--config`; a versão 0.169.0 foi exercitada pelo `CodexAdapter` e `LocalRuntime` reais em workspace Git descartável, sem remote. A única inferência autorizada criou `adder.py`; recebeu-se `turn.completed`, o adapter aceitou os eventos JSONL, o processo terminou com código 0 em 10,459 s e stderr permaneceu separado (0 bytes). O gate independente compilou o arquivo e verificou `add(2, 3) == 5` (`PASS`). Único artefato produzido: `adder.py`. Evidência sanitizada: `%TEMP%\AgentFlow-Codex-0.169.0-14e50488c6574d91800e6748cd09c489\evidence\homologation.json`; stdout/stderr redigidos pelo `LocalRuntime` permanecem em `process-logs`. Não houve repetição automática.

O SHA-256 de `%USERPROFILE%\.codex\config.toml` antes e depois coincidiu (`DCB6E598BBA070DCC8E71DDED093B9800914EFEC054177E959549B049B891810`); nenhuma configuração global foi escrita ou substituída. A invocação não continha `--sandbox` nem `--config`, portanto o Codex aplicou sua configuração nativa. Isto não prova isolamento ou confinamento ao workspace. O custo real não foi consultado nem limitado; US$ 0,10 era apenas a referência estimada autorizada, sem teto financeiro garantido. Timeout, cancelamento e falhas continuam cobertos por testes sintéticos, sem inferência adicional.

Com a execução real e os testes de protocolo, `0.169.0` foi adicionado à allowlist comportamental do adapter. A homologação cobre o protocolo básico observado no Windows, não isolamento, qualidade geral do agente, dispatch APL real ou execução autônoma do MVP-20.
