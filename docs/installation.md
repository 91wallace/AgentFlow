# Instalação e uso da Fase 1

Requisitos: Python 3.11+ e Git. Codex CLI e Docker são opcionais no perfil local padrão. Nenhum agente é executado nesta fase.

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
agent = "none"
```

`schema_version` é um inteiro e atualmente só aceita `1`. `runtime` aceita `local` ou `docker`. `agent` aceita `none` ou `codex`. Campos ausentes assumem os valores acima; campos desconhecidos, tipos errados e versões não suportadas são erros. A seleção de Docker ou Codex apenas muda os requisitos avaliados por `doctor`; não habilita execução nesta fase.

`agentflow doctor --path caminho/do/repositorio` mostra Python, Git, Codex CLI e Docker separadamente. Saída 0 significa que todos os requisitos obrigatórios do perfil estão disponíveis; 1 significa requisito obrigatório ausente ou inválido; 2 significa argumentos, caminho ou configuração inválidos. Python e Git são sempre obrigatórios. Codex é obrigatório somente se `agent = "codex"`; Docker somente se `runtime = "docker"`. A sondagem verifica versões/comandos disponíveis, não autenticação nem funcionamento do daemon Docker.

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
