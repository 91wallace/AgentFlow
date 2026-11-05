# ADR 0001: Arquitetura híbrida

Status: aprovada para planejamento, 2026-11-04.

## Contexto

O protótipo ADR 0.1 executa Codex CLI localmente e guarda estado em JSON. O produto precisa de execução local ou em contêiner, recuperação confiável e adaptadores futuros sem obrigar Docker no uso local.

## Decisão

- CLI e núcleo de orquestração são processos locais em Python 3.11+.
- Execução de tarefas será selecionada por interface de runtime: local ou Docker. Docker é opcional.
- Codex CLI será o primeiro adaptador de agente; outros agentes dependem da mesma interface, sem alterar políticas do núcleo.
- SQLite será a fonte de verdade do estado operacional, com transações para reservas e transições. Git versionará código e checkpoints, sem substituir o banco.
- TOML configurará o projeto; pytest verificará os contratos; licença do projeto é Apache 2.0.
- A Fase 1 entrega instalação, CLI, `init`, `doctor`, configuração, estrutura, testes, CI e documentação. Não executa agentes.

## Consequências

O protótipo `adr` é preservado para referência e eventual migração planejada. Nenhum dado JSON existente é convertido nesta fase. O núcleo dependerá de protocolos, com subprocessos, SQLite, Docker e Git nas bordas. A integração com [Vibe Coding Toolkit](https://github.com/soumatheusgomes/vibe-coding-toolkit) será avaliada somente como adaptadores opcionais: o repositório externo publica prompts, templates e guias para agentes e indica licença MIT; antes de qualquer reutilização de código ou conteúdo será necessária revisão de licença, atribuição, versão e dependências. Nenhum código externo é copiado nesta fase.

## Alternativas consideradas

- Somente local: menor complexidade inicial, mas não atende ao isolamento opcional aprovado.
- Docker obrigatório: prejudica a operação local e o diagnóstico em máquinas sem Docker.
- JSON como estado definitivo: simples, mas inadequado para reserva concorrente e transições transacionais planejadas.
- Git como único estado: útil para auditoria de código, insuficiente para tentativas, eventos e recuperação operacional.
