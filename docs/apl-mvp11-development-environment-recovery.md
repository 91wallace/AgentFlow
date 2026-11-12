# AgentFlow APL MVP-11 — Development Environment Recovery

**Data:** 2026-11-09
**Checkpoint:** Hyper-V feature enabled, reboot pending; system intentionally not restarted.
**Parecer de preparação:** **PARTIAL** — feature registered; active Hyper-V/VM host capability still needs post-reboot verification.

## 1. Baseline do repositório

- Branch: `feat/intelligent-model-routing-phase-7`.
- HEAD: `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`.
- Último checkpoint do APL: MVP-09.2 **PARTIAL**. MVP-10 **CONDITIONAL**, with disposable isolated Windows VM recommended for future investigation.
- MVP-11 report did not exist before this continuation. It is the only file created by this step.
- The working tree already contained user changes in architecture/phase documentation, `src/agentflow/cli.py`, APL docs/code/tests, RB-09C notes and `autonomous-development-runner/`. All were preserved. No `.agentflow/` data was altered.
- No commit, push, merge, rebase, reset, clean or branch synchronization occurred.

## 2. Windows compatibility and Hyper-V activation

Read-only environment checks:

- Windows 10 Pro, edition `Professional`, version `2009`, image build `26300` (boot entry labels the OS Windows 11); x64.
- Processor: AMD FX-8300, 64-bit. `VirtualizationFirmwareEnabled=True`; WMI reported `VMMonitorModeExtensions=False` and `SecondLevelAddressTranslationExtensions=False` while Windows also reported an active hypervisor. VBS status was `2` (running), security service `{2}`. Because the hypervisor is already active, `systeminfo` says its Hyper-V requirements are not displayed. The existing status does not prove that a new guest can be created; verify after the required restart.
- AMD's Family 15h FX processor data sheet lists AMD-V and Rapid Virtualization Indexing/nested paging. Microsoft requires a supported Pro edition, 64-bit CPU, SLAT, VM monitor extensions, firmware virtualization and adequate memory for Hyper-V. The observed WMI/hypervisor reporting mismatch is retained as a post-reboot verification item; no firmware/BIOS setting was changed. [Microsoft Hyper-V requirements](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/system-requirements-for-hyper-v-on-windows?pivots=windows), [AMD Family 15h FX processor data sheet](https://www.amd.com/content/dam/amd/en/documents/archived-tech-docs/datasheets/49686_15h_Mod_00h-0Fh_FX-Series_PDS.pdf).
- The current token is elevated and in Administrators. DISM completed the authorized command successfully:

  ```powershell
  dism.exe /Online /Enable-Feature /FeatureName:Microsoft-Hyper-V-All /All /NoRestart
  ```

- `Microsoft-Hyper-V-All`: **Enabled**. `CBS RebootPending=True` after the operation. `WindowsUpdate RebootRequired=False` at check time. The computer was not restarted.
- The PowerShell `Enable-WindowsOptionalFeature` path failed with “Classe não registrada” before changing the feature; DISM was used instead. No partial PowerShell activation occurred.
- `vmms` was not returned as an available service in the post-operation query; `hvhost` was Running/Manual. This is not proof the hypervisor/VM management plane is ready before reboot.
- Windows Sandbox remains disabled. The activation did not enable Sandbox, change global security settings, or alter BIOS/UEFI.

Hyper-V may affect compatibility/performance of third-party virtualization products when its hypervisor is active. No third-party virtualization product was configured or disabled. Prior PATH checks found no Docker/VMware/VirtualBox management CLI, which is not an exhaustive software inventory.

## 3. Checkpoint before restart — STOP HERE

**A restart is pending. The system was not restarted automatically.**

After the user restarts Windows, resume with read-only verification first:

1. Confirm DISM reports `Microsoft-Hyper-V-All` Enabled and query Hyper-V requirements/VMMS again.
2. Confirm `HypervisorPresent`, `systeminfo` requirements, processor virtualization/SLAT exposure and adequate RAM/disk. If any required feature is unavailable, do not provision a VM.
3. Confirm no unexpected WSL or third-party virtualization regression. Do not change or disable an existing component to force compatibility.
4. Confirm APL remains supervision-only and `RestrictedInferenceHarness.probe()` reports backend unavailable.

The outstanding restart is the next user action. No `Restart-Computer`, shutdown or boot configuration modification was issued.

## 4. WSL state

- DISM `Microsoft-Windows-Subsystem-Linux`: **Enabled**.
- DISM `VirtualMachinePlatform`: **Disabled**.
- `wsl --status` and `wsl --list --verbose`: report that WSL is not installed; there is no configured WSL runtime/kernel or installed distribution visible to WSL.
- Hypervisor Platform: **Disabled**. No WSL distribution was installed and no project/workspace integration was configured.

