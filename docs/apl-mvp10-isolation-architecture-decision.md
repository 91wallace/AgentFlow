# AgentFlow APL MVP-10 — Isolation Architecture Decision

**Data:** 2026-11-09
**Classificação:** **CONDITIONAL**
**Decisão:** preferir, como próxima fronteira a homologar, uma **VM Windows descartável e isolada**, sem integração de filesystem/credenciais do host e com rede negada por padrão. Esta é uma direção arquitetural; disponibilidade e controles ainda precisam ser preparados e homologados antes da implementação. Nenhum backend foi habilitado.

## 1. Checkpoint e escopo

MVP-10 não existia no repositório no início desta retomada: não havia relatório nem marcador de progresso. O último checkpoint válido é **MVP-09.2 — PARTIAL**. O APL supervisionado e seus contratos continuam no pacote `agentflow.apl`; o `RestrictedInferenceHarness` permanece fail-closed e não tem launcher real. Esta decisão não conecta o experimento ao `PlannerAdapter`, não inicia provider/modelo e não autoriza execução automática de jobs.

Foram consolidados, sem repetir testes conclusivos, os relatórios MVP-07, MVP-08, MVP-09, MVP-09.1 e MVP-09.2 e o checkpoint `docs/apl-mvp-progress.md`.

## 2. Evidências consolidadas

### Comprovadas no ambiente e no escopo testado

- APL fornece `PlannerAdapter.propose(ReplanContext) -> PlanProposal`, decodificação JSON estrita e validação de escopo/proposta; o adaptador semântico real permanece indisponível e o determinístico é a opção local existente.
- `RestrictedInferenceHarness` não inicia processo e retorna `BLOCKED` sem backend revisado.
- O experimento AppContainer usa SID temporário com zero capabilities; `cmd.exe` e Python autocontido iniciaram. ACLs Windows negaram leitura/escrita de arquivo sentinela sintético e leitura de credencial sintética (`PermissionError 13`). A pasta de saída explicitamente autorizada aceitou a escrita de smoke.
- Timeout/cancelamento do worker persistente e término dos processos observados no Job Object ocorreram no ensaio MVP-09.2. O perfil e diretório sintéticos foram removidos.
- O protocolo recomendado de APL pode transportar snapshot de contexto e resposta limitada sem incluir comandos executáveis; isso é controle de interface, não isolamento do processo.
- Ambiente de teste: `.venv` tem `include-system-site-packages=false` e não encontra `cryptography`; `pyproject.toml` declara `cryptography>=46`. Nove módulos da suíte falham na coleta pelo import de `cryptography.exceptions`. Não instalar dependências foi intencional.

### Parciais ou não comprovadas

- AppContainer: os testes de ACL se limitaram a recursos sintéticos sob o mesmo Windows/build e não sondaram todos os caminhos operacionais do workspace/perfil.
- Rede: teste de loopback expirou sem conexão recebida e sem erro de negação inequívoco; não há política de egress vinculada ao worker demonstrada.
- Memória: Job Object configurado em 64 MiB; `MemoryError` ocorreu, mas o pico contabilizado foi 67.702.784 bytes, 593.920 bytes acima da quota informada.
- Processos: accounting chegou a 3 quando o limite configurado era 2; sem identificação de todos os PIDs não há comprovação do limite. O Job terminou os processos observados no cenário testado.
- Isolamento contra o usuário pai administrativo, isolamento de credenciais reais, ausência de extensão/plugin e rede externa não foram homologados.
- Linux não foi testado. A suíte completa na `.venv` permanece não homologada. Os 435 testes executados no Python global no checkpoint anterior não substituem execução no ambiente selecionado.

### Riscos residuais

Texto/resultados usados pelo planejador podem conter instruções maliciosas; permanecem dados não confiáveis e só podem gerar proposta para validação/aceite supervisionados. Um canal host-guest pode virar caminho de exfiltração ou de entrada de dados excessivos se compartilhar diretórios, clipboard, credenciais ou rede ampla. Snapshot/reversão não resolve vazamento para fora da VM. A decisão não cobre confidencialidade/retention de nenhum provider externo.

## 3. Disponibilidade local

Consultas estáticas somente leitura deste host Windows:

- Windows 10 Pro x64, build 26300.9711, segundo os checkpoints anteriores.
- `Microsoft-Hyper-V-All`, `VirtualMachinePlatform` e `Containers-DisposableClientVM` aparecem como **Desabilitado** via DISM.
- `wsl.exe --list --quiet` informou que WSL não está instalado.
- Não foram encontrados `docker.exe`, `vmrun.exe`, `VirtualBoxVM.exe` ou cmdlet `Get-VM` no PATH disponível. Isso não prova inexistência de hypervisor/binário fora do PATH.
- Não há host de inferência administrativamente separado identificado nos inventários MVP-07/08.

Portanto, nenhuma VM ou host remoto está pronto/verificado para uso nesta sessão. Nenhum recurso foi ativado, instalado ou configurado.

## 4. Comparação das alternativas

| Alternativa | Filesystem e credenciais | Rede, quotas e processos | Recuperação | Disponibilidade e custo | Portabilidade / integração APL |
|---|---|---|---|---|---|
| **A. VM Windows isolada** | Guest tem disco/identidade próprios. Forte separação se não houver pastas compartilhadas, clipboard, drives redirecionados, mounts ou credenciais do host. | Adaptador de rede removido ou rede privada com egress negado no host; quota de memória/CPU da VM no hypervisor; controles de processo dentro do guest continuam necessários. | VM descartável e snapshot/recriação dão reset claro; estado de request/resposta deve continuar no store do APL, fora do guest. | Hipervisor Windows não está habilitado nesta máquina. Custo médio/alto: imagem, patching, licença, disco, RAM e manutenção do guest. | Boa compatibilidade com host Windows; acoplamento ao hypervisor para canal de dados. Adapter pode serializar `ReplanContext` e validar retorno JSON; o transporte ainda exige projeto seguro. |
| **B. VM Linux isolada** | Guest independente, sem mounts do host, separa workspace e credenciais; namespaces/read-only podem reforçar isolamento dentro do guest. | Egress bloqueado no hypervisor; cgroups/process limits/seccomp no guest, se configurados e testados. | Imagem imutável e snapshot/recriação; unknown após interrupção permanece no APL, sem retry automático. | WSL não instalado e plataforma de virtualização desabilitada. Custo alto e operação de Linux/runtime adicional. | Menor portabilidade operacional para este host; requer transporte/worker e suporte Linux separados, sem evidência atual. |
| **C. Serviço em host administrativamente separado** | Pode usar conta/host sem workspace nem credenciais do executor; depende de separação administrativa real, sem shares ou secret forwarding. | Firewall/egress e quotas são administrados no host/serviço; precisa provar cancelamento remoto e limites reais. | Pode manter operação remota consultável, mas exige identidade, auditoria, recuperação/idempotência e tratamento de resultado desconhecido. | Nenhum host foi identificado. Custo operacional recorrente e dependência de equipe/administração/monitoramento. | Interface HTTP/RPC pode adaptar-se bem ao `PlannerAdapter`; adiciona rede, autenticação, gestão de credenciais e revisão de dados/retention. |
| **D. AppContainer com controles adicionais** | Evidência sintética existente para ACLs; não comprovou todos os caminhos operacionais nem identidade administrativa de baixa confiança. | Rede continua inconclusiva; quotas de memória e processos apresentaram lacunas; Job Object só controla processos/recursos, não FS/credenciais/rede. | Ciclo do Job e cleanup foram observados em cenários específicos; falha parcial precisa continuar fail-closed. | Já há harness experimental, mas os controles pendentes exigem engenharia Windows de baixo nível e manutenção por versão/build. | Menor overhead e boa integração local pelo adapter, mas específico de Windows e ainda sem elegibilidade de segurança. |

## 5. Justificativa e condições da decisão

A alternativa **A** é preferida para a próxima homologação porque cria uma fronteira de VM entre o processo de inferência e a identidade/workspace do AgentFlow e corresponde ao host-alvo atual. Ela não é considerada disponível agora: Hyper-V e Virtual Machine Platform estão desabilitados e sua habilitação requer ação administrativa/alteração de configuração fora do trabalho documental desta etapa. A alternativa C pode substituir A se um host separado vier a ser disponibilizado sob administração aprovada; nenhum está identificado. B não é escolhida porque acrescenta uma plataforma guest sem disponibilidade Linux local. D fica preservada como experimento parcial, não como caminho ativo.

