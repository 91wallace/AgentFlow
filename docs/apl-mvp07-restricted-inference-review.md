# AgentFlow APL MVP-07 — Restricted Inference Backend Review

**Data da inspeção:** 2026-11-08
**Parecer:** **BLOCKED** para implementação de backend real. Nenhuma alternativa foi comprovada elegível no ambiente inspecionado. O contrato `PlannerAdapter` e o decodificador estrito existentes permanecem utilizáveis para adaptadores simulados; não foi adicionado, instalado, iniciado ou autenticado backend algum.

## 1. Baseline e inventário local

O APL está em `src/agentflow/apl/`. `PlannerAdapter.propose(ReplanContext) -> PlanProposal` é a interface de integração. `ReplanContext` carrega objetivo, escopo autorizado, jobs e revisão atual, resultados/evidências verificadas, bloqueios e limites. `proposal_from_mapping` aceita apenas os campos tipados previstos; as propostas ainda precisam passar por `validate_proposal` e pelo fluxo de aceite supervisionado de `SupervisedCycleService`. O CLI não oferece seleção `semantic`; a seleção disponível é `deterministic`. `UnavailableSemanticPlanner` falha fechado.

O decodificador estrito e o validador são controles do contrato e da proposta. Eles **não** confinam o processo que produziu a resposta: texto dentro de campos permitidos continua sendo conteúdo não confiável, e a validação da proposta não prova que o planejador não executou ações.

Verificações estáticas locais, sem iniciar processos de inferência:

- `Get-Command` não encontrou `ollama`, `llama-server`, `llama-cli`, `vllm` nem `lmstudio` no `PATH`; o único `codex` encontrado é o CLI já registrado na revisão MVP-06.
- A inspeção de metadados do `.venv` não encontrou pacotes Python com nomes `llama`, `ollama`, `transformers`, `torch`, `vllm` ou `openai`.
- A consulta de serviços Windows não encontrou serviços com nomes/apresentações Ollama, Llama, LM Studio ou vLLM.
- Isso não é uma varredura exaustiva do disco: runtimes portáteis fora do `PATH`, serviços com nomes genéricos ou processos não registrados não foram excluídos.
- `pyproject.toml` não declara biblioteca/runtime de inferência. `config.toml` na raiz não existe. Não foram lidos valores de `.agentflow/` nem criadas credenciais.
- O runtime local existente cria subprocessos com o ambiente allowlist de execução (inclui variáveis de perfil e `CODEX_HOME`), diretório de trabalho informado e controle de vida de árvore. Em Windows, `WindowsJob` usa Job Object para encerramento da árvore ao fechar e `TerminateJobObject`; não configura por si só uma identidade restrita, ACL do filesystem, isolamento de segredos ou bloqueio de rede. Não foi iniciado nem modificado.

## 2. Alternativas e matriz de capacidades

Legenda: **D** = descrito em documentação ou código existente; **I** = controle implementado no AgentFlow; **V** = verificado estaticamente nesta inspeção; **U** = não comprovado para a alternativa no ambiente atual. `—` significa que não há evidência/instalação local examinada. Um controle documentado não é considerado efetivamente imposto.

