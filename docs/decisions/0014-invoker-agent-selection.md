# ADR 0014 — Seleção do executor pelo início do job

Atualização: a [ADR 0015](0015-native-cli-model-defaults.md) remove a exigência
de provider/modelo explícitos mencionada abaixo; ambos passam a ser opcionais.

Status: autorizado pelo proprietário para implementação em 2026-11-20.
Substitui a restrição Codex-only da ADR 0011; não reescreve configuração ou histórico.

## Precedência e configuração

1. `--agent` explícito.
2. `--invoker-agent` informado pela integração, quando selection é invoker-or-default.
3. Padrão do projeto.
4. Codex se não houver configuração.

Não há adivinhação de processo pai, autenticação da identidade chamadora ou troca
automática de provider. Uma integração deve informar seu ID explicitamente.

```toml
schema_version = 2
runtime = "local"

[agents]
default = "codex"
selection = "invoker-or-default"

[agents.profiles.opencode]
provider = "anthropic"
model = "modelo-configurado-no-cli"

[agents.profiles.claude-code]
provider = "anthropic"
model = "modelo-configurado-no-cli"

[agents.profiles.antigravity]
provider = "provider-configurado-no-cli"
model = "modelo-configurado-no-cli"
```

`project-default` ignora o invoker para seleção implícita. `--agent` ainda prevalece.
Provider/modelo pertencem ao perfil do agente selecionado, nunca ao anterior.
`--provider` e `--model` devem ser usados juntos para sobrescrever esse par.
Agentes alternativos exigem modelo explícito, no perfil ou nos argumentos.
V1 permanece legível (`none` resolve para Codex). Não há migração automática.
`init --invoker-agent opencode` registra o padrão em v2 se o arquivo estiver
ausente. Inicializar novamente preserva o arquivo, informando conflito quando
uma seleção diferente é solicitada. Init simples Codex preserva o formato v1.

## Fluxos

```text
agentflow run --invoker-agent claude-code
agentflow run --agent opencode --provider anthropic --model modelo
agentflow run --plan-dir .agentflow --invoker-agent antigravity --provider provider --model modelo
agentflow execute --task tarefa --operation-id tentativa --approval never --invoker-agent opencode --provider anthropic --model modelo
agentflow apl run-resume --run-id RUN --invoker-agent claude-code
```

As opções de seleção valem para `run`, `apl run` e `execute`. O planejador
estruturado compartilhado recebe o adaptador selecionado: não instancia Codex
para planejar com outro agente. `--plan-dir` usa TOMLs prontos dentro do projeto,
dispensando qualquer planejador. Gates, validação de DAG e escopo permanecem.

O executor genérico `AuthorizedJobExecutor` reaproveita controle de processo,
quotas, recovery e gates. O nome antigo é alias de compatibilidade. A composição
na borda traduz seleção e política para cada adaptador existente. Antigravity
recebe approval=None; Claude Code usa seu modo nativo sem prompts interativos;
OpenCode usa suas capabilities nativas. Não se adicionam flags de acesso total.
Descoberta de versão não comprova capacidade: prepare continua verificando a
interface nativa e o resultado deve confirmar a identidade de modelo/provider.

Mensagens finais dos três protocolos são normalizadas para planejamento/handoff.
Receipts alternativos usam `agent_process_receipt` e `agent_id`, enquanto os
receipts históricos Codex continuam aceitos pelos verificadores independentes.

## Persistência e retomada

Antes do início, agente/provider/modelo são publicados uma vez em manifesto local
sob `.agentflow/executor-selections/`, com nome derivado do cycle ID. O digest da
seleção é composto ao digest da política de aceite persistido no contrato SQLite.
Não é um banco de estado paralelo: o manifesto é configuração imutável daquele
job; estado operacional, versão do executor, resultados e journal ficam no SQLite.
Cada check antes de spawn relê o manifesto e compara o digest ligado ao contrato.
Troca/remoção/corrupção invalida o vínculo; não libera outra seleção.

Retomada lê a seleção persistida e a versão do contrato, sem consultar o padrão
atual ou usar o invoker como executor. Jobs históricos Codex sem manifesto
continuam com a política original; job alternativo sem seleção persistida bloqueia.
Troca de agente exige outro job; não há comando para trocar executor de job ativo.

## Validação e limites

Suíte sintética: precedência, config v1/v2, perfis sem vazamento, init preservado,
conflito/digest de seleção, plano pronto sem Codex, planejadores nativos simulados
e duas tarefas dependentes com handoff/gates para cada agente alternativo.
Testes isolados existentes dos adaptadores e regressões anteriores são mantidos.

Não houve inferência real ou homologação operacional dos CLIs nesta mudança.
Os executáveis precisam estar instalados/configurados pelo usuário. Login nativo,
catálogo de modelos, versões/capacidades e limites nativos de argumento continuam
aplicáveis; o limite de argumento do adaptador OpenCode é reportado como erro,
nunca truncamento do handoff. Modelos explícitos de Codex ainda são recusados:
o adaptador legado não atesta sua identidade efetiva. Codex mantém o default nativo.
