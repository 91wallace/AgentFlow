# MVP-19.5 — Integração CLI e homologação de consentimento local

## Estado

**PARTIAL** — o comando e a composição da autoridade foram integrados e exercitados com stores SQLite reais e proposta sintética persistida. Não houve enrollment nem assinatura de consentimento por uma pessoa nesta execução. Dispatch continua desabilitado.

## CLI

O novo comando é:

```powershell
agentflow apl handoff-review --path <projeto> --cycle-id <id> --proposal-id <id>
```

Ele requer um terminal interativo, perfil explicitamente selecionado `LOCAL_SUPERVISED`, State Store existente, credencial ativa do SID/UID atual e `.agentflow/local-consent.toml` válida. Não recebe objetivo, digest, workspace, operações ou limites na linha de comando. Resolve esses dados pelo APL State Store, vínculo de handoff, Broker snapshot e journal. Confere o workspace real e o estado atual; propostas inexistentes, expiradas, canceladas, obsoletas, divergentes ou já decididas não voltam a ser apresentadas como aprováveis.

O arquivo local de política é explícito e contém as allowlists e limites confiáveis usados pelo resolver. Exemplo:

```toml
schema_version = 1
broker_database = ".agentflow-broker.sqlite3" # BrokerStore rejeita bancos dentro de .agentflow/
project_id = "project-local"
authorized_project_ids = ["project-local"]
proposer_principal = "apl-local"
workspace_root = "."
max_jobs = 3
max_iterations = 2
max_timeout_seconds = 120
max_estimated_budget = "2.00"
budget_currency = "USD"
authorized_consumer_principals = ["local-executor"]
consent_ttl_seconds = 90
max_grant_ttl_seconds = 90
```

Paths relativos são resolvidos a partir da raiz Git do projeto. Configuração ausente, campos extras, stores iguais, workspace indisponível, escopo inconsistente ou limites inválidos bloqueiam a revisão.

O caminho é: resolver autoritativamente → apresentar objetivo e campos persistidos → `approve`, `reject` ou `cancel` → confirmar `APROVAR/REJEITAR <prefixo do digest>` → senha oculta → verificar assinatura do nonce persistido → persistir decisão e, para aprovação, grant local assinado no Broker/journal. O signer usa a chave Ed25519 derivada da credencial já cadastrada, mantém a chave privada somente na sessão da operação e a limpa após o resultado. A resposta mostra decisão, IDs, estado e `dispatch_enabled=false`, sem revelar senha, chave ou grant completo.

Uma segunda execução de `handoff-review` para a mesma decisão devolve o registro persistido sem pedir nova aprovação ou senha. Isso permite conferir recuperação após reiniciar o processo. Revogação dos grants continua limitada aos contratos MVP-17 existentes; esta CLI ainda não oferece um comando de revogação de grants.

## Cadastro e revisão manual

Cadastro e revogação de credencial seguem os comandos do MVP-19.4 e exigem TTY:

```powershell
agentflow local-auth enroll --path <projeto> --confirm-bootstrap
agentflow local-auth status --path <projeto>
agentflow apl handoff-review --path <projeto> --cycle-id <id> --proposal-id <id>
```

O enrollment solicita senha duas vezes via `getpass`. A revisão pede a decisão, a confirmação digitada vinculada ao digest e novamente a senha oculta. Nenhum comando aceita senha em argumento, arquivo, variável de ambiente ou stdin redirecionado.

## Ambiente sintético preparado

Foi adicionado `experiments/prepare_mvp19_5_consent_demo.py`. Ele cria um projeto Git temporário novo, APL e Broker SQLite separados, workspace sintético, handoff e snapshot pending. Não chama provider, não cadastra credencial e não executa job. O ambiente anterior (`AgentFlow-MVP19.5-consent-demo-2`) foi preservado, mas sua janela curta antiga expirou. O novo ambiente preparado está em:

`%TEMP%\AgentFlow-MVP19.5-consent-demo-20261112-160713-101`

IDs: cycle `cycle-mvp19-5-97706f438b694618`; proposal `ab5935f9f04a7ff0bf1b23922324a6ab`. `local-auth status` confirmou `credential_enrolled=false`. Nenhum consentimento foi iniciado nesta preparação: não há request binding nem desafio criado. O handoff está `pending` nos dois stores, com digest e revisão vinculados; o Broker recebeu a proposta em `2026-11-12T16:07:14.751671Z`, e sua expiração de sete dias é `2026-11-19T16:07:14.751671Z`. A expiração do Broker foi verificada sem alterar o relógio do sistema.

### Correção `proposal_expired`

Na demo anterior, o evento durable `snapshot.proposed` ocorreu em `2026-11-12T13:31:41.656310Z`. A política Broker dá sete dias à proposta pending, mas o resolver também calculava a janela curta de consentimento de 90 segundos desde esse evento. Assim, a janela terminou em `13:33:11.656310Z`, embora a proposta continuasse pending no Broker. Quando o proprietário tentou revisar mais tarde, o resolver corretamente retornou `proposal_expired` conforme aquele prazo curto.

A janela de consentimento agora começa na primeira resolução autoritativa, limitada pela expiração de sete dias do Broker. Ela é persistida por proposta e não se renova em reaberturas; após expirar, a mesma proposta continua bloqueada. A nova demo usa o máximo de 120 segundos já aceito pelo contrato local. A proposta antiga e o primeiro diretório incompleto foram preservados, sem edição ou reutilização. Cada execução do helper gera ciclo e ID de replanejamento novos, portanto também um proposal ID novo.

Comandos da demo preparada (a primeira cerimônia humana já foi concluída):

