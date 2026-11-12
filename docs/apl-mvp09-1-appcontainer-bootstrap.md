# AgentFlow APL MVP-09.1 — AppContainer Runtime Bootstrap

**Data:** 2026-11-08

**Parecer:** **PARTIAL** — worker mínimo e controles sintéticos de filesystem demonstrados; revisão adversarial completa ainda não elegível. O backend continua indisponível no APL.

## 1. Baseline

O experimento está em `experiments/apl_windows_isolation/proof.py`, fora da rota importável/ativa de `agentflow.apl`. A revisão MVP-09 registrou `STATUS_DLL_INIT_FAILED` (`0xC0000142`) no Python copiado e smoke bem-sucedido com `cmd.exe`. Criação e remoção de AppContainer usam `CreateAppContainerProfile`/`DeleteAppContainerProfile`; o processo recebe `SECURITY_CAPABILITIES` com zero capabilities, é criado suspenso e atribuído a Job Object antes de retomar. O Job Object configura `KILL_ON_JOB_CLOSE`, limite de memória e limite de processos. Recursos do experimento vivem numa pasta única sob `%WINDIR%\Temp`; arquivos protegidos têm ACL própria que não concede acesso ao SID do container.

O host é Windows 10 Pro build 26300.9711 x64, com sessão administrativa de alta integridade. `py -0p` lista Python 3.13.17; a `.venv` usa esse Python, mas `include-system-site-packages=false`. `pyproject.toml` declara `cryptography>=46`; a `.venv` não contém esse pacote, enquanto o Python global do usuário contém cryptography 50.0.2. A falha de coleta já observada em nove módulos da suíte é de importação `agentflow.approval.webauthn -> cryptography.exceptions`. Nenhuma dependência foi instalada.

## 2. Diagnóstico e correções do bootstrap

O diagnóstico identificou dois defeitos reproduzíveis no arranjo anterior:

1. Copiar apenas `python.exe` e DLLs para o diretório permitido não formava uma distribuição Python; sem biblioteca padrão e DLLs de extensão, o inicializador não podia carregar o runtime. Um segundo arranjo com o launcher da `.venv` tentou alcançar o Python base em `%LOCALAPPDATA%` e recebeu **“Acesso negado”** do Windows. Não concedemos ACL a `%LOCALAPPDATA%`, `.venv`, workspace nem caminhos existentes.
2. A captura padrão de stdout/stderr usava handles do pai sem marcar herança e o `CreateProcessW` tinha `bInheritHandles=False`, deixando diagnósticos do filho indisponíveis.

O harness agora prepara dentro da pasta de output sintética uma distribuição mínima autocontida a partir do Python base já instalado: executável, DLLs do runtime, `DLLs`, biblioteca padrão e arquivo `python._pth` sem `site`. Não inclui site-packages, projeto, credenciais nem modelo. Essa árvore recebe ACL restrita específica do AppContainer. Stdout/stderr são handles explicitamente herdáveis; `CreateProcessW` só herda esses handles padrão. O environment continua allowlist reduzida; cwd é o diretório do runtime sintético. Nenhuma ACL preexistente foi modificada.

Isto corrige a causa operacional do Python base inacessível e o problema de captura. O `0xC0000142` ainda ocorre em tentativas de processo Python de longa duração; portanto não atribuímos esse sintoma a uma causa única. O worker curto de probe inicializa consistentemente.

## 3. Worker mínimo e política

O worker de teste é o Python embutido/copied, sem `site`, carregando apenas código de probe fixo passado por argumento, dentro da distribuição sintética. Não carrega modelos, não aceita comandos arbitrários do usuário, não abre projeto e não recebe credenciais. O smoke de escrita usa apenas `cmd.exe` e um marcador fixo no output concedido. Os probes de filesystem usam código fixo para tentar abrir somente sentinelas sintéticas cujo SID AppContainer está ausente da ACL.

A política mantém AppContainer sem capabilities, nenhum acesso ao workspace operacional, ACLs deny-by-default dos recursos sintéticos, nenhum acesso a credenciais reais, e Job Object aplicado antes do resume. Uma pasta de output temporária é explicitamente permitida. A rede permanece sem política conclusiva: não existe regra efetiva de firewall/egresso associada ao worker comprovada por esta entrega. Não afirmamos que AppContainer zero-capability, isoladamente, bloqueie toda rede/IPC.

## 4. Evidências e testes adversariais

Última repetição: `run_id=23082a933fb34af89b865eb3e95fc8cb`. O script termina com status `partial` (exit code 2 intencional: não declara prova aprovada). Diretório sintético e perfil foram removidos com sucesso.

