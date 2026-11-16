# MVP-13.2 — recuperação da homologação real Codex

## Parecer: REAL_CODEX_PLANNER_HOMOLOGATED

O Codex CLI 0.167.0 produziu uma proposta semântica real para um ciclo APL sintético. A resposta JSONL foi interpretada, decodificada, validada pelos contratos atuais e persistida como `proposed`. Nenhum job foi disparado e nenhum aceite foi realizado.

## Causa do bloqueio anterior

Havia dois problemas distintos no bootstrap do workspace:

1. `CodexCLIPlannerAdapter` apenas criava uma pasta `.git`; isso não cria um repositório reconhecível por `git rev-parse`.
2. Codex CLI 0.167.0 exige que o projeto seja confiável. Uma pasta temporária nova não tinha confiança efetiva, mesmo após corrigir a posição das opções.

O primeiro processo anterior também revelou a posição incorreta de `--ask-for-approval`: é opção global e deve preceder `exec`. Isso já foi corrigido no MVP-13.1. A posição agora está coberta por teste.

## Correção

- O adapter agora executa `git init --quiet --no-template` no diretório descartável e confirma `git rev-parse --show-toplevel` igual à raiz solicitada. Também confirma ausência de remotes. Não cria commit.
- Só no caminho experimental de planejamento, o `CodexAdapter` acrescenta `--config projects."<raiz temporária>".trust_level="trusted"` à própria invocação. Esse override limita-se à raiz sintética e ao processo atual; não grava `~/.codex/config.toml`, não adiciona arquivo `.codex/config.toml` e não usa `--skip-git-repo-check`.
- A chave `projects.<path>.trust_level` e os níveis `trusted`/`untrusted` constam da [referência oficial de configuração Codex](https://learn.chatgpt.com/docs/config-file/config-reference). O CLI documenta `--config` como override de configuração para a invocação. Esse trust por processo não isola o Codex nem altera a política de sandbox.
- O usuário local permanece a identidade efetiva do processo; a invocação usa `read-only` e approval `never`. Não se alega isolamento de filesystem, rede, credenciais, subprocessos ou ferramentas.

## Execução real e evidências

- Codex CLI: **0.167.0**; adapter marcado experimental, não homologado para execução geral.
- Pré-validação sem inferência: workspace era raiz Git válida; override `--config` aceito; `exec --help` retornou 0; configuração global não ganhou entrada para a pasta temporária.
- Uma solicitação real: planejar `square(n)=n*n` e testes unitários. O prompt declarou explicitamente que eram propostas, sem criar/execução de arquivos.
- Resultado do processo: `process_succeeded`, exit 0, 11,542 s, stdout 1.671 bytes, stderr 0. Digest stdout `6863e8a71d7f65de7bd661b38a57f377f4391a8cb1f9bd5c555ab3fdd9997fd8`.
- Resposta estruturada decodificada: duas tarefas adicionadas, `implementation` (depende de `design`) e `tests` (depende de `design` e `implementation`), escopos `task:implementation`/`task:tests`, critérios de aceite presentes. O decoder aceitou schema v1 e a validação APL confirmou ciclo/revisão de origem, escopo, IDs, critérios e dependências.
- SQLite `integrity_check`: `ok`. Proposta `c906253401270569cdfe554989c342f3`, digest `377f3db1e5d938a1e708a5f1fea73b972c6ce18dee5de70345d79878cba30406`, estado `proposed`; eventos `started` e `succeeded`; revisão de plano permanece 1 (baseline); ciclo `READY`; tentativas no ExecutionStore: 0.
- Conteúdo da pasta Git temporária do provider teve hashes idênticos antes/depois. Sem remotes. Evidência serializada em `%TEMP%\AgentFlow-MVP13.2-mtcemmkn\.agentflow\mvp13-2-evidence.json`; banco sintético preservado no mesmo diretório.

## Testes

- Testes focados de APL, planner, Codex adapter e runtime: **74 passed**.
- Suíte completa após as alterações: **446 passed in 276.67s** (Python 3.13.17, Windows).
- Cobertura nova/verificada: raiz Git, ausência de remotes, `cwd` e `-C` alinhados, confiança somente via override da invocação, resposta estruturada simulada, timeout, cancelamento, erro de processo, schema inválido, idempotência/recuperação e zero dispatch.
- Nenhum provider real foi chamado pelos testes automatizados; houve exatamente uma solicitação bem-sucedida de inferência real nesta etapa.

## Limitações

- Esta é homologação funcional local do planejador, não homologação de segurança do Codex CLI. O processo ainda roda com permissões do usuário; `read-only` e trust scoped não demonstram confinamento.
- A proposta é sintética e segue sem aceite. A execução de jobs pelo APL continua desabilitada; a integração com executor permanece bloqueada pelas decisões de Fase 7/Broker/TEG. Não executar jobs reais nesta etapa.
- A proposta é mantida no SQLite descartável para inspeção e não altera `.agentflow/tasks.toml` do repositório principal.

**Nota arquitetural (2026-11-14):** o trust override por `--config` documentado acima foi removido do adapter. Hoje AgentFlow não injeta configuração de sandbox nem override de confiança; a confiança e o sandbox são determinados pela configuração e pelos mecanismos nativos do Codex.
