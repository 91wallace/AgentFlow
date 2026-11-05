# ADR 0006: Ecossistema de adaptadores e integração opcional

Status: **decidido para a Fase 6; F6-11 localmente implementada, sem execução real de agentes; CI remota pendente**.

## Contexto

O roadmap (`docs/implementation-plan.md`) define a Fase 6 como “Ecossistema”: adaptadores de outros agentes e possível integração opcional ao Vibe Coding Toolkit após revisão legal/técnica. ADR 0001 estabelece a arquitetura híbrida e permite adapters futuros através do contrato comum; ADR 0003 fixa execução explícita, sem retry automático, sandbox/aprovação verificáveis e recuperação conservadora. As Fases 2, 4 e 5 já relacionam execuções a revisões, workspaces/checkpoints, validação, evidências e conclusão separadas.

A implementação existente fornece `AgentAdapter.prepare/interpret`, `ExecutionRequest`, `ProcessSpec`, `ProcessRuntime`, identidade de processo e colunas de adapter/version/capabilities na persistência. Isso é um ponto de partida, mas não prova que versões, capabilities ou configurações heterogêneas de outros providers estejam representadas com precisão.

## Escopo proposto

- Preservar Codex como adapter funcional e compatível, sem ampliar implicitamente a versão Codex homologada.
- Preparar integrações na prioridade: **Antigravity**, **Claude Code**, **OpenCode**, **Hermes Agent**. A ordem é organizacional e não autoriza fallback/roteamento; verificar versões/plataformas, modelo/provider efetivamente identificável, capabilities, autenticação, custo, termos e segurança.
- Manter seleção e execução por pedido explícito. Não iniciar agente no load/inspection, não executar automaticamente, não tentar provider alternativo após falha e não repetir efeito externo desconhecido.
- Na Fase 6, permitir seleção manual de modelo apenas quando ferramenta/provider puder identificá-lo e confirmar a seleção efetiva. A Fase 6 não escolhe nem troca modelo/provider automaticamente.
- Tratar adapters como fronteira do domínio `agents`; o núcleo e `ProcessRuntime` continuam independentes de fornecedores.
- Avaliar o Toolkit como potencial fonte de instruções/prompts/templates; não pressupor que seja runtime, SDK ou `AgentAdapter`.
- Não modificar Docker, quality gates, builds, delivery ou conclusão. Não introduzir automação de agentes, paralelismo ou nova política de retries.

## Contratos separados obrigatórios

1. **`AgentAdapter` — integração com a ferramenta:** interface que prepara uma solicitação para Antigravity, Claude Code, OpenCode, Hermes Agent ou Codex; verifica capabilities da ferramenta, delega ao `ProcessRuntime` existente e interpreta o resultado. Não escolhe/escalona modelo automaticamente, não repete tentativa e não conclui tarefa.
2. **`ProviderDescriptor` — provider:** identidade/version/API do provider de modelo, capabilities/limites, requisitos de autenticação por referência e disponibilidade observável. Um agent host pode ocultar seu provider; registrar `unknown`, nunca deduzir pelo nome do produto.
3. **`ModelDescriptor` — modelo:** ID e versão/revisão, `provider_id`, capabilities, limites (contexto/modalidades/tools), disponibilidade com instante/fonte e metadados de custo com unidade/moeda/fonte, quando verificáveis. Alias mutável, disponibilidade stale e preço desconhecido não são tratados como fatos atuais.
4. **`ModelSelection` — seleção da tentativa:** valor imutável por `attempt_id` com agent/provider/model IDs e versions, digests dos descriptors, escolha manual/default explícito, motivo/autor/tempo e identidade efetiva reportada. Em F6 é manual; divergência entre selecionado e efetivo bloqueia/fica inconclusiva, sem fallback.
5. **`ModelRoutingPolicy` — política futura:** contrato/schema versionado para classificação de complexidade, constraints, capacidade/custo/latência/risco, budgets, escalation triggers, exclusões de erro de ambiente e autorização. F6 pode validar e persistir seu digest, mas não avalia nem executa a policy.

