# Progresso da Fase 7

Registro incremental local. Nenhum agente/modelo real foi executado; não houve commit ou push F7.

## Preparação e base F6

- Branch de trabalho: `feat/intelligent-model-routing-phase-7`.
- PR #6 conferido antes do merge: head `3742e39ab7b540906fe59a8aea6262b7027ce969`, base `main`, estado `OPEN`, `MERGEABLE/CLEAN`; arquivos revisados limitados à extensão F6.
- Checks CI do PR #6: sucesso em Ubuntu e Windows para Python 3.11, 3.12 e 3.13 (12 check runs listados em dois workflows, seis combinações distintas; todos passaram).
- Merge autorizado e realizado sem reescrita: `2b8546d7d71fec160900df40ab22c35099cd8cfe`.
- `origin/main` atualizado por fetch autorizado para `2b8546d7d71fec160900df40ab22c35099cd8cfe`. O commit F6 `3742e39ab7b540906fe59a8aea6262b7027ce969` é ancestral (segundo pai do merge). Branch F7 avançou por `--ff-only`; merge-base com `origin/main` é o merge commit. Nenhum conflito; alterações locais e protótipo não foram limpos ou sobrescritos.

## F7-01 — auditoria arquitetural e gate WebAuthn

- Estado: **bloqueada; F7-01 não liberou mudanças estruturais**.
- Inventário de código: catálogo e `ModelRoutingPolicy` F6 existem, policy continua inerte; seleção manual é snapshot imutável por tentativa. Não há authenticator, broker ou UI de aprovação; dependências de runtime em `pyproject.toml` são vazias.
- Inventário SQLite: `SQLiteStore` migra até `user_version=5`; componentes SDD first-class e budgets exigiriam v6 aditiva, sujeita a backup consistente, transação, integrity/FK checks e rollback. Nenhuma migração foi executada.
- ADRs 0003/0004/0005 permitem/registram runtime local sem isolamento OS garantido, `danger-full-access` explícito, worktree não sandbox e ausência de garantia de isolamento de filesystem/rede. Não existe isolamento de sessão gráfica/aprovador nem armazenamento de aprovação protegido de processo same-user.
- Evidência técnica WebAuthn: W3C requer challenge aleatório, validação exata de challenge/origin/RP ID e flags UP/UV quando UV requerida; isso autentica a ceremony, mas não prova que o usuário viu/entendeu a transação. Microsoft documenta API WebAuthn para Windows 10 1903+/Windows 11, Hello e chaves FIDO2. Linux depende de browser/desktop/authenticator presente; headless CI não tem UI confiável. Fontes estão no ADR 0008.
- Decisão: desenhado enrollment e aprovação com UV, challenge scoped, UI confiável, grants 120 s/one-use, store separado e fail-closed. O runtime atual não comprova que agente não controle UI/store/sessão. Portanto grants e operações dependentes permanecem desabilitados; não se substitui WebAuthn por prompt CLI/terminal. Sem broker OS-isolado, F7-02 a F7-10 não iniciam.
- Pendência para liberar F7: instalar/homologar um Approval Broker com principal OS separado, armazenamento/ACL não acessível pelo agente, UI com resumo imutável sob controle do broker, IPC autenticado e prova que o processo agente não controla UI/broker. Isso requer decisão/autorização específica para componente e provisionamento fora do repositório. Até então `approval_unavailable` é o único estado permitido.

## Estado das tarefas

| Tarefa | Estado |
| --- | --- |
| F7-01 | Auditoria concluída; gate **blocked** pela ausência de fronteira independente de aprovação verificável. ADR 0008 registra o desenho e a evidência. |
| F7-02 a F7-10 | **Não iniciadas**, dependem de F7-01 satisfatória. |

Nenhum código, teste ou migração F7 foi executado. `git diff --check` passou após os documentos F7-01 (Git emitiu somente avisos de conversão LF/CRLF já configurada no working tree).

## Autorização de desenvolvimento local controlado

### Approval Broker / F7-01

