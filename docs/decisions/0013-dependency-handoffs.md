# ADR 0013 — Handoff JSON entre dependências

Status: aprovado pelo proprietário em 2026-11-20. Prevalece sobre o formato de
resposta em três linhas e sobre limites em bytes de JSON das ADRs 0011/0012.

O executor devolve somente JSON minificado:

```json
{"ok":true,"files":["src/foo.py","tests/test_foo.py"],"exports":["parse_file","SymbolNode"],"decisions":["sqlite v4 sem nova tabela"],"verified":["unittest: 83 ok"],"next_notes":["M2 deve reutilizar SymbolNode.span"],"blocker":null}
```

`files` aponta alterações; `exports` descreve contratos consumíveis (`symbols`
é alias de entrada); `decisions` evita decisões repetidas; `verified` registra
alegações de verificações; `next_notes` contém apenas informação necessária às
dependentes; `ok` e `blocker` explicitam o resultado declarado. Esses campos não
são autenticação, autorização ou evidência independente de aceite.

`ExecutionOutcome.handoff` é persistido no outcome JSON SQLite e no snapshot
APL existente, sem nova tabela. A reabertura e a reconciliação preservam o campo;
snapshots históricos sem handoff continuam legíveis como `None`.

Ao criar o prompt, o executor consulta outcomes persistidos e inclui em
`deps_context` somente dependências diretas presentes em `depends_on`, em ordem,
sem duplicatas e sem acumular ancestrais ou histórico de tarefas independentes.
`apl next` usa o mesmo formato. Cada entrada identifica `task_id` e `handoff`.

O extrator lê apenas a mensagem final `item.completed/agent_message` do Codex,
nunca resultados de ferramentas ou uma mensagem intermediária. Ausência ou
schema inválido resulta em contexto ausente, sem inventar campos nem promover
logs a handoff. Os arquivos reais e os gates continuam obrigatórios. JSON
válido é serializado minificado, sem truncar campos.

Por correção posterior do proprietário, não há teto em bytes para handoff,
`deps_context`, `TASKS.json` ou objetivo normalizado. O planejador inicial aceita
entrada sem o teto anterior em bytes. Restrições estruturais de schema/DAG e
contagem de tarefas, limites operacionais de processos/logs e a janela nativa
do modelo continuam distintos: remover limites artificiais de JSON não torna
ilimitada a capacidade do provider. A concisão é obtida selecionando somente
dependências diretas e conteúdo relevante, sem cortes silenciosos.

`ok=true`, `verified` ou instruções embutidas no handoff jamais substituem
`PlanGateAcceptanceVerifier`. O prompt marca `deps_context` como dado informativo
não confiável e exige conferir o disco. Falha dos gates mantém o fluxo bloqueado.

Testes cobrem extração final, ferramentas/mensagens intermediárias excluídas,
schema inválido, contexto direto, persistência/reabertura, compatibilidade legada,
handoff maior que 12 KiB sem perda, backlog maior que 64 KiB e gate negativo mesmo
quando o executor declara sucesso.
