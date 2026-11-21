# Instalação e uso da Fase 1

Provider/modelo são opcionais: sem perfil ou argumentos, vale o padrão nativo
do CLI escolhido, conforme a [ADR 0015](decisions/0015-native-cli-model-defaults.md).

Atualização: a [ADR 0014](decisions/0014-invoker-agent-selection.md) documenta
seleção por integração chamadora, configuração v2 e perfis dos quatro agentes.
Escolher um executor não altera o padrão do projeto; retomar preserva a escolha
persistida. As instruções históricas abaixo sobre Codex referem-se ao default.

Requisitos: Python 3.11+ e Git; Codex CLI para planejamento/execução local. Docker é opcional. `init` não executa agentes. Perfil de segurança, senha AgentFlow e WebAuthn não são necessários.

```shell
python -m pip install .
agentflow --version
cd caminho/do/repositorio-git
agentflow init
agentflow doctor
```

Para inicializar outro repositório: `agentflow init --path caminho/do/repositorio`. Um caminho dentro do repositório também é aceito; a configuração é criada na raiz Git. `init` cria apenas `.agentflow/config.toml` se ausente. Se o arquivo existir e for válido, ele permanece intacto. Se for inválido ou houver um conflito de tipo, o comando informa o erro e não sobrescreve o conteúdo.

## Configuração

Arquivo `.agentflow/config.toml`:

```toml
schema_version = 1
runtime = "local"
agent = "codex"
```

`schema_version` aceita o inteiro `1`; `runtime` aceita `local` ou `docker`; `agent` aceita `codex` ou `none` legado, que agora resolve para Codex sem reescrever o arquivo. Campos ausentes assumem os valores acima; campos desconhecidos, tipos errados e versões incompatíveis são erros. A inicialização preserva arquivos existentes. Use `agentflow run "<objetivo>"` ou `agentflow run` para ler `TASKS.json` na raiz. `--tasks-file backlog.json` seleciona outro arquivo. Consulte formatos e limites na [ADR 0011](decisions/0011-local-default-agent-and-backlog.md).

`agentflow doctor --path caminho/do/repositorio` mostra Python, Git, Codex CLI e Docker. Saída 0 indica requisitos disponíveis; 1 indica requisito obrigatório ausente/inválido; 2 indica argumentos, caminho ou configuração inválidos. Python, Git e Codex são obrigatórios; Docker somente se `runtime = "docker"`. A sondagem não verifica autenticação nativa nem funcionamento do daemon Docker. `profile status` informa `not_required`; comandos antigos de enrollment não cadastram credenciais.

Para desenvolvimento: `python -m pip install '.[test]'` e `python -m pytest -q`. A CI executa essas verificações em Ubuntu com Python 3.11, 3.12 e 3.13.

O protótipo legado `autonomous-development-runner/` não é instalado pelo pacote da raiz e permanece apenas como referência.

## Fase 2: plano e estado

Os únicos arquivos de `.agentflow/` destinados ao Git são `spec.toml` e `tasks.toml`. `config.toml`, `state.sqlite3`, WAL/SHM e backups são dados locais ignorados. O formato v1 exige IDs estáveis em letras minúsculas, dígitos e hífen e critérios com `id` e `description`.

```toml
# .agentflow/spec.toml
schema_version = 1
project_id = "example"
title = "Example"
objective = "Deliver a verifiable result"
[[acceptance_criteria]]
id = "reviewed"
description = "Requirements reviewed"
```

```toml
# .agentflow/tasks.toml
schema_version = 1
[[tasks]]
id = "first"
title = "First task"
description = "Prepare the first deliverable"
depends_on = []
[[tasks.acceptance_criteria]]
id = "done"
description = "Deliverable reviewed"
```

`agentflow plan --path caminho` valida os dois TOML e imprime a ordem de tarefas; não grava estado. `agentflow state --path caminho` lê o banco existente sem modificá-lo e informa as tarefas. Se houver WAL pendente, a inspeção falha de modo controlado para evitar criar arquivos auxiliares ou apresentar um snapshot incompleto; use a reabertura/reconciliação do estado antes de inspecionar. Plano alterado ou banco inexistente também gera erro. Ambos usam código 0 para sucesso e 2 para entrada/estado inválido. Não há comandos de execução, reserva ou verificação automática na CLI desta fase. APIs internas de estado são destinadas a testes de contrato e integração futura.