- Implementado `ApprovalAuthority` (status/verificação/consumo) e facade `ApprovalBroker` fixado em indisponível até homologação. O broker não possui API de emissão nem backend injetável, flag ou configuração local para ativar grants.
- **Não implementados/não verificados:** serviço OS isolado, proteção de identidade/ACL, IPC autenticado, WebAuthn/FIDO2, enrollment, armazenamento de credenciais públicas, UI confiável ou processo de revogação. Nenhum grant real foi ativado ou usado. O requisito de fronteira confiável continua bloqueado.
- Threat model: agente é considerado processo hostil e pode compartilhar privilégios do usuário; worktree/conta separada sem prova de isolamento não basta. Se o agente puder controlar sessão/UI ou alcançar IPC/store, autoridade deve permanecer indisponível. Uma instalação remota/dispositivo independente pode ser necessária.

### Componentes locais independentes de grants

- F7-02: SQLite v6 aditiva para snapshots SDD/plan, ChangeImpact, catálogo, policy e decisões; entidades revisionadas são imutáveis via triggers. `SDDStore.propose` cria somente estado proposto/legacy-unmapped e é idempotente por `operation_id`; divergência conflita.
- F7-03: tasks TOML v4 adiciona `satisfies` e `deferred_requirements`; cobertura não é inferida. v1–v3 continuam suportadas. Digests legados permanecem estáveis; schema v4 inclui rationale dos adiamentos no digest do plano.
- F7-04/05: catálogo distingue capabilities verificadas de declaradas desconhecidas; classificador fixo e routing evaluator puro geram somente preview. Sem evidência, dado atual ou budget estimável, seleção bloqueia. Nenhum provider é invocado.
- F7-09 parcial: API `inspect_audit` somente leitura. CLI de auditoria completa não implementada.
- F7-06 a F7-08 não concluídas: nenhum ledger/reserva de budget, autorização real, escalonamento ou retry.

### Validação incremental

- Baseline antes do código: 236 testes aprovados em Python 3.13.
- Testes focados em planejamento, F7, schema e seleção: 39 aprovados em Python 3.13.
- Coberturas novas incluem compatibilidade/rastreabilidade TOML, proposta/imutabilidade/idempotência, rollback v6 injetado, fail-closed sem broker, avaliação preview/capabilities desconhecidas e simulador concorrente de grant one-use/expiração/digest. Essas simulações não atestam segurança real.
- Primeira suíte completa após a implementação inicial: 247 passaram, um teste legado falhou porque ainda classificava SQLite v6 como versão futura; atualizado para v7, pois v6 agora é schema conhecido.
- A suíte integral, após as últimas alterações de catálogo/preview e snapshot da policy, passou: **251 testes aprovados** em Python 3.13 (`py -3.13 -m pytest -q`, 223,64 s).
- Smoke da CLI passou com `PYTHONPATH=src` usando `py -3.13 -m agentflow --help`. Build/instalação isolada não foi executada: `setuptools` não está instalado neste Python e nenhuma dependência foi instalada da rede.
- `git diff --check` passou; apenas avisos Git já configurados sobre conversão LF/CRLF. Não há homologação CI remota nesta autorização.
- Sem verificações de separação de identidade, permissão, IPC ou UI em Windows/Linux; nenhum autenticador/credencial foi acessado. CI remota não executada.

## Continuação local autorizada — budgets, auditoria e escalonamento

### Implementado sem autoridade real

- **F7-02:** schema SDD v6 mantido. Propostas/revisões SDD, snapshots e trilha existentes permanecem imutáveis/idempotentes e não existe caminho de aceite local.
- **F7-04/F7-05:** preview de roteamento persiste snapshot/digest da especificação, policy, catálogo, capabilities verificadas, seleção, alternativas classificadas com motivos, restrições e previsão de budget. A seleção é validada contra o catálogo imutável; o digest cobre os snapshots. A gravação força `authorization_required=true` e `dispatch_allowed=false`.
- **F7-06:** migração SQLite v7 aditiva; contas imutáveis por execução/tarefa/tentativa, reservas e operações idempotentes por `operation_id`, ledger append-only e reconciliação conhecida/estimada/desconhecida. Transações serializadas impedem dupla reserva sob concorrência. Recovery transforma reservas interrompidas em desconhecidas e mantém o hold até reconciliação explícita. `applied_verified` requer referência de evidência e significa somente enforcement local da admissão pelo ledger; `provider_invoice_enforcement` permanece `unavailable` e não há alegação de teto de fatura.
- **F7-08:** propostas imutáveis de escalonamento requerem falha de gate F5 com evidência verificada ligada a requisito/critério, checkpoint e snapshot; validam workspace, tentativa e identidade de modelo confirmada. Persistem origem/destino, motivo, digest/evidências, delta de custo apenas estimado, requisito de autorização, expiração e operation ID. A proposta não cria tentativa nem chama adapter/runtime; não há API local para executar/aceitar.
- **F7-09:** inspeção de auditoria permanece somente leitura no store. CLI completa para auditoria e inspeção de budgets ainda não implementada.
- **F7-01/F7-07:** continuam bloqueadas para qualquer aprovação/troca real. ADR 0008 agora contém plano detalhado de broker remoto ou em domínio de confiança separado, modelo de ameaças, IPC autenticado, WebAuthn, UI, armazenamento, consumo/revogação/recovery e homologação Windows/Linux. Nenhum broker foi instalado, nenhuma credencial foi usada e nenhum grant foi ativado.

