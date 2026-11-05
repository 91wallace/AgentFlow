# ADR 0003: Política proposta para execução local da Fase 3

Status: diretrizes aprovadas; execução de agentes ocorre somente por solicitação explícita na CLI.

## Contexto

A Fase 2 persiste tarefas e revisões, mas não executa agentes. A Fase 3 precisa integrar Codex CLI por adaptador substituível e runtime local, preservar a configuração do usuário e recuperar interrupções sem duplicar efeitos externos.

## Decisões consolidadas

1. Uma solicitação explícita executa no máximo uma tarefa. Timeout padrão de 1800 segundos por tentativa, configurável de 1 a 7200; inatividade desativada por padrão. No máximo três tentativas totais por tarefa, incluindo tentativa `prepared` cujo spawn falhou.
2. Não há repetição automática. Nova tentativa exige `operation_id` novo, capacidade restante e reconciliação registrada do workspace. Falha antes de spawn comprovada sem efeitos pode ser liberada; falha após spawn, cancelamento, timeout, falha de validação e resultado desconhecido exigem inspeção e decisão humana. Resultado desconhecido não é classificado como sucesso ou falha.
3. O executor depende de protocolos de agente e processo. O adaptador Codex detecta versão, ajuda/capacidades e configuração efetiva; flags não verificadas falham na pré-checagem. Configuração Codex existente nunca é reescrita. AgentFlow não ativa sandbox automaticamente; aceita a escolha explícita do usuário, inclusive `danger-full-access`. Aprovação não interativa é permitida quando explícita e suportada. Sandbox do Codex e Docker são mecanismos distintos.

   A configuração de uma tentativa entra por opções explícitas do pedido; campos omitidos herdam a configuração do Codex. O adaptador registra apenas valores efetivos que a CLI instalada consiga confirmar por meio suportado, sem ler ou gravar segredos. Se não conseguir confirmar sandbox/aprovação efetivos em modo não interativo, rejeita a tentativa antes do spawn. A exceção manual de conclusão da Fase 2 não é usada pelo executor: conclusão automática requer evidências verificadas.
4. SQLite v2 guarda tentativa, revisão, `operation_id`, estado, identidade de processo e eventos. Reserva/`prepared` são transação anterior ao spawn; PID/início/grupo são confirmados em transação posterior. SQLite e subprocesso não são atomicamente coordenáveis: intervalo ambíguo bloqueia e exige reconciliação. Índice único impede duas tentativas ativas da mesma tarefa.
5. Identidade de processo inclui PID, horário de criação fornecido pelo sistema, grupo/sessão ou Job Object, `attempt_id`, token e pai. PID isolado nunca autoriza matar/adotar. Linux usa nova sessão, SIGTERM do grupo e SIGKILL após 10 segundos; Windows usa Job Object para encerrar a árvore, com espera de até 10 segundos. Sem controle confiável, não iniciar. Morte do pai pode deixar grupo vivo no Linux; recuperação bloqueia até verificar identidade. Reabrir não reinicia processo.
6. Estados da tentativa: `prepared`, `running` e terminais `process_succeeded`, `process_failed`, `timed_out`, `cancelled`, `interrupted`. Validação tem resultado separado `validation_passed`/`validation_failed`; somente evidência verificada da revisão correta pode justificar `completed` na tarefa. `interrupted` significa resultado desconhecido.
7. Logs incrementais sob `.agentflow/runs/<attempt_id>/`, ignorados: limite padrão de 10 MiB por fluxo, máximo configurável de 50 MiB; excesso causa cancelamento e `log_limit`, com drenagem para evitar bloqueio. Retenção padrão de 30 dias, limpeza apenas por operação explícita após estado terminal. Eventos SQLite têm payload máximo de 4 KiB, sem saída integral; metadados e visualizações são sanitizados. Backup precede migração e manutenção do banco.

## Registro da implementação inicial

A versão local `codex-cli 0.166.0` teve `exec --help`, `--help` e `login status` verificados sem iniciar agente. O adaptador só aceita esta versão homologada e exige sandbox e política de aprovação explícitos, pois não consegue confirmar com segurança a configuração herdada de todas as camadas. A CLI não grava nem reescreve a configuração Codex do usuário. `on-request` é recusado porque uma execução não interativa não pode aguardar aprovação interativa.

## Consequências e riscos residuais

O usuário pode escolher acesso completo ou aprovações não interativas; AgentFlow registra a configuração efetiva e não a altera silenciosamente. Esses modos ampliam o alcance de efeitos do agente e exigem clareza no pedido de execução. Versões do Codex podem mudar flags ou não expor configuração efetiva; nesse caso, a execução falha antes de spawn. Autoria de verificação continua sem autenticação forte, logs podem conter dados sensíveis apesar da sanitização e CPU/memória rígidas não são prometidas no runtime local. Nenhum código é implementado por este ADR.
