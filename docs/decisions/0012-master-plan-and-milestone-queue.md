# ADR 0012 — Plano Mestre Markdown e fila de marcos

Atualização: a [ADR 0016](0016-progressive-master-plan-and-responsive-stdin.md)
remove o teto de tamanho do Markdown e usa contexto progressivo por seções/linhas.

Status: orientação do proprietário implementada em 2026-11-20.

## Duas etapas

1. Fechar o Plano Mestre `.md` antes da decomposição: objetivo e limites;
   contratos/invariantes; marcos verticais que entregam fluxo ponta a ponta;
   critérios observáveis de saída por marco. O AgentFlow lê esse documento como
   contexto de arquitetura; sua existência não é certificação automática de qualidade.
2. Manter `TASKS.json` como grafo executável. `milestone` organiza a sequência
   macro e não é despachado como job. `work` é uma unidade funcional verificável
   com uma responsabilidade, testes e validação. Apenas o marco ativo exige
   detalhamento. Não gerar antecipadamente subtarefas dos marcos futuros.

Exemplo mínimo (os critérios devem descrever resultados binários verificáveis):

```json
{
  "schema_version": 1,
  "master_plan": "IMPLEMENTATION_PLAN.md",
  "active_milestone": "M0",
  "tasks": [
    {
      "id": "M0", "type": "milestone", "depends_on": [],
      "goal": "Entregar o primeiro fluxo local completo.",
      "acceptance": ["O teste de integração do fluxo passa."]
    },
    {
      "id": "M1", "type": "milestone", "depends_on": ["M0"],
      "goal": "Expandir o fluxo após validar M0.",
      "acceptance": ["Os testes da expansão passam."]
    },
    {
      "id": "first-flow", "type": "work", "parent_id": "M0", "depends_on": [],
      "goal": "Implementar o primeiro fluxo e seu teste de integração.",
      "acceptance": ["O teste de integração do fluxo passa."]
    }
  ]
}
```

`master_plan` é relativo à raiz do projeto (padrão `IMPLEMENTATION_PLAN.md`),
deve ser `.md`, UTF-8, não vazio e estar dentro do projeto. `active_milestone`
pode ser omitido somente quando um único marco tem tarefas work detalhadas.
IDs de milestone podem usar maiúsculas; IDs work seguem o schema executável
minúsculo de até 48 caracteres. Todos são únicos e todas as dependências formam
um DAG. Milestone depende de milestone; work depende de work do mesmo pai.
A ordem entre marcos pertence ao pai, sem criar ciclos entre pai e filhos.

`goal` tem uma ou duas frases objetivas, sem exigir uma contagem textual rígida.
`acceptance` é lista não vazia de textos ou objetos com `description`. O planejador
preserva goal como descrição e os textos de acceptance como critérios das tasks;
deriva checks de artefatos e gates pelos contratos existentes, sem executar
comandos encontrados no JSON. Critérios vagos não são tornados verificáveis
apenas por estarem nesse campo; a revisão do plano deve definir testes/assertions
adequados. Esta fatia não implementa um avaliador semântico dos critérios.

## Execução e passagem entre marcos

O planejador recebe o Markdown e somente as tarefas do marco ativo. O digest
SHA-256 dos bytes do Plano Mestre entra na identidade desse pedido. Alterar o
Plano Mestre invalida o vínculo anterior; não se reaproveita conclusão silenciosamente.
Metadados de marcos futuros não são enviados como tarefas executáveis. Arquivos
de origem não são reescritos, e não há decomposição/avanço automáticos de marcos.

Para executar um marco com predecessores, cada predecessor deve manter seus
work nodes e registrar `completed_run_id` com o run ID emitido pelo AgentFlow.
O CLI consulta SQLite sem inicializar/migrar o banco: execução/ciclo concluídos,
todas as tarefas concluídas, journal/evidências íntegros, workspace, revisão e
plano exatos. O objetivo deve corresponder ao mesmo Markdown, marco e work.
`status = "completed"` no JSON não substitui essa evidência.
O gate comprova os critérios work por meio dos validadores existentes; revisão
do Plano Mestre deve garantir que esses critérios demonstrem a saída do marco.
Não se declara rastreabilidade estrita completa entre critérios de marco e work:
o mapeamento por IDs da ADR 0007 continua sendo evolução própria.

Limites: fila tipada até 64 KiB/128 nós; Plano Mestre até 32 KiB; 1–12 work
nodes no marco executado; objetivo normalizado ativo até 8192 bytes. Backlogs
simples da ADR 0011 continuam aceitos com os limites anteriores. TOMLs continuam
como snapshots executáveis gerados; SQLite continua fonte do estado operacional.

O comando `apl next` e os prompts de execução mostram somente a próxima tarefa,
seu aceite e escopo, sem repetir o plano, dependências já satisfeitas ou lista
de marcos. O executor recebe instrução de parar nessa fronteira e responder
concisamente; evidências e logs completos não são truncados.
