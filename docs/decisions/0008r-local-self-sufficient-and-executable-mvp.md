# ADR 0008-R — AgentFlow local autossuficiente e MVP executável

**Status: APPROVED FOR LOCAL_SUPERVISED DEVELOPMENT — profile not homologated; grants and dispatch remain disabled.**

**Data:** 2026-11-11

**Escopo:** revisão de produto e arquitetura de autorização/execução. Este documento não altera a ADR 0008 vigente, não aprova H-01–H-10, não inicia RB-09C.2 e não habilita consentimento, grants ou dispatch.

## 1. Direção arquitetural aprovada

### Registro da aprovação do proprietário — 2026-11-11

O proprietário aprovou a direção de distribuir o AgentFlow como ferramenta local autossuficiente, definiu `LOCAL_SUPERVISED` como caminho principal do MVP e preservou `INDEPENDENT_AUTHORITY` como alternativa de alta garantia, sem remover H-01–H-10. Esta aprovação autoriza implementar a seleção explícita do perfil e contratos preparatórios; não é autorização de consentimento de proposta, emissão de grant, dispatch, job real ou declaração de homologação.

Os limites de ameaça desta proposta correspondem a essa separação: `LOCAL_SUPERVISED` confia no computador e no SO locais, não promete resistir ao administrador/root ou ao comprometimento do host, e precisa impor separação local entre Broker e executor. `INDEPENDENT_AUTHORITY` mantém os requisitos de independência operacional e continua sujeito aos gates H-01–H-10. Critérios e mecanismos concretos abaixo continuam sujeitos a implementação e validação; a direção aprovada não os declara comprovados.

Esta aprovação não modifica retroativamente `docs/decisions/0008-phase-7-routing-and-approval.md` nem converte propostas R6/R8 em contratos aprovados.

Distribuir o AgentFlow como ferramenta instalável que opere localmente em Windows e Linux. Broker, APL, TEG, executor, estado, recuperação e auditoria podem ser componentes locais; uma autoridade hospedada externamente, VM, container ou serviço remoto não é dependência obrigatória do produto.

Manter dois perfis de segurança com nomes e gates distintos:

1. **`LOCAL_SUPERVISED`** — execução local após consentimento humano explícito sobre uma proposta imutável e limites definidos. O usuário confia no host, no sistema operacional e nos componentes locais instalados. A proteção contra comprometimento do administrador local ou do próprio host não é alegada.
2. **`INDEPENDENT_AUTHORITY`** — perfil de maior garantia, com domínios administrativos independentes para Broker/TEG e as demais autoridades exigidas. A arquitetura-alvo B da ADR 0008 R2 permanece válida para esse perfil. A aprovação de `LOCAL_SUPERVISED` nunca satisfaz automaticamente nem reduz os requisitos deste perfil.

O consentimento de produto para construir um MVP local não equivale a aprovar esta proposta, ativar grants, autorizar dispatch real ou homologar qualquer perfil. Até aprovação formal desta ADR e conclusão dos gates locais abaixo, o comportamento atual fail-closed permanece obrigatório.

## 2. Relação com decisões existentes

- A ADR 0008 atual e seus adendos continuam sendo a norma vigente até esta proposta ser aprovada. O bloqueio F7-01 de grants e os defaults indisponíveis continuam ativos.
- ADR 0008 R2 aprovou `INDEPENDENT_AUTHORITY` como alvo; não demonstrou sua implantação. Esta proposta mantém esse compromisso sem exigir tal infraestrutura para a distribuição local básica.
- H-01–H-10 pertencem ao fechamento e à homologação do perfil independente descrito em RB-09C.1. Continuam pendentes para esse perfil. Não são considerados aprovados nem silenciosamente reaproveitados para o perfil local.
- R6 continua uma proposta de contrato. R8 foi rejeitada pela revisão R8-V e não é adotada como norma. O perfil local precisa de uma especificação própria aprovada, especialmente para revogação, recibos, tempo, persistência e recuperação.
- RB-09C e os contratos Fase 7 não são alterados nesta proposta. Uma revisão futura deverá identificar explicitamente quais cláusulas autorizadas se aplicam a cada perfil e resolver conflitos antes de implementação.
- A decisão estreita já registrada para `kid=bstr` permanece com seu escopo histórico. Nenhum outro elemento de R3/R5/R6/R8 se torna normativo por referência a este documento.

