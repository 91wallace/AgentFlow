# ADR 0020 — M2.2: remover o ciclo approval/security

Status: segunda fatia de M2 autorizada pelo proprietário em 2026-11-20,
após [M2.1](0019-state-validation-contract-boundary.md). As alterações anteriores
do working tree foram preservadas; esta etapa não cria commits ou push.

## Problema e direção

`approval.persistence` importava DTOs, enums e validadores de `security` dentro
de oito pontos do store. Em sentido contrário, serviços de segurança usavam
o store e WebAuthn de approval. Mudar apenas imports para dentro de funções
não quebraria esse ciclo nem separaria dados de serviços.

Direção adotada: `security → approval`; approval não importa security ou APL,
nem para tipagem. O componente fortemente conexo passa de
`{apl, approval, security}` para `{apl, security}`. A dependência APL/security
continua explícita para M2.3.

## Contratos extraídos

- `approval.consent_contracts`: erros/enums, limites, pedidos, recibos e binding
  de execução, com suas validações e digests. Configuração TOML, arquivos e
  diagnóstico permanecem em `security.profile`.
- `approval.local_grants`: `LocalGrant`, `LocalConsentAuditRecord`, erro de
  autorização, protocolo de verificação e helpers exatos de serialização,
  tempo e validação. Signers criptográficos e serviços da autoridade permanecem
  em `security.authorization`.
- `approval.contracts` reexporta os DTOs, `ConsentDecision` e os validadores
  públicos `validate_grant_id`/`validate_grant_digest` usados pelo store.
  Helpers privados de security continuam aliases das mesmas funções.

Os contratos têm somente dependências da biblioteca padrão e entre si.
Não carregam crypto, SQLite, configuração, serviços ou implementações de agentes.
Os dois módulos entram na lista de contratos puros do teste arquitetural.

## Compatibilidade e invariantes

- Os nomes em `security`, `security.profile` e `security.authorization` são
  preservados como aliases. Identidade entre imports, enums usados por `is`,
  hierarquia de erros e leitura de referências pickle antigas permanecem.
  O módulo de definição dos tipos extraídos passa a approval.
- `_id`/`_digest` de grant mantêm regex, tipos, mensagens e `LocalAuthorizationError`.
  Os validadores de perfil continuam distintos, usando `ProfileConfigurationError`.
  Helpers de nomes iguais não são unificados.
- Payloads, JSON canônico, SHA-256, schema de grant v1/v2, binding, IDs,
  assinaturas e eventos mantêm o formato. Vetores congelados de ambos os schemas
  foram gerados a partir dos fontes anteriores à extração, sem execução de agente
  ou acesso à rede; a assinatura determinística desses vetores é exclusivamente
  de teste. Os testes existentes verificam Ed25519 com chaves efêmeras reais.
- No store só mudam destinos dos imports. Consultas, transações, locks, decisões
  append-only, CAS, rollback, consumo único, integridade e recuperação permanecem.
  Não há migração, mudança de versão de schema ou divisão de conexão/transação.
- Nenhuma autenticação é reativada no fluxo local. A dependência de instalação
  de cryptography permanece para M4; o BrokerStore avançado ainda usa WebAuthn.

## Critérios de saída

1. AST encontra zero imports de security/APL em approval, inclusive para tipagem;
   approval não participa de ciclos. Remover somente nove exceções obsoletas do
   baseline (26 → 17), incluindo arestas que deixaram de integrar o mesmo
   componente cíclico. Imports local → pilha avançada não são ocultados.
2. Processo novo bloqueia security, APL, SQLite e crypto e carrega os contratos.
3. Outro processo bloqueia security/APL e persiste grants v1/v2, reabre o banco,
   lê decisões, consome grants e verifica integridade do journal sem carregar
   serviços. Esse teste permite crypto, exigida pelo store avançado atual.
4. Vetores anteriores preservam bytes de JSON, hashes de pedidos/Unicode,
   assinatura determinística e binding v2. Aliases/pickle e erros distintos
   continuam cobertos. O inventário M1 dos 213 exports continua passando.
5. Testes existentes de autorização, rejeição, replay, concorrência, expiração,
   revogação e rollback passam; regressões locais e suíte completa permanecem.

Próxima fatia: M2.3, APL/security. Extrações de serviços/monólitos e empacotamento
continuam sendo M3/M4, fora desta entrega.

## Resultado da validação

Windows/Python 3.13: 40 testes focados de autorização/perfil/exports e 18 testes
da nova fronteira/arquitetura passaram. Suíte completa final:
**800 aprovados e 1 ignorado**, em 605,49 segundos.

Comparação AST com os fontes capturados antes de M2.2 confirma que todas as
instruções da persistência permanecem idênticas, exceto destinos dos imports.
As 15 definições de consentimento e 13 de grants extraídas mantêm seus corpos;
a única adaptação interna é o destino do import do binding de execução.
Os vetores de JSON/hash vêm dos fontes anteriores, não de expectativas geradas
pela implementação nova durante pytest. `git diff --check` passou.

M2.1 e M2.2 permanecem no working tree. Nenhum commit, push, instalação de
dependências ou inferência real foi realizado nesta etapa.
