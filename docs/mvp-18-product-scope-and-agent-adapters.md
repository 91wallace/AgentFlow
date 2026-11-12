# MVP-18 — Escopo do produto e contrato de adaptadores

## Decisões consolidadas

O AgentFlow é uma ferramenta local, instalável e compatível com Windows e Linux. O núcleo mantém planejamento, políticas, persistência, execução de processos, validação, evidências, auditoria, cancelamento e recuperação. Agentes de IA são dependências substituíveis chamadas por adaptadores; eles não escolhem ou ampliam o escopo autorizado, não são a fonte de verdade do estado e não certificam suas próprias evidências.

O produto deve aceitar diferentes agentes por adaptadores. Codex CLI é a integração inicial. A operação ocorre pela CLI e, no futuro, por integrações que usem os mesmos contratos públicos; integração não significa criar um daemon obrigatório ou um segundo orquestrador.

A direção local aprovada na ADR 0008-R e o perfil `LOCAL_SUPERVISED` são preservados. `INDEPENDENT_AUTHORITY` e H-01–H-10 continuam sendo um perfil separado. Este documento não altera a ADR 0008 original, não declara o perfil local homologado e não libera grants operacionais nem dispatch.

## Responsabilidades

| AgentFlow | Adaptador/agente |
|---|---|
| Valida tarefa, workspace, política e limites antes da preparação. | Traduz uma solicitação tipada para o protocolo específico do CLI. |
| Mantém estado, identidade da operação, runtime, cancelamento, timeout, logs, resultados e recuperação. | Executa sob a identidade e permissões efetivas do processo local. |
| Associa resultados a tentativas e coleta digests/evidências no `ExecutionStore`. | Interpreta seu protocolo de saída sem poder marcar critérios do AgentFlow como verificados. |
| Decide bloqueio, validação e próximo passo conforme autorização persistida. | Informa capabilities observadas ou desconhecidas; nunca concede autorização. |

O conteúdo do objetivo, instruções, respostas e artefatos do agente é entrada não confiável. O prompt não constitui isolamento de ferramentas, filesystem, rede ou credenciais.

## Contrato de adaptador

O contrato histórico `AgentAdapter` (`prepare` e `interpret`) continua válido. `LifecycleAgentAdapter` acrescenta uma superfície comum v1:

- `discover() -> AgentAvailability`: presença/versão e estado `available`, `unavailable` ou `unknown`, com fonte e código de motivo; descoberta é somente preflight e não inicia inferência.
- `capabilities() -> AgentCapabilities`: capabilities de sessão são separadas das operações de processo mantidas pelo AgentFlow.
- `prepare_task(request) -> ProcessSpec`: prepara uma invocação tipada; não executa o processo.
- `collect_result(ProcessResult) -> AgentResult`: decodifica resultado específico do agente. Evidência verificável continua sob o `ExecutionStore` e validadores.
- `normalize_state(status) -> NormalizedAgentState`: normaliza estados comuns sem converter `timed_out`, `interrupted` ou desconhecidos em sucesso/falha conclusiva.

O registry mantém registro explícito, não importa plugins nem descobre entry points automaticamente. `resolve_lifecycle` valida a superfície rica; `resolve` preserva compatibilidade com adaptadores existentes. Não há fallback automático de agente quando capabilities, política ou resultado são desconhecidos.

### Codex CLI

`CodexAdapter` agora implementa o contrato lifecycle mantendo `prepare`/`interpret` e seus argumentos compatíveis. `discover` consulta `codex --version`; apenas a versão na lista de homologação é marcada `available`. Uma versão executável não homologada permanece `unknown`. `prepare_task`/`collect_result` delegam aos métodos existentes, preservando os preflights de versão, opções, autenticação, workspace Git, sandbox e política de aprovação.

O processo continua rodando como o usuário local. As capabilities declaram JSONL e inicialização de tarefa; retomada de sessão, tracking de sessão e cancelamento dentro do agente são não suportados. Monitoramento e cancelamento de processo pertencem ao `LocalRuntime`; reconciliação conservadora pertence ao `ExecutionStore` e marca resultado ambíguo `interrupted/unknown`, sem reiniciar a sessão Codex. Evidências persistidas também pertencem ao store/validadores.

Versões experimentais ou flags de confiança não são controles de segurança. Nenhuma configuração global Codex ou sandbox foi alterada. Esta etapa não executou jobs ou providers.