| Capacidade / controle | A. Inferência local sem ferramentas | B. Serviço de inferência isolado em processo | C. API remota sem ferramentas (documental) |
|---|---|---|---|
| Mecanismo localizado no ambiente | —; runtimes comuns testados por nome ausentes (**V**) | —; nenhum serviço dedicado localizado (**V**) | Alternativa arquitetural apenas; nenhum serviço escolhido ou chamado |
| Ferramentas, comandos e subprocessos | Um modelo/runtime sem tool API reduziria a superfície funcional (**D**, genérico); capacidade do runtime localizado: — | Protocolo sem ferramentas não prova ausência de subprocessos no processo hospedeiro (**U**) | Endpoint de geração sem tools pode omitir chamadas de ferramenta no contrato (**D**, depende do serviço); isso não prova isolamento do cliente ou do processo |
| Filesystem e workspace operacional | Execução na mesma identidade pode ler arquivos acessíveis; sem runtime para inspecionar (**U**) | Processo separado não é sandbox. Runtime AgentFlow passa `cwd` e ambiente configurados (**D/I/V**); nenhum confinamento APL configurado (**U**) | O serviço remoto não tem filesystem local por padrão, mas o cliente/serializador local continua com suas permissões (**U** para isolamento do cliente) |
| Credenciais do executor | Processo local herdaria credenciais acessíveis à sua identidade; separação não configurada (**U**) | Allowlist do executor inclui `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` e `CODEX_HOME` (**D/I/V**); não serve como isolamento do planejador (**U**) | Segredo de API pode ser necessário ao cliente; separação da credencial do executor, armazenamento e injeção não implementados (**U**) |
| Subagentes, plugins e extensões | Não há runtime/configuração carregados para verificar; desligar ferramentas na aplicação seria insuficiente se extensões do host pudessem atuar (**U**) | Sem configuração implantada; nenhum mecanismo de plugins/extensões auditado (**U**) | Capacidade depende do produto/endpoint selecionado; sem seleção e contrato verificados (**U**) |
| Rede e efeitos externos | Política de rede/egresso do processo não verificada; inferência local não implica processo offline (**U**) | Sem regra de firewall/egresso vinculada ao serviço (**U**); Job Object não é controle de rede | Rede para o endpoint é requisito; destino, proxy, egress, retenção e efeitos do serviço não escolhidos (**U**) |
| Permissões / isolamento administrativo | Identidade dedicada, token restrito e ACL não configurados (**U**) | O runtime processual existente não configura AppContainer, token restrito, usuário dedicado ou ACL (**D/I/V** quanto ao que o código faz; ausência de confinamento é **U**) | Credencial da API e política administrativa do serviço não definidos (**U**) |
| Persistência / retenção | Diretório/cache/modelo do runtime inexistente ou desconhecido; retenção não verificada (**U**) | Sem serviço ou configuração APL para logs, cache e retenção (**U**); APL persiste contexto/propostas em state store já existente quando usado | Retenção, treinamento, telemetria e localização dependem do contrato do serviço (**U**) |
| Limites e cancelamento | Limites de CPU/memória/tokens e cancelamento cooperativo do runtime: **U** | Runtime local tem timeout/encerramento de subprocesso na execução geral (**D/I**); Job Object encerra a árvore no Windows (**D/I**); recursos e identidade isolada não estão comprovados para inferência (**U**) | Timeout/cancelamento do pedido não necessariamente cancela processamento remoto; cotas/custo e cancelamento do serviço não verificados (**U**) |
| Tratamento de falhas e repetição | Nenhum adaptador implementado. Política APL conserva falhas/unknown; replay de inferência ainda precisa de chave de idempotência (**I** parcial) | Sem contrato de serviço. Subprocesso interrompido teria resposta ambígua; deve ficar em `unknown`, nunca ser reenviado automaticamente (**I** requerido) | Timeout após envio pode ter sido processado e faturado; sem idempotência/consulta de operação documentada, resultado `unknown` e não repetir automaticamente |

**Resultado do inventário:** A não está presente como opção comprovada. B é uma possibilidade de arquitetura, mas processo separado sozinho não satisfaz os requisitos; o código Windows existente contém gestão de árvore de processos, não um boundary administrativo/FS/rede. C é alternativa documental, não elegível nesta etapa: não há serviço escolhido, credenciais ou condições de tratamento de dados avaliadas.

## 3. Fronteiras de confiança

### A. Inferência local sem ferramentas

`APL controller → adapter → runtime/modelo local` seria a fronteira proposta. O objetivo, escopo, plano, resultados e evidências saem do APL e devem ser tratados como dados potencialmente maliciosos, especialmente texto oriundo de jobs. Sem runtime identificado, não se verificou carregamento de plugins, execução nativa, acesso ao perfil do usuário, arquivos, rede ou credenciais. “Sem tool calling” no protocolo do modelo não impediria código do próprio runtime/modelo ou extensões locais.

### B. Serviço em processo separado

`APL principal → IPC local autenticado → worker de inferência restrito → resposta JSON limitada → decodificador → validador APL`. A fronteira só existe se o sistema operacional aplicar identidade/permissões distintas e o worker não compartilhar handles, diretórios, perfil, variáveis secretas ou tokens do executor. Comunicação por pipe/socket local teria ACL de identidade; o serviço precisaria de egress negado, ou permitido apenas a endpoint de inferência explicitamente configurado. O worker não teria mounts/acesso ao workspace e usaria diretório temporário privado com quota e retenção efêmera. Um subprocesso sob o mesmo usuário com `cwd` separado não satisfaz esses requisitos.

### C. API remota sem ferramentas