Os cinco tipos têm responsabilidades diferentes e snapshots separados. `AgentAdapter` não é `ProviderDescriptor`; provider não é modelo; seleção não é política. Catálogo/discovery não dispara execução. Informações não confirmadas são explicitamente desconhecidas.

Por compatibilidade com ADR 0002, policy/allowlist/configuração versionada por projeto, se aprovada, fica dentro de `.agentflow/spec.toml`; não será criado outro TOML versionado sem revisão daquele ADR. `ModelSelection`, observações temporais de disponibilidade e resultados de tentativa ficam no SQLite operacional. Um catálogo distribuído com AgentFlow é identificado pela versão/digest do pacote; overrides por projeto são snapshots/digests do `spec.toml`.

## Fase futura de orquestração

A Fase 7 (futura, fora deste ADR de implementação) poderá usar esses contratos para classificar complexidade, versionar catálogo de modelos, selecionar por qualidade/capacidade/custo/latência/risco, usar modelos econômicos em tarefas simples, intermediários em implementação convencional e avançados em planejamento/arquitetura/revisão crítica/problemas complexos, aplicar budgets rígidos, validar independentemente e escalonar somente quando critérios verificáveis falharem por insuficiência do modelo. Erros de ambiente, autenticação, quota ou infraestrutura não autorizam escalation. Toda mudança de modelo/provider exige autorização explícita; nova tentativa respeita limite de tentativas e só ocorre após estado terminal/reconciliação, sem replay de efeitos desconhecidos. Auditoria/métricas, comparação de qualidade e testes de regressão são obrigatórios. F7 exige planejamento/aprovação próprios.

## Decisões arquiteturais consolidadas

1. **Identidade versionada:** distinguir `agent_id`, `adapter_id`, `adapter_api_version`, `adapter_version`, `provider_id`, `provider_version`, `model_id`, model revision, identidade do executable, capabilities e digest do profile/config. Cada tentativa persiste snapshot imutável desses valores e sua `ModelSelection`.
2. **Registry determinístico:** iniciar com adapters built-in aprovados e resolver por ID explícito. Não importar Python entry points/plugins dinamicamente. Um mecanismo de plugins externos exige decisão de confiança/isolamento separada.
3. **Seleção compatível:** nova ferramenta e modelo são escolhidos explicitamente por operação. Projeto legado sem seleção permanece no caminho Codex existente. Falha do adapter selecionado encerra com erro; nunca há fallback automático na Fase 6.
4. **Configuração:** manter TOML versionado somente nos arquivos permitidos por ADR 0002. Se houver necessidade de declarar agent IDs/capabilities permitidas no projeto, evoluir `spec.toml` de forma versionada e compatível; `tasks.toml` só muda mediante requisito por tarefa. Localizações de binários, perfis locais e referências de credenciais ficam na configuração operacional ignorada. Valores de segredo nunca são versionados, persistidos ou logados.
5. **Capabilities e políticas:** negotiated capabilities distinguem `supported` de `effective/enforced` por tentativa. Sandbox, aprovação não interativa, rede, cancelamento/encerramento e structured output só são aceitos se o provider os puder cumprir e provar conforme a semântica requerida. Política não suportada bloqueia antes do spawn; não se relaxa nem se substitui pelo comportamento padrão do provider.
6. **Persistência:** a tentativa registra adapter/provider/API version, profile/request digests, capabilities efetivas e valores de política sem segredos. SQLite v4 já persiste parte disso. Primeiro mapear colunas/digests existentes; criar migração v5 somente se informação necessária não puder ser derivada sem ambiguidade. Se necessária, usar backup consistente, transação única, integridade/FKs, rollback para v4 e testes de reabertura; nunca reinterpretar tentativas antigas.
7. **Routing futuro:** `ModelRoutingPolicy` é declarativa e não executável na Fase 6. Não existe classificação/escalonamento automático, retry ou troca de provider nesta fase. Seleção manual pode ser persistida por tentativa se o modelo efetivo for confirmável.
8. **Toolkit opcional:** só após avaliação de uma revisão upstream fixa, arquivos específicos, licença/proveniência, dependências, manutenção e compatibilidade. Se integração for aprovada, tratar como `TemplateSource`/`InstructionCatalog` separado do `AgentAdapter`, por conteúdo fixado em digest e exposição explícita; sem instalação, fetch, execução de prompt ou alteração automática. Resultado negativo/deferido é aceitável e não requer código.
9. **Testabilidade:** fakes e executáveis de teste cobrem todos os adapters no CI; nenhum agente real é iniciado por testes. Smoke com provider real, se algum dia necessário, requer autorização específica e constitui homologação separada.

