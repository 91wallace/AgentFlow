# Progresso da Fase 2

Fase 2 integrada à `main` pelo [PR #2](https://github.com/91wallace/AgentFlow/pull/2) em 2026-11-04, merge commit `c82ece560f2d1bf618d2f80e29c15a40ea19444b`. O commit revisado `71f8b326e1d2981cced8091b89064eb1ed3d2cb8` passou nos seis jobs de push/PR para Python 3.11, 3.12 e 3.13. A Fase 3 ainda não foi implementada.

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
| F2-10 | Concluída e revalidada na CI | Pytest: 51 aprovados em Python 3.13 após correções de backup pré-migração, namespace de recuperação, dependências alteradas e inspeção sem escrita. Jobs Python 3.11, 3.12 e 3.13 passaram em [push](https://github.com/91wallace/AgentFlow/actions/runs/41950906259) e [PR](https://github.com/91wallace/AgentFlow/actions/runs/41950912979). |