### Evidências de validação local

- Testes focados de SDD, ledger, migração e escalonamento: **23 aprovados** em Python 3.13.
- Suíte completa: `py -3.13 -m pytest -q` — **259 aprovados**, 231,34 s.
- Regressões cobrem concorrência de reservas (duas reservas concorrentes para uma única capacidade disponível), idempotência e divergência de parâmetros, reconciliação/recovery de consumo desconhecido, rollback de v7 preservando schema v6 íntegro, e proposta de escalonamento derivada de falha de validação/checkpoint verificáveis.
- CLI: `PYTHONPATH=src py -3.13 -m agentflow --help` — sucesso; nenhum adapter/agente foi invocado.
- `git diff --check` — sucesso. Git imprimiu apenas avisos de conversão LF/CRLF já configurados.
- Instalação isolada não executada: `py -3.13 -m pip show setuptools` confirmou que setuptools não está disponível; para preservar dependências globais e não baixar pacotes sem necessidade autorizada, não tentei instalar dependências. Smoke foi feito via `PYTHONPATH=src`.
- Somente Python 3.13 está disponível para esta validação local. CI remota/Windows/Linux ainda não foi executada.

### Estado e pendências

| Tarefa | Estado local atual |
| --- | --- |
| F7-01 | Contratos/facade fail-closed e plano de homologação definidos; fronteira real continua bloqueada. |
| F7-02 | Propostas SDD e persistência v6 implementadas; aceite real desabilitado. |
| F7-03 | TOML v4/trace explícito e compatibilidade legado implementados; operações de aceite dependentes de broker desabilitadas. |
| F7-04 | Catálogo versionado e provenance implementados; provas reais de capabilities permanecem desconhecidas até integração homologada. |
| F7-05 | Classificador/evaluator e decisões de preview persistentes implementados; dispatch automático proibido. |
| F7-06 | Ledger/reservas/reconciliação/recovery SQLite v7 implementados e testados; sem enforcement de fatura/provider. |
| F7-07 | Bloqueada por ausência de autoridade real independente. |
| F7-08 | Proposta verificável, idempotente e expirável implementada; execução/troca de modelo bloqueada. |
| F7-09 | Auditoria read-only no store; CLI completa pendente. |
| F7-10 | Homologação final pendente: instalação isolada, CI Python 3.11–3.13 e plataformas Windows/Linux, e gate do broker. |

O baseline Git antes da publicação era `2b8546d7d71fec160900df40ab22c35099cd8cfe`. As alterações preexistentes e `autonomous-development-runner/` foram preservados; nenhum dado operacional `.agentflow/` foi listado ou alterado.

## F7-09 e homologação técnica local — fechamento desta autorização

