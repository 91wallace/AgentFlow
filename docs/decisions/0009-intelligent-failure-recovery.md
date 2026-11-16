# ADR 0009 — Intelligent Failure Recovery (IFR)

**Estado:** direção aprovada; fundação operacional inicial implementada no MVP-20.1; homologação integral e intervenção autenticada/retomada pendentes.

**Data:** 2026-11-12

**Escopo:** recuperação compartilhada pelo controlador de execução e futuro AQA. Esta decisão não habilita dispatch, não altera grants existentes e não antecipa a implementação completa do AQA.

## Contexto

O fluxo contínuo precisa responder a falhas sem repetir cegamente uma operação, confundir avaliação de qualidade com execução, nem permitir que um agente transforme seu próprio diagnóstico em autorização. Resultado operacional ambíguo pode representar efeito externo já ocorrido e exige reconciliação conservadora.

## Decisão

### Resultados independentes

Qualidade da saída é um eixo separado de execução e autorização:

- `PASS`: critérios avaliados com evidência suficiente e todos satisfeitos.
- `FAIL`: critérios avaliados com evidência suficiente e pelo menos um não satisfeito.
- `INCONCLUSIVE`: evidência insuficiente, inválida, conflitante ou stale para decidir.

O resultado de qualidade não substitui estados de processo (por exemplo `succeeded`, `failed`, `cancelled`, `unknown`) nem estados de autorização (por exemplo `approved`, `expired`, `revoked`, `blocked`). Em particular, execução bem-sucedida não implica `PASS`; `INCONCLUSIVE` nunca é convertido em `PASS` ou `FAIL` por timeout ou conveniência.

### Diagnóstico e decisão de retry

Antes de cada nova tentativa, um componente IFR registra diagnóstico e decide separadamente se repetir é necessário, potencialmente útil, autorizado e seguro. O agente pode sugerir hipótese ou estratégia; somente a policy/authority já existente pode permitir a operação. IFR não emite, renova nem amplia grants.

Classificações de diagnóstico:

- natureza: `deterministic`, `transient`, `environment`, `evaluation_inconclusive`, `operational_unknown`, `security_or_authorization_block`;
- `quality_failure` para critérios avaliados que falharam, sem presumir por si só a causa operacional;
- recuperabilidade: `potentially_recoverable` ou `not_recoverable`.

Essas dimensões não são intercambiáveis. Por exemplo, `operational_unknown` não vira transitória por uma mensagem de erro conhecida; sem reconciliação é inelegível para retry. Falha determinística sem correção/estratégia nova encerra sem repetição inútil. Bloqueio de segurança/autorização sempre suspende.

O limite inicial proposto é **3 tentativas totais por incidente, incluindo a execução inicial**. É configurável por policy e pode encerrar antes. Limites separados e persistidos cobrem repetições equivalentes, duração e orçamento. Orçamento estimado não é teto financeiro de provider sem enforcement externo comprovado. O grant ainda pode impor limite menor, nunca maior que o escopo autorizado.

### Evidência e tentativas

Cada incidente e tentativa persistida deve ligar:

- IDs de ciclo, plano/revisão, tarefa, grant/escopo aplicável e `operation_id`;
- índice de tentativa, estratégia/versão e assinatura normalizada de falha;
- contexto relevante da assinatura (adapter/provider/model, ambiente/plataforma, fase/ação, código/causa normalizada e digests de entrada), sem tratar texto idêntico como causa idêntica;
- hipótese, decisão IFR e razões para necessidade/utilidade/autorização/segurança;
- ações e operações realizadas, horários, efeitos observados, mudanças/diff/checkpoint digests;
- falhas, resultado de qualidade, referências/digests de evidência e resultado de reconciliação;
- limites configurados/consumidos, intervenção humana e resultado de retomada.

Registros e decisões são append-only no State Store/journal existente. Replays com os mesmos IDs e mesmo conteúdo são idempotentes; conteúdo divergente conflita. Checkpoints não sobrescrevem tentativas anteriores.

### Ambiguidade e reconciliação