## 3. Perfis e comparação

| Propriedade | `LOCAL_SUPERVISED` | `INDEPENDENT_AUTHORITY` |
|---|---|---|
| Localização | Componentes instalados no computador do usuário; offline funcional quando o planner/modelo local assim permitir. Codex CLI pode operar como adaptador local opt-in. | Broker, TEG, custódia, anchor e/ou dispositivo em domínios administrativos independentes, possivelmente remotos. |
| Consentimento | Ação explícita do operador em interface local confiável, ligada ao digest e limites exatos da revisão. Replanejamento material exige consentimento novo. | Ceremony/interface independente e autenticador conforme contratos aprovados; a UI e os dados mostrados não podem ser controlados pelo agente. |
| Separação | Broker mantém autoridade de emissão separada do executor por processo, identidade, armazenamento e canal do SO. A garantia depende do host local confiável; administrador local pode subverter controles. | Independência administrativa verificável entre tenants/control planes, principals, operadores, custódia e recuperação. Processos/contas no mesmo host não bastam. |
| Chaves | Nunca embutidas no pacote ou workspace. Chave do Broker protegida pelo mecanismo do SO sob identidade dedicada. A resistência a administrador/root local não é alegada. | Custódia no domínio independente escolhido, bootstrap externo e recovery aprovados. |
| Execução | TEG local valida grant de uso limitado; executor não recebe chave nem interface de emissão. Worker restrito ao workspace autorizado; execução somente dentro do grant e quotas. | TEG/ECA valida autorização e estado/claim independentes; provider e fencing devem satisfazer o contrato aprovado. |
| Falha | Negar por padrão; timeout/estado `unknown` não reexecuta efeitos; reconciliação somente consulta; Broker indisponível bloqueia. | Mesmas regras, acrescidas de quorum/anchor, revogação e independência operacional homologados. |
| Evidência | Journal local append-only, cadeia de hashes/digests, receipts do TEG e evidência do executor. Evidência local não é atestação externa contra administrador hostil. | Evidência independente, receipts e testemunhas/anchor conforme protocolo aprovado. |
| Modelo de ameaça | Defende contra erros, propostas maliciosas e processos worker sem privilégios, sob host/OS confiáveis. Não defende contra administrador/root, malware do host, adulteração física ou comprometimento simultâneo do Broker local. | Visa manter confiança mesmo quando o host agente é hostil/comprometido, sujeito a um modelo e limites formalmente homologados. |
| Resultado de homologação | Autoriza somente o perfil local, no OS/versão/configuração testados e com limites aprovados. Não é produção de alta garantia. | Independente; não inferir sucesso Windows a partir de Linux ou vice-versa. |

O pacote pode oferecer ambos os perfis, mas deve exibir o perfil ativo em status, logs e consentimento. Não deve fazer fallback silencioso de `INDEPENDENT_AUTHORITY` para `LOCAL_SUPERVISED`.

## 4. Arquitetura candidata de `LOCAL_SUPERVISED`

```text
Operador local
   │ revisa a proposta imutável e confirma digest + limites
   ▼
Broker local ── estado/auditoria protegidos pelo SO
   │ grant local assinado, uso único, curto e limitado
   ▼
TEG local ── valida identidade, grant, escopo, revogação e quotas
   │ despacho permitido somente para operação autorizada
   ▼
Executor/worker ── sem chave, sem API de emissão, workspace delimitado
   │ resultado estruturado + evidências
   ▼
APL ── avalia; revisão semântica alterada volta para consentimento
```

### Responsabilidades e invariantes

