# MVP-13.1 — homologação real do planejador Codex

## Estado após MVP-13.2

O bloqueio documentado abaixo foi resolvido no MVP-13.2 com raiz Git temporária válida e trust via override estritamente por invocação para essa raiz. Uma proposta real foi decodificada, validada e persistida. O relatório de conclusão está em [MVP-13.2](apl-mvp13-2-real-codex-planner-recovery.md). A configuração global não foi alterada; nenhuma execução de job ocorreu.

## Parecer original do MVP-13.1: BLOCKED

O adaptador iniciou Codex CLI 0.167.0 em um workspace Git sintético, mas a CLI recusou o workspace por não estar na lista de diretórios confiáveis. O processo terminou antes de inferência: nenhum stdout, nenhuma proposta e nenhuma execução de job. Não foi usado `--skip-git-repo-check`, não foi alterada configuração global e não foi usado o repositório principal como workspace do provider.

## Baseline e compatibilidade

- Repositório: `C:\Users\criarte\projects\AgentFlow`.
- Branch/HEAD: `feat/intelligent-model-routing-phase-7` / `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`.
- Codex CLI instalado: `codex-cli 0.167.0`.
- Adapter normal: mantém `TESTED_VERSIONS = {"0.166.0"}`. O opt-in do planejador aceita versão não homologada somente no caminho experimental e adiciona a marca `experimental-unverified-version`.
- Help local confirmou `--sandbox`, `--config`, `--json`, `--cd`, `--strict-config`; `--ask-for-approval` é opção global e precisa preceder `exec`.
- Preflight real `CodexAdapter.prepare` passou e `codex login status` retornou sucesso, sem iniciar sessão de inferência.

## Chamadas e diagnóstico

Foi feita uma solicitação APL com objetivo matemático sintético: planejar `square(n)=n*n` e testes unitários. O contexto não continha conteúdo do projeto nem credenciais.

1. Primeira invocação do processo: CLI exit 2. A auditoria estática de `--help` confirmou que `--ask-for-approval never` depois de `exec` é rejeitado. Correção aplicada: mover a opção global antes do subcomando. Nenhuma inferência ocorreu.
2. Uma tentativa após a correção: CLI exit 1 em 0,502 s; stdout 0 bytes; stderr 76 bytes. Diagnóstico sanitizado: `Not inside a trusted directory and --skip-git-repo-check was not specified.` O parser aceitou os argumentos, mas o gate de confiança recusou a pasta temporária antes de inferir.

Argumentos registrados, com caminho substituído por marcador:

```text
--ask-for-approval never exec --strict-config --json -C <synthetic-empty-provider-workspace> --sandbox read-only -
```

Na segunda tentativa, a pasta de trabalho do provider estava vazia antes e depois; não houve alteração de arquivos. A opção read-only foi solicitada por invocação, sem alegação de isolamento. Nenhuma saída do modelo foi produzida.

## Estado APL e persistência

No banco SQLite sintético preservado em `%TEMP%\AgentFlow-MVP13.1-n71dzgk8\.agentflow\state.sqlite3`:

- Ciclo permanece `READY`.
- Eventos append-only do pedido: `started`, `failed`; request digest registrado.
- Propostas: 0.
- Revisões do plano: 1 (baseline inicial, nenhuma revisão aceita).
- Tentativas no `ExecutionStore`: 0.
- A evidência resumida da chamada está em `%TEMP%\AgentFlow-MVP13.1-n71dzgk8\.agentflow\mvp13-1-evidence.json`; contém versão, argumentos sem caminho pessoal, status/exit, duração, tamanhos/digests e comparação de arquivos. O diretório temporário foi mantido para inspeção.

A primeira tentativa de processo possui evidência separada em `%TEMP%\AgentFlow-MVP13.1-d9_bn_94`. Seu stderr foi gravado antes da correção do redator e contém uma flag parcialmente redigida; essa evidência não é usada para inferir o motivo. A causa da posição incorreta da flag foi confirmada por probes `--help` sem inferência.

O decoder e `validate_proposal` não foram alcançados na chamada real; logo, MVP-13.1 não comprova proposta real validada ou persistida. O status `failed` e a causa detalhada constam do registro sintético/evidência externa, mas o adapter não converte a falha em falha do ciclo APL.

## Correções e testes

- Corrigida a posição de `--ask-for-approval` no `CodexAdapter`.
- Ajustado o redator do `LocalRuntime`: agora exige limite de palavra antes de `sk-`, para não corromper a flag `--ask-for-approval` como falso positivo; tokens continuam redigidos.
- Testes focados: `tests/test_codex_adapter.py`, `tests/test_local_runtime.py` e módulos APL: **71 passed**.
- Suíte completa após as correções: **443 passed in 272.24s** (Python 3.13.17, Windows).
- `git diff --check`: passou, com avisos de normalização LF/CRLF em arquivos preexistentes.
- Testes simulados continuam cobrindo timeout, cancelamento, JSON inválido, falha, idempotência e request interrompido; nenhum provider foi chamado por pytest.

## Limites e próxima etapa

Codex CLI executa sob a identidade do usuário e pode ter acesso às permissões/credenciais do ambiente; `read-only`, workspace temporário e instruções não provam isolamento de filesystem, rede, ferramentas ou credenciais. Configuração global do Codex, Broker/TEG, RB-09C, Production Mode, RestrictedInferenceHarness e dispatch automático permaneceram inalterados.

Para nova homologação é necessário um mecanismo autorizado que permita confiar somente no workspace sintético desta execução sem alterar a configuração global nem ignorar o gate de confiança. Até que exista esse caminho, não repetir chamadas reais. Não habilitar inferência real para ciclos de trabalho nem execução automática.

**Nota de manutenção (2026-11-14):** os argumentos `--sandbox read-only` acima descrevem fielmente o experimento histórico MVP-13.1; não são a política atual. AgentFlow agora não injeta `--sandbox` ou `--config` e deixa o sandbox inteiramente ao Codex CLI. A configuração nativa é herdada e o modo efetivo fica registrado como `unknown`.