Thus the optional WSL component is enabled, matching the stated baseline, but usable WSL/WSL 2 is not configured. Enabling Hyper-V does not by itself install WSL runtime/kernel or a Linux distribution. WSL is not treated as a sufficient security boundary for the planner.

## 5. Python environment

`.venv\Scripts\python.exe` is Python 3.13.17 and excludes global site-packages. `pyproject.toml` declares runtime dependency `cryptography>=46` and test extra `pytest>=8,<10`. The selected environment has pytest 9.2.0 but `importlib.util.find_spec("cryptography")` returns `None`.

`pip check` reports no broken requirements, but installed editable AgentFlow metadata lists only the `pytest` extra and does not list the runtime `cryptography` dependency. This metadata is inconsistent with the source `pyproject.toml`; consequently `pip check` is not evidence that the declared project runtime dependencies are installed. The prior nine collection failures in approval/recovery/TEG tests remain attributable to `ModuleNotFoundError` at `cryptography.exceptions`. The full suite is not homologated in `.venv`.

Reproducible correction, **not executed**:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

This asks pip to install the dependencies declared by the editable project and test extra and refreshes editable metadata. No packages were installed or updated during MVP-11.

## 6. VM preparation specification (not provisioned)

Once Hyper-V is active and a separate VM provisioning step is authorized:

- Use a disposable, patched Windows guest with a separate standard local identity. Do not enroll it into the host profile or copy host credentials/tokens.
- Do not attach the workspace, `.agentflow`, user profile, executor stores, shared folders, host drives or clipboard. Disable enhanced session features, device redirection and guest services that are not explicitly required.
- Default to no virtual NIC. If the APL must exchange requests, design a single authenticated, bounded host↔guest channel separately; permit only that channel and prove that it cannot route outward. Do not use a shared project folder as the channel.
- Set fixed guest memory, vCPU count and bounded virtual disk; record these values and validate effective ceilings under load. Use no dynamic expansion until quota behavior is verified.
- Keep the APL store and audit history on the host. Send only a versioned, size-limited `ReplanContext` snapshot; accept only bounded JSON and pass it through the existing decoder, scope validator and supervised acceptance flow. Never pass commands, host paths, credentials or executable objects.
- Use a clean baseline checkpoint and discard/revert the guest after a cycle; persist only approved audit metadata on the host. Timeout/cancel must stop the VM/worker and confirm no residual process. Unknown outcomes must not trigger automatic retry.
- No VM, virtual switch, disk, guest, model or provider was created or started in MVP-11.

Hyper-V activation is a platform prerequisite only. It does not establish guest isolation, network denial, quota enforcement, recoverability or APL authorization.

## 7. Security and next tests

The APL remains supervised; `RestrictedInferenceHarness` remains fail-closed. Broker/TEG remain blocked, RB-09C is unchanged, and no real job/provider/model was run.

After the user completes the pending restart, first verify Hyper-V is operational. A later, separately authorized VM preparation stage should then review VM policy before any creation, followed by tests for host-file/credential denial, no shared devices, network denial and sole-channel behavior, hard CPU/memory/disk ceilings, timeout/cancel/reset, process-tree cleanup, reboot/revert recovery, malformed/oversized response, and no automatic execution/retry. The AppContainer MVP-09.2 evidence remains **PARTIAL** and is not replaced by Hyper-V activation.

## 8. Estado final

- Hyper-V feature activation: **successfully registered; reboot pending**.
- Hypervisor/VM runtime: **not yet verified after restart**.
- WSL: optional component enabled; runtime and distributions absent; WSL 2 prerequisites not ready (`VirtualMachinePlatform` disabled).
- `.venv`: `cryptography` missing; suite not homologated; no installation performed.
- Git: branch `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`; existing local changes preserved. Only this report was added by MVP-11.
- Next step: user restarts Windows, then the session performs read-only Hyper-V/WSL verification. No VM provisioning follows automatically.

## 9. MVP-11.1 — validação pós-reinicialização

**Atualização:** a reinicialização ocorreu em `2026-11-09 10:28:22` (LastBootUpTime). As consultas abaixo foram feitas depois dela e são somente leitura. Esta seção substitui o estado “reboot pending” das seções históricas acima.

### Hyper-V operacional