- **APL/planner:** propõe plano e revisão; conteúdo do planner e de jobs é não confiável. Não emite consentimento ou grant.
- **Broker local:** única autoridade local capaz de transformar uma ação explícita do operador em um grant do perfil local. Lê/reconcilia o plano exato, apresenta objetivo, tarefas, diff, revisão, digest, escopo, operações, destino, limites e expiração. Não executa jobs.
- **Consent UI:** superfície pertencente ao Broker, fora do controle do planner/worker. Exige confirmação deliberada e identificador claro do perfil `LOCAL_SUPERVISED`. Texto do projeto é escapado e não pode se fazer passar por interface ou instrução do Broker. Conteúdo truncado, stale, não reconciliado ou com digest divergente é negado.
- **Grant local:** proposta candidata: assinatura verificável pelo TEG, `grant_id` único, `operation_id`, proposal/revision/digest, principal operador, workspace e escopo, allowlist de operações/jobs, limites (jobs, tentativas, iterações, tempo, recursos e orçamento estimado), issued/expiry e nonce. Sem bearer reutilizável entregue ao planner. TTL, algoritmo, serialização e revogação dependem de contrato local aprovado; não se usarão valores propostos em R6/R8 como defaults normativos.
- **TEG:** valida assinatura e binding, status e expiração, revogação online/local consistente, uso único/replay, destino, limites e estado de execução imediatamente antes do efeito. Nenhuma validação parcial permite execução. Se revogação ou persistência não estiver disponível, bloqueia.
- **Executor:** processo/identidade separados onde o SO permitir; sem material de chave/credential Broker, sem acesso de escrita ao store de autoridade e sem chamada de emissão/consentimento. Recebe somente operação já autorizada e payload identificado. Resultados `unknown` ficam bloqueados e não são repetidos automaticamente.
- **Persistência:** SQLite local com migrações transacionais, backup consistente, journal append-only e recuperação. Isso assegura consistência de aplicação, não integridade contra administrador local. Material de autoridade em store separado sob controle do serviço Broker.
- **Revogação:** Broker registra evento terminal append-only; TEG verifica antes de cada nova operação. Cancelar uma operação já iniciada só será afirmado quando executor/provider produzir evidência suficiente; revogação não desfaz efeitos passados.
- **Replanejamento:** resultado validado pode originar proposta nova. Qualquer mudança material de plano, escopo, operações, destino ou limite exige revisão e consentimento novos. Aprovar um plano nunca concede ao planner poder de ampliá-lo.
- **Limites:** limites financeiros só são teto se o provider os aplicar e fornecer evidência. Caso contrário o campo é orçamento estimado e o perfil bloqueia operações sem limite de custo operacional aceitável.

### Plataforma e separação local

O desenho deverá oferecer mecanismos nativos específicos, sem os confundir com `INDEPENDENT_AUTHORITY`:

- Windows: serviço Broker sob identidade de serviço dedicada, ACL de store/keystore, IPC local autenticado e serviço/worker com privilégios mínimos. A instalação pode exigir elevação; UAC, nome de serviço ou processo separado não provam proteção contra administrador local. UI e sessão do operador precisam de teste contra automação e spoofing.
- Linux: usuário de serviço dedicado, arquivos/socket/store com permissões mínimas e serviço supervisionado quando disponível; MAC/systemd são controles condicionais, nunca presumidos. Em ambientes PRoot, sem namespaces/cgroups/systemd, o dispatch local fica inelegível se os controles exigidos não puderem ser impostos.
- Ambos: nenhum caminho de acesso às chaves no workspace; nenhuma credencial operacional no processo worker; instalação não executa providers; incapacidade de impor um controle obrigatório resulta em `approval_unavailable`/blocked, não em fallback permissivo.

## 5. Modelo de ameaças do perfil local

### Ativos

Proposta e digest aprovados, identidade/gesto do operador, grants e revogações, credenciais de providers, workspace, histórico SQLite, receipts, limites e integridade da apresentação de consentimento.

### Adversários e riscos considerados

