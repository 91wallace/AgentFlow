# Progresso da Fase 6

Registro incremental para retomada. Nenhum agente real foi executado; não houve operação remota Git, entrega ou deploy.

## F6-01 — auditoria e fechamento do ADR 0006

- Estado: concluída documentalmente em 2026-11-05.
- Evidência: documentação oficial Google Antigravity consultada; `agy` headless, protocolo stream-json, seleção manual `--model`, status/erro, permissão e sandbox documentados. Fontes e distinção entre `supported`, `effective`, `enforced` e `unknown` constam no ADR 0006.
- Limite: não foi executado `agy` nem testada conta/provider real. Sandbox/rede sem atestação por execução permanecem `unknown`; perfis que exigem enforcement serão bloqueados.

## F6-02 — contratos do adapter e registry

- Estado: implementada e validada localmente.
- Alterações: `AdapterDescriptor`, `CapabilityReport`, `AgentRegistry`; registro explícito e determinístico, sem descoberta dinâmica; extensão de `ProcessSpec` com versão da API e identidade provider/model (`unknown` por padrão).
- Validação: `py -3.13 -m pytest -q tests/test_agent_registry.py tests/test_execution_contracts.py tests/test_codex_adapter.py` — 9 passed.

## F6-03 — descritores de provider e modelo

- Estado: implementada e validada localmente.
- Alterações: descritores frozen com digest canônico, disponibilidade/fonte/instante, capabilities, modalidades, limite de contexto e custo decimal/moeda/fonte; detecção de disponibilidade stale.
- Validação inicial: um teste esperava digest hex sem prefixo. O contrato da base inclui `sha256:`; a asserção foi corrigida para testar o formato real, sem alterar comportamento de produção.
- Validação repetida: `py -3.13 -m pytest -q tests/test_agent_registry.py tests/test_agent_models.py tests/test_execution_contracts.py tests/test_codex_adapter.py` — 12 passed.

## F6-04 — seleção e persistência por tentativa

- Estado: implementada e validada localmente.
- Alterações: `ModelSelectionRequest` explícita; migração SQLite v5 opcional para `AgentExecutionStore`, com snapshot da seleção na mesma transação que reserva a tentativa; observação efetiva append-only separada; triggers impedem update/delete; digest verificado ao reabrir. Reutilização de `operation_id` com outro modelo/provider falha como conflito.
- Compatibilidade: o `ExecutionStore` legado mantém seu schema até v3; `ValidationStateStore` v4 continua legível em banco v5; reader genérico continua rejeitando schemas que não suporta.
- Defeitos corrigidos durante validação: uma inserção de classe antecipada havia colocado métodos da execução na subclasse F6; foi reposicionada e os testes de reserva/recovery voltaram a exercitar `ExecutionStore`. A política de versão SQLite foi ajustada por perfil após o reader genérico aceitar v4 indevidamente; readers de execução/validação aceitam v5 e o perfil base permanece limitado.
- Validação: `py -3.13 -m pytest -q tests/test_agent_selection.py tests/test_phase5_schema.py tests/test_execution_schema.py tests/test_execution_reservation.py tests/test_execution_contracts.py tests/test_agent_registry.py tests/test_agent_models.py` — 31 passed.

## F6-05 — contrato da política de roteamento futura

- Estado: implementada e validada localmente como schema inerte.
- Alterações: parser TOML estrito v1, campos de capabilities, classes de falha excluídas, evidências para eventual escalonamento, budgets de tentativas/tokens/tempo/custo, moeda ISO e autorização obrigatória. A política só oferece `snapshot` e digest; não implementa avaliador, seletor nem executor.
- Persistência: snapshot/digest da policy, quando presente, é incorporado ao snapshot imutável de `ModelSelection` antes do spawn. Mudança de policy altera o fingerprint da operação e conflita com o mesmo `operation_id`.
- Validação: `py -3.13 -m pytest -q tests/test_model_routing_policy.py tests/test_agent_selection.py tests/test_agent_models.py tests/test_agent_registry.py tests/test_phase5_schema.py` — 23 passed.