`APL principal → adaptador cliente → rede controlada → endpoint remoto → resposta não confiável`. O servidor pode não receber ferramentas e ainda assim processar conteúdo sensível; prompt/instruções e evidências podem revelar código, caminhos, resultados e informações internas. O cliente mantém acesso local às credenciais disponíveis à identidade do AgentFlow, a menos que as separemos explicitamente. Controles de rede, endpoint, contrato de retenção, região, logging, treinamento e cancelamento não foram avaliados porque nenhum serviço foi selecionado. Esta alternativa requer revisão e autorização de dados/provedor à parte.

## 4. Compatibilidade proposta com APL

Não implementado nesta etapa. Para as três alternativas, o contrato futuro recomendado é:

1. **Entrada:** snapshot imutável serializado de `ReplanContext`, com versão explícita; incluir somente campos existentes no contrato, evidências por ID e digest, limites restantes e revisão de origem. Resultados/instruções de jobs são dados não confiáveis, nunca instruções do controlador. Excluir caminhos locais desnecessários, credenciais, ambiente, conteúdo integral de arquivos e handles dos stores.
2. **Saída:** JSON UTF-8 limitado, com `schema_version` e os campos aceitos por `proposal_from_mapping`, convertido em `PlanProposal`. Não incluir ferramentas, comandos de shell, caminhos de escrita executáveis, callbacks ou objetos Python arbitrários. `instruction` é texto de proposta e continua não confiável; não o passar diretamente a executor algum.
3. **Validação:** rejeitar JSON inválido, campos desconhecidos/ausentes, versão não suportada, tipos incorretos, saída acima dos limites, ID/revisão de origem desatualizados e qualquer proposta que falhe `validate_proposal`. Aceitação continua humana/supervisionada e sujeita a autorização existente; o planejador não concede permissões.
4. **Tamanho:** estabelecer limites configuráveis e testados para bytes de entrada/saída, quantidade de tarefas, comprimento dos campos e evidências antes de implementar. Os limites numéricos ainda são decisão pendente; não presumir que limites do modelo/API ou o limite geral dos logs façam esse papel.
5. **Timeout/cancelamento:** deadline monotônico para request completo e cancelamento propagado ao cliente; worker local, se escolhido, deve terminar a árvore e confirmar término. Timeout/cancelamento depois de eventual envio/processamento produz estado desconhecido, sem retry automático. APL deve persistir intenção, request digest, revisão, estado e resultado antes/depois das fronteiras críticas.
6. **Falhas e idempotência:** distinguir falha comprovada antes do envio, erro de schema/validação, recusa, timeout, interrupção e resultado ambíguo. Chave idempotente deriva de ciclo, revisão, request ID e digest do contexto; colisões com payload diferente conflitam. Sem confirmação da operação, `unknown` exige inspeção/decisão humana.
7. **Evidências:** o modelo pode citar somente IDs de evidências verificadas presentes no contexto. A referência é checada pelo APL contra store existente; o modelo não autentica, cria nem promove evidência. Armazenar a proposta, versão do schema/adaptador, digest do contexto, referências, resultado de validação e eventos de aceite/rejeição como registros auditáveis no store APL existente.

JSON estrito reduz ambiguidades de contrato; não neutraliza prompt injection semântico nem substitui o isolamento. A saída requer revisão humana antes do aceite, e tarefas aceitas ainda não são execução autorizada.

## 5. Alternativa recomendada

**Não há alternativa recomendada para implementação imediata.** Parecer global permanece **BLOCKED**. Como direção de investigação (não como aprovação), B — worker separado sob identidade administrativa restrita — é a opção mais adequada para separar inferência e aplicação. Poderia consumir inferência local (A) sem rede ou um endpoint remoto explicitamente aprovado; a escolha do modelo/provedor é posterior. No Windows, o `WindowsJob` atual oferece encerramento de árvore, mas não prova AppContainer/token restrito, ACL sem workspace, ambiente sem segredos, egress bloqueado ou quota de CPU/memória. Logo, a existência desse código não torna B elegível.

Para reabrir a decisão, seria necessário selecionar e comprovar, nesta máquina alvo ou ambiente de homologação equivalente: (a) identidade distinta com permissões mínimas; (b) ausência de acesso ao workspace, diretório `.agentflow`, perfis e segredos do executor; (c) política de rede aplicada pelo SO/host; (d) lista de extensões/plugins/tools vazia ou desabilitada por mecanismo efetivo; (e) limites de CPU, memória, processos, I/O, tokens/bytes e deadline aplicados; (f) término verificável em timeout/cancelamento; (g) persistência efêmera/retida claramente definida; (h) isolamento administrativo resistente à configuração do prompt/modelo; (i) teste adversarial confirmando que tentativas de ferramenta, filesystem, subprocesso, rede e acesso a segredos falham por negação do sistema operacional.

