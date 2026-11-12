# AgentFlow APL MVP-12 — Homologação de VM Windows descartável

**Data da inspeção:** 2026-11-09

**Classificação:** **CONDITIONAL** — o host Hyper-V está operacional para planejar uma homologação; ainda não há mídia/licença de guest verificada nem espaço livre conservador para um disco Windows 11. Nenhuma VM foi criada ou iniciada.

## 1. Checkpoint e escopo

MVP-11.1 permanece o último checkpoint concluído: **HOST_READY** para iniciar uma homologação controlada. Isso comprova serviços e gerenciamento Hyper-V, não boot de guest nem isolamento. MVP-09.2 continua **PARTIAL**; AppContainer/harness continua experimental e fail-closed. O APL segue supervisionado, sem backend semântico real e sem execução automática de jobs.

Nesta retomada, foi confirmado que este relatório MVP-12 ainda não existia. A inspeção foi somente leitura. Nenhuma mídia foi baixada, nenhuma configuração de host/switch foi alterada e nenhuma VM foi provisionada.

## 2. Baseline do repositório

- Diretório: `C:\Users\criarte\projects\AgentFlow`
- Branch: `feat/intelligent-model-routing-phase-7`
- HEAD: `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`
- Working tree já estava sujo, com modificações rastreadas e arquivos não rastreados relativos à documentação, APL, RB-09C, testes e `autonomous-development-runner/`; foram preservados. Consulte o `git status` desta retomada para a lista completa.
- `.agentflow/` foi inspecionado e preservado. `autonomous-development-runner/` foi preservado.
- Único arquivo criado nesta etapa: `docs/apl-mvp12-disposable-vm-homologation.md`.

Foram lidos `docs/apl.md`, `docs/apl-mvp09-2-adversarial-closure.md`, `docs/apl-mvp10-isolation-architecture-decision.md` e `docs/apl-mvp11-development-environment-recovery.md`. O módulo APL confirma fluxo supervisionado e harness de inferência fail-closed; aprovação, autorização e integração com execução real não são ampliadas por esta homologação.

## 3. Pré-requisitos observados

Verificações locais não destrutivas realizadas nesta retomada:

| Item | Observação | Consequência |
|---|---|---|
| Host | Windows 11 Pro, versão 10.0.26300 | Hyper-V disponível no host; guest ainda não testado |
| CPU | AMD FX-8300, 8 processadores lógicos visíveis | Há capacidade para alocar poucos vCPUs, mas WMI anteriormente reportou SLAT/VM-monitor como falso; a discrepância só será resolvida por boot real controlado |
| RAM | 15,92 GiB total; 11,05 GiB livres no momento da consulta | Proposta inicial de 4 GiB fixos cabe na leitura atual; revalidar imediatamente antes de provisionar |
| Hyper-V | `vmms`, `vmcompute` e `hvhost` em execução; `Get-VMHost` e `Get-VM` funcionam | Camada de gerenciamento disponível |
| Host Hyper-V | 8 processadores lógicos; capacidade reportada ~17,09 GB | Permite propor no máximo 2 vCPU para o ensaio; quota/estabilidade do guest não comprovada |
| VMs existentes | `Get-VM` retornou zero | Nenhuma VM registrada no momento da consulta |
| Versões | cmdlet `Get-VMHostSupportedVersion` lista versões 8.0–12.0 | Geração/versionamento de VM são suportados pelo cmdlet; não provam boot de um guest específico |
| Switch | Existe `Default Switch`, tipo `Internal` | Não será alterado nem conectado à VM; a VM experimental deve ficar sem NIC virtual |
| Disco C: | 96,77 GiB total; 39,17 GiB livres | Insuficiente para reservar conservadoramente um VHDX Windows de 64 GiB mais mídia e margem |
| ISO | Nenhum `.iso` encontrado nos diretórios inspecionados: Downloads do usuário, `C:\ISO`, `C:\ISOs`, `D:\ISO`, `D:\ISOs` | Mídia não identificada; busca não foi uma varredura completa do sistema |
| Licenciamento | Windows Pro do host aparece ativado em registro anterior | Ativação do host não comprova licença de guest. Uma licença ou direito de avaliação do guest deve ser confirmado separadamente |

