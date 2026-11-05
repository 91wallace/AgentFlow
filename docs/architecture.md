# Arquitetura do AgentFlow

## Estado e objetivo

O repositório versiona `README.md`, `LICENSE` Apache 2.0 e `autonomous-development-runner-v0.1.zip`. O protótipo extraído em `autonomous-development-runner/` está não versionado: pacote `adr`, CLI monolítica, estado em JSON e execução direta de `codex exec`. Não há testes ou CI. A Fase 1 cria a fundação do novo produto sem alterar esse protótipo nem ativar execução autônoma.

O processo `agentflow` roda localmente. No produto completo, o núcleo escolhe tarefas e aplica políticas; adaptadores executam agentes em ambiente local ou Docker; validadores produzem evidências; SQLite guarda estado operacional e Git guarda alterações de código e checkpoints. Docker nunca é requisito para o modo local.

```text
CLI -> core -> planning, state, agents, runtime, validation, git
           \-> environment, delivery
agents -> interface de runtime; adaptadores concretos ficam nas bordas
```

## Limites dos módulos

| Módulo | Responsabilidade e contrato planejado |
| --- | --- |
| `cli` | Analisa argumentos, apresenta resultados e traduz erros em códigos de saída; chama serviços por interfaces. |
| `core` | Coordena transições e políticas; não conhece subprocessos, SQLite ou Docker diretamente. |
| `planning` | Lê especificações, tarefas, dependências e critérios; entrega plano validado e imutável ao núcleo. |
| `state` | Repositório de estados, tentativas, eventos e retomada; implementação SQLite com transações em fase posterior. |
| `agents` | Protocolo de execução de tarefa e resultados; primeiro adaptador Codex CLI em fase posterior. |
| `runtime` | Protocolo de processos isolados; implementações local e Docker em fases posteriores. |
| `validation` | Executa verificações declaradas e devolve evidência estruturada; não decide sozinho a conclusão. |
| `git` | Consulta repositório e cria branches/checkpoints apenas quando uma política futura autorizar. |
| `environment` | Inspeciona Python, Git, Codex CLI e Docker sem alterar o sistema. |
| `delivery` | Contratos futuros para build e implantação; sem implementação funcional na Fase 1. |

Na Fase 1, só `cli`, configuração e `environment` têm comportamento de produto. Os demais módulos recebem estrutura e contratos mínimos onde necessários para impedir acoplamento, sem implementar seus fluxos futuros.

## Fluxos e dados planejados

1. `init` identifica o diretório alvo e cria somente arquivos ausentes de configuração e instruções iniciais. Arquivos existentes permanecem byte a byte iguais. Repetir o comando deve produzir o mesmo estado e saída clara.
2. `doctor` coleta disponibilidade e versões de Python, Git, Codex CLI e Docker. Ausência de Docker é aviso opcional; ausência de Codex limita execução futura, mas não impede `init` ou uso local básico. O comando não instala nem executa agentes.
3. Em fases futuras, um plano validado define tarefas e dependências; `core` reserva uma tarefa por transação SQLite, invoca agente via `runtime`, valida, registra evidências e solicita checkpoint Git. Uma execução interrompida reabre estado persistido e reconcilia o trabalho antes de qualquer repetição.

Configuração TOML terá versão de esquema, modo de execução `local` por padrão, seleção de agente e opções de diagnóstico. Valores desconhecidos, tipos inválidos e versões de esquema incompatíveis devem gerar erros claros. Segredos não entram no arquivo gerado por `init`.

## Limitações e riscos conhecidos

- O ADR 0.1 não implementa a arquitetura alvo: usa JSON, Python 3.10+, CLI `adr` e não oferece isolamento ou reconciliação transacional. Ele não deve ser promovido silenciosamente a `agentflow`.
- Estado SQLite, migração de dados ADR, execução, recuperação, checkpoints e Docker não pertencem à Fase 1.
- A forma exata dos modelos de tarefa e dos contratos de execução será fixada quando esses fluxos forem implementados; evitar interfaces especulativas profundas agora.
- `doctor` mede disponibilidade, não autenticação efetiva nem segurança de um agente ou Docker.
- Dependências do sistema variam por plataforma; testes devem simular sondagens sem exigir Codex CLI ou Docker instalados.