| Cenário | Resultado reproduzido | Classificação |
|---|---|---|
| Worker `cmd.exe` mínimo no AppContainer | saiu com código 0 e criou marcador na pasta de saída autorizada | **Worker nativo iniciou; escrita autorizada demonstrada** |
| Python mínimo autocontido | probes curtos executaram; stdout capturado; código de saída do probe observável | **Bootstrap funcional para tarefa curta** |
| Leitura de arquivo protegido sintético | `PermissionError 13`; retorno de probe 13 | **Negação pelo Windows (ACL)** |
| Escrita no arquivo protegido sintético | `PermissionError 13`; retorno 13; conteúdo original inalterado | **Negação pelo Windows (ACL)** |
| Leitura de credencial sintética | `PermissionError 13`; retorno 13; conteúdo original inalterado | **Negação pelo Windows (ACL)** |
| Loopback de controle do host | processo de controle alcançou o listener; worker não conectou dentro do timeout, recebeu `TimeoutError` | **Inconclusivo, não classificado como negação** |
| Timeout/cancelamento de tarefa longa | tentativa com `timeout.exe`/Python não sustentou execução e terminou com falha de inicialização | **Não executado de modo válido** |
| Descendentes e encerramento da árvore | não foi lançado descendente sintético confirmado | **Não executado** |
| Quota Job Object | flags e limite de memória consultados; pico curto observado ~16 MiB sob quota de 512 MiB | **Configuração/accounting verificados; limite não estressado** |
| Limpeza | `synthetic_directory_removed=true`, perfil removido, HRESULT `0x00000000` | **Comprovada** |

As negações de filesystem/credencial são evidência efetiva do Windows: o interpretador chegou à operação e reportou `PermissionError` com código de acesso negado; não são falhas pré-despacho. O cenário de rede não tem uma negação explícita de socket nem prova por regra de rede, logo permanece pendente. Job Object em uso encerrou o processo principal; não alegamos encerramento de árvore sem descendente observado.

## 5. Ambiente de testes e instalação recomendada

A causa da ausência de `cryptography` é isolamento normal de virtualenv: `.venv` exclui pacotes do Python global. O teste focado disponível foi executado: `.venv\Scripts\python.exe -m pytest -q tests/test_apl_restricted_worker.py tests/test_apl.py` — **54 passed**. A suíte completa ainda não foi homologada; a coleta na `.venv` falha nos nove módulos listados acima. O pytest global coletou 435 testes; a execução de 435 passada consta do checkpoint MVP-08 e não foi repetida neste MVP.

Com a intenção explícita de instalar as dependências de projeto/teste, o comando recomendado pelo próprio projeto é:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

Esse comando não foi executado. Ele instala `cryptography>=46` e `pytest>=8,<10` conforme `pyproject.toml`, possivelmente requerendo acesso ao índice configurado. A correção do ambiente Python é independente da política AppContainer e deve ser feita separadamente.

## 6. Testes pendentes, limitações e próximos passos

Pendentes: negação de rede por política OS verificável; timeout e cancelamento de worker que permaneça ativo; criação e encerramento de descendentes; limite de processos sob tentativa de spawn; stress acima da quota de memória; falha parcial ao instalar política; repetição adversarial em ambiente Windows limpo. Não há compilador C/C++ local disponível para produzir um worker nativo dedicado, então nenhum compilador ou ferramenta foi instalado.

O ambiente atual combina usuário pai administrativo, comportamento de carregamento Python específico do build e rede loopback local. Os resultados valem só para este Windows/build e recursos sintéticos descritos; nada é inferido para Linux. O worker experimental não implementa o protocolo `PlannerAdapter` e não executa jobs APL.

**Decisão:** `PARTIAL`, não `READY_FOR_ADVERSARIAL_REVIEW`. Preservar `RestrictedInferenceHarness` fail-closed. Próximo passo é obter um worker mínimo de longa duração (binário já aprovado/disponível ou harness nativo revisado) e política de rede vinculada ao AppContainer em host de teste; só então executar stress, cancelamento, árvore e confirmar rede. Não habilitar backend semântico automaticamente.

## 7. Estado Git

Novos/alterados nesta etapa: `experiments/apl_windows_isolation/proof.py` e `docs/apl-mvp09-1-appcontainer-bootstrap.md`. As alterações locais, `.agentflow/` e `autonomous-development-runner/` foram preservadas. Não houve commit, push, merge, reset, clean ou sincronização. `git status` contém também todos os arquivos locais já listados no checkpoint MVP-09.
