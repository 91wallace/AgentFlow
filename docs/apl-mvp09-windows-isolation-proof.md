# AgentFlow APL MVP-09 — Windows Isolation Proof

**Data:** 2026-11-08

**Parecer Windows:** **PARTIAL**

**Parecer Linux:** **BLOCKED / não testado**

**Parecer geral:** **PARTIAL; insuficiente para revisão de piloto de worker restrito**

Esta experiência não integra o caminho ativo de `agentflow.apl`, não inicia inferência real e não libera o `RestrictedInferenceHarness`. O harness permanece fail-closed. Nenhum provider, modelo, credencial real, configuração global, serviço ou firewall foi ativado ou alterado. As únicas ACLs alteradas foram de diretórios e arquivos sintéticos recém-criados em `%WINDIR%\Temp`; esses artefatos e o perfil AppContainer temporário foram removidos no encerramento.

## 1. Ambiente Python e baseline

- Windows 10 Pro, versão 2009, build 26300.9711, 64 bits. A sessão atual tem token administrativo de alta integridade; portanto não foi tratado como identidade de baixa permissão.
- `py -0p` lista apenas Python 3.13 em `C:\Users\criarte\AppData\Local\Programs\Python\Python313\python.exe`; `.venv\Scripts\python.exe` também é Python 3.13.17.
- `.venv\pyvenv.cfg` define `include-system-site-packages = false`. `cryptography` está declarado em `pyproject.toml` como `cryptography>=46`, mas não está instalado na `.venv`. O Python global do usuário tem `cryptography` 50.0.2; pytest global é 9.2.0. `python.exe` sem caminho resolve para alias da Microsoft Store e falha; usar `py` ou o caminho explícito.
- A ausência da dependência na `.venv` explica a falha de coleta dos nove módulos `test_approval_identity`, `test_approval_persistence`, `test_approval_presentation`, `test_approval_transport`, `test_approval_webauthn`, `test_phase7_audit_cli`, `test_phase7_sdd`, `test_recovery` e `test_teg`: importação converge em `agentflow.approval.webauthn` e falha em `cryptography.exceptions`. Nenhuma dependência foi instalada.
- Na inspeção MVP-08, pytest global completou 435 testes; nesta etapa apenas 435 foram coletados com Python global, não executados de novo. A suíte não é declarada homologada enquanto o ambiente selecionado do projeto (`.venv`) continuar inconsistente. Os testes focados APL, executados nesta etapa na `.venv`, passaram: 54.

## 2. Mecanismo selecionado e fronteira

Foi investigada uma combinação experimental de **AppContainer sem capacidades** (SID temporário dedicado) + ACLs explícitas em recursos sintéticos + **Job Object** para encerramento e limites. O processo é criado suspenso, associado ao Job Object e só então retomado. Isso é um harness manual de prova, separado de APL e do runtime normal.

AppContainer é a fronteira candidata para token e acesso a recursos Windows. ACLs precisam provar a negação por recurso; Job Objects servem a ciclo de vida/quotas, mas **não** demonstram isolamento de filesystem, credenciais ou rede. O teste de perfil apenas prova que APIs de criação/remoção funcionaram; não prova sozinho o token ou cada política efetiva. O executor local existente e o `WindowsJob` normal não foram substituídos nem chamados.

Windows Sandbox está desabilitado e não foi habilitado. Não há WSL instalado. Não foram configurados usuários dedicados, restricted tokens, regras globais de firewall, permissões globais ou políticas de rede do host.

## 3. Prova operacional e evidência

Harness experimental: `experiments/apl_windows_isolation/proof.py`. Última execução: `run_id=c0fdc5dc89f24ecfbfbef14faeb03edc`.

| Controle / cenário | Resultado observado | Classificação |
|---|---|---|
| Criar e remover perfil AppContainer temporário | `CreateAppContainerProfile` retornou sucesso; remoção HRESULT `0x00000000` | **Aplicado e operação API verificada** |
| Lançar worker sintético sem capacidades | `cmd.exe` criado via atributo `SECURITY_CAPABILITIES`, executado e saiu `0` | **Lançamento comprovado** |
| ACL de output sintético | SID AppContainer recebeu Modify; `cmd.exe` gravou `appcontainer-smoke.txt`, conteúdo conferido | **Escrita permitida comprovada** |
| Job Object | flags observadas `8712` (`KILL_ON_JOB_CLOSE`, job memory, active process count); limite configurado 256 MiB no smoke; pico observado ~8,1 MiB | **Configuração e accounting observados; limite não estressado** |
| Encerramento do processo executado | após término, accounting reportou `active_processes=0` | **Processo principal encerrado; árvore não demonstrada** |
| Leitura/escrita de sentinela protegida | ACL sintética omitiu o SID; tentativas Python terminaram com código 106 (`STATUS_DLL_INIT_FAILED`) antes de executar o código de acesso | **Falha de inicialização; teste não executado, não é negação pelo SO** |
| Leitura de credencial sintética | mesma falha 106 antes da chamada de filesystem | **Teste não executado** |
| Rede/loopback | controle do host alcançou listener; cliente sob AppContainer terminou com 106, sem tentativa de socket | **Teste não executado; rede não comprovada** |
| Timeout / cancelamento | worker Python terminou com `0xC0000142` antes do prazo | **Teste não executado** |
| Descendentes / limite de processos | nenhum descendente confirmado | **Teste não executado** |
| Excesso de memória | quota Job Object configurada, sem carga adversarial | **Não estressado** |
| Remoção dos recursos do experimento | diretório sintético removido; perfil removido | **Limpeza verificada** |

