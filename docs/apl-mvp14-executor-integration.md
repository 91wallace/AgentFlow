# MVP-14 — Integração controlada do APL com o executor

## Resultado

**Classificação: PARTIAL.** Foi formalizada e testada a integração de leitura entre o APL supervisionado e o `ExecutionStore`: o APL consulta resultados já concluídos e evidências verificadas por uma API pública tipada. O APL continua sem despachar jobs. O dispatch encadeado permanece bloqueado pelos contratos atuais da Fase 7 e pela homologação operacional pendente de Broker/TEG.

## Contratos e componentes mapeados

- `SupervisedCycleService.next_job` seleciona a próxima tarefa cujas dependências estejam concluídas e exige que o escopo requerido esteja contido no escopo imutável do ciclo.
- `ExecutionStore.prepare` reserva uma tentativa usando `operation_id`, valida digest/revisão, dependências, limite de tentativas e vínculo de workspace/escopo. A reserva não é, por si só, autorização para o APL iniciar execução.
- `ExecutionStore.finish`, `set_validation` e `reconcile_interrupted` registram o resultado, validação e recuperação conservadora. Execuções interrompidas não são repetidas automaticamente.
- `CodexAdapter` e `LocalRuntime` pertencem ao fluxo existente de execução individual (`agentflow execute`). O APL não os chama.
- A avaliação e o replanejamento supervisionado continuam separados da execução. `submit_result` só associa uma tentativa persistida, validada e com evidência verificável; `unknown` bloqueia o ciclo.
- ADR 0008 e `docs/apl-mvp-progress.md` mantêm Fase 7/Broker/TEG fail-closed para dispatch automático pelo APL. Testes com simuladores não alteram essa autorização.

## Mudança implementada

`ExecutionStore.validated_evidence(attempt_id)` é uma consulta somente de leitura que retorna referências tipadas apenas quando:

1. A tentativa terminou em `process_succeeded` e `validation_passed`.
2. Existe exatamente um evento persistido `validation_passed`.
3. O evento contém uma lista válida e não duplicada de IDs.
4. Cada ID referencia evidência `verified`, aprovada e vinculada ao projeto, tarefa e revisão daquela tentativa.

`SupervisedCycleService.submit_result` usa essa interface pública em vez de consultar tabelas SQL internas. A associação APL continua sujeita à cobertura de todos os critérios de aceite da tarefa e aos controles de duplicação do store de ciclos. Não houve migração de schema nem alteração de Broker/TEG, RB-09C, Production Mode ou RestrictedInferenceHarness.

## Caminhos permitidos e bloqueados

**Permitido:** selecionar uma proposta elegível; ler tentativa e evidências já persistidas; associar resultado validado uma única vez; persistir o estado e o histórico do ciclo; bloquear resultados desconhecidos ou incompletos.

**Bloqueado:** APL chamando `CodexAdapter`, `LocalRuntime`, scheduler de dispatch ou `ExecutionStore.prepare` para iniciar um job; loop automático de execução/retry; tratar aprovação pendente, confiança do workspace ou resultado simulado como autorização. O fluxo `agentflow execute` continua sendo a interface de execução individual sob suas próprias verificações e não é encadeado pelo APL.

## Evidências e testes

Testes focados executados após a mudança:

```text
.venv\Scripts\python.exe -m pytest tests/test_apl.py tests/test_execution_reservation.py tests/test_execution_schema.py -q
46 passed in 8.27s
```

Os testes incluem associação de evidência persistida, resultado não validado, resultado `unknown`, escopo, dependências, deduplicação entre ciclos e concorrência de transições. Foram adicionados testes para a API pública tipada e para recusar IDs de evidência divergentes do evento de validação persistido. São testes com stores/fixtures locais; não são execução real de provider ou homologação Broker/TEG.

Suíte completa executada após as alterações:

```text
.venv\Scripts\python.exe -m pytest -q
448 passed in 317.89s (0:05:17)
```

Não houve falhas nem skips reportados pelo pytest. `git diff --check` foi executado ao fechar o checkpoint.

## Limitações e próxima etapa

O caminho de associação agora não depende do esquema SQL interno do executor, mas a composição continua em dois serviços locais e não representa autorização distribuída nem dispatch protegido. Permanecem sem homologação: integração operacional Broker/TEG, autorização independente para loop de jobs, execução real pelo APL, e garantias de segurança de provider. Próximo passo técnico: obter a homologação independente dos contratos de autorização/dispatch da Fase 7; somente então avaliar uma interface explícita de dispatch aprovada, preservando `operation_id`, receipt, reconciliação e estado `unknown` sem retry automático.
