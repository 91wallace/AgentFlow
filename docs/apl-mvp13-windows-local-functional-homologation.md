# MVP-13 — homologação funcional local no Windows

## Estado recuperado

- Branch `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`.
- O working tree já continha alterações locais do APL e documentação; foram preservadas. Migração/publicação permanecem suspensas e o backup externo não foi tocado.
- MVP-12.1 era o último checkpoint registrado. Não havia documento MVP-13 nem adapter Codex APL.
- `agentflow.apl` já oferecia ciclo supervisionado, revisões imutáveis, validação de escopo, associações a tentativas verificadas no `ExecutionStore`, replanejamento determinístico e recuperação conservadora.
- A `.venv` existente usa Python 3.13.17, `cryptography` 50.0.2 e pytest 9.2.0; nenhuma dependência foi instalada.

## Implementação desta retomada

- Adicionado `CodexCLIPlannerAdapter`, que implementa o contrato `PlannerAdapter` e reutiliza o `CodexAdapter` e `LocalRuntime` do AgentFlow. Somente a proposta é produzida; nenhum job é aceito nem executado por este caminho.
- A CLI permite `replan --planner codex` somente com `--enable-experimental-codex`. Timeout e limites de bytes são configuráveis; entrada/saída precisam cumprir os contratos estritos existentes e a proposta ainda passa por `validate_proposal`.
- Persistência reutiliza o SQLite operacional: `apl_planner_requests` faz claim único do pedido e `apl_planner_events` registra início, sucesso ou falha. Eventos são append-only. Proposta salva é idempotente; pedido ainda `started` ou `failed` não é repetido com a mesma chave. Uma interrupção abrupta permanece conservadoramente em `started`.
- A resposta é JSONL do Codex CLI, exige evento de turno concluído e mensagem estruturada; JSON fora do schema, campos adicionais ou revisão obsoleta são rejeitados.
- A documentação MVP-05..12 não foi reescrita; `docs/apl.md` descreve o caminho MVP-13 e os limites.

## Comandos

```powershell
agentflow apl replan --path <workspace> --cycle-id <id> --planner deterministic --request-id <id>
agentflow apl replan --path <workspace> --cycle-id <id> --planner codex `
  --enable-experimental-codex --request-id <id-unico> `
  --planner-timeout 240 --planner-max-input-bytes 65536 --planner-max-output-bytes 65536
agentflow apl proposals --path <workspace> --cycle-id <id>
agentflow apl accept-plan --path <workspace> --cycle-id <id> --proposal-id <proposal> --confirm
agentflow apl history --path <workspace> --cycle-id <id>
```

O aceite continua humano e validado pelo APL. Este comando não executa jobs.

## Verificações

- Baseline focado antes da alteração: `tests/test_apl.py` e `tests/test_apl_restricted_worker.py`: **54 passed**.
- Após implementação, o conjunto APL focado, incluindo `tests/test_apl_codex_planner.py`: **61 passed**.
- Suíte completa após implementação: `python -m pytest -q`: **442 passed in 273.88s** (Python 3.13.17, Windows).
- Testes novos usam runtime/adaptador simulados; nenhum provider real foi iniciado.
- Ainda não executada nesta retomada: chamada de inferência Codex real, execução real de job pelo APL e recuperação de processo real do provider.

## Integração Codex e permissões efetivas

O CLI local observado é `codex-cli 0.167.0`. O `CodexAdapter` normal de execução continua homologando apenas 0.166.0. Para permitir experimento explicitamente opt-in de planejamento, o adapter admite versão parseável não homologada neste único caminho e marca a capacidade `experimental-unverified-version`. Isso **não** altera a allowlist padrão de execução nem homologa 0.167.0.

O processo Codex é executado sob a identidade e o ambiente do usuário, com caminhos de configuração/credenciais disponíveis ao runtime local. Recebe `--sandbox read-only` e `--ask-for-approval never`; isso limita a invocação conforme o CLI, mas não prova isolamento de filesystem, credenciais, subprocessos, plugins/subagentes ou rede. Não há afirmação de que prompts restrinjam essas capacidades. Nenhuma inferência real foi iniciada nesta homologação, portanto compatibilidade/resultado semântico do provider segue sem evidência operacional.

## Bloqueio do ciclo de execução automática

O objetivo de encadear automaticamente planejamento → execução → avaliação → replanejamento não foi implementado. O APL continua com `next` e `submit-result` supervisionados: este último só associa uma tentativa já persistida e validada no `ExecutionStore`. O executor local existente é invocado pelo comando existente de execução, mas o plano de Fase 7 e a documentação APL mantêm dispatch automático pelo APL bloqueado até homologação independente de Broker/TEG/autorização. Não criamos executor, bypass ou substituto desses controles. Assim, o nível 3 (job real conduzido pelo APL), recuperação real de execução e ciclo completo de três jobs não foram demonstrados.

## Limitações e próximo passo

- O Codex CLI experimental não é um backend de inferência restrito e não é apropriado para produção.
- Limites do APL sobre custo continuam estimativas; não impõem teto financeiro ao provider.
- Replanejamento grava propostas; requer inspeção e aceite explícito.
- Pedido `started` após interrupção exige revisão e novo request ID, evitando repetição automática.
- Production Mode, RestrictedInferenceHarness, Broker/TEG e RB-09C continuam inalterados/fechados.
- Para homologar o ciclo de job completo, primeiro é necessária autorização/homologação independente que permita composição pelo núcleo sem bypass. Até lá, a próxima verificação segura é rodar a suíte completa e exercitar os comandos de proposta com fixtures locais, sem provider real.

## Parecer

**PARTIAL** — o provider experimental foi implementado e testado por simulação; o fluxo real com Codex e o ciclo automático de jobs não foram homologados. Não declarar pronto para produção ou execução autônoma.