As decisões acima são normativas para F6. F6-01 confirmou que existe uma interface oficial não interativa adequada para uma integração Antigravity: o CLI `agy` em headless/print mode, com `-p`, entrada por `stdin`, saída `text`/`json`/`stream-json`, versão consultável e escolha explícita de modelo. Isso não certifica que todas as capacidades de segurança sejam aplicadas em toda instalação. O adapter deve falhar fechado sempre que uma capacidade solicitada não puder ser verificada.

## F6-01 — auditoria técnica do Antigravity (2026-11-05)

### Evidência oficial consultada

Consulta documental em `antigravity.google/docs/` em 2026-11-05; nenhum executável foi iniciado e nenhum agente real foi chamado.

- A documentação identifica Antigravity CLI `agy` como superfície de terminal, atualmente documentada a partir de v1.2+. Instalação documentada para macOS/Linux/Googlebook e Windows; os requisitos publicados incluem macOS 12+, Windows 10/11 x64/ARM64 e Linux x64/ARM64 com glibc >= 2.28 e glibcxx >= 3.4.25. CLI é distinto do desktop app e da SDK Python.
- `agy --version` é o comando oficial documentado para verificar a versão. A documentação lista flags headless `-p`, `--input-format`, `--output-format`, `--print-timeout`, `--model`, `--effort`, `--sandbox` e `--dangerously-skip-permissions`. A disponibilidade de `--mode auto` varia por plataforma/conta/versão (macOS/Linux v1.3.5+, Windows v1.4.4+) e requer terminal sandbox; portanto não é assumida como capacidade universal.
- Headless mode é explicitamente destinado a automação/CI. Prompt pode ser enviado em JSONL por stdin; stdout fornece resposta ou eventos NDJSON; stderr contém diagnósticos, erros e avisos de permissões. JSON/stream-json fornece `status`, `error`, `conversation_id`, duração e uso; stream inicia com `init` e termina com `result`. A documentação publica estados `SUCCESS`, `ERROR`, `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING` e `RUNNING`, e códigos de saída para falhas. Entrada malformada/unsupported falha; não se deve interpretar apenas exit code zero como conclusão da tarefa.
- A escolha manual é suportada via `--model <slug>`; `agy models` documenta inventário de slugs. Modelo inválido falha sem fallback silencioso. Em stream JSON, o evento `init` expõe `model` quando foi explicitamente definido, além do `permission_mode` efetivo e do conjunto de ferramentas. Assim, F6 só pode registrar seleção confirmada quando a execução reportar o modelo solicitado; modelo implícito/ausente fica `unknown` e torna a tentativa inconclusiva para seleção explícita.
- Autenticação documentada: credenciais OAuth em cache para contas pessoais/empresariais, Gemini API key e Google Cloud ADC/Vertex AI em superfícies aplicáveis. Headless sem credenciais prévias termina com erro de autenticação, em vez de esperar interação. AgentFlow não coleta nem persiste segredos; só passa referências/ambiente permitido pelo usuário.
- Permissões são configuráveis por modo e regras `allow`/`deny`/`ask`; em headless, pedidos de aprovação indisponíveis são soft-denied, podem deixar o processo com exit 0 e emitem aviso em stderr. `--dangerously-skip-permissions` aprova tudo e nunca será inserido pelo adapter sem seleção explícita do usuário. O stream expõe `permission_mode` (`request-review` por padrão; `always-proceed` com skip-permissions). As escolhas e config efetivas não podem ser silenciosamente substituídas.
- Sandbox é configurável via settings e flag `--sandbox`; documentação adverte que a flag pode não ter efeito quando indisponível. A saída estruturada consultada não atesta de forma independente o enforcement real do sandbox ou da rede em cada host. Logo, sandbox/restrição de rede solicitados só são marcados `effective` após evidência verificável; caso contrário ficam `unknown` e qualquer perfil que exija enforcement é bloqueado antes do spawn. Perfil explicitamente sem sandbox pode prosseguir apenas se a política de execução do projeto permitir, registrando `unavailable/unverified`, nunca `enforced`.
- Cancelamento headless é documentado por interrupção (por exemplo SIGINT), que resulta em status `INTERRUPTED`; CLI também documenta `Ctrl+C`. Encerramento de árvore de processos fica sob o `ProcessRuntime` AgentFlow e deve usar mecanismos de plataforma existentes; documentação não garante comportamento de árvore órfã nem recovery do processo pai.
- SDK Python `google-antigravity` (Python 3.10+) é oficialmente documentada para construir agentes próprios com `Agent`/`LocalAgentConfig`, políticas programáticas e lifecycle assíncrono. Não foi escolhida como adapter primário: a evidência consultada não estabelece contrato mínimo estável para seleção/identidade do modelo de catálogo do CLI, identidade do processo/subprocesso e integração com `ProcessRuntime`/recovery existentes. O CLI é a fronteira preferida e substituível.

