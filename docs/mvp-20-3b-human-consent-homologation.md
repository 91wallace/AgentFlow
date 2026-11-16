# MVP-20.3B — Homologação do consentimento humano interativo

**Resultado: PARTIAL — aguarda interação humana real. H1 não homologado.**

## Correção H1 — resolução autoritativa do objetivo

A falha ocorreu porque o resolver passado ao terminal recalculava o contexto duas vezes para a mesma solicitação. O contexto gera uma validade curta a partir do relógio atual; a segunda criação podia produzir um `request` diferente e fazer o callback retornar `None`. A interface então tentava chamar um `objective_resolver` que não havia sido fornecido, causando `TypeError` em vez de uma recusa controlada.

O fluxo `authorize-plan` agora captura um instante para construir a validade da solicitação e usa um callback nomeado que resolve o contexto uma vez por consulta. Contexto ausente, objetivo ausente ou divergência entre a solicitação exibida e o plano atual são rejeitados antes da apresentação/autenticação e antes da emissão do grant. O objetivo é lido do `CycleSnapshot` persistido, apresentado ao operador e vinculado ao grant por `objective_digest`; o resolver operacional compara esse digest ao objetivo persistido antes de validar a execução. O fluxo `handoff-review` mantém seu resolver autoritativo existente. A UI não cai mais para uma resolução alternativa quando um resolver autoritativo falha.

Regressões adicionadas cobrem resolver inválido/ausente, objetivo vazio, contexto autoritativo indisponível e alteração posterior do objetivo persistido. O teste sintético público de `authorize-plan` cobre o caminho válido; cancelamento continua sem grant. Esses testes não equivalem a consentimento humano real.

## Auditoria e caminho público

`agentflow apl handoff-review` continua sendo revisão de uma proposta Broker pendente; não é autorização operacional de um plano aceito. A API `review_accepted_execution_plan` é o caminho de consentimento para plano completo. Foi exposta pela CLI existente como `agentflow apl authorize-plan`, reutilizando a mesma autoridade local, credencial por senha, desafio persistido, assinatura e BrokerStore. O comando exige terminal interativo, perfil `LOCAL_SUPERVISED`, State Store e configuração local existentes. Após aprovação, ele revalida assinatura, estado, expiração e binding com `BrokerExecutionGrantResolver`. Não chama controlador, executor, Codex ou provider.

## Workspace sintético preparado

- Raiz descartável: `%TEMP%\AgentFlow-MVP20.3B-85a8194f6409`
- Workspace: `%TEMP%\AgentFlow-MVP20.3B-85a8194f6409\workspace`
- Ciclo APL: `cycle-mvp20-3b-f7454ecd8dc2404d` — estado `READY`
- Revisão: `d9911f31d66b497a8e075f5f3df59471`
- Digest do plano: `sha256:f7653009e987b78a901b24a3b19f596272398d92d014b8ece9963dc323c65f1c`
- Tarefas persistidas: `demo-create`, seguida por `demo-verify`
- Executor apresentado: `synthetic-only`, versão `1`
- Política financeira prevista: `estimated_cost_accepted`; estimativa local de USD 1,00 não é teto do provider.

O helper `experiments/prepare_mvp20_3b_consent_demo.py` cria um projeto Git temporário novo, State Store e Broker SQLite vazios, perfil explícito e workspace. Não cria credencial, decisão ou grant; não executa tarefas. A consulta `agentflow apl inspect` confirmou estado, escopo, digest, instruções, critérios e dependência. `local-auth status` confirmou credencial ainda não cadastrada nesse State Store. Consulta SQLite read-only encontrou zero decisões, grants e registros de dispatch.

## Testes e evidências

- Testes focados de ponte, consentimento autoritativo e UI: **36 passaram**.
- Suíte completa: **649 passaram, 1 ignorado** em 297,82 s (Windows).
- `git diff --check`: passou; Git emitiu apenas avisos de conversão LF/CRLF para arquivos já modificados.
- `agentflow apl authorize-plan --help` foi executado e mostra os argumentos requeridos.
- Nenhuma senha ou confirmação foi solicitada nesta sessão; nenhum segredo foi registrado.
- Nenhuma decisão, grant, tarefa, dispatch, inferência ou provider foi iniciado.

Casos sintéticos existentes cobrem senha/prova inválida, negação, cancelamento, desafio de uso único, proposta expirada, plano alterado, workspace divergente, grant v1 não aceito pelo resolver operacional, grant v2 revogado/expirado e executor incompatível. Eles validam negação, mas não substituem H1 com operador real.

## Interação humana necessária

O cadastro inicial deste projeto temporário ainda não existe. Nesta máquina, `agentflow` não está instalado globalmente no `PATH`; execute no PowerShell interativo a partir da raiz do repositório usando o entry point da `.venv`:

```powershell
& .\.venv\Scripts\agentflow.exe local-auth enroll --path "$env:TEMP\AgentFlow-MVP20.3B-85a8194f6409" --confirm-bootstrap
```

Depois do cadastro, revisar e, se decidir, aprovar o plano no mesmo terminal:

```powershell
& .\.venv\Scripts\agentflow.exe apl authorize-plan --path "$env:TEMP\AgentFlow-MVP20.3B-85a8194f6409" --cycle-id cycle-mvp20-3b-f7454ecd8dc2404d --executor-id synthetic-only --executor-version 1 --max-attempts-per-job 1 --financial-budget-mode estimated_cost_accepted
```

A CLI exibirá o vínculo autoritativo. `approve` seguido da confirmação `APROVAR <12 caracteres do digest>` e da senha cadastra/usa a credencial local e pode emitir um grant; `reject` ou `cancel` não emite grant. Não passe a senha como argumento, variável ou arquivo. A validade do pedido é curta (até 120 segundos), então inicie `authorize-plan` somente quando puder revisar imediatamente.

Após aprovação, ainda será necessário validar a assinatura/estado do grant e registrar IDs de decisão/grant e validade. Este piloto não executará as tarefas; não habilita dispatch global. A senha ficará no cadastro local do State Store deste workspace temporário, nunca neste relatório.

## Limites

O perfil `LOCAL_SUPERVISED` confia na conta e no sistema operacional local, não oferece independência administrativa e não protege contra administrador local comprometido. H1 depende de uma ação humana autenticada observável e ainda não ocorreu. H2/H3 não foram avaliados nem declarados.