Pré-requisitos para abrir uma implementação de VM, em etapa autorizada separadamente:

1. Disponibilizar hypervisor/VM por processo administrativo aprovado ou um host de homologação já pronto; verificar versão, atualização, recursos de hardware, licença e proprietário operacional.
2. Criar guest descartável sem credenciais, workspace, `.agentflow`, diretórios compartilhados, clipboard, drives redirecionados ou integração host-guest não essencial.
3. Definir rede default-deny. Preferência: sem NIC/egress para o guest; se um canal host-guest for necessário, permitir somente endpoint/porta autenticada e demonstrar que não alcança destinos externos.
4. Limitar memória/CPU/disco e processos na VM e no guest. Testar a quota imposta pelo hypervisor com margem observada e teste de carga limitado; não depender apenas do accounting do processo.
5. Definir transporte pequeno/versionado para snapshot imutável de `ReplanContext` e JSON de resposta; não enviar ambiente, caminhos desnecessários, tokens ou credenciais. Validar resposta com decoder e validador APL existentes e manter aceite humano.
6. Definir timeout, cancelamento, encerramento/reset da VM, limpeza, logs sem payload sensível, digest/idempotência e recuperação conservadora em estado `unknown`.

## 6. Testes de homologação necessários

Sem executar nesta etapa: verificar por inspeção do hypervisor que sharing/clipboard/guest services estão desligados; tentar ler sentinelas sintéticas do host e do perfil de executor; tentar escrever fora do disco privado do guest; comprovar rede negada e, se aplicável, único endpoint host autorizado; confirmar limites de memória/CPU/disco/processos com cargas limitadas; testar timeout, cancelamento, desligamento abrupto e reinício/snapshot; buscar processos órfãos; validar JSON grande/malformado/duplicado e prompt injection como dado; testar repetição, concorrência e `unknown` sem nova inferência automática; repetir em cada imagem/build suportado.

Para host remoto, executar revisão equivalente de identidade administrativa, rede/egress, retenção, autenticação, limites/cancelamento e recuperação, além da avaliação de privacidade/provider. Para reconsiderar AppContainer, corrigir a discrepância de quota de memória/processos e demonstrar rede com resultado inequívoco sob policy do sistema operacional.

## 7. Ambiente de testes e correção proposta

`.venv\Scripts\python.exe -m pip show cryptography` retorna `Package(s) not found`; `find_spec("cryptography")` retorna `None`. O motivo é que a `.venv` não herda site-packages do Python global. A dependência de runtime já declarada é `cryptography>=46`; extra de teste declara `pytest>=8,<10`.

Correção reproduzível recomendada, **não executada**:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

Isto usa as dependências declaradas por `pyproject.toml`; não é instalação automática nesta decisão. Até a suíte completar no ambiente selecionado, não declarar homologação da suíte completa.

## 8. Próxima etapa proposta

Próxima etapa recomendada: **MVP-11 — VM Boundary Readiness and Design**, começando pela decisão administrativa sobre disponibilidade do hypervisor/host. Se não houver ambiente pronto, permanecer documental e fail-closed. Se houver, especificar apenas plano revisável de guest descartável, canal de dados, policy de rede e matriz de testes antes de instalar ou iniciar qualquer VM. A autorização para habilitar recursos de virtualização, criar guest ou operar host separado deve ser explícita e independente; esta decisão não inicia tais ações.

## 9. Estado Git e parecer

No início e ao final: branch `feat/intelligent-model-routing-phase-7`; HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`. Working tree já tinha modificações e arquivos não rastreados em docs, `src/agentflow/cli.py`, APL, RB-09C e `autonomous-development-runner/`; preservados sem limpeza nem sobrescrita. `.agentflow/` foi preservado. O único arquivo criado nesta etapa é `docs/apl-mvp10-isolation-architecture-decision.md`. Não houve commit, push, merge, rebase, reset, clean ou sincronização.

**Classificação final: CONDITIONAL.** A arquitetura preferida está indicada, mas a infraestrutura não está disponível/verificada e os controles de rede e quota da alternativa não foram homologados. AppContainer encerra como **PARTIAL**. O planejador semântico permanece indisponível; nenhuma implementação foi iniciada.
