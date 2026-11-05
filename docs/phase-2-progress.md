# Progresso da Fase 2

| Tarefa | Estado | Evidência |
| --- | --- | --- |
| F2-01 | Concluída | ADR 0002 registrado; `git diff --check` sem erros. |
| F2-02 | Concluída | Modelos imutáveis e parser TOML v1; pytest: 27 aprovados. |
| F2-03 | Concluída | Validação de referências e ciclos, ordem estável; pytest: 27 aprovados. |
| F2-04 | Concluída | Protocolo e tipos de domínio sem SQLite; pytest: 27 aprovados. |
| F2-05 | Concluída | Esquema v1, WAL, FKs, reabertura, falha de backup/migração; pytest: 31 aprovados. |
| F2-06 | Concluída | Importação idempotente, revisões imutáveis e revalidação auditável; pytest: 36 aprovados. |
| F2-07 | Concluída | Transições, evidências, checkpoints, idempotência e disputa entre duas conexões e dois processos; pytest: 47 aprovados. |
| F2-08 | Concluída | Reabertura, reserva órfã bloqueada uma vez, rollback e corrupção; pytest: 41 aprovados. |
| F2-09 | Concluída | CLI `plan` e `state` somente leitura, ignore seletivo e guia; pytest: 43 aprovados. |
| F2-10 | Homologação local concluída; CI remota pendente | Pytest: 48 aprovados em Python 3.13; instalação local do wheel e `agentflow --version` passaram; `pip check` sem erros. Python 3.11/3.12 e GitHub CI serão verificados após publicação da branch. |