Um `unknown` suspende a tarefa e o ciclo. Retry só é elegível depois de (a) prova verificável de que nenhum efeito começou ou ocorreu, ou (b) reconciliação autoritativa que determine o estado atual e torne a próxima operação segura/idempotente. Ausência de resposta, PID encerrado, timeout, ou resultado semelhante não prova ausência de efeito. Se não houver prova suficiente, solicitar intervenção e preservar estado.

### Estratégias e loops

Quando uma estratégia falha, o diagnóstico considera evidências anteriores antes de recomendar alternativa. A policy deve impedir repetição equivalente sem progresso: assinatura normalizada mais contexto, estratégia, operação e resultados anteriores alimentam a detecção; mensagens iguais isoladas não provam recorrência. Uma alternativa só pode ser executada se continuar coberta pelo escopo, grant, limites e validade originais. Alteração do plano, tarefa, operação, workspace ou permissões exige consentimento novo conforme contrato vigente.

### Escalonamento humano

Ao atingir limites, encontrar bloqueio de autorização, não ter estratégia segura ou permanecer inconclusivo, IFR persiste `awaiting_operator`/bloqueio e apresenta resumo com: pedido original; trabalho executado; o que falhou/está inconclusivo; correções tentadas; evidências; motivo da pausa; alternativas seguras. A resposta humana fica autenticada conforme o perfil, vinculada ao incidente/tarefa e registrada com digest/escopo. Ela pode escolher apenas alternativa já coberta ou pedir uma autorização nova; nunca amplia o grant implicitamente.

## Integração e ordem

Durante MVP-20/MVP-21 será implementada a base comum de contratos, persistência, diagnóstico/retry policy, limites, journal e retomada conservadora usada pelo controlador. O MVP-20.1 já acrescentou esses contratos e a integração inicial ao controlador grant-scoped, incluindo limites imutáveis por run, tentativas identificadas e escalonamento persistido. Ainda faltam a homologação integral dos cenários, a resposta humana autenticada e retomada vinculada, reconciliação de efeitos e eventual uso operacional. Ela será compartilhável com o futuro AQA por interfaces explícitas; AQA não será implementado por antecipação.

Somente depois da homologação de execução real poderão iniciar os marcos AQA: avaliação adversarial independente, correção orientada por Critic e Quality Gates. Esses componentes não concedem autorização e `PASS` só pode vir de avaliador definido pela policy com evidência adequada.

## Critérios de homologação IFR

Testes determinísticos devem demonstrar:

1. Falha transitória recuperada depois de prova de ausência de efeito, com tentativa e `operation_id` corretos.
2. Falha determinística sem repetição inútil.
3. `INCONCLUSIVE` preservado, sem falso `PASS`/`FAIL`.
4. Estratégia alternativa selecionada e registrada após falha da anterior.
5. Repetição sem progresso detectada com assinatura e contexto.
6. Limite configurado de tentativas respeitado, contando a execução inicial; encerramento antecipado quando retry não se justifica.
7. `unknown` bloqueado até reconciliação suficiente, sem replay automático.
8. Escalonamento contém resumo, evidências e alternativas seguras.
9. Retomada após resposta humana válida e persistida, sem aceitar resposta stale/replay.
10. Nenhuma proposta IFR amplia escopo, grant, validade ou operações.

Executar também crash/reopen, concorrência, replay de eventos, corrupção de journal, falha de persistência, expiração/revogação e compatibilidade Windows/Linux. Testes sintéticos não homologam execução real.

## Consequências e limites

- O retry deixa de ser uma consequência automática de estado `failed`; exige decisão persistida elegível.
- Limites e decisão IFR fazem parte do checkpoint de execução e da auditoria.
- AQA e avaliadores independentes permanecem futuros; esta ADR não os implementa.
- Perfis `LOCAL_SUPERVISED` e `INDEPENDENT_AUTHORITY` mantêm seus próprios controles; IFR não reduz nenhum gate.
- Nenhuma fase homologada é renumerada. O roadmap recebe uma trilha transversal MVP-20/MVP-21 e marcos AQA posteriores.