## F6-06 — adapter Antigravity (prioridade 1)

- Estado: implementada localmente com base no F6-01; sem binário real ou agente real.
- Alterações: adapter de `agy` com preflight injetável para versão, help, flags e catálogo local; headless stream JSONL e modelo manual; sandbox/rede desconhecidos e políticas não mapeáveis bloqueadas.
- Segurança: resultados estruturados precisam de `init`/`result` únicos e modelo efetivo igual ao escolhido; status/diagnósticos analisados separadamente do exit code. Não injeta `--dangerously-skip-permissions`.
- Validação: `py -3.13 -m pytest -q tests/test_antigravity_adapter.py tests/test_agent_selection.py tests/test_agent_registry.py tests/test_agent_models.py tests/test_model_routing_policy.py tests/test_codex_adapter.py tests/test_local_runtime.py` — 33 passed.
- Limite: plataforma/versão instalada e autenticação reais não homologadas; nenhuma execução de agente.

## F6-07 — adapter Claude Code (prioridade 2)

- Estado: implementada localmente com base em documentação oficial; sem CLI ou agente real.
- Alterações: preflight `--version` e `auth status`; seleção Anthropic/modelo manual; `-p`, stream JSON, prompts de permissão negados e sem persistência de sessão. Settings do usuário não são editados.
- Segurança: requer confirmação do modelo pelo evento `system/init`; perfil sandbox genérico é rejeitado, pois a sandbox oficial limita-se a shell e não está disponível em Windows nativo. Eventos `system/api_retry` são contados como atividade interna e não geram nova tentativa AgentFlow.
- Validação direcionada: `py -3.13 -m pytest -q tests/test_claude_code_adapter.py tests/test_execution_contracts.py tests/test_codex_adapter.py` — 10 passed. O fake valida explicitamente `auth status --json`.
- Risco: CLI/provider auth reais e matriz plataforma não homologados.

## F6-08 — adapter OpenCode (prioridade 3)

- Estado: implementada localmente com base em documentação oficial; sem CLI ou agente real.
- Alterações: `opencode run --format json`, seleção provider/model explícita e preflight por versão, capabilities (`--pure`, `run --format`, `run --model`) e inventário de catálogo sem refresh. `--pure` impede plugins externos no subprocesso; `--auto` não é passado. Sandbox/rede permanecem unknown.
- Segurança: identidade só é confirmada por campos estruturados explícitos; ausência/divergência permanece unknown/mismatch. O snapshot guarda provider e model IDs separados, mesmo que CLI receba `provider/model`.
- Validação direcionada final: `py -3.13 -m pytest -q tests/test_opencode_adapter.py tests/test_claude_code_adapter.py tests/test_antigravity_adapter.py tests/test_agent_selection.py` — 20 passed; inclui capabilities `--pure`/`run --format`/`run --model` observadas no help e IDs coerentes com o snapshot por tentativa.
- Risco: documentação não garante campo de identidade efetiva no JSONL; CLI/auth reais e matriz plataforma não homologados.

## F6-09 — Toolkit e conformance entre adapters

- Estado: decisão mantida de não integrar dependência, código ou conteúdo do Vibe Coding Toolkit. Conformance coberta com fakes, sem descoberta dinâmica.
- Limite: nenhuma revisão de checkout upstream foi necessária porque não há artefato ou integração candidata sendo incorporada; nenhuma licença é inferida.

## F6-10 — validação local

