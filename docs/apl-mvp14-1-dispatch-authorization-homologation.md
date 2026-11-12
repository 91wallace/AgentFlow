# MVP-14.1 — Homologação dos contratos de autorização e dispatch

## Parecer

**PARTIAL — dispatch continua BLOCKED/fail-closed.** A implementação local de contratos e stores é extensa e vários invariantes passam em testes determinísticos. Porém, os testes de autoridade positiva usam simuladores e os componentes operacionais independentes não estão implantados nem homologados. Não há autorização verificável para conectar o APL ao dispatch, portanto nenhum caminho foi liberado ou chamado.

## Baseline e preservação

- Branch: `feat/intelligent-model-routing-phase-7`.
- HEAD: `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`.
- Alterações rastreadas e não rastreadas locais foram preservadas; nenhum arquivo de segurança foi alterado nesta etapa.
- Os relatórios MVP-13.2 e MVP-14 foram lidos. MVP-13.2 comprova uma inferência semântica real de planejamento, com proposta persistida não aceita e zero tentativas de execução. MVP-14 comprova apenas associação local de resultado validado; explicitamente não conecta o APL a `CodexAdapter`, `LocalRuntime` ou `ExecutionStore.prepare`.
- Não foram executados providers, jobs reais, Broker, TEG, autenticadores, rede ou efeitos externos.

## Matriz dos contratos de Fase 7

