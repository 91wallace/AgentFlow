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
| F2-10 | Concluída e validada na CI | Pytest: 48 aprovados em Python 3.13 local; wheel instalado, `agentflow --version` e `pip check` aprovados. PR #2: jobs Python 3.11, 3.12 e 3.13 aprovados em [push](https://github.com/91wallace/AgentFlow/actions/runs/41950220867) e [pull request](https://github.com/91wallace/AgentFlow/actions/runs/41950242639). |