### Decisões fechadas por F6-01

1. Antigravity será integrado somente via CLI oficial `agy` headless na primeira implementação; SDK, desktop app e IDE não são superfícies de execução do AgentFlow nesta fase.
2. Versão mínima documental: CLI >= 1.2 para considerar a superfície; antes de qualquer tentativa, o adapter deve inspecionar executável/versão e capabilities sem iniciar agente, rejeitar versões/flags desconhecidas e exigir capability checks específicos. A documentação dinâmica não é promessa de compatibilidade futura; homologação fixa a versão testada.
3. Seleção é explícita e manual por tentativa. Provider/model desconhecidos permanecem `unknown`; a tentativa não pode afirmar seleção efetiva sem o `init.model` correspondente. Não há default de modelo inventado, fallback, retry ou troca automática.
4. Usar stream-json via pipes para prompt/output estruturados; `ProcessRuntime` mantém limites, identidade, logs, cancelamento e recuperação. Tratamento de erro considera status, envelope, stderr e exit code em conjunto. Sucesso do processo não conclui tarefa nem substitui validação/checkpoint/evidências.
5. Não inserir flags de sandbox ou aprovação que mudem a preferência do usuário. Configuração solicitada deve ser lida/registrada sem segredo e a observação de execução correlacionada ao snapshot. Sandbox/rede não observáveis são `unknown`; perfis que exigem esses controles bloqueiam, sem inferir enforcement pelo flag.
6. `--dangerously-skip-permissions` só pode ser utilizado se pedido explicitamente pelo usuário para aquela operação e persistido no snapshot de seleção/política; nunca é default.
7. A documentação declara macOS/Linux/Windows, mas a matriz AgentFlow fica limitada aos sistemas/arquiteturas realmente testados na homologação; não há inferência de compatibilidade por instalação possível.
8. F6-01 não executou `agy`, não chamou SDK/provider e não autenticou. Smoke com agente real não é gate local/CI e exigirá autorização específica em homologação separada.

### Evidência e decisões de Claude Code (F6-07)

Consulta documental oficial em `code.claude.com/docs` em 2026-11-05; nenhum binário, conta ou agente foi iniciado. A CLI documenta `claude -p`, entrada por stdin, saída JSON/stream-json, seleção `--model`, `--version`, `auth status --json`, evento `system/init` com modelo e evento terminal `result`. A versão mínima adotada pelo adapter é 2.1.259, para utilizar `--permission-prompts none` junto ao contrato headless assumido; o preflight verifica a versão e o método de autenticação antes de preparar a tentativa. A identidade efetiva só é confirmada pelo modelo presente em `system/init`; ausência/divergência permanece unknown/mismatch.

