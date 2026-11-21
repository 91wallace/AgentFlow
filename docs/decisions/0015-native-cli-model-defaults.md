# ADR 0015 — Provider e modelo nativos opcionais

Status: solicitado pelo proprietário e implementado em 2026-11-20.
Prevalece sobre a obrigatoriedade de provider/modelo alternativos da ADR 0014
e sobre a seleção manual obrigatória dos adaptadores na ADR 0006.

O agente pode ser iniciado sem perfil ou argumentos de provider/modelo:

```text
agentflow run --agent antigravity
agentflow run --invoker-agent opencode
agentflow run --agent claude-code
```

Na ausência de seleção explícita e de par configurado para aquele agente, o
AgentFlow omite `--model` e deixa o CLI resolver seu próprio padrão. Não consulta
catálogo, não exige perfil AgentFlow e não importa modelo de outro agente.
As pré-checagens de protocolo/versão e os gates independentes permanecem.
No Claude Code, a comprovação adicional de provider Anthropic aplica-se somente
à seleção manual; o modo nativo delega autenticação/provider ao CLI.

Provider/modelo explícitos continuam opcionais como par, com as verificações
anteriores de catálogo/identidade. Configuração parcial continua sendo erro.
Configuração nativa do CLI nunca é reescrita.

O manifesto e a política do contrato registram o pedido sem provider/modelo;
o status apresenta `selection_mode = "cli-default"`, em contraste com `manual`.
Isso registra a intenção de usar o padrão do CLI, não fixa um modelo descoberto
posteriormente. Retomar conserva o agente, versão e esse modo de seleção; se
a configuração nativa mudar, o modelo resolvido pelo CLI pode mudar. Não há
promessa de congelamento de um modelo que o operador não selecionou.

Resultados registram provider/modelo efetivamente reportados quando disponíveis;
ausência de informação fica `unknown`. No modo nativo, não há comparação com
uma seleção manual inexistente. Resultado de processo e qualidade continuam
separados: identidade desconhecida não transforma sucesso em aceite, e falha
estruturada do CLI continua falha mesmo sem metadata de modelo.

Testes sintéticos cobrem preparo sem `--model`/catálogo, modelos desconhecidos,
falha estruturada, persistência do modo nativo, planejamento e duas tarefas
dependentes com handoff/gates nos três agentes alternativos, além das seleções
manuais existentes. Não houve inferência real dos CLIs.