1. Prompt injection ou conteúdo malicioso no objetivo, repositório, dependências, saída do planner ou resultado de job tentando expandir escopo ou induzir aprovação.
2. Worker/adaptador comprometido tentando ler grants/segredos, modificar o plano, chamar Broker, executar uma operação adicional ou forjar evidência.
3. Reuso, corrida ou replay de grant/operation após resposta perdida, reinício, clone ou restauração de backup.
4. Alteração de digest/revisão/scope, downgrade de protocolo, identidade errada, pin/credencial revogada e divergência de relógio.
5. UI spoofing, conteúdo truncado, sobrecarga de aprovações e consentimento para plano diferente daquele executado.
6. Corrupção ou perda de SQLite, falha entre commit e efeito externo, cancelamento incerto, esgotamento de recurso e custos sem teto enforcement.
7. Processo não privilegiado local tentando escapar de workspace ou alcançar Broker/TEG; as capacidades efetivas variam por plataforma e precisam de prova operacional.

### Fora do modelo de garantia local

Administrador/root local malicioso ou comprometido, kernel/firmware hostil, malware com controle da sessão do operador, comprometimento simultâneo do serviço Broker e do host, acesso físico e adulteração posterior por administrador. Se esses adversários estiverem no escopo, o perfil local não pode ser apresentado como suficiente; usar `INDEPENDENT_AUTHORITY` homologada ou bloquear.

### Controles e comportamento de falha

Negação por padrão; aprovação exata e explícita; contratos tipados e versionados; digest/revisão/escopo vinculados; chave apenas no Broker; TEG valida antes do efeito; grant único, revogável e limitado; dependências/allowlist verificadas; limites e timeout; journal append-only; reconciliação read-only para `unknown`; sem retry de efeito ambíguo; stop/cancel; evidência estruturada; testes adversariais por OS. Nenhuma frase de prompt é controle de segurança.

## 6. Critérios propostos para aprovar esta revisão e o MVP

### Gate arquitetural (antes de dispatch)

- Proprietário aprova explicitamente esta ADR e seu threat model limitado, inclusive os adversários fora do perfil local.
- Revisão Fase 7 local identifica cláusula por cláusula o que muda e o que permanece; proprietário decide qualquer conflito com F7-01 sem interpretar silêncio como liberação.
- Contratos `LOCAL_SUPERVISED` para consent, grant, revogação, tempo, limites, receipts, armazenamento e recuperação são especificados; eles não importam automaticamente contratos R6/R8.
- Defaults `Unavailable*` e production gate permanecem até homologação e uma autorização operacional posterior.

### Gate funcional por OS

- Instalação limpa e desinstalação/rollback documentados e repetíveis em Windows suportado e Oracle Linux guest; dependências locais declaradas e sem serviço externo obrigatório.
- Identidades/ACLs efetivas testadas: worker não lê/escreve store/chave Broker; executor não chama emissão; cliente não autorizado é negado. Processo elevado/admin é testado e a limitação declarada.
- Consentimento humano real em interface local separada do planner: UI mostra conteúdo completo, digests verificáveis, operação, workspace, limites, riscos e perfil; rejeição, ausência, stale, truncamento, foco/automation e clique cancelado nunca autorizam.
- TEG aceita somente grant genuíno com binding exato; testes negativos cobrem alteração de digest/revisão/escopo/destino, expiração, revogação, replay, concorrência, status terminal e tentativa de self-approval.
- Zero grants/efeitos em ausência de autoridade, falha de persistência, timeout, relógio incerto, estado `unknown`, dependência cíclica, fora de escopo ou limite esgotado.
- Recuperação após falha nos boundaries consent→grant, grant→TEG, TEG→executor, efeito externo→receipt e revogação; nenhum efeito ambíguo é repetido automaticamente.
- Primeiro piloto usa workspace descartável sintético, um job não destrutivo e efeito verificável; prova planejamento→proposta→consentimento real→dispatch autorizado→resultado/evidência→avaliação→recuperação/conclusão. Cada job real exige plano e limites explícitos autorizados.
- Testes e evidências específicos de Windows e Oracle Linux são registrados separadamente. Suíte completa, regressões e `git diff --check` passam. Mocks não contam como prova de identidade, UI, ACL, efeito ou consentimento real.
- Aviso visível: `LOCAL_SUPERVISED` confia no host/OS local e não é homologação de `INDEPENDENT_AUTHORITY` nem declaração de segurança para produção contra administrador hostil.