```powershell
$demo = "$env:TEMP\AgentFlow-MVP19.5-consent-demo-20261112-160713-101"
.\.venv\Scripts\agentflow.exe local-auth enroll --path $demo --confirm-bootstrap
.\.venv\Scripts\agentflow.exe apl handoff-review --path $demo --cycle-id cycle-mvp19-5-97706f438b694618 --proposal-id ab5935f9f04a7ff0bf1b23922324a6ab
.\.venv\Scripts\agentflow.exe apl handoff-review --path $demo --cycle-id cycle-mvp19-5-97706f438b694618 --proposal-id ab5935f9f04a7ff0bf1b23922324a6ab
```

O cadastro foi feito pelo proprietário e a aprovação foi autenticada. O grant foi emitido sem conectar dispatch. A segunda chamada ocorreu após os vencimentos e retornou `Expired` antes de consultar a decisão já persistida.

### Recuperação da decisão autenticada

Inspeção SQLite somente leitura confirmou a decisão `local-decision-432a215db477639bd8c1399de261ed92` e o grant `local-grant-a2dc83a2da2b42238e9cf14b38c1dd9b`. A decisão foi registrada às `2026-11-12T16:59:26.568397Z`, com ação explícita e proof digest, antes da expiração da janela em `17:00:25.699549Z`. O digest do registro da decisão e a assinatura do grant foram verificados; ambos apontam para o mesmo request digest. Há uma decisão e um grant, sem duplicação. As tabelas de execução continuam vazias.

O grant expirou em `2026-11-12T17:00:25.699549Z`. O status persistido da projeção ainda era `approved`; pela validade temporal, seu estado efetivo agora é `expired`. A proposta Broker permanece `pending` até `2026-11-19T16:07:14.751671Z`. Assim, os três prazos são distintos: janela de consentimento e grant já venceram; a validade pending da proposta ainda não.

A causa da segunda falha era a ordem do caminho da CLI: `review_persisted_handoff` chamava o resolver estrito antes de buscar a decisão existente. O resolver lançava `proposal_expired`, impedindo a recuperação histórica. Agora o modo de consulta histórica revalida os vínculos APL/Broker, consulta primeiro a decisão persistida, valida sua integridade e a assinatura/binding do grant, e calcula `expired` sem alterar registros. Não solicita confirmação nem senha e não emite outro grant. Sem decisão anterior, propostas com janela ou validade Broker vencida continuam rejeitadas. A validação de uso continua negando grants expirados ou revogados.

A segunda consulta pela interface terminal após esta correção não foi executada: a criação de PTY foi negada pelo Windows (`Acesso negado`) e a inicialização de PowerShell pela automação de UI foi bloqueada por política do sistema. Os testes de regressão cobrem o caminho no serviço e confirmam que nenhum prompt, desafio, emissão ou mutação ocorre.

Para conferir a recuperação no terminal do proprietário, execute a mesma consulta; ela não deve pedir senha:

```powershell
$demo = "$env:TEMP\AgentFlow-MVP19.5-consent-demo-20261112-160713-101"
.\.venv\Scripts\agentflow.exe apl handoff-review --path $demo --cycle-id cycle-mvp19-5-97706f438b694618 --proposal-id ab5935f9f04a7ff0bf1b23922324a6ab
```

## Testes e evidências automatizadas

Os testes usam fixtures de operador e stores SQLite temporários. Cobrem resolução APL/Broker, workspace autoritativo, proposta inexistente, consentimento aprovado/cancelado, senha errada, digest e nonce, replay, revogação, expiração, recuperação do registro após reabrir Broker SQLite, assinatura do grant e ausência de linhas em `execution_authorizations` e `execution_receipts`. Fixtures não contam como autenticação humana operacional.

O teste de modo POSIX é ignorado no Windows porque bits Unix `0600` não modelam ACL NTFS. A proteção de arquivo Linux continua verificável no ambiente Linux; ACLs Windows não foram auditadas nesta etapa. O CLI exige TTY e o teste manual confirmou a recusa com stdin/stdout não interativos.

## Revisão de segurança

- scrypt usa N=32768, r=8, p=1, sal aleatório de 16 bytes e ~32 MiB por derivação. Isso encarece tentativa offline, mas credenciais fracas ainda podem ser adivinhadas contra a chave pública; use frase-senha longa e exclusiva.
- SQLite persiste sal e chave pública; a chave privada é derivada na sessão e não gravada. No Linux, a base é restringida para modo `0600`. No Windows, a base usa as ACLs herdadas; o comando não altera ACLs e a revisão dessas ACLs é pendente.
- O principal vem do SID do token Windows ou UID efetivo Linux, não de claim do APL.
- O modo local confia no SO e no proprietário da conta. Não protege contra administrador comprometido, outro processo malicioso sob a mesma conta, agente capaz de alterar o banco/configuração local, terminal controlado ou modificação do executável. Não representa independência administrativa.
- O serviço local de aprovação permanece uma composição explícita por comando. A presença do signer/grant não conecta executor nem autoriza dispatch.

## Homologação restante

1. Proprietário executa uma segunda consulta no terminal para confirmar o resultado `decision=approve`, `state=expired`, mesmo decision/grant ID, sem prompt e com `dispatch_enabled=false`.
2. Rodar ACL inspection no Windows e repetir cadastro/revisão em Linux antes de declarar suporte operacional multiplataforma.
3. Implementar CLI de revogação de grant antes de oferecer ciclo de vida operacional completo.

Os testes de regressão incluem proposta pending cuja criação antecede a janela curta de consentimento, imutabilidade do prazo após primeira revisão e proposta Broker expirada. O primeiro caso continua elegível antes do TTL de sete dias; o último permanece não aprovável.

Resultado desta etapa: **PARTIAL**. A homologação operacional depende da interação explícita do proprietário, e o isolamento contra a mesma conta permanece fora das garantias do perfil local.
