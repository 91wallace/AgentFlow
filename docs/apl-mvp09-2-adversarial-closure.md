# AgentFlow APL MVP-09.2 — AppContainer Adversarial Closure

**Data:** 2026-11-09
**Parecer:** **PARTIAL** — controles de filesystem e ciclo de vida foram comprovados; rede e limite de memória não satisfazem os critérios completos. APL continua fail-closed.

## 1. Baseline e causa do bootstrap

Foram revisados os relatórios MVP-09 e MVP-09.1, `experiments/apl_windows_isolation/proof.py` e `tests/test_apl_restricted_worker.py`. O harness permanece fora da rota ativa do APL. O ambiente é Windows 10 Pro x64 build 26300.9711, Python 3.13.17. A `.venv` não contém `cryptography`, embora `pyproject.toml` declare `cryptography>=46`; a coleta da suíte completa nela continua falhando em nove módulos de aprovação/recuperação/TEG que importam `cryptography.exceptions`. A ausência é independente do isolamento e nenhuma dependência foi instalada. O comando recomendado, não executado, é `\.venv\Scripts\python.exe -m pip install -e ".[test]"`.

O bootstrap anterior copiava apenas o launcher e algumas DLLs ou usava o launcher da `.venv`, que tentou localizar o interpretador base em `%LOCALAPPDATA%`; nesse caminho o Windows negou acesso ao AppContainer. A cópia parcial também não tinha runtime/biblioteca padrão suficientes. A versão atual monta runtime mínimo autocontido na pasta sintética autorizada, sem `site`, packages do projeto, segredos ou modelo. Também corrigiu a captura: os handles stdout/stderr são herdáveis e `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` limita herança a esses dois handles. Com o bootstrap atual, tanto probes curtos como worker persistente executaram. Logo, o `STATUS_DLL_INIT_FAILED` anterior foi causado pelo runtime incompleto/caminho de runtime inacessível; o processo longo já não falha sob o arranjo corrigido.

O processo é iniciado com AppContainer e zero capabilities, suspenso, atribuído a Job Object e então retomado. Nenhuma ACL de diretório existente, workspace, `.venv`, perfil ou configuração global foi ampliada. As ACLs manipuladas pertencem exclusivamente a arquivos novos do experimento em `%WINDIR%\Temp`.

## 2. Execução e resultados

Última execução reproduzível do harness: `run_id=a6426225dfc04bf3882549e68eb65fff`. O processo de controle iniciou um listener TCP em `127.0.0.1` numa porta efêmera; nenhuma conexão externa foi tentada. O harness terminou `partial`, removeu o perfil AppContainer (HRESULT `0x00000000`) e a pasta sintética (`synthetic_directory_removed=true`).

| Teste | Evidência observada | Resultado |
|---|---|---|
| Bootstrap e saída | CMD AppContainer sem capabilities criou o marcador permitido e saiu `0`; runtime Python autocontido executou probes | **Passou** |
| Leitura de sentinela protegida | Python executou `read_text`; Windows retornou `PermissionError 13`; exit `13`; conteúdo mantido | **Negação OS comprovada** |
| Escrita em sentinela protegida | Python executou `write_text`; Windows retornou `PermissionError 13`; exit `13`; conteúdo mantido | **Negação OS comprovada** |
| Leitura de credencial sintética | tentativa executada; `PermissionError 13`; exit `13`; conteúdo mantido | **Negação OS comprovada** |
| Rede local | Listener de controle no host aceitou o teste de controle; worker AppContainer fez tentativa ao mesmo loopback, mas recebeu `TimeoutError`; listener não recebeu conexão | **Inconclusivo**; timeout não prova negação |
| Timeout | Worker Python permaneceu ativo; deadline de 2 s expirou; status `timed_out`; `deadline_expired=true`; término Job ~0,005 s; `active_processes=0`; exit observado `1` | **Passou** |
| Cancelamento | Worker Python permaneceu ativo; cancelamento explícito após 2 s; status `cancelled`; término Job ~0,005 s; `active_processes=0`; exit observado `1` | **Passou** |
| Descendente | Worker iniciou filho Python sintético; accounting atingiu 3 processos (baseline do worker isolado: 2); stdout registrou PID do filho; após `TerminateJobObject`, `active_processes=0` em ~0,005 s; exit do pai `1` | **Árvore controlada encerrada** |
| Limite de processos | limite configurado em 2 para o teste de descendente, accounting máximo observado 3 | **Não comprovado**: excede a contagem esperada, mas não foi possível identificar individualmente todos os PIDs contabilizados; não homologado |
| Limite de memória | Job configurado em 64 MiB; worker alocou blocos sintéticos até `MemoryError` (exit `1`); pico reportado `67.702.784` bytes, limite `67.108.864` bytes | **Não passou**: peak accounting excedeu o valor configurado em `593.920` bytes |
| Limpeza | Job terminou sem processo ativo; pasta sintética e perfil removidos | **Passou** |