## 7. Fatias verticais de implementação propostas

| Fase | Resultado concreto | Gate de saída |
|---|---|---|
| L0 — decisão e contratos | Aprovar esta ADR; fechar matriz F7/RB-09C por perfil; registrar threat model, ownership e limites fora do perfil | Decisão formal do proprietário; nenhuma alteração no gate independente. |
| L1 — contrato local | Tipos/versionamento para ConsentRecord, LocalGrant, revogação, limites, receipt e estado `unknown`; decodificador estrito e vetores | Testes de binding/negativos, sem issuer operacional; revisão de segurança local. |
| L2 — Broker local | Processo/serviço local com identidade OS, store segregado, IPC autenticado, journal e recovery; ainda sem dispatch por default | Testes reais por OS de ACL/identidade, negação de worker e crash/restart; fail-closed. |
| L3 — consentimento | UI Broker independente do planner, apresentação integral de proposal/digest/limits, gesto explícito e registro do operador | Testes humanos/UX e de automation/injection por plataforma; consentimento é para digest exato. |
| L4 — TEG local | Verificação do issuer, binding, validade, revogação, uso único, quotas e decisão persistida; interface do executor continua fechada por feature gate | Testes de concorrência, replay, revogação, falha e `unknown`; ausência de grant não produz dispatch. |
| L5 — instalação Windows | Pacote instalável, service principal/ACL/IPC, atualização/rollback e primeiro laboratório sintético | Homologação Windows documentada; limitação admin/root e requisitos de elevação explicitados. |
| L6 — Oracle Linux | Repetir instalação e campanha no guest Oracle Linux; adaptar serviço/socket/ACL sem pressupor systemd/containers | SSH funcional, capacidade e imagem/backup confirmados; testes reais específicos do guest. Provisionamento exige aprovação operacional quando alterar VM. |
| L7 — primeiro job piloto | Opt-in explícito; job sintético não destrutivo em workspace descartável; consentimento, limite, efeito e evidências verificáveis | Autorização separada do proprietário para dispatch do piloto, gates L0–L6 aprovados, `unknown`/recovery demonstrados. Não é autorização automática para produção. |
| L8 — perfil independente (futuro) | Resolver H-01–H-10, R8 blockers, domínios e campanha independente | Aprovação/homologação própria. Não é requisito para instalar o produto local e não pode ser declarado pelo sucesso L0–L7. |

## 8. Plano de homologação Windows e Oracle Linux

### Windows principal

1. Primeiro inventário somente leitura: versão/arquitetura, Python, SQLite, Codex CLI, política de serviço/IPC, usuários de serviço existentes, permissões efetivas de diretórios temporários e recursos de teste. Não mudar configurações globais.
2. Após aprovar L0 e autorizar provisionamento: instalar em sandbox/workspace sintético, com caminhos dedicados e dados sintéticos; registrar versão, hashes de artefato e identidade efetiva de cada processo.
3. Validar serviço Broker, IPC, ACL, keystore, worker e tentativas negativas (leitura/escrita, named pipe, subprocesso, UI automation) sob worker não privilegiado e adversários previstos.
4. Validar consentimento real somente com operador que aceite participar, dispositivo de teste e plano sintético explícito; nenhuma chave operacional real nos testes.
5. Testar crash/restart, SQLite integrity, journal, cancelamento, quotas, replay, revogação, timeout e `unknown`; confirmar zero execução fora do grant.
6. Piloto de um job sintético só após autorização específica de dispatch; não executar no repositório principal.

### Oracle Linux VM auxiliar

