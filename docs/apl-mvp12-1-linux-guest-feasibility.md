# AgentFlow APL MVP-12.1 — Viabilidade de guest Linux

**Data:** 2026-11-09

**Classificação:** **CONDITIONAL** — há um candidato documentalmente compatível e armazenamento suficiente na leitura atual, mas o boot do guest no host AMD FX-8300 não foi demonstrado. Não houve download, configuração de WSL ou criação/início de VM.

## 1. Baseline recuperado

Foram lidos `docs/apl-mvp10-isolation-architecture-decision.md`, `docs/apl-mvp11-development-environment-recovery.md` e `docs/apl-mvp12-disposable-vm-homologation.md`.

- MVP-10: **CONDITIONAL**, com VM Windows isolada como alternativa inicialmente preferida.
- MVP-11.1: **HOST_READY** para começar homologação; gerenciamento Hyper-V operacional, guest não testado.
- MVP-12: **CONDITIONAL**; sem guest/mídia/licença definidos para Windows e espaço insuficiente em C: para uma VM Windows de 64 GiB.
- APL continua supervisionado; backend semântico indisponível, RestrictedInferenceHarness fail-closed e execução automática não autorizada.

Branch atual `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`. O working tree já continha as mudanças e arquivos não rastreados listados na inspeção; foram preservados. Único arquivo criado nesta etapa: este relatório.

## 2. Comparação de distribuições

| Distribuição candidata | x86-64 e Hyper-V | Generation 2 / kernel | Requisitos documentados | Avaliação |
|---|---|---|---|---|
| **Ubuntu Server 24.04 LTS amd64** | Canonical publica imagem amd64; Microsoft documenta suporte a Ubuntu no Hyper-V e LIS integrado para 24.04 | UEFI/Gen2 é a configuração preferida para Linux atual; a imagem assinada deve ser testada com Secure Boot usando template UEFI CA. Drivers Hyper-V no kernel/distribuição | Canonical: 1,5 GB RAM e 5 GB disco mínimos para instalação ISO; sugere pelo menos 3 GB RAM e 25 GB disco para uso mais realista | **Recomendado**: versão LTS explicitamente coberta pela matriz Hyper-V consultada, imagem oficial e documentação clara. Requisitos cabem na RAM e no volume observados com disco virtual limitado. |
| Debian 12 Bookworm amd64 | Microsoft documenta Debian no Hyper-V; LIS integrado e UEFI/Gen2/Secure Boot listados para Debian 12 | Kernel contém integração Hyper-V; suporte/bugs são da distribuição para o guest | O guia de instalação Debian lista 512 MB RAM e 4 GB disco como mínimo de instalação (não recomendação de worker) | Alternativa tecnicamente viável e mais mínima, mas Ubuntu 24.04 tem requisitos operacionais e orientação da Canonical mais diretos para esta homologação. |
| Debian 13 Trixie amd64 | Suporte x86-64 e mídia oficial; a matriz Microsoft consultada especifica Debian 12 (Bookworm), não confirmou explicitamente Debian 13 | Hyper-V pode funcionar via drivers Linux upstream; compatibilidade exata de Secure Boot/integração não foi homologada neste host | Requisitos variam por perfil; validar no guia da versão escolhida antes do provisionamento | Não recomendado para o primeiro ensaio, até a matriz Hyper-V ou um boot controlado confirmar a combinação. |

