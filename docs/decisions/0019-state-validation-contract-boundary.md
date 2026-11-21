# ADR 0019 — M2.1: remover o ciclo state/validation

Status: primeira fatia de M2 autorizada pelo proprietário em 2026-11-20,
após [M1](0018-lean-facades-local-import-isolation.md). M1 foi consolidado
separadamente no commit `59007a4`.

## Problema e direção

`state.validation` implementa persistência sobre os contratos de validação.
O caminho inverso existia em `validation.scheduler`: importava erros de
`state.contracts` e anotava seu store com a classe concreta de persistência,
importada sob `TYPE_CHECKING`. Imports tardios não resolveriam essa dependência.

Direção adotada:

```text
state.validation -> validation.contracts <- validation.scheduler
state.contracts -> contracts.state_errors <- validation.scheduler
```

O pacote `contracts` tem inicializador vazio de implementações. Seu primeiro
módulo contém exclusivamente a família de erros de operações de estado:
`StateError`, `IntegrityError`, `ConflictError` e `TransitionError`.
Não é um repositório genérico para transferir modelos de outros domínios.

## Contratos e compatibilidade

- `state` e `state.contracts` reexportam as mesmas classes de erro do novo módulo.
  Identidade entre aliases, hierarquia, mensagens e captura por `except` são
  preservadas. O módulo de definição passa a `contracts.state_errors`; pickles
  antigos que referenciam os caminhos anteriores continuam resolvendo os aliases.
- `ValidationReservation` passa a `validation.contracts`, mantendo campos,
  defaults e imutabilidade. `state.validation.ValidationReservation` permanece
  alias da mesma classe. Não é formato persistido no SQLite e não muda os digests.
- `ValidationStore` é um protocolo estrutural em `validation.contracts` com
  as operações já consumidas pelo scheduler. A implementação SQLite continua
  em `state.validation`, sem nova classe base ou obrigatoriedade de herança.
- O scheduler não importa `state`, nem mesmo para tipagem. O protocolo fica
  disponível para introspecção de anotações sem carregar o store concreto.
- `ValidationStateStore` permanece disponível em `state.__init__` e no módulo
  original: esse reexport pertence à própria implementação de estado e não
  cria a dependência inversa que foi removida. Não quebramos usuários desse import.

O scheduler ainda contém consultas SQL de quota. O protocolo explicita a
superfície mínima `db.execute(...).fetchone()` por interfaces estruturais,
sem importar SQLite. Remover SQL da coordenação será uma extração própria;
esta fatia não afirma independência de linguagem de consulta ou de schema.

## Invariantes

Nenhum método da implementação SQLite muda de corpo. Schemas, migrações,
conexões, unidades transacionais, digests, operações idempotentes, fencing,
concorrência e recuperação conservadora permanecem. O scheduler mantém seu
fluxo e suas consultas. Não há autenticação nova, instalação de dependências,
inferência real, Docker real, commit de M2 ou push nesta fatia.

## Critérios de saída

- AST não encontra imports de `state` em nenhum módulo de `validation`,
  incluindo imports para tipagem. O componente cíclico `state/validation` some.
- Remover somente as duas exceções obsoletas do baseline: 28 → 26.
  `contracts.state_errors` entra na lista de contratos puros protegidos pelas
  regras existentes; nenhuma exceção nova é adicionada.
- Processo novo bloqueia `state`, SQLite e criptografia: validação importa,
  resolve a anotação do scheduler e usa store em memória para retomada de
  resultado terminal e recuperação, sem carregar persistência concreta.
- Testes confirmam aliases, hierarquia dos erros, defaults da reserva e leitura
  de referências pickle antigas. Suítes existentes cobrem persistência,
  transições, conflitos concorrentes, gates, evidências, quotas e recuperação.
- Testes M1 e suíte completa continuam passando.

As próximas fatias de M2 são `approval/security` e `apl/security`, preservando
serialização e transações. M3/M4 continuam no roadmap da ADR 0017.

## Resultado da validação

Windows/Python 3.13: 17 testes da nova fronteira/arquitetura passaram após a
redução do baseline. Na primeira rodada de contratos/schema/scheduler, os 22
testes comportamentais passaram; os dois testes de baseline apontaram exatamente
as exceções que haviam se tornado obsoletas, posteriormente removidas.
Suíte completa final: **792 aprovados e 1 ignorado**, em 551,55 segundos.

Comparação AST com o commit M1 confirma os 13 métodos de `ValidationStateStore`
idênticos, incluindo consultas, transações e mensagens. `git diff --check`
passou. M1 está no commit `59007a4`; M2.1 permanece no working tree, sem push.