- DISM confirma `Microsoft-Hyper-V-All`: **Enabled**. `CBS RebootPending=False` depois do boot.
- `Win32_ComputerSystem.HypervisorPresent=True`; `systeminfo` informa **“Hipervisor detectado”**. VBS continua em status `2` (running), serviço `{2}`.
- Serviços: `vmms` **Running/Automatic**, `vmcompute` **Running/Manual**, `hvhost` **Running/Manual**.
- Cmdlets de gerenciamento disponíveis pelo módulo Hyper-V: `Get-VMHost` e `Get-VM` executaram. `Get-VMHost` retornou computador `PECE`, 8 processadores lógicos, capacidade de memória ~17,09 GB, caminho padrão de VMs `C:\ProgramData\Microsoft\Windows\Hyper-V` e VHDs `C:\ProgramData\Microsoft\Windows\Hyper-V\Virtual Hard Disks`. A unidade C: tinha ~39,20 GiB livres. `Get-VM` retornou zero VMs registradas.
- Desde o boot, logs Hyper-V-Hypervisor-Admin, Compute-Admin e Worker-Admin não registraram erros/avisos de nível 1–3. VMMS-Admin registrou um aviso 12520: o VMMS gerou automaticamente certificado autoassinado para autenticação de servidor. VMMS-Operational e VmSwitch-Operational não registraram erros críticos desde o boot. Esse aviso foi observado, não alterado; nenhuma VM ou função de replicação foi configurada.
- Não foi executada tentativa de criação/início de VM, conforme escopo. O `Get-VMHost` funcional, serviços de gerenciamento em execução, hypervisor presente, recursos disponíveis e ausência de erros críticos fornecem evidência suficiente para iniciar uma homologação controlada de VM; não comprovam que a VM isolará os recursos requeridos.

### Discrepância AMD/WMI

Depois do boot, o AMD FX-8300 continua reportando `VirtualizationFirmwareEnabled=True`, mas `VMMonitorModeExtensions=False` e `SecondLevelAddressTranslationExtensions=False`. Ao mesmo tempo, `HypervisorPresent=True`, VBS está rodando, `systeminfo` detecta hypervisor e `Get-VMHost` funciona. A documentação AMD da família FX15h lista AMD-V e Rapid Virtualization Indexing (nested paging/RVI); os requisitos Microsoft para instalar/usar Hyper-V pedem SLAT, extensões VM Monitor, virtualização de firmware habilitada e RAM suficiente (referências oficiais já registradas na seção 2).

Interpretação conservadora: as propriedades WMI de capacidade não são confiáveis neste estado com hypervisor/VBS já ativo; `systeminfo` explicitamente omite a lista quando detecta hypervisor. O funcionamento dos cmdlets e serviços comprova a camada de gerenciamento do host, mas, sem criar uma VM, a capacidade de boot de guest ainda não foi exercitada. Não alteramos BIOS/UEFI nem configurações de VBS. A discrepância fica como limitação a observar durante o próximo experimento; se a criação/inicialização controlada falhar com requisito de SLAT/VM monitor, parar e reavaliar por host compatível, sem forçar flags.

### WSL pós-boot

- `Microsoft-Windows-Subsystem-Linux`: **Enabled**.
- `VirtualMachinePlatform`: **Disabled**.
- `wsl --version`, `wsl --status` e `wsl --list --verbose` reportam que o runtime WSL não está instalado; nenhuma distribuição existe.
- Portanto WSL 2 segue indisponível/não configurado. Hyper-V operacional não instalou nem configurou runtime, kernel ou distribuição WSL.

### Python pós-boot

- `.venv` continua em Python 3.13.17; pytest 9.2.0 instalado; `cryptography` continua ausente (`find_spec` retorna `None`).
- `pyproject.toml` declara `cryptography>=46`; a metadata editable instalada lista apenas o extra pytest. `pip check` diz “No broken requirements found”, mas não detecta essa divergência na metadata; a suíte completa continua não homologada.
- Nenhum pacote foi instalado. Correção recomendada e ainda não executada permanece: `.\.venv\Scripts\python.exe -m pip install -e ".[test]"`, depois `pip check` e `pytest -q` no mesmo ambiente.

### Decisão e próximo passo

**Classificação: HOST_READY.** Há evidência suficiente de que o hypervisor e a camada de gerenciamento Hyper-V estão operacionais para iniciar a próxima homologação controlada. Isto não significa que guest isolation, rede negada, ausência de compartilhamentos, quotas ou recuperação estejam homologados. `RestrictedInferenceHarness.probe()` continua com backend `unavailable` e 11 controles obrigatórios faltando.

Próximo experimento mínimo recomendado, sujeito à autorização específica já exigida para provisionamento: criar uma única VM Windows descartável, com geração suportada, NIC ausente por padrão, sem checkpoints automáticos de produção, sem enhanced session/clipboard/drives/pastas compartilhadas e com vCPU, memória fixa e VHD limitados. Antes de carregar qualquer runtime/modelo, validar inicialização do guest, acesso negado a sentinelas do host, inexistência de integração de arquivos/credenciais, canal de resposta isolado, limites, desligamento e descarte. Não iniciar esse experimento automaticamente; esta etapa não criou nem iniciou VM.

Git permanece na branch `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`; alterações locais e arquivos não rastreados foram preservados. Esta atualização só alterou este relatório. Nenhum commit ou operação de sincronização ocorreu.