`--permission-prompts none` nega prompts de permissão em execução não interativa; não concede permissões. O adapter não usa `--dangerously-skip-permissions`, não altera settings e recusa perfis sandbox genéricos: a sandbox oficial cobre comandos shell em macOS/Linux/WSL2, não todos os built-in tools, hooks ou MCP, e não é suporte nativo no Windows. A configuração efetiva de sandbox permanece `unknown`. Eventos `system/api_retry` são tentativas internas do provider Claude; são apenas contados no resultado e nunca disparam nova tentativa AgentFlow. Credenciais de providers terceiros são recusadas para evitar atribuição falsa ao provider Anthropic.

Fontes: [CLI reference](https://code.claude.com/docs/en/cli-reference), [headless mode](https://code.claude.com/docs/en/headless), [permissions](https://code.claude.com/docs/en/permissions), [sandboxing](https://code.claude.com/docs/en/sandboxing), [model configuration](https://code.claude.com/docs/en/model-config), [settings](https://code.claude.com/docs/en/settings), [overview](https://code.claude.com/docs/en/overview).

### Evidência e decisões de OpenCode (F6-08)

Consulta documental oficial em `opencode.ai/docs` e `dev.opencode.ai/docs` em 2026-11-05; nenhum binário ou agente foi iniciado. `opencode run` é headless, aceita `--model provider/model` e `--format json`; `opencode --version`, `opencode models <provider>`, `opencode auth list --format json` e a flag global `--pure` são documentados. O adapter usa `--pure` para não carregar plugins externos durante preflight e execução, seleciona o provider/model explicitamente e não usa `--auto`. Inventário local sem refresh pode verificar que o ID consta entre os modelos configurados.

A documentação lista eventos de sessão/mensagem e JSONL, mas não garante um campo único de identidade efetiva do modelo em todo evento/versão. Por isso o adapter só confirma identidade caso o stream contenha campos estruturados explícitos `providerID` e `modelID`; caso contrário retorna seleção unknown e o resultado não pode fundamentar conclusão. Campos divergentes tornam a seleção mismatch. Nenhum texto da resposta é usado para inferir modelo. A sandbox e a restrição de rede não são comprovadas pelo CLI; políticas que as exijam bloqueiam a preparação. A aprovação existente é respeitada; AgentFlow não injeta `--auto`, e uma execução não interativa que peça interação fica sujeita ao timeout/cancelamento do runtime, sem retry.

Fontes: [CLI](https://opencode.ai/docs/cli/), [commands](https://dev.opencode.ai/v2/docs/cli/commands/), [permissions](https://opencode.ai/docs/en/permissions/), [models](https://dev.opencode.ai/docs/models/), [providers](https://dev.opencode.ai/docs/providers/), [plugins](https://dev.opencode.ai/docs/plugins/).

### Evidência e decisões de Hermes Agent (F6-11)

Consulta documental à documentação oficial Nous Research em 2026-11-05; nenhum executável ou agente foi iniciado. `hermes --version` identifica a instalação. `hermes chat --oneshot --query-file PATH --provider PROVIDER --model MODEL --format stream-json` é a interface documentada; `--query-file -` lê o prompt de stdin, sem gravar arquivo temporário. `--format stream-json` emite JSONL em stdout, diagnósticos em stderr, inicia com `system/init` (inclui `model`) e termina em `result` (inclui `exit_code`, tokens e eventual `error`). A documentação afirma que o exit code do envelope coincide com o do processo; interrupção Ctrl-C usa 130.

O `system/init` documentado não garante campo de provider. O adapter compara `model` exatamente com o valor pedido; quando provider não é reportado, a observação é `(unknown, model, unknown)` e nunca é promovida a confirmação completa. Provider/model divergente, JSONL malformado/incompleto, evento desconhecido, ferramenta não permitida, código de saída divergente ou ausência de resultado terminal não pode virar sucesso. O parser consome cada linha; texto da resposta não é copiado para eventos de domínio.

A CLI Hermes padrão habilita muitos toolsets, incluindo terminal, memory, skills e delegation. Cada chamada solicita explicitamente somente `--toolsets file`, mais `--safe-mode` e `--ignore-rules`; não passa `--skills`, `--yolo` ou delegation. O conjunto oficial `file` contém `patch`, `read_file`, `search_files` e `write_file`. O preflight exige que help exponha as flags e protocolo atuais. Configuração Hermes não é editada e nenhuma credencial é copiada para o ambiente do processo. O runtime AgentFlow conserva timeout, cancelamento da árvore, identidade e redaction. Flags não atestam sandbox de filesystem, rede ou provider efetivo; capacidades ficam unknown e políticas que requeiram enforcement não comprovado são bloqueadas. Nenhum código/dependência Hermes é importado.

Fontes: [CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), [CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli), [toolsets](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference), [built-in tools](https://hermes-agent.nousresearch.com/docs/reference/tools-reference), [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration), [memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory).

### Decisões adiadas para tarefas dependentes

- A documentação oficial comprova superfícies headless para as quatro integrações; versões concretas instaladas, autenticação real, comportamento por plataforma e homologação de execução permanecem pendentes e não são inferidos a partir da documentação.
- Persistência de snapshots/capabilities foi mapeada contra SQLite v4 em F6-04; migração v5 fica isolada ao perfil AgentExecutionStore e preserva os perfis legados.
- F6 usa somente adapters built-in explicitamente registrados; plugins dinâmicos ficam excluídos. Toolkit não será integrado/copied na Fase 6; é apenas referência opcional sujeita a revisão futura.
- `ModelRoutingPolicy` terá schema versionado e digest, mas será não executável em F6; sem classificador, seleção automática, escalation, fallback, retry ou troca de modelo/provider.

## Estado das decisões

Decisões normativas consolidadas acima. A documentação oficial das quatro superfícies foi verificada. Permanecem pendentes homologação em binários/contas reais e testes da matriz por plataforma; nenhuma pendência autoriza fallback ou roteamento. A matriz de suporte do AgentFlow só será publicada após homologação por plataforma.

## Compatibilidade e consequências

O contrato Codex e suas proteções permanecem; adapters compartilham execução e estado, não presumem equivalência de segurança nem de output. A persistência existente já registra nomes/versões/capabilities, mas a separação entre versão do CLI e versão da implementação adapter precisa ser avaliada. TOMLs antigos devem continuar legíveis; migração de configuração é explícita e sem rewrite in-place. Reabertura com provider removido/atualizado preserva resultado e mostra a indisponibilidade histórica, sem reexecutar.

`ModelSelection` por tentativa é auditável e fica separada da futura `ModelRoutingPolicy`. Um adapter que não confirma modelo efetivo não pode afirmar seleção confirmada. A Fase 6 fornece extensibilidade e opções manuais; nenhuma regra automática pode escalonar ou consumir budgets.

Riscos residuais incluem CLI/API instável, autenticação/credenciais/custos, licença e termos de uso, execução de código por plugins, cancelamento diferente em Windows/Linux, capabilities anunciadas mas não aplicadas, migração de estado, compatibilidade dos formatos de saída e obsolescência de prompt packs. Implementar um adapter não certifica o provider nem estabelece equivalência com Codex.

## Referências consultadas

- [ADR 0001 — arquitetura híbrida](0001-hybrid-architecture.md)
- [ADR 0002 — contratos e estado](0002-phase-2-state.md)
- [ADR 0003 — política de execução](0003-local-execution-policy.md)
- [ADR 0004 — recuperação e Git](0004-phase-4-recovery-git.md)
- [ADR 0005 — validação e entrega](0005-phase-5-validation-delivery.md)
- [Google Antigravity CLI — Getting Started](https://antigravity.google/docs/getting-started?tab=cli)
- [Google Antigravity CLI — Headless mode](https://antigravity.google/docs/cli/headless/)
- [Google Antigravity CLI — Commands and flags](https://antigravity.google/docs/cli/reference/)
- [Google Antigravity — Choose a model](https://antigravity.google/docs/models/)
- [Google Antigravity — Permissions](https://antigravity.google/docs/permissions/)
- [Google Antigravity — Settings](https://antigravity.google/docs/settings/)
- [Google Antigravity SDK overview](https://antigravity.google/docs/sdk/overview/)
- [Vibe Coding Toolkit](https://github.com/soumatheusgomes/vibe-coding-toolkit) — referência externa atual, não aprovada para reutilização nem dependência.