O teste de memória não foi repetido com carga maior nem com limite de host relaxado. O `MemoryError` indica que a alocação do Python foi recusada durante a carga, mas o accounting do próprio Job Object superou o limite nominal; portanto não declaramos que o worker ficou abaixo da quota. No teste de processos, o accounting chegou a 3 com limite configurado em 2; sem identificar os PIDs individuais contabilizados, não afirmamos qual processo excedeu a cota, mas o limite também não é homologado. São pendências de enforcement que exigem investigação antes de qualquer uso.

## 3. Rede e fronteira OS

Classificação implementada no harness diferencia `connection_allowed`, `connection_denied`, `timeout_inconclusive`, `bootstrap_failure` e `worker_or_service_error`. Neste run, o resultado foi **`timeout_inconclusive`**. O listener local estava vivo, como demonstrou o controle do host; o worker não completou a conexão no prazo e nenhum erro explícito de negação de socket foi produzido. Não houve regra de firewall global nem regra por AppContainer instalada. Assim, a restrição de rede continua sem prova conclusiva.

As negações de arquivos protegidos são evidência direta de ACL do Windows em recursos criados para o teste. Elas não demonstram, por si só, ACL efetiva de cada caminho do workspace operacional, perfil de usuário ou stores de credenciais. O harness nunca recebe esses caminhos como alvos de teste e não os alterou.

## 4. Testes, suíte e arquivos

- `.venv\Scripts\python.exe -m py_compile experiments/apl_windows_isolation/proof.py` — passou.
- `.venv\Scripts\python.exe experiments/apl_windows_isolation/proof.py` — executou a rodada adversarial acima, status parcial intencional.
- `.venv\Scripts\python.exe -m pytest -q tests/test_apl_restricted_worker.py tests/test_apl.py` — **54 passed**.
- `git diff --check` — passou; Git emitiu avisos LF/CRLF em arquivos locais preexistentes.
- Verificação independente: `pip show cryptography` na `.venv` diz `Package(s) not found`; `find_spec` retorna `None`. A suíte completa na `.venv` não é homologada. O checkpoint anterior com 435 testes no Python global não substitui execução no ambiente selecionado.
- Após o ensaio, não restaram diretórios `AgentFlowAplProof-*` no Windows Temp.

Alteração desta etapa: harness em `experiments/apl_windows_isolation/proof.py` e este relatório. Os relatórios MVP-09/MVP-09.1 foram mantidos como registros históricos das etapas anteriores.

## 5. Riscos residuais e decisão

Riscos e lacunas: rede sem resultado conclusivo; accounting de memória acima do limite nominal; limite de processos não efetivo como configurado; não há teste externo de egresso por política Windows; ACLs do workspace operacional não foram sondadas; usuário pai do experimento é administrador de alta integridade; resultado limitado a este build de Windows. Linux não foi testado.

**Parecer final: PARTIAL.** O critério `READY_FOR_ADVERSARIAL_REVIEW` não foi satisfeito. Não transformar os casos inconclusivos em aprovação, não integrar este harness ao executor/PlannerAdapter e não liberar backend semântico ou execução autônoma.

## 6. Estado Git

O working tree já estava sujo, com edições e arquivos não rastreados do usuário relativos a fases, RB-09C, APL e `autonomous-development-runner/`. Foram preservados, incluindo `.agentflow/` (sem alteração). Nenhum commit, push, merge, reset, clean ou sincronização foi feito. O status final inclui os mesmos itens locais preexistentes e as alterações/untracked do harness e deste relatório.
