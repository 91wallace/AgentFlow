# MVP-20 — Execução contínua sob autorização inicial

## Estado

**PARTIAL — mecanismo persistente exercitado com executor sintético; dispatch real não homologado nem exposto pela CLI.**

Esta fatia implementa a máquina de execução grant-scoped em `agentflow.apl.authorized_execution`. Não altera `UnavailableLocalAuthorizationAuthority`, não habilita dispatch global e não fornece uma implementação padrão para o executor ou para a resolução do grant.

## O que foi implementado

- `VerifiedExecutionGrant` representa uma resolução feita por autoridade confiável. Inclui grant e request IDs, digest/revisão da proposta, digest/revisão do plano aceito, identidade e caminho do workspace, digest da identidade do workspace, operações, escopo, validade e limites.
- `SQLiteAuthorizedExecutionStore` usa o mesmo banco operacional do APL. Vincula um grant a uma única sessão, persiste plano e tarefas, gera `operation_id` estável por tarefa e mantém journal append-only com triggers contra atualização/remoção.
- `AuthorizedExecutionController` valida o grant antes da sessão e imediatamente antes de cada despacho, exige cobertura exata de todas as tarefas, executa somente jobs elegíveis, valida evidências, persiste checkpoints e encerra automaticamente ao concluir.
- Um resultado `unknown`, falha genérica do executor, alteração do plano ou revogação bloqueia o restante. Só `NoEffectExecutionError`, que o adaptador confiável pode emitir quando comprova que nenhum processo/efeito começou, permite retry com o mesmo `operation_id` e dentro do limite.
- A reserva é persistida antes do executor. Depois de crash, PID e marcador de criação são verificados: processo vivo mantém a reserva; processo ausente converte tarefa em `unknown`. `unknown` nunca é redispatchado automaticamente.
- Cancelamento persistido impede o próximo job; revogação/expiração é consultada de novo antes de cada próximo dispatch. Nenhum desses mecanismos afirma que consegue desfazer um efeito já iniciado.
- A resolução autoritativa de consentimento agora apresenta e vincula as operações de **todo o plano resultante**, em vez de somente a lista de alterações. Mudanças do plano aceito continuam exigindo grant compatível com o digest/revisão novos.

## Contrato de integração

Uma aplicação deve injetar `ExecutionGrantResolver`, `AuthorizedJobExecutor` e `ResultEvidenceValidator`. Não há fallback permissivo. O resolver é responsável por validar assinatura, estado `approved`, expiração, revogação, request digest, identidade do Broker/autoridade e por revalidar o caminho canônico e a identidade atual do workspace contra a vinculação persistida. O controlador compara a resposta com o snapshot imutável do plano e rejeita grant que não cubra literalmente `job:<id>` para cada tarefa.

`start(cycle_id, grant_id)` cria/reutiliza uma única sessão idempotente para esse grant e executa sequencialmente. `resume(run_id)` retoma somente sessão segura. `SQLiteAuthorizedExecutionStore.request_cancel(run_id)` registra cancelamento; a operação ativa depende da interface do executor para cancelamento e pode exigir reconciliação.

Este contrato ainda não está ligado ao comando `agentflow apl` nem ao dispatcher local de `ExecutionStore`/`LocalRuntime`. Não se deve configurar um resolver sintético para trabalho real. O adaptador existente de execução precisa oferecer identity binding, autorização TEG imediatamente antes do efeito, timeout/cancelamento, coleta de evidência verificável e reconciliação de processos sob as políticas por sistema operacional.

## Limites de segurança

- A sessão não emite, renova, amplia ou altera grants.
- O resolver é reconsultado antes de cada job. Um grant inválido bloqueia a sessão.
- Uma sessão única por `grant_id` impede reutilização da mesma autorização em uma nova sessão por este controlador.
- O orçamento é estimado e conferido com o custo reportado; não é teto financeiro do provider.
- O executor recebe apenas escopo e operação já autorizados; o mecanismo genérico não prova isolamento do processo.
- A recuperação usa identidade PID + marcador de criação para não confundir PID reutilizado. Ela não prova que um processo externo terminou efeitos; resultados ambíguos ficam `unknown`.
- A aprovação local continua confiando no host/OS; isso não satisfaz `INDEPENDENT_AUTHORITY` nem os gates de alta garantia.

## Bloqueios para dispatch real

1. A revisão ADR 0008-R separa implementação local de liberação operacional; seu gate L7 requer autorização separada do proprietário para o primeiro dispatch sintético.
2. A solicitação atual não identifica um ciclo, proposta, digest, workspace e tarefa concretos para esse primeiro piloto; nenhum grant válido atual foi selecionado para execução.
3. O fluxo `handoff-review` atual aprova a proposta de replanejamento, enquanto a ativação da revisão e a identidade do plano aceito ainda não estão ligadas atomicamente ao grant. A fatia nova recusa despacho sem resolver esse vínculo.
4. O executor APL existente ainda recebe resultados previamente validados; `LocalRuntime` pertence ao fluxo de execução de tarefas do State Store. Falta o adaptador autorizado entre ambos, com TEG local homologado e `operation_id` transferido até o efeito.
5. A autoridade local e o consentimento por senha não foram homologados como fronteira de dispatch e não provam isolamento contra processos maliciosos da mesma conta ou administrador local.

Por esses motivos, não houve chamada a provider, execução real de job, aceite de plano ou dispatch. Um plano real não pode ser escolhido implicitamente pelo agente: o proprietário precisa autorizar um plano específico depois que o adaptador e os gates acima estiverem prontos.

## Testes

`tests/test_apl_authorized_execution.py` usa SQLite real e executor/evidence-validator sintéticos. Cobre execução sequencial, dependências, grant revogado/expirado, scope/digest incompatível, `unknown`, recovery sem retry, retry apenas quando o executor confirma zero efeito, exclusividade de sessão, replay de conclusão, concorrência de workers, revogação entre tarefas, cancelamento entre tarefas e integridade do journal.

Verificações desta entrega: testes focados **67 passaram**; suíte completa **593 passaram, 1 skip em 302,65 s**; `git diff --check` passou. O skip é a prova POSIX já condicionada a plataformas POSIX no Windows.

Os testes sintéticos provam a máquina de estados e a persistência local. Eles não provam autenticação humana operacional, assinatura de grant real, integração ao Broker/TEG, isolamento do executor, efeitos de provider ou conclusão do MVP. Não houve chamada de provider nem job real.

## Próxima fatia

Implementar o vínculo atômico entre grant aprovado, ativação da proposta como revisão aceita e verificador persistido de grant/workspace; depois integrar esse resolver ao adaptador existente de `ExecutionStore`/`LocalRuntime`, sem alterar os defaults fail-closed. Só então preparar um plano sintético, pequeno e reversível para autorização operacional específica do proprietário.
