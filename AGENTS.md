# Instruções para agentes no AgentFlow

## Escopo e estado atual

O produto aprovado é um orquestrador híbrido: CLI e núcleo locais, execução futura local ou em Docker, Codex CLI como primeiro adaptador, SQLite para estado operacional e Git para versionamento. Use Python 3.11+, pytest, TOML e Apache 2.0.

O diretório `autonomous-development-runner/` contém o protótipo ADR 0.1 extraído do ZIP versionado. Preserve seus arquivos e comportamento enquanto uma migração explícita não for aprovada. O README da raiz descreve uma visão mais ampla que o código atual.

## Regras de trabalho

- Leia `docs/architecture.md`, `docs/implementation-plan.md` e as decisões em `docs/decisions/` antes de alterar interfaces.
- Implemente somente a fase autorizada e seus critérios de aceite. A Fase 1 não executa agentes, testes de projetos de terceiros, Docker nem operações de Git que alterem o histórico.
- Preserve alterações existentes; não sobrescreva arquivos de usuário. Não faça commit ou push sem instrução explícita.
- Mantenha interfaces entre módulos explícitas, substituíveis e testáveis. Não importe adaptadores concretos para dentro do núcleo.
- `init` deve ser repetível, não destrutivo e informar conflitos sem editar arquivos existentes. `doctor` é somente leitura e Docker é opcional.
- Evite dependências de runtime quando a biblioteca padrão atender. Não instale dependências ou execute scripts obtidos da internet sem autorização.
- Cubra comportamentos e falhas relevantes com pytest; execute os testes disponíveis antes de concluir uma implementação.
- Registre mudanças de escopo, decisões e limitações na documentação.

## Integrações externas

O Vibe Coding Toolkit é uma referência potencial, não uma dependência. Qualquer integração futura deve ser opcional, isolada por adaptador e revisada quanto a licença, versão, segurança e proveniência. Não copie seu código na Fase 1.