A documentação Microsoft recomenda Generation 2 quando compatível e lista Windows 11 somente como guest Generation 2. Os requisitos de Windows 11 incluem 4 GB de RAM, armazenamento de 64 GB ou mais, UEFI/Secure Boot e TPM 2.0. A lista Microsoft consultada de CPUs AMD suportadas para Windows 11 não contém `FX-8300`; portanto Windows 11 não é candidato homologado neste host. Não recomendamos contornar os requisitos. Consulte [requisitos do Windows 11](https://learn.microsoft.com/en-us/windows/whats-new/windows-11-requirements), [gerações Hyper-V](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/plan/should-i-create-a-generation-1-or-2-virtual-machine-in-hyper-v) e [CPUs AMD suportadas](https://learn.microsoft.com/en-us/windows-hardware/design/minimum/supported/windows-11-supported-amd-processors).

**Mídia indicada:** nenhuma foi encontrada/validada. Antes do provisionamento, selecionar imagem Microsoft obtida por canal autorizado, conferir edição, hash, licença/termos de avaliação e requisitos da versão guest para Generation 2 e este processador. Não usar mídia de origem desconhecida. O relatório não presume que a licença do host cubra o guest.

## 4. Configuração proposta (não provisionada)

A configuração abaixo é um plano sujeito a autorização explícita de provisionamento e a revalidação dos recursos no momento da execução:

- **Nome:** `AgentFlow-APL-Disposable-<UTC timestamp>` (único; registrar nome e GUID no checkpoint).
- **Geração:** 2, somente após confirmar compatibilidade do guest selecionado. Usar Secure Boot no template exigido/suportado pelo guest. Não selecionar Windows 11 neste host sem uma base oficialmente suportada; não desativar requisitos.
- **CPU/RAM:** 2 vCPU; 4 GiB de RAM estática, sem memória dinâmica para tornar o limite simples de observar. Revalidar RAM livre e deixar margem para o host.
- **Disco:** VHDX temporário em diretório dedicado fora do workspace e fora de `.agentflow/`. Tamanho conforme requisitos da mídia. Para Windows 11, mínimo documental de 64 GiB; reservar pelo menos 80 GiB livres antes de tentar esse perfil. A unidade C: atual não satisfaz essa reserva. Não criar VHDX que possa preencher o volume do host.
- **Rede:** nenhuma NIC virtual conectada e nenhum switch. Não alterar `Default Switch`, firewall, NAT ou switches do host. A ausência de NIC reduz a conectividade do guest, mas deve ser verificada dentro e fora do guest; não prova sozinha ausência de todo canal de rede/integração.
- **Compartilhamento e identidade:** sem pasta compartilhada, mount, workspace, `.agentflow`, perfil do usuário, credenciais, token, clipboard, drive redirecionado, USB ou dispositivo desnecessário. Não habilitar enhanced session. O host tem enhanced session habilitado; não alterar esse ajuste global. Usar conexão básica de console e confirmar ausência de redirecionamento.
- **Integrações/checkpoints:** desabilitar Guest Service Interface e integrações não necessárias, verificando serviço por serviço antes do boot. Desabilitar checkpoints automáticos e não manter checkpoint persistente. Não pressupor que desligar essas integrações substitui a verificação de isolamento.
- **Identidade e acesso:** usuário guest sintético/local sem conta ou credenciais do host; nenhuma credencial operacional do AgentFlow dentro da VM.
- **Descarte:** VM e VHDX ficam em raiz temporária dedicada e identificável. Ao fim, desligar, inspecionar o caminho e a identidade, remover apenas a VM experimental e seu diretório dedicado e verificar inventário, switch, espaço e workspace. Preservar mídia licenciada, se ela não tiver sido copiada para a raiz descartável.

O canal futuro com PlannerAdapter não será habilitado neste MVP-12. Para validar apenas a fronteira, usar dados sintéticos e comunicação manual/local mediada pelo operador; nenhum token ou job operacional será transmitido. Um canal host-guest restrito requer desenho e homologação próprios.

## 5. Plano de homologação

Executar somente após autorização específica para criar/iniciar a VM e depois de resolver mídia/licença/espaço:

1. Capturar inventário inicial: `Get-VM`, `Get-VMSwitch`, capacidade do host, espaço livre, estado Git e hashes/estado dos diretórios protegidos.
2. Validar origem, hash, licença/termos e compatibilidade do ISO. Registrar versão e digest, sem guardar segredos.
3. Criar uma VM com identificador único, Generation 2, 2 vCPU, RAM fixa 4 GiB, VHDX limitado, sem switch/NIC, sem pastas partilhadas e sem checkpoints automáticos.
4. Inspecionar configuração efetiva antes do boot: firmware/Secure Boot, NICs e switches, integração, checkpoints, discos, caminhos, sessão e ausência de redirecionamentos. Se algo diferir do plano, não iniciar.
5. Fazer boot real do guest e registrar código/estado. Se o firmware, CPU, licença ou instalação bloquear o boot, parar sem flexibilizar requisitos; recolher somente logs necessários.
6. Dentro do guest, confirmar CPU/RAM atribuídas e inexistência de adaptador/endereço/rota externa. Tentar alcançar apenas um serviço sintético local controlado pelo host, sem endpoints externos, e classificar permitido/negado/timeout inconclusivo/falha de serviço. Nenhum resultado de timeout será tratado como prova de negação.
7. Criar sentinelas sintéticas no host fora do workspace, com permissões intactas. Verificar que o guest não consegue ler/escrever por compartilhamento; inspecionar também clipboard, drives e Guest Service Interface. Não alterar ACLs de recursos existentes.
8. Verificar credenciais sintéticas isoladas e ausência de credenciais do host no guest. Não usar secrets reais.
9. Testar shutdown, timeout, cancelamento/desligamento forçado em operação sintética, busca de processo/recurso remanescente e reinicialização somente do guest se necessária. O host não será reiniciado sem autorização explícita.
10. Remover VM/VHDX temporários pela identidade/caminho conferidos. Comparar inventário de VMs/switches, espaço em disco, hashes e Git com o baseline; confirmar nenhum artefato no workspace.

Resultados devem separar criação, boot, negação pelo SO, controles apenas configurados, inconclusivos e não executados. Não inferir isolamento completo de uma única negação.

## 6. Comandos previstos (não executados)

Os comandos abaixo são uma referência de implementação PowerShell e requerem revisão/ajuste depois de fixar guest, imagem e volume. **Nenhum deles foi executado.** A primeira autorização deverá permitir especificamente criação e boot de uma única VM de teste; não autoriza baixar mídia, instalar outro software, alterar switch/rede ou habilitar backend APL.

```powershell
# Revalidar recursos e garantir que o nome escolhido não existe.
Get-VMHost
Get-VM
Get-VMSwitch
Get-Volume

# Depois de escolher diretório temporário com espaço livre suficiente:
$name = 'AgentFlow-APL-Disposable-<UTC timestamp>'
$root = '<raiz temporária dedicada e revisada>'
New-VM -Name $name -Generation 2 -NoVHD -Path $root
Set-VMProcessor -VMName $name -Count 2
Set-VMMemory -VMName $name -DynamicMemoryEnabled $false -StartupBytes 4GB
Set-VM -VMName $name -AutomaticCheckpointsEnabled $false
New-VHD -Path (Join-Path $root 'guest.vhdx') -Dynamic -SizeBytes 64GB
Add-VMHardDiskDrive -VMName $name -Path (Join-Path $root 'guest.vhdx')
# Confirmar que não há NIC/switch; NÃO adicionar adaptador de rede.
Get-VMNetworkAdapter -VMName $name
# A associação da mídia bootável só ocorrerá após verificar proveniência e autorização.

# Rollback, após shutdown e confirmação manual do nome e caminho exatos:
Stop-VM -Name $name -TurnOff
Get-VM -Name $name
Get-VMHardDiskDrive -VMName $name
Remove-VM -Name $name -Force
# Remover somente a raiz temporária exclusiva depois de resolver e verificar caminho.
```

O exemplo de 64 GiB é somente para um perfil de guest cuja documentação o exija; tamanho e espaço reservado devem ser definidos pela mídia final. O uso de `-Dynamic` limita o máximo virtual, não garante que o host tenha espaço físico para o crescimento. O plano exige margem livre suficiente antes da criação. O script final deverá remover qualquer NIC gerada por padrão (ou impedir sua criação) e verificar o resultado; `New-VM -NoVHD` não substitui a inspeção da configuração efetiva.

## 7. Plano de rollback e risco

**Rollback esperado:** desligar a VM experimental; verificar nome, GUID, caminho do VHDX e que o diretório é uma raiz exclusiva e recém-criada fora do repositório; remover somente esses recursos; confirmar `Get-VM` e volume; verificar que switches/VMs preexistentes, workspace, `.agentflow/`, `autonomous-development-runner/` e Git não mudaram. Não usar `Remove-VM` ou remoção recursiva em caminho calculado sem validação explícita do caminho absoluto. Se houver dúvida sobre propriedade de um arquivo, preservá-lo e parar.

Riscos e lacunas atuais:

- Não houve criação/boot real; guest isolation continua não comprovado.
- O FX-8300 não consta na lista de CPUs AMD suportadas consultada para Windows 11; evitar esse guest nesta máquina.
- `VirtualMachineMonitorModeExtensions` e SLAT reportaram valores WMI inconsistentes no MVP-11.1; boot é experimento discriminante, não garantia.
- 39,17 GiB livres em C: não cobrem conservadoramente o VHD de 64 GiB mais ISO e margem. Não provisionar nessa unidade com a configuração de Windows 11.
- Mídia e direito de uso do guest não identificados; ativação do host não supre essa lacuna.
- A presença de um `Default Switch` interno no host não comprova a política de rede do guest; não conectar a VM a ele.
- Quotas, ausência de canal lateral, clipboard, credenciais, rede, recuperação e limpeza ainda exigem evidências observáveis.
- A VM descartável reduz exposição, mas não constitui certificação formal de isolamento ou aprovação para inferência.
- Ambiente Python separado: `.venv` permanece em Python 3.13.17 sem `cryptography`; `pyproject.toml` declara `cryptography>=46`. Correção reproduzível já declarada pelo projeto é `\.venv\Scripts\python.exe -m pip install -e ".[test]"`, mas não foi executada. A suíte completa não está homologada neste ambiente.

## 8. Pendências e próxima ação

**Classificação final: CONDITIONAL.** A camada Hyper-V do host está pronta para uma tentativa de homologação, mas não há combinação atualmente verificada de guest suportado, mídia licenciada e espaço de armazenamento suficiente. A WMI/SLAT discrepante também deixa risco de criação/boot.

Ação necessária antes do provisionamento:

1. Disponibilizar mídia Windows genuína por canal autorizado, indicar o guest/edição e comprovar licença ou termos de avaliação aplicáveis a uma VM descartável. Confirmar suporte do guest ao FX-8300 e Generation 2. Se isso não for possível, escolher um host suportado; não contornar requisitos do Windows 11.
2. Disponibilizar diretório/volume temporário com espaço físico livre conservador (para perfil Windows 11, pelo menos 80 GiB, além de revalidar os demais recursos) sem tocar no workspace.
3. Conceder autorização explícita específica para criar e iniciar uma única VM descartável com configuração revisada. Esta documentação não é essa autorização e não provisiona automaticamente.

Após esses itens, a próxima etapa autorizável é um ensaio mínimo de criação e boot de guest sem rede, dados sintéticos e descarte controlado. Até lá, manter RestrictedInferenceHarness fail-closed, APL supervisionado, backend semântico indisponível e nenhum job real.