Tentativas anteriores de usar PowerShell no AppContainer também falharam na inicialização. Uma tentativa com `cmd.exe` e status de saída 1 sem mensagem legível foi descartada como inconclusiva. A execução com Node foi rejeitada no despacho por `CreateProcessW(AppContainer)` / erro 5, logo não conta como teste do worker. Nenhum cenário de negação por SO é declarado aprovado nesta prova.

O código 106 do runtime Python copiado à pasta do experimento mostrou que a carga de DLL/runtime não foi inicializada sob esta configuração AppContainer. Apesar de o processo ser criado e contabilizado, a lógica Python adversarial não chegou a ser executada. Esta distinção impede transformar um crash precoce em “acesso negado”.

## 4. Controles não comprovados e riscos

- A negação efetiva de leitura/escrita do workspace, arquivos protegidos e credenciais não foi demonstrada por tentativa executada. ACLs observadas parecem restringir os recursos sintéticos, mas não substituem a tentativa adversarial bem-sucedida em iniciar um worker.
- Rede/egresso não foi restrita nem validada. AppContainer com zero capacidades não é considerado prova suficiente de negação de toda rede/IPC; não há firewall efetivo associado ao processo comprovado.
- Não foi testada criação de subprocesso, limite de processos com descendente, encerramento de árvore, limite de memória por excesso, CPU ou I/O.
- Job Object não reduz identidade nem permissões e não impede acesso a filesystem, credenciais ou rede. O processo pai administrativo continua sendo uma preocupação; a prova não seleciona nem homologa uma identidade operacional dedicada.
- A falha ao executar Python sob o AppContainer aponta para falta de runtime/dependências acessíveis ou incompatibilidade de inicialização. Não se tentou ampliar permissões para contornar a falha.
- Não foram criados dados reais ou usados providers externos. A prova não homologa Linux nem outras versões/configurações do Windows.

## 5. Comandos e verificações

Executados nesta etapa:

- `.venv\Scripts\python.exe -m py_compile experiments/apl_windows_isolation/proof.py` — passou.
- `.venv\Scripts\python.exe experiments/apl_windows_isolation/proof.py` — terminou com status do harness `partial` (exit code 2 intencional para resultado não aprovado); executou smoke CMD e configuração/cleanup, demais probes de runtime falharam ou foram não executados.
- `.venv\Scripts\python.exe -m pytest -q tests/test_apl_restricted_worker.py tests/test_apl.py` — **54 passed**.
- `git diff --check` — passou; Git emitiu apenas avisos de conversão LF/CRLF em arquivos locais preexistentes.
- `pytest -q --collect-only` no Python global — **435 coletados**. A execução global de 435 testes consta no checkpoint MVP-08, não foi repetida nesta etapa. A coleta/execução completa na `.venv` continua impedida pela ausência de `cryptography`.

O harness experimental retorna exit code diferente de zero porque não pode declarar aprovação quando o status não é `passed`.

## 6. Parecer e estado Git

**Windows: PARTIAL.** Há prova operacional limitada de criação/remoção do perfil AppContainer, despacho de `cmd.exe` sem capacidades, escrita no diretório de saída explicitamente permitido e accounting/encerramento do processo. Os controles obrigatórios de negação filesystem/credencial/rede e limites sob abuso não têm prova válida.

**Linux: BLOCKED / não testado.** Host Windows sem WSL instalado; nenhum resultado Windows foi extrapolado.

**Conclusão:** não atende `READY_FOR_WINDOWS_ISOLATION_REVIEW`. Manter `RestrictedInferenceHarness` fail-closed e worker real indisponível.

Arquivos novos desta entrega: `experiments/apl_windows_isolation/proof.py` e este relatório. As alterações locais preexistentes foram preservadas; não houve commit, reset, clean, merge, push ou sincronização. `git status` continua incluindo as alterações do usuário já presentes antes desta etapa, além dos artefatos novos `experiments/` e `docs/apl-mvp09-windows-isolation-proof.md`.

Próximo passo recomendado: em host de teste Windows dedicado, resolver a inicialização mínima do worker dentro de AppContainer sem relaxar ACLs, depois executar sentinelas sintéticas de leitura/escrita, teste de rede com listener controlado, limite de processos/descendentes, stress de memória, timeout e cancelamento. Enquanto qualquer prova obrigatória não executar e verificar enforcement, manter bloqueado o backend.