| Contrato / origem | Componente e estado local | Testes/evidência disponível | Lacuna de homologação / dependências |
|---|---|---|---|
| **RB-01 / F7-01 — fronteira e autoridade independente** (ADR 0008 e emenda RB-09C.1-R2) | Threat model, arquitetura e `ApprovalAuthority` tipada existem. `ApprovalBroker` retorna indisponível e não possui caminho local de ativação. A decisão exige domínios administrativos independentes para Broker e TEG. **Implementação operacional ausente.** | Testes confirmam fail-closed e ausência de API de emissão; ADR e progresso registram o gate. | Nenhum host/owner/operador independente designado; serviço, identidade isolada, store/ACL, UI independente e ancoragem não implantados. Este é o bloqueio raiz para grants reais.
| **RB-02 — schemas/protocolo** | Contratos fechados e versionados, canonicalização, digests, limites e erros `unknown` implementados. **Localmente testado; não é uma autoridade.** | Testes golden/round-trip, campos desconhecidos, tamanho, digest, replay e inconsistência. | A API remota confiável e operação de compatibilidade entre serviços não estão implantadas; precisa de Broker/TEG homologados.
| **RB-03 — persistência e snapshots Broker** | `BrokerStore` SQLite com migrações, append-only, hash-chain, lock single-instance, idempotência e backup/restore implementados no escopo local. **Persistência implementada; decisão de consentimento operacional ausente.** | Crash/rollback, FK/integridade, concorrência, conflito de operation ID, corrupção e restore em fixtures. | Banco local sob o mesmo host/domínio não é fonte independente nem protegido do agente; política operacional de backup/retention e serviço remoto permanecem pendentes.
| **RB-04 — identidade e canal** | Contratos de cliente, mTLS de desenvolvimento loopback, pin/CA/ALPN/correlação e revogação existem. **Parcial/demonstrativo.** | Certificados sintéticos testam CA, pin, identidade, expiração, downgrade, timeout e correlação. | Sem PKI/provisionamento real, serviço remoto, rate limiting operacional ou prova contra host/processo agente; teste loopback não é boundary.
| **RB-05 — WebAuthn** | Verificador criptográfico e metadados de credencial/challenge no store. Asserções em testes são sintéticas. **Parcial.** | Vectores de assinatura, origin/RP, UP/UV, replay, expiração, revogação, concorrência e recovery. | Sem enrollment operacional, autenticador real, sessão/dispositivo confiável ou UI operacional. Prova de autenticação não equivale a consentimento.
| **RB-06 — consentimento/UI** | `ConsentPresentation` e preview HTML somente leitura existem como demonstração. **Não homologado como UI de confiança.** | Determinismo/digest, escaping, conteúdo integral, truncamento e leitura sem mutação do banco. | Preview roda no domínio do agente e pode ser controlado por ele; falta serviço/UI independente e teste de consentimento informado.
| **RB-07 — TEG e execução protegida** | Núcleo `agentflow.teg`, protocolos v1/v2, store separado, registry explícito e recuperação existem. `UnavailableGatewayAuthority` é o padrão. Nenhum handler de produção, provider, shell ou executor protegido está registrado. **Contratos locais implementados; dispatch operacional ausente.** | Testes em `tests/test_teg.py` usam authority/handler simulados para binding, allowlist, orçamento, concorrência, idempotência e `unknown`. | Falta autoridade Broker autenticada, source autoritativa de snapshots, identidade/admin independentes, armazenamento de credenciais, enforcement de rede e handlers aprovados. Simulador positivo não habilita dispatch.
| **RB-08 — recovery/auditoria** | Journals, integridade, reconciliação read-only, backup/restore e export local existem; estados incertos bloqueiam repetição. **Parcialmente implementado em stores locais.** | Falhas/crash em fronteiras, restore, hash-chain, receipts e `unknown` testados com SQLite/fixtures. | Não há recuperação distribuída homologada entre domínios Broker–TEG–provider; âncora de auditoria externa independente e observabilidade operacional ausentes.
| **RB-09 — homologação independente** | Plano/critério documental existe. **Não executado.** | Nenhuma evidência de ambiente implantado, red-team/bypass ou revisão independente. | Requer ambientes/administradores/autenticadores de teste autorizados, relatórios independentes Broker e Gateway, prova Windows/Linux conforme plataformas suportadas e testes reais end-to-end.
| **RB-10 — piloto** | **Não iniciado.** | Nenhuma evidência operacional. | Depende de RB-09 aprovada e autorização específica de implantação, operadores, ações, usuários, resposta a incidentes, rollback e kill switch.
| **F7-02/03 — SDD, revisões e rastreabilidade** | Store SDD e tasks TOML versionados; aceite externo permanece desabilitado. **Implementados localmente.** | Migrações, imutabilidade, cobertura explícita, idempotência e rollback testados. | Aceite de snapshot autoritativo não pode ser alegado sem Broker operacional.
| **F7-04/05 — catálogo, política e routing** | Catálogo/capabilities e evaluator produzem preview persistente; `dispatch_allowed` é forçado a `false`. **Preview local; sem dispatch.** | Determinismo, capability desconhecida, policy e digest/snapshot testados. | Proveniência independente de capability e decisão de routing autorizada por operação não estão operacionais.
| **F7-06 — budget ledger** | Reservas locais transacionais/idempotentes e recovery conservador implementados. **Enforcement local limitado.** | Concorrência, duplicação, limites e reserva interrompida/unknown testados. | Não há teto financeiro garantido pelo provider/fatura; evidência local não substitui enforcement no domínio do efeito.
| **F7-07 — autorização/escalonamento protegido** | Estado registrado como bloqueado pela ausência de authority independente; nenhuma aceitação/execução real disponível. | Fail-closed coberto pelo Broker facade e simuladores. | Bloqueio depende da conclusão de RB-01/04/05/06/07/09, conforme escopo da operação.
| **F7-08 — proposta de escalonamento** | Propostas imutáveis e verificáveis são registradas sem aceitar, criar tentativa ou chamar executor. **Proposta local somente.** | Evidência de gate/checkpoint, validade, escopo e idempotência testados. | Execução/troca de modelo permanece bloqueada até consentimento e TEG operacionais.
| **F7-09 — auditoria** | API/CLI de inspeção read-only e minimização de dados implementadas. | Hash/integridade antes e depois, WAL, saída JSON/humana e redaction testados. | Auditoria local não oferece âncora externa nem prova de integridade contra administrador do host.
| **F7-10 — homologação F7** | **Pendente.** | A CI referenciada no progresso cobre seis combinações de OS/Python do SHA publicado; não cobre este working tree nem homologa deployment Broker/TEG. | Requer integração instalada, matriz alvo, critérios RB-09 aprovados e decisão explícita para RB-10.

## Controles operacionais avaliados