## Instalação e configuração

Os contratos atuais reutilizados são:

| Necessidade | Interface existente / regra |
|---|---|
| Instalação global | `pyproject.toml` define pacote Python e entry point `agentflow = agentflow.cli:main`; instalação é feita pelo gerenciador Python escolhido pelo usuário. Não há bootstrap privilegiado próprio. |
| Inicialização do projeto | `agentflow init` chama `initialize(path)`, encontra a raiz Git, cria `.agentflow/config.toml` somente quando ausente e recusa conflito; repetição não sobrescreve. |
| Configuração por workspace | `Config` schema v1 fica em `.agentflow/config.toml`; não contém credenciais. `agent` aceita hoje `none` ou `codex`, e `runtime` `local` ou `docker`. |
| Seleção do adaptador | Registry é explícito; `ModelSelectionRequest`/política de roteamento carregam seleção manual, sem descoberta/import de plugins. Codex continua adaptador inicial conectado à rota CLI de execução atual. |
| Descoberta do executável | Adaptadores resolvem caminho explícito ou `PATH`; preflight local valida versão/capabilities. Não baixa binários ou modelos. |
| Diagnóstico de dependências | `agentflow doctor` usa `environment.doctor.diagnose`, somente leitura; Docker é opcional em runtime local. |
| Atualização/remoção | Não há auto-updater nem desinstalador. O contrato do produto é delegar à ferramenta que instalou o pacote (por exemplo, pip/pipx/uv), preservar `.agentflow/` e nunca remover dados do projeto como efeito implícito. Atualização deve manter CLI/API compatíveis ou versionar migrations; remoção não apaga configuração nem estado operacional sem ação separada e explícita. |

O suporte atual para seleção por configuração ainda é `none`/`codex`; suportar IDs adicionais nessa configuração exige alteração versionada posterior, sem alterar instaladores nesta etapa. Os adaptadores de outros agentes existentes não foram implementados nem modificados pelo MVP-18.

## Modelo de autorização local

Qualquer autonomia futura exige consentimento explícito, autenticado e verificável sobre uma proposta específica: objetivo, digest/revisão do plano, workspace, operações permitidas, limites de jobs/iterações/tempo, validade, identidade do operador e regra de orçamento. Deve existir revogação, condição de parada, evidência e auditoria. Replanejamento que altere materialmente digest, workspace, operações ou limites exige novo consentimento. O planner só propõe.

O orçamento estimado não é teto financeiro do provider sem enforcement independente. Falha, revogação, cancelamento incerto ou estado `unknown` bloqueia e não autoriza repetição de efeitos. Os contratos MVP-17 são preparatórios: UI/autenticador real, identidade de SO/custódia de chave e homologação local seguem pendentes. Dispatch continua bloqueado.

## Regressão TLS do teste local

O teste `test_preparation_pin_check_only_reports_tls_peer_identity` era um teste de handshake/pin, mas não completava o encerramento TLS simétrico. No Windows isso podia produzir RST/`ConnectionAbortedError(10053)` ou `ConnectionResetError(10054)` quando o contexto TLS era fechado, e o helper classificava esse fechamento como erro do servidor.

O cenário sintético `response="drop"` agora conclui `close_notify` nos dois peers com `unwrap()`. O teste verifica que nenhuma mensagem de operação foi enviada após a pin mismatch. A correção elimina o fechamento abrupto e testa o comportamento desejado sem retry nem skip. A mudança é apenas no teste TLS local; não altera Broker nem transporte do produto.

## Testes e limites desta etapa

Testes devem validar discovery conhecida/desconhecida, capabilities distintas entre sessão e processo, aliases compatíveis do Codex, estados ambíguos e registry lifecycle. Testes de TLS usam certificados e loopback sintéticos; nenhum endpoint externo é acessado. A suíte não comprova isolamento de Codex, identidade humana, grant/dispatch, autenticação independente nem funcionamento de agentes que não sejam Codex.

Próximos marcos: escolher versão Codex homologada para o alvo Windows/Linux; exercitar o protocolo dessa versão sem mudar as configurações globais; conectar os outros adapters existentes à superfície lifecycle conforme suas capabilities reais; definir instalação testada para Windows/Linux; e homologar UI/autenticador local antes de qualquer proposta de dispatch autorizado.
