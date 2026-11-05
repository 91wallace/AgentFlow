# Progresso da Fase 4

Branch local: `feat/git-recovery-phase-4`, baseada em `main` após o merge da Fase 3 (`edb7c4f7f0e672d8d1432d2d8948c3b49b7f4b91`). Sem commit/push/merge. Testes usam repositórios temporários e doubles; nenhum agente real é executado. Alterações locais anteriores em `docs/phase-2-progress.md`, `docs/phase-3-progress.md` e `autonomous-development-runner/` foram preservadas.

| Tarefa | Estado | Evidência |
|---|---|---|
| F4-01 | Concluída | ADR/arquitetura fixam workspace/branch UUID, digests SHA-256, limites, locks Windows/Linux, identidade do owner, recovery de órfão, retenção, critérios de checkpoint e configuração/hooks/filtros. |
| F4-02 | Concluída | Contratos substituíveis de workspace/Git e fake provider; `tasks.toml` schema v2 declara `versioned_paths`; v1 preserva escopo vazio/digest compatível. 19 testes de planejamento/contrato passaram. |
| F4-03 | Concluída | Migração v2→v3 transacional com backup, workspaces/operações/checkpoints/eventos, FKs e rollback injetado para v2. 4 testes de migração/schema passaram. |
| F4-04 | Concluída | Locks interprocesso `flock`/`msvcrt`, owner identity, lock abandonado persistente, preflight de dirty state/configuração e proteção read-only. 16 testes de provider/lock passaram. |
| F4-05 | Concluída | Worktree no git common dir, branch por workspace, identidade SQLite e idempotência; checkout original preservado byte a byte (HEAD e status mantidos). 2 testes integrados passaram. |
| F4-06 | Concluída | Escopo literal vinculado à revisão; path parsing NUL-safe; rejeição de traversal, symlink, arquivo operacional/segredo/cache/legado, colisão Windows e submódulo. Paths com espaços, Unicode e metacaracteres legais testados. |
| F4-07 | Concluída | Index temporário isolado; blobs sem filtros; `update-index --index-info -z`, `commit-tree` e CAS `update-ref`; paths revalidados antes do staging/commit; OID/parent/tree/ref/trailer/paths verificados. Repetição idempotente; regressão TOCTOU bloqueia sem mover HEAD. |
| F4-08 | Implementada localmente | Reconciliação só observa fatos Git; checkpoint único identificado é persistido sem criar segundo commit. Locks abandonados tornam operações `unknown`; verificação explícita de workspace limpo também é idempotente e recuperável. |
| F4-09 | Implementada localmente | Execução de tarefa com `versioned_paths` cria/reusa worktree própria. CLI expõe `workspaces`, `git-recover`, `checkpoint`, `verify-no-changes` e `complete`; conclusão verifica tentativa, validação, evidências da revisão e checkpoint/resultado limpo atual. |
| F4-10 | Homologação técnica aprovada nos commits de código e registro | Suíte completa local aprovada em Windows/Python 3.13; instalação editável e wheel com build isolation aprovadas, incluindo smoke test em venv limpo. Os jobs Ubuntu passaram para os commits de implementação `ccb94de` e registro `bf5a01a`; o estado do HEAD corrente é acompanhado pelos checks do PR. |

## Evidência incremental

- Baseline antes de alterações de código: 78 testes passaram em `.venv` Python 3.13.
- `tests/test_planning.py` e `tests/test_execution_schema.py`: 22 passaram; cobrem schema v2/path scope e migração/rollback v3.
- F4-04/05: lock/provider/workspace suites passaram; original manteve HEAD, bytes e status exatamente iguais.
- F4-06/07: 28 testes passaram incluindo checkpoint especial Unicode, idempotência, escopo, arquivos fora do escopo preservados, race entre validação/staging/commit e interrupção ambígua sem commit duplicado.
- F4-08/09: recuperação de checkpoint sem commit duplicado, recuperação idempotente de verificação sem alterações, e recusa de conclusão sem checkpoint foram testadas com repositórios temporários e processo simulado.
- Validação local mais recente: `python -m pytest -q` — 121 passaram em 64,74 s, incluindo limite de saída Git em streaming.
- Packaging diagnosis: `pip install --no-deps --no-build-isolation -e .` falhou porque o `.venv` não contém o backend de build declarado (`setuptools.build_meta`). A instalação normal com build isolation passou, usando o requisito de build já declarado no `pyproject.toml`; não alterei dependências de runtime/teste.
- Package smoke: wheel `agentflow-0.1.0-py3-none-any.whl` construída com `pip wheel --no-deps`, instalada em venv Python 3.13 limpo; `agentflow --version` retornou `agentflow 0.1.0` e import apontou para `site-packages`. SHA-256 da wheel: `ccf025879caa384f70bb69a47dff549a21c2e86e3ed4ce273496df175d11d40f`.
- Limitação de plataforma antes da publicação: runtime local é Windows. Linux/Ubuntu e Python 3.11/3.12 dependem dos jobs remotos da CI.
- `git diff --check` passou antes da preparação do staging isolado; a branch não foi mergeada nem publicada como release.

## Publicação e homologação remota

- PR aberto para `main`: https://github.com/91wallace/AgentFlow/pull/4
- Commit de implementação: `ccb94de0899962a6cfcdf0793fb1888fe8735eb8` (`feat: add local Git workspaces and checkpoints`), 27 arquivos Fase 4; alterações locais preexistentes e protótipo legado ficaram fora.
- Branch enviada: `feat/git-recovery-phase-4`; sem merge.
- CI Actions em Ubuntu, execução push `42039333340` e execução PR `42039384664`: `test (3.11)` passou, `test (3.12)` passou, `test (3.13)` passou em ambas as execuções.
- CI Actions em Ubuntu para o commit documental `bf5a01a7eea91f3b570cfcd66c58ef855ef6a0e5`, execução push `42039630387` e execução PR `42039639843`: os três jobs passaram em ambas as execuções.
- Instalação com build isolation corrigiu a falha anterior de `setuptools.build_meta`; o backend declarado foi usado temporariamente no ambiente de build. Nenhuma dependência de runtime/teste foi adicionada ou alterada.
- O pacote wheel instalado em venv Python 3.13 limpo importou de `site-packages`; `agentflow --version` passou. O hash da wheel consta acima.