- Testes direcionados finais de adapters/contratos: `py -3.13 -m pytest -q tests/test_agent_registry.py tests/test_agent_models.py tests/test_agent_selection.py tests/test_model_routing_policy.py tests/test_antigravity_adapter.py tests/test_claude_code_adapter.py tests/test_opencode_adapter.py tests/test_codex_adapter.py` — 36 passed.
- Suíte completa final: `py -3.13 -m pytest -q` — **214 passed** (221,71 s), exit code 0, após a validação de capabilities OpenCode no preflight.
- Instalação isolada final: build/install de `agentflow 0.1.0` em venv temporário, `--no-deps`; wheel SHA-256 `361e9379bc8e2b67680eefe3195aececc7817c2a279745a8603665f2818cb314`. Build backend `setuptools` foi obtido pelo isolamento temporário do pip, sem alteração de dependências no projeto. Smoke da CLI instalada: `agentflow --version` → 0 (`agentflow 0.1.0`); `agentflow --help` → 0.
- `git diff --check`: concluído sem whitespace errors; Git emite apenas avisos de conversão LF/CRLF já configurada no working tree.
- CI remota da homologação anterior permaneceu pendente; a extensão F6-11 a reabriu e agora os jobs Windows/Linux × Python 3.11/3.12/3.13 estão autorizados após publicação seletiva da branch.
- Homologação real de cada CLI/provider e testes em Linux continuam pendentes; nenhum agente foi executado.

## F6-11 — Hermes Agent (prioridade 4)

- Estado: implementação local concluída; CI remota autorizada e pendente.
- Fonte: documentação oficial Nous Research verificada em 2026-11-05; `hermes --version`, `hermes chat --help`, `--oneshot --query-file - --provider --model --format stream-json` e o evento terminal `result` documentados. Nenhum binário Hermes, conta ou agente real foi iniciado.
- Adapter: `HermesAdapter` reutiliza `AgentAdapter`, `ModelSelection`, `ProcessSpec`, `AgentRegistry` e SQLite v5; versão/flags são preflight sem chat. Não importa código/dependência Hermes.
- Segurança: prompt vai por stdin sem arquivo persistente; ambiente do processo não recebe variáveis de credenciais; pede somente toolset `file`, `--safe-mode` e `--ignore-rules`, sem `--skills`, `--yolo`, terminal ou delegação. Configuração Hermes não é editada. Sandbox/rede e provider efetivo não comprovados ficam unknown; policy obrigatória bloqueia.
- Parser: JSONL lido linha a linha; exige `system/init` e `result` terminais únicos, verifica exit code externo/interno, rejeita eventos/ferramentas fora do contrato e não promove ausência/malformed a sucesso. Sem provider no init, seleção permanece unknown mesmo com model confirmado.
- Testes Hermes: `py -3.13 -m pytest -q tests/test_hermes_adapter.py tests/test_agent_selection.py` — **26 passed**. Cobrem executable/version/flags, seleção/provider/model, JSONL válido/incompleto/malformado, eventos e ferramentas inesperadas, divergência, cancelamento, timeout, exit codes, falta de autenticação, ausência de segredo no ambiente/diagnóstico, registro com os três adapters existentes e persistência `unknown` no SQLite v5. Tudo usa fakes e valores sintéticos; Hermes real não foi executado.
- F6-10 está reaberta para incluir Hermes e workflow CI em Linux/Windows × Python 3.11/3.12/3.13.

### F6-10 reavaliada após F6-11

- Suíte completa Windows/Python 3.13: `py -3.13 -m pytest -q` — **236 passed** (220,84 s), exit code 0.
- `git diff --check` passou; Git emite somente avisos de conversão LF/CRLF do working tree.
- Instalação final de `agentflow 0.1.0` em venv temporário, sem dependências de runtime: wheel SHA-256 `d067c7500adf0a08fcf568d94f527db000f6ca02a67ba4fada94d513a8af9e47`. `agentflow --version` e `agentflow --help` passaram.
- `.github/workflows/ci.yml` expandido para seis combinações: Ubuntu/Windows × Python 3.11/3.12/3.13. Jobs remotos aguardam o commit/push desta branch; Linux ainda não executado localmente.