Fontes oficiais consultadas: [requisitos Ubuntu Server 24.04](https://ubuntu.com/server/docs/system-requirements/), [mídia oficial Ubuntu 24.04](https://releases.ubuntu.com/24.04/), [Ubuntu 24.04 no Hyper-V](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/supported-ubuntu-virtual-machines-on-hyper-v), [Debian no Hyper-V](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/Supported-Debian-virtual-machines-on-Hyper-V), [requisitos do instalador Debian amd64](https://www.debian.org/releases/stable/amd64/ch02s05.en.html) e [gerações de VM Hyper-V](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/plan/should-i-create-a-generation-1-or-2-virtual-machine-in-hyper-v).

A distribuição Ubuntu é software livre sob várias licenças open source; termos de imagens/pacotes específicos devem ser respeitados. Ubuntu Pro/serviços pagos não são necessários para a instalação base. Não usar imagens de terceiros ou customizadas.

## 3. Compatibilidade esperada e lacunas

O host expõe AMD FX-8300 e Windows Hyper-V com capacidade x86-64. Ubuntu Server amd64 requer arquitetura 64-bit Intel/AMD; isso é uma compatibilidade de ISA esperada para o FX-8300. A matriz Microsoft confirma suporte de Hyper-V para Ubuntu 24.04 e suas Integration Services principais estão incluídas na distribuição. A documentação Hyper-V recomenda Gen2 para Linux moderno e documenta UEFI CA como template de Secure Boot Linux.

Essas evidências **não comprovam** que o host específico consiga criar e dar boot na VM. O relatório MVP-11.1 registrou inconsistência WMI (`VMMonitorModeExtensions=False`, `SecondLevelAddressTranslationExtensions=False`) apesar de hypervisor presente e gerenciamento funcional. Nenhum guest iniciou até agora. Somente um boot real, sem contornar política ou desativar requisitos às cegas, pode fechar a questão de compatibilidade do FX-8300/host. Caso Gen2 ou Secure Boot falhe, interromper e diagnosticar; não trocar para configuração menos segura sem revisão e autorização explícitas.

Integration Services de Linux habilitam capacidades de comunicação entre host e guest. Estar integrado não significa que compartilhamentos estarão automaticamente ativos; por outro lado, desligar serviços no guest não comprova ausência de todo canal do hypervisor. Inspecionar cada integração e demonstrar ausência de file copy/metadata channels desnecessários. A decisão atual é de fronteira experimental, não garantia formal contra vulnerabilidade de hypervisor/guest escape.

## 4. Recursos atuais e configuração proposta

Consulta somente leitura nesta retomada:

| Recurso | Estado |
|---|---|
| Hyper-V | Serviços `vmms`, `vmcompute`, `hvhost` rodando; `Get-VMHost` funcional |
| VMs registradas | 0 |
| Switch existente | `Default Switch` (Internal); não será alterado nem conectado |
| Volume C: NTFS | 96,77 GiB total, **39,10 GiB livres** |
| Outros volumes com letra/label | Nenhum retornado por `Get-Volume` |
| RAM | 15,92 GiB total; 11,05 GiB livres na consulta anterior desta sessão |
| CPU | AMD FX-8300, 8 processadores lógicos |
| WSL | Componente habilitado, runtime e distribuição ausentes; `VirtualMachinePlatform` estava desabilitado no MVP-11.1. Não é alternativa configurada nem fronteira homologada. |
| Python | `.venv` Python 3.13.17; `cryptography` ausente; nenhuma instalação feita. |

Proposta para um único guest descartável Ubuntu Server 24.04 LTS:

- Nome exclusivo `AgentFlow-APL-Linux-Disposable-<UTC timestamp>` e GUID anotado.
- Hyper-V **Generation 2**, VHDX e boot UEFI. Secure Boot ligado com template UEFI CA apropriado ao Linux; confirmar no primeiro boot. Não desabilitar como ajuste automático.
- 2 vCPU e **3 GiB RAM fixa** (sem Dynamic Memory) para alinhar com o mínimo sugerido pelo Ubuntu e manter quota observável. Revalidar carga e memória livre antes de qualquer provisionamento.
- VHDX dinâmico com máximo **25 GiB**, sistema mínimo sem GUI e apenas componentes necessários ao ensaio. Reserva física conservadora de **pelo menos 24 GiB livres** no volume escolhido antes de criar (máximo do disco + ISO + aproximadamente 6 GiB de margem operacional). A leitura atual de C: deixa cerca de 5 GiB após essa reserva e atende por pouco; revalidar imediatamente antes, sem criar disco agora. Dynamic VHDX limita capacidade virtual, não garante cota física contra crescimento; monitorar espaço do host durante o teste.
- Armazenamento e identidade separados em raiz temporária exclusiva fora do repositório, `.agentflow/`, perfil do usuário, `autonomous-development-runner/` e diretórios sincronizados.
- **Sem NIC virtual**; sem switch, NAT ou rota configurada. Não alterar o `Default Switch`. Inspecionar a VM e o guest para confirmar. Sem compartilhamentos, discos do host, workspace/mount, credenciais, clipboard, Enhanced Session/RDP, USB/dispositivos ou Guest Service Interface/file copy.
- Desabilitar Guest Service Interface, key-value data exchange e integrações não necessárias ao teste. Manter somente serviços que forem demonstrados necessários para ciclo operacional mínimo; registrar o que for ligado e por quê. Ausência de clipboard deve ser verificada no caminho de console escolhido, não presumida.
- Sem checkpoints automáticos ou persistentes; guest reconstituível por reinstalação a partir da mídia aprovada. Sem modelo, provider, executor ou credencial.

**Mídia:** imagem Ubuntu Server 24.04 LTS amd64, somente do domínio oficial `releases.ubuntu.com/24.04/`. A imagem não foi encontrada/baixada/verificada nesta etapa. Na futura etapa, registrar filename, SHA-256 publicado pela Canonical, versão, licença aplicável e resultado da verificação antes de anexar. O runtime do guest não deverá ser atualizado pela rede: preparar a mídia necessária fora da VM somente com autorização independente, ou assumir imagem base sem atualização até uma política de rede específica ser aprovada.

## 5. Plano de homologação futura

Todos os testes abaixo são **pendentes**, não aprovados:

1. **Baseline e criação:** revalidar volumes/RAM/CPU, Hyper-V, VM inventory, switches, Git e caminhos; criar exclusivamente VM e VHDX temporários após autorização; confirmar Generation 2 e limites efetivos.
2. **Boot e hardware:** anexar ISO oficial verificada; boot sem NIC; confirmar kernel amd64, UEFI, vCPU/RAM atribuídas, Secure Boot, dispositivos StorVSC/VMBus/SCSI e logs de integração Hyper-V. Capturar mensagens de erro de boot e parar ao primeiro requisito não atendido.
3. **Rede:** inspeção de configuração Hyper-V deve confirmar ausência de NIC. No guest, verificar interfaces, rotas e sockets. Tentar apenas endpoint sintético local conhecido quando tecnicamente acessível; separar conexão permitida, negada pelo SO, timeout inconclusivo, bootstrap failure e erro do serviço. Sem endpoint externo e sem confiar só em timeout.
4. **Arquivos e credenciais:** manter sentinelas sintéticas do host fora do workspace, permissões originais intactas; tentar acesso apenas por interfaces configuradas, provar que não há compartilhamento, mount, file copy, clipboard ou credencial do host. Confirmar que o guest lê/escreve apenas seu VHDX temporário.
5. **Quotas/processos:** carga sintética limitada e com deadline; verificar limite de CPU/memória do Hyper-V e processos dentro do guest (cgroups/ulimits somente se realmente configurados e necessários). Não tratar quota configurada como enforcement comprovado. Registrar picos, OOM, códigos e tempos.
6. **Timeout/cancelamento:** interromper worker sintético; confirmar `Stop-VM`/encerramento, ausência de processos ou sockets residuais e estado final explícito. Não repetir automaticamente ação externa ou resultado `unknown`.
7. **Descarte/rollback:** desligar guest, conferir nome/GUID e caminhos absolutos antes de remover VM/VHDX isolados; descartar somente raiz exclusiva; validar VM count inicial, switches, volume, arquivos sentinela, Git e diretórios protegidos inalterados. Se qualquer recurso não puder ser atribuído com certeza ao experimento, preservar e parar.

A máquina não terá canal de execução APL. Um futuro `PlannerAdapter` poderia receber contexto minimizado em arquivo JSON assinado ou RPC controlado, mas ambos criariam canais que exigem desenho próprio, integridade/autenticação, limite de tamanho, replay/idempotência e validação estrita de `PlanProposal`. Não enviar contexto sensível nem criar esse transporte nesta etapa.

## 6. Limitações de ambiente Python

A `.venv` continua em Python 3.13.17 e `find_spec("cryptography")` retorna `None`. `pyproject.toml` declara `cryptography>=46`; extra `test` declara `pytest>=8,<10`. Instalação não foi feita. O comando reproduzível já documentado permanece `\.venv\Scripts\python.exe -m pip install -e ".[test]"`, seguido de `pip check` e `pytest -q`, mediante autorização para instalar dependências. A suíte completa não está homologada no ambiente selecionado.

## 7. Riscos, autorização e parecer

Riscos atuais: nenhuma VM Linux foi criada ou iniciada neste host; WMI/SLAT continua discrepante; ausência de NIC não prova ausência de todos os canais de hypervisor; integração nativa Hyper-V traz funções que precisam ser individualmente verificadas; quota de recurso é só proposta; o host está em edição/build específica e nenhum guest escape foi avaliado. WSL não substitui essa prova.

**Classificação final: CONDITIONAL.** Ubuntu Server 24.04 LTS amd64 é o candidato preferido, com compatibilidade documental razoável com Hyper-V Gen2 e requisitos modestos; viabilidade operacional no FX-8300 e propriedades de isolamento continuam pendentes de homologação.

Antes de provisionar, é necessária autorização explícita separada para criar e iniciar **uma** VM descartável com a configuração acima. Essa autorização não incluirá baixar ISO, instalar/configurar WSL, alterar switch/firewall/global security, instalar dependências no host, iniciar providers/modelos, executar jobs, habilitar APL semântico ou integrar Broker/TEG. Se a autorização não incluir mídia, o usuário deve fornecê-la previamente ou autorizar seu download oficial em etapa separada.

Até a homologação: manter APL supervisionado, RestrictedInferenceHarness fail-closed, backend semântico indisponível e nenhuma execução real.