| Controle | Evidência local | Decisão para APL dispatch |
|---|---|---|
| Identidade da execução | Tentativas vinculadas a task/revision/operation; identidade do processo registrada pelo runtime. A identidade continua a do usuário host no caminho local. | Não prova identidade independente de TEG; bloqueia operação protegida.
| Autorização explícita | Tipos `ApprovalRequest`/`GrantReference`; `ApprovalBroker`/authority padrão indisponíveis; preview de consentimento não autoriza. | **Ausente operacionalmente; negar.**
| Escopo do workspace | `ExecutionStore.prepare` compara revisão, paths autorizados e workspace registrado quando aplicável. Runtime local/worktree não é sandbox comprovado. | Serve como guarda local, não como isolamento nem autorização Broker/TEG.
| Validação de comandos/ações | Adapter usa invocação tipada; TEG exige allowlist explícita, mas runtime não tem handler de produção. | Sem handler protegido aprovado; comandos locais arbitrários são classe não mediada.
| Tentativas e duplicidade | `ExecutionStore` limita tentativas locais e exige retry explícito; operação SQLite idempotente. TEG possui journal e reservation/receipt em testes. | Sem retry automático do APL. Idempotência simulada não prova idempotência do provider.
| Decisões persistentes | Broker store persiste fixtures/estados; não há UI/issuer operacional para decisão humana. | Estado técnico local não é decisão autorizativa válida.
| Recovery e `unknown` | ExecutionStore, Broker/TEG stores e APL recuperam conservadoramente em testes locais. | Bom controle local; não há reconciliação distribuída de produção comprovada.
| Cancelamento | Runtime local pode cancelar processo/árvore conforme plataforma; TEG não pode garantir cancelamento de efeito remoto irreversível. | Timeout/cancelamento de processo não revoga autorização nem desfaz efeito; exige estado e receipt do TEG.
| Limites de recursos/orçamento | Runtime e APL possuem limites locais; ledger registra/enforça dimensões locais com estados de evidência. | Sem garantia de teto financeiro provider; TEG/handler teria de aplicar limites no momento do efeito.
| Evidências | APL associa somente resultado `process_succeeded` + `validation_passed` e evidências verificadas vinculadas à tentativa. | Adequado para ingestão local; não substitui receipt de efeito protegido.
| Negação por padrão | Approval facade e Gateway authority indisponíveis; routing registra `dispatch_allowed=false`; ausência/unknown bloqueia. | **Comprovado em nível de código e testes locais; preservado.**

## Testes executados nesta etapa

Comando:

```text
.venv\Scripts\python.exe -m pytest tests/test_approval_identity.py tests/test_approval_transport.py tests/test_approval_protocol.py tests/test_approval_persistence.py tests/test_approval_presentation.py tests/test_approval_webauthn.py tests/test_phase7_budgets.py tests/test_phase7_escalation.py tests/test_phase7_sdd.py tests/test_phase7_audit_cli.py tests/test_execution_reservation.py tests/test_execution_cli.py tests/test_local_runtime.py tests/test_apl.py -q
154 passed in 34.03s
```

Falhas: 0. Skips: 0. Os testes são locais e usam SQLite temporário, certificados/assinaturas sintéticos, simuladores e processos de teste; não são homologação operacional de Broker, TEG, UI confiável, identidade, provider ou fronteira administrativa. A suíte completa mais recente, após a mudança de código MVP-14, registrou 448 testes aprovados; nesta etapa não houve alteração de código e foi executada a suíte focada acima.

`git diff --check` executado ao fechar esta documentação: exit 0, sem erro de whitespace. Git emitiu avisos LF/CRLF já configurados no working tree.

## Bloqueio e critérios pendentes

Dispatch continua bloqueado porque não há Approval Broker operacional em domínio independente, cerimônia e UI confiáveis, autoridade autenticada utilizável por Gateway, TEG implantado com identidade/segredos isolados, handler protegido aprovado, nem evidência RB-09. A decisão arquitetural RB-09C.1-R2 exige domínios administrativos independentes; a existência de código local, mocks, confiança de workspace, certificados sintéticos ou testes unitários não satisfaz essa fronteira.

Não encontrei defeito de implementação cuja correção dentro dos contratos atuais habilite o dispatch. Não foram alterados Broker/TEG, RB-09C, Production Mode, RestrictedInferenceHarness, políticas globais, código do executor ou wiring do APL. O MVP-14 permanece no limite de associação de resultados já validados.

Para reavaliar o gate: concluir a implementação operacional Broker/TEG conforme ADR 0008; escolher e provisionar domínios/operadores/identidades e ambiente de homologação sob autorização específica; executar RB-09 independente com evidência de identidade, consentimento, enforcement, bypass negativo, idempotência, recovery, cancelamento e limites; então obter decisão de piloto RB-10. Até esse momento, a próxima ação técnica segura é manter os testes fail-closed e preparar apenas a matriz de homologação aprovada — não conectar APL ao dispatch.