1. Verificação não destrutiva inicial planejada: resolução/handshake SSH, sistema/arquitetura/kernel, usuário efetivo e privilegiamento, memória/disco disponíveis, Python/SQLite/Git já presentes, init/service manager e recursos de segurança efetivamente disponíveis. Não ler segredos, conteúdo de home ou configurações privadas.
2. Nesta sessão o cliente `ssh` está instalado e `ssh -G oracle-vm` produz configuração; a conexão `ssh -o BatchMode=yes -o ConnectTimeout=5 oracle-vm ...` falhou com **hostname não resolvido**. Nenhum comando foi executado na VM. A correção do alias/DNS/rede requer ação do operador; não foi tentada.
3. Depois de conectividade e autorização para mudanças: conferir Oracle Linux release e capacidades; instalar somente artefatos aprovados, com origem/hash/licença registrados. Não presumir systemd, namespaces, cgroups, Docker ou isolamento por ser VM.
4. Aplicar o mesmo conjunto de casos de contrato, consentimento, replay, revogação, limites, recuperação e worker adversarial, distinguindo teste funcional do teste de fronteira do SO.
5. Nenhuma instalação de pacotes/serviços, criação de usuários, ajuste de ACL, chave, mudança de rede ou implantação na Oracle VM está autorizada por esta proposta. Solicitar escopo específico quando a etapa chegar.

Relatórios devem classificar cada controle como `configured`, `observed`, `enforced-and-tested`, `not-proven` ou `failed`; nunca extrapolar evidência de um OS para o outro.

## 9. Decisões que requerem aprovação antes da autoridade operacional ou dispatch

1. A direção desta ADR e o limite de ameaça local foram aprovados. Antes de autoridade operacional, confirmar os detalhes do perfil de ameaça e a aceitação explícita de que administrador/root e comprometimento do host ficam fora da garantia de `LOCAL_SUPERVISED`.
2. Aprovar o limite de autonomia: quais jobs/ações podem ser pré-autorizados num plano, quais exigem consentimento por job e quais são sempre proibidos. Replanejamento material exige consentimento novo.
3. Escolher política de identidade local, instalação privilegiada, IPC e armazenamento de chaves para Windows/Linux; aprovar quais plataformas/versões entram no primeiro release e o comportamento em PRoot.
4. Aprovar o contrato local de grant/revogação/tempo/limites/receipts e a relação com F7-01/RB-09C, incluindo quais gates de alta garantia continuam sem exceção.
5. Aprovar UX/identidade do operador, enrollment, tratamento de dispositivo perdido, recovery e auditoria local.
6. Autorizar separadamente modificações na Oracle VM (pacotes, serviço, usuários/permissões, arquivos) depois que o acesso SSH for corrigido e o plano exato for apresentado.
7. Autorizar separadamente o primeiro dispatch sintético depois dos gates de implementação/homologação. A presente decisão de produto não é essa autorização.
8. Manter H-01–H-10 e `INDEPENDENT_AUTHORITY` bloqueados até suas próprias decisões e evidências. Aprovar esta ADR não aprova R8 nem RB-09C.2.

## 10. Resultado desta revisão

Esta ADR aprova a direção local autossuficiente e o perfil `LOCAL_SUPERVISED` como caminho principal do MVP, preservando `INDEPENDENT_AUTHORITY` e H-01–H-10 como trilha de alta garantia separada. A fundação de seleção e contratos preparatórios pode ser implementada. **O perfil local ainda não está homologado e nenhum dispatch real foi habilitado.** Antes de implementar autoridade positiva ou executar jobs, ainda são necessários os contratos detalhados, os gates por OS e autorização operacional específica.

Classificação da direção: **`LOCAL_ARCHITECTURE_APPROVED_FOR_DEVELOPMENT`**. Estado de homologação permanece pendente.

## Supersessão parcial pela ADR 0010

Em 2026-11-13, o proprietário decidiu pela ADR 0010 que o início de jobs comuns `LOCAL_SUPERVISED` não exige senha/autenticação adicional nem consentimento por tarefa. As passagens deste documento que exigem consentimento humano autenticado como condição universal para execução local estão supersedidas somente nesse ponto. O contrato local persistido, escopo estrito, limites, expiração, revogação, recuperação conservadora e confiança no host continuam necessários. `INDEPENDENT_AUTHORITY`, a arquitetura independente da ADR 0008 R2 e H-01–H-10 não são afetados. Esta nota não declara o contrato ou dispatch implementados/homologados.
