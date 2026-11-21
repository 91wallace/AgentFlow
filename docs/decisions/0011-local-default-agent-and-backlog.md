# ADR 0011 — Entrada local sem autenticação e backlog JSON

Status: aprovado pelo proprietário para implementação em 2026-11-20.

## Decisão

`agentflow init` cria configuração v1 com `runtime = "local"` e `agent = "codex"`.
Agente omitido ou `none` legado resolve para Codex sem reescrever arquivos existentes.
`init --agent codex` torna a escolha explícita; Codex é o único agente suportado
por este caminho operacional. Não há instalação automática, troca silenciosa de
provider/modelo ou alteração da configuração nativa do executor. `init` não inicia jobs.

O fluxo local não exige perfil, senha AgentFlow, WebAuthn ou consentimento
autenticado. `profile status/select` são aliases de compatibilidade somente leitura:
retornam `configuration_status = "not_required"`, sem habilitar dispatch global.
`local-auth`, `webauthn-enroll`, `apl authorize-plan` e `apl handoff-review` deixam
de cadastrar credenciais/emitir consentimento pela CLI e indicam `agentflow run`.
Argumentos antigos continuam reconhecidos para diagnóstico de migração.
Persistência e contratos históricos são preservados: esta mudança não apaga
credenciais, grants, eventos ou bancos antigos. Interfaces independentes Broker/TEG
permanecem separadas, sem conversão de grants.

O início explícito usa o contrato operacional existente da ADR 0010, com
plano/revisão, workspace, executor, quotas, cancelamento e recuperação. Esses dados
coordenam jobs, sem autenticar o usuário. Não há confirmação por tarefa.
`run --allow-unconfined-network` continua aceito, mas deixa de ser requisito:
execução local usa os acessos do host, sem prometer isolamento de rede/filesystem.
Requisitos nativos de login do Codex não são removidos pelo AgentFlow.

## TASKS.json

`agentflow run` sem objetivo lê `TASKS.json` na raiz Git. `--tasks-file` aceita
outro caminho, relativo à raiz ou absoluto. Objetivo e arquivo explícitos são
mutuamente exclusivos; objetivo explícito não importa o backlog implicitamente.

Formatos: lista de textos, lista de objetos com `title`, `description` ou
`objective`, ou objeto com `tasks` contendo essa lista. Todos os campos descritivos
são encaminhados ao planejador existente, que deriva escopos e gates verificáveis.
Não é migração do estado do protótipo ADR. Comandos no JSON não são executados
pelo leitor nem usados diretamente como validadores.

Limites: 1–12 tarefas, arquivo até 8192 bytes e objetivo normalizado até 8192 bytes.
IDs explícitos são únicos e seguem `[a-z][a-z0-9-]*`; `depends_on` exige ID na
tarefa e referências existentes, sem duplicatas ou ciclos. JSON inválido, chaves
duplicadas e constantes não JSON falham antes de criar configuração/estado.
IDs e dependências declarados são conferidos na saída do planejador; alterações
são recusadas. Formatos externos não contemplados exigem adaptação explícita.

O original nunca é reescrito. Conteúdo canônico integra o objetivo e a identidade
idempotente do pedido; conteúdo alterado gera outro pedido. TOMLs manuais são
preservados pelo fluxo de planos existente. A importação ainda usa o planejador
Codex para derivar gates; não é execução direta de JSON arbitrário.

## Prompts e validação

Cada execução recebe somente a tarefa atual: instrução, critérios e caminhos.
Título idêntico à descrição aparece uma vez. Objetivo/backlog completo e tarefas
futuras não são incluídos. O prompt exige parar ao concluir/encontrar bloqueio
e responder em até três linhas: resultado, verificação e bloqueio. É instrução
ao executor, não garantia de tamanho da resposta; logs/evidências ficam completos.

Testes cobrem defaults, legado preservado, repetição, backlog automático/alternativo,
erros antes de escrita, idempotência, IDs/dependências, enrollment inativo e
prompts de tarefa. Não se executa Codex real nem backlog do usuário nesta fatia.
Verificações de executor/versão e evidência continuam vigentes.

Esta ADR prevalece sobre o default `none` e requisitos anteriores de perfil/auth
local. Não altera o protótipo, histórico Git ou validação/recuperação das ADRs
0002–0009. Códigos históricos de autenticação permanecem para leitura e regressão.
