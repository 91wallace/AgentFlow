# AgentFlow

AgentFlow é um projeto de orquestrador híbrido de desenvolvimento de software. O núcleo e a CLI rodam localmente. Fases futuras acrescentarão planejamento de tarefas, estado SQLite, execução de agentes local ou em Docker, validação, recuperação e checkpoints Git.

## Estado atual: Fases 1 e 2 locais

O pacote Python 3.11+ oferece `agentflow --version`, `agentflow init`, `agentflow doctor`, `agentflow plan` e `agentflow state`. A Fase 2 acrescenta contratos TOML versionados, validação de dependências e estado SQLite local. **Não existe execução autônoma de agentes nesta fase.** O modo local não exige Docker. Codex CLI e Docker são diagnósticos opcionais, exceto quando selecionados na configuração.

```shell
python -m pip install .
cd caminho/do/repositorio-git
agentflow init
agentflow doctor
```

`agentflow init --path caminho` inicializa a raiz do repositório Git que contém o caminho. Cria `.agentflow/config.toml` apenas quando ausente e preserva configurações existentes. Veja [instalação e uso](docs/installation.md) para esquema TOML, testes e códigos de saída.

## Planejamento

- [Arquitetura](docs/architecture.md)
- [Plano e fases](docs/implementation-plan.md)
- [Decisão de arquitetura híbrida](docs/decisions/0001-hybrid-architecture.md)

O ZIP `autonomous-development-runner-v0.1.zip` e seu diretório extraído são um protótipo legado `adr`, preservado como referência. Eles não fazem parte do pacote `agentflow` da raiz.

Licença: [Apache 2.0](LICENSE).
