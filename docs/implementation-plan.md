# Plano de implementação

## Resumo e regras de fase

Fase 1 estabelece um pacote `agentflow` instalável, comandos seguros `--version`, `init` e `doctor`, configuração TOML, testes pytest, CI e instruções de instalação. O protótipo `autonomous-development-runner/` permanece intacto. Nenhuma tarefa desta fase inicia agentes, cria contêineres, grava SQLite ou faz commits. Cada tarefa abaixo deve ser concluída e verificada antes das dependentes.

## Fase 1 — Fundação

| ID | Tarefa | Dependências | Critério de aceite |
| --- | --- | --- | --- |
| F1-01 | Definir layout `src/agentflow`, metadados de pacote, Python >=3.11 e entry point `agentflow`. | Nenhuma | Instalação local reproduzível; importação e `agentflow --version` funcionam sem Codex ou Docker. O pacote ADR permanece intacto. |
| F1-02 | Criar estrutura dos dez módulos e contratos mínimos de fronteira, sem comportamento de fases futuras. | F1-01 | Imports independentes; núcleo não importa adaptadores concretos; nenhum comando de execução é exposto. |
| F1-03 | Definir esquema versionado de TOML, valores padrão e validação com erros úteis. | F1-01 | Configuração válida carrega; tipos, chaves e versões inválidas falham com mensagem e código de saída consistentes; Docker não é obrigatório. |
| F1-04 | Implementar `agentflow init` não destrutivo e idempotente. | F1-03 | Primeira chamada cria somente arquivos previstos; segunda não altera bytes; conflitos e diretório sem permissão são informados sem sobrescrita; nenhuma execução de agente. |
| F1-05 | Implementar sondagens somente leitura para Python, Git, Codex CLI e Docker. | F1-02 | Resultados estruturados distinguem disponível, ausente e erro; ausência de Docker é opcional; testes usam sondagens simuladas. |
| F1-06 | Expor `agentflow doctor` e códigos de saída documentados. | F1-05 | Mostra versões/estado; funciona sem Docker; não instala, autentica ou executa agentes; status crítico tem saída previsível. |
| F1-07 | Adicionar pytest para instalação/CLI, TOML, `init` repetido e conflitos, e diagnóstico com dependências ausentes. | F1-03, F1-04, F1-06 | Testes passam em ambiente limpo sem Codex/Docker; testes verificam efeitos, não detalhes internos. |
| F1-08 | Adicionar workflow CI para Python 3.11+ e instalação/testes. | F1-07 | CI instala pacote e dependências de teste declaradas e executa pytest em sistema sem Codex/Docker. |
| F1-09 | Atualizar README e guia de instalação/uso para os comandos realmente entregues. | F1-04, F1-06, F1-08 | Exemplos executáveis, pré-requisitos, significado de `doctor` e limites da Fase 1 documentados; remover contradições com o protótipo. |

### Gate de conclusão da Fase 1

Instalação reproduzível com Python 3.11+; `agentflow --version`, `init` e `doctor` funcionais; `init` seguro e repetível; configuração TOML validada; pytest e CI passando; operação local sem Docker; nenhum agente executado e nenhuma dependência do Vibe Coding Toolkit.

## Fases seguintes, sem implementação antecipada

| Fase | Dependência | Entregas planejadas e gate |
| --- | --- | --- |
| 2 — Planejamento e estado | Fase 1 | Especificações e tarefas versionadas, dependências, esquema SQLite, transições e migrações; testes de transação e reabertura. |
| 3 — Execução local | Fase 2 | Adaptador Codex CLI, runtime local, políticas de tentativa e timeout, validação e evidências; execução de tarefa isolada por testes de contrato. |
| 4 — Recuperação e Git | Fase 3 | Reconciliação após interrupção, prevenção de duplicidade, branches e checkpoints explícitos; testes de falha entre transições. |
| 5 — Docker e entrega | Fase 4 | Runtime Docker opcional, isolamento configurável, builds e implantação sob política explícita; teste de paridade local/contêiner. |
| 6 — Ecossistema | Fase 5 | Adaptadores de outros agentes e eventual integração opcional com Vibe Coding Toolkit após revisão legal e técnica. |

## Questões e riscos a resolver antes de implementar

1. Resolvido: `.agentflow/config.toml` com `schema_version = 1`, `runtime = "local"` e `agent = "none"`.
2. Resolvido: `init` usa a raiz do repositório Git atual ou a raiz que contém `--path`.
3. Resolvido: `doctor` usa códigos 0, 1 e 2. Python e Git são obrigatórios; Codex e Docker tornam-se obrigatórios apenas quando selecionados.
4. Resolvido: `adr` permanece referência legada intacta, sem migração nesta fase.
5. Resolvido: CI em Ubuntu com Python 3.11, 3.12 e 3.13.

## Progresso da Fase 1

| Tarefa | Estado | Evidência local |
| --- | --- | --- |
| F1-01 | Validada localmente | `pip install '.[test]'` construiu e instalou wheel `agentflow-0.1.0`; entry point instalado retornou `agentflow 0.1.0`. |
| F1-02 | Validada localmente | Imports de módulos cobertos pela suíte; `compileall` passou. |
| F1-03 | Validada localmente | Casos de configuração válida e inválida passaram no pytest. |
| F1-04 | Validada localmente | Pytest e teste de integração confirmaram criação, repetição sem alteração e conflito sem sobrescrita. |
| F1-05 | Validada localmente | Pytest confirmou obrigatoriedade por perfil e ausência de dependências opcionais. |
| F1-06 | Validada localmente | Teste com CLI instalada confirmou códigos 0, 1 e 2; ausência de Codex e Docker no PATH foi simulada. |
| F1-07 | Validada localmente | `python -m pytest -q`: 17 aprovados, 0 falhos. |
| F1-08 | Validada na CI remota | Workflow Ubuntu/Python 3.11, 3.12 e 3.13 passou no GitHub Actions para push e Pull Request #1. |
| F1-09 | Validada localmente | README e guia revisados contra comandos instalados e saídas observadas. |

## Homologação local — 2026-11-04

- Interpretador: Python 3.13.17 em `.venv` dentro do projeto. Python global não foi modificado.
- Dependências instaladas apenas no ambiente virtual: AgentFlow 0.1.0, pytest 9.2.0, colorama 0.4.6, iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0 e Pygments 2.21.0; pip 26.2.2 veio no ambiente.
- Empacotamento: wheel construído pela instalação PEP 517; metadados e entry point `agentflow` confirmados; `pip check` sem requisitos quebrados.
- Integração: em repositório Git temporário, `init` criou `config.toml`; segunda chamada preservou bytes; configuração inválida foi preservada com saída 2. `doctor` retornou 0 com Codex/Docker opcionais ausentes, 1 com Codex obrigatório ausente e 2 com TOML inválido. O repositório temporário foi descartado após o teste.
- `git diff --check` passou. O aviso de normalização LF/CRLF do README não é falha de whitespace.
- CI do GitHub: os jobs Python 3.11, 3.12 e 3.13 passaram nas execuções de push e Pull Request #1. A Fase 1 está pronta para revisão e merge, sem merge automático.

Revalidação antes da publicação: a suíte passou novamente com `--basetemp .pytest-run` (17 aprovados). Dois testes foram corrigidos para usar um caminho inexistente explícito; assim não dependem da localização do diretório temporário em relação a um repositório Git pai.