## 6. Testes necessários antes de implementação/ativação

- Teste de contrato de `PlannerAdapter` e serialização versionada de `ReplanContext`/`PlanProposal`; campos extras, duplicados, limites de bytes e schema futuro devem falhar fechado.
- Testes APL existentes de escopo, dependências/ciclos, evidência falsa/inexistente, alterações retroativas, bloqueios, aceite/rejeição e CAS/concorrência.
- Testes de isolamento de identidade tentando ler workspace, `.agentflow`, perfil, variáveis e arquivos sentinela de credenciais do executor; os acessos precisam ser negados pelo OS, não por prompt.
- Tentativas negativas de subprocesso, shell, ferramenta, plugin/extensão, subagente, rede DNS/HTTP/loopback e escrita fora do diretório efêmero; demonstrar negação por política efetiva.
- Testes de quotas CPU/memória/PIDs/I/O/entrada/saída e timeout; confirmar cancelamento de processo/árvore e ausência de processo órfão.
- Falhas injetadas antes/depois do dispatch, resposta parcial, timeout após processamento, crash e reinício; manter `unknown` sem nova chamada automática.
- Testes de concorrência e idempotência no store, repetição da mesma chave e colisão da mesma chave com digest diferente.
- Testes adversariais de prompt injection em resultados/evidências e payload malformado; verificar que nenhuma instrução passa a ser executada e que a validação semântica estrita bloqueia fora de escopo.
- Para serviço remoto: revisar endpoint, contrato de retenção/uso de dados, autenticação, rotação, vazamento em logs, egress allowlist e semântica de timeout/cancelamento sem usar credenciais operacionais em testes.

## 7. Riscos e pendências

- O inventário pelo `PATH`, metadados do `.venv` e serviços nomeados não garante ausência de binários portáteis ou serviços com nomes desconhecidos.
- Não há evidência local de um inferidor sem ferramentas, worker restrito ou endpoint remoto avaliável; capabilities de qualquer candidato mudam por versão/configuração.
- O APL existente permite testar decodificação/validação, mas ainda não define limites dedicados de payload para um transporte de inferência, identidade de adaptador/backend, protocolo de cancelamento ou operação de inferência persistente recuperável.
- O modo de processo do executor herda variáveis de perfil e trabalha com o `cwd` autorizado; não pode ser reutilizado como isolamento do planejador.
- Um modelo pode produzir texto malicioso ou enganoso em campos válidos. Schema, escopo, evidência, revisão humana e não execução automática permanecem obrigatórios.
- API remota adicionaria transferência de dados e credencial a serviço externo; decisão de privacidade, rede e provider não está no escopo de implementação aprovado nesta etapa.

## 8. Estado e checkpoint

**MVP-07 — inventário estático e decisão de elegibilidade concluídos; implementação de backend não iniciada.** Foram inspecionados o APL, os contratos/adaptadores, CLI, documentação de MVP-06, `pyproject.toml`, política local de execução e código de runtime/processo pertinente. Nenhum serviço foi instalado, iniciado ou autenticado; nenhum provider foi chamado; não se alteraram sandbox, Codex, Broker/TEG ou contratos RB-09C.

Estado Git inicial já continha alterações em documentação e `src/agentflow/cli.py`, além dos diretórios/arquivos não rastreados `src/agentflow/apl/`, `tests/test_apl.py`, documentação APL/RB-09C e `autonomous-development-runner/`. Foram preservados sem modificação nesta etapa. O único arquivo criado por MVP-07 é este relatório.

## 9. Parecer final

**BLOCKED.** Nenhuma alternativa satisfaz com evidência local os requisitos mínimos de não execução, ausência de acesso ao workspace/credenciais, isolamento de processo, controles de rede e limites/cancelamento. A opção B é somente uma direção futura condicionada a uma fronteira de segurança aplicada e verificada pelo sistema operacional. A e C permanecem inventariadas, sem candidato elegível. O planejador semântico continua indisponível; somente o adaptador determinístico existente pode ser selecionado.

**Próxima implementação autorizável:** não iniciar backend. A próxima etapa pode especificar/testar um harness de worker restrito depois que a equipe decidir o mecanismo administrativo de isolamento e disponibilizar evidência verificável de identidade, filesystem, segredos, rede, recursos, plugins e cancelamento. A seleção de serviço remoto/provedor e qualquer credencial requerem autorização e avaliação de dados independentes.