- Implementado `agentflow audit --path <repo> --format human|json`. A saída cobre revisões e digests, cobertura coberta/adiada/não mapeada, impactos, catálogo e capabilities conhecidas/desconhecidas, decisões de preview, limites/reservas/deltas do ledger, estados de tarefas, propostas de escalonamento, evidências de estado e gates ligadas a tentativa/checkpoint, eventos SDD e operações desconhecidas/pendentes.
- O comando abre SQLite em `mode=ro&immutable=1`; não instancia um store migrável e não executa adapters. Se encontra WAL, bloqueia e explica que o checkpoint deve ser feito pelo fluxo operacional. A saída omite o texto da especificação/policy, rationale, payloads de evidência, logs, prompts e credenciais; restringe razões/status a códigos permitidos e valida digests. Testes confirmam o mesmo hash do banco antes/depois, ausência de um sentinel secreto na saída, formato humano/JSON, database antigo sem migração e rejeição de WAL sem modificá-lo.
- Suíte final após as alterações: `py -3.13 -m pytest -q` — **262 testes aprovados**, 245,71 s. Testes focados finais em auditoria + SDD + budgets + escalonamento: **26 aprovados** (11,50 s antes do último campo de estado, que também passou na suíte completa).
- Smoke da CLI: `PYTHONPATH=src py -3.13 -m agentflow --help` e `agentflow audit --help` — sucesso. Exercício funcional dos formatos human/JSON foi feito em banco temporário pelos testes; nenhum agente/provider foi chamado.
- Migração v5→v6→v7, FK/integridade e falha injetada em v7: os testes usam banco temporário e confirmam rollback da v7 para uma v6 íntegra, sem tabela parcial.
- Instalação descartável: criado venv e cópia temporária do pacote em `%TEMP%`; `setuptools` e wheel local não estavam disponíveis. `pip install --no-build-isolation --no-deps --no-index -e <cópia>` falhou em `Cannot import 'setuptools.build_meta'`. Não acessei a rede, não alterei dependências nem instalação global. Alternativa verificada: com `PYTHONPATH=<cópia>/src`, a CLI iniciou no venv.
- `git diff --check` passou; avisos reportados limitam-se à conversão LF/CRLF configurada no Git.
- Plataforma efetiva de teste desta execução: Windows, Python 3.13. Não há resultado local para Linux ou Python 3.11/3.12 e não houve CI remota. Nenhuma aprovação multiplataforma é declarada.

### Situação final

- **F7-09:** concluída para o escopo de inspeção local independente de aprovação.
- **F7-10:** parcial, homologação final não aprovada; faltam instalação editável com backend disponível, CI e testes efetivos nas plataformas/versões homologadas e fronteira real do broker.
- **F7-01/F7-07:** permanecem bloqueadas para grants. F7 continua não aprovada como fase completa.
- **Etapas C/D:** especificação remota e plano incremental RB-01 a RB-10 estão no ADR 0008. As garantias são classificadas explicitamente como comprovadas localmente, projetadas ou não comprovadas. Não houve implantação remota, enrollment ou credenciais reais.
- Estado Git preservado: branch `feat/intelligent-model-routing-phase-7`, HEAD `2b8546d7d71fec160900df40ab22c35099cd8cfe`, índice vazio, sem commit/push/merge. Protótipo legado e arquivos locais preexistentes permaneceram intactos; dados operacionais não foram inspecionados ou modificados.

## Preparação autorizada para homologação remota

- Empacotamento revisto: `pyproject.toml` usa `setuptools.build_meta` com `setuptools>=68` como dependência de build; runtime não declara dependências externas; `pytest>=8,<10` é extra de teste; `requires-python >=3.11`; script `agentflow=agentflow.cli:main`; descoberta de pacotes em `src`. Módulos SDD/routing/budget/audit/approval estão dentro de `agentflow` e são cobertos por verificação de conteúdo do wheel na CI. O backend não foi alterado para contornar ausência local.
- Limitação offline local confirmada em venv descartável: setuptools/wheel não estavam disponíveis e instalação editável sem isolamento falhou ao importar `setuptools.build_meta`. O ambiente global e dependências do projeto não foram alterados; a alternativa `PYTHONPATH=src` foi verificada. Instalação wheel/editable usando build isolation fica para CI com dependências disponíveis.
- `.github/workflows/ci.yml` atualizado para seis combinações Windows/Linux × Python 3.11/3.12/3.13. Cada job cria venv, constrói wheel PEP 517 isolada, verifica módulos F7 e entry point, instala projeto + pytest, executa suíte completa, valida instalação editable e smoke da CLI, roda `pip check` e confere whitespace do diff. Resultado ainda pendente do PR.
- Commit do núcleo: `8791a35` (`feat: add SDD, routing, and budget core`). Commit de CLI read-only/CI será registrado após staging seletivo. Nenhum PR ou CI remoto foi criado/executado ainda.
