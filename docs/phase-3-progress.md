# Progresso da Fase 3

Base: merge da Fase 2 em `origin/main` (`c82ece560f2d1bf618d2f80e29c15a40ea19444b`). Branch: `feat/codex-executor-phase-3`. Publicação autorizada; merge automático proibido.

| Tarefa | Estado | Evidência |
| --- | --- | --- |
| F3-01 | Concluída | ADR 0003 alinhado à autorização; `codex --version` e `codex exec --help` inspecionados sem executar agente; suíte de base: 51 aprovados. |
| F3-02 | Concluída | Contratos de pedido, política, processo, resultado e protocolos independentes do adaptador; 2 testes aprovados. |
| F3-03 | Concluída | Migração opcional v1→v2, backup consistente, rollback testado, FKs e índice ativo; suíte: 55 aprovados. |
| F3-04 | Concluída | Reserva e tentativa `prepared` na mesma transação, revisão/hash e limite de 3; idempotência e falha de pré-checagem: 2 testes aprovados. |
| F3-05 | Concluída | Runtime local com timeout, cancelamento, drenagem, limite de log e marcadores de criação do processo/pai; Job Object Windows implementado; testes simulados no host Windows. |
| F3-06 | Concluída | Adaptador Codex com versão 0.166.0 homologada, inspeção de ajuda/capacidades e login, opções explícitas e falha quando configuração efetiva é incerta; 4 testes simulados aprovados. |
| F3-07 | Concluída | Eventos transacionais, metadados atômicos, digest/limite/truncamento, falha de escrita controlada, redação de tokens comuns e limpeza explícita de logs terminais. |
| F3-08 | Concluída | Resultado do processo separado da validação; conclusão exige evidências verificadas para cada critério da revisão; 6 testes de persistência aprovados. |
| F3-09 | Concluída | `recover` reconcilia reservas; tentativas vivas verificam PID/criação/grupo e continuam bloqueando; `cancel` sinaliza a tentativa e só encerra processo cuja identidade confere; reabertura repetida é idempotente. |
| F3-10 | Concluída | CLI explícita `execute`, `cancel`, `recover`, `cleanup`; 77 testes locais aprovados em Python 3.13 e smoke do pacote instalado aprovado. CI do PR #3 no commit `a204171`: Python 3.11, 3.12 e 3.13 aprovados (run `42021136155`). A primeira CI expôs testes que monkeypatchavam o módulo errado; correção publicada em `a204171`. |
