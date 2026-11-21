# ADR 0023 — M3.1: identidade canônica de execução APL

Status: primeira fatia de M3 autorizada em 2026-11-21. O working tree já contém
M2.1–M2.3 e a correção da ADR 0022; essas alterações são preservadas. Esta fatia
não cria commit ou push e não autoriza as demais extrações M3/M4.

## Decisão

Os cálculos puros de identidade e serialização de execução pertencem a
`agentflow.apl.execution_identity`:

- JSON canônico (`sort_keys`, separadores compactos e Unicode sem escape);
- digest SHA-256 com prefixo `sha256:`;
- digest do vínculo completo do plano local;
- IDs determinísticos de operação e tentativa.

O módulo depende apenas de biblioteca padrão e `apl.contracts`. É incluído no
conjunto arquitetural de módulos puros. `authorized_execution` importa os nomes
com aliases privados históricos (`_canonical`, `_sha`, `_local_plan_digest`,
`_operation_id`, `_attempt_id`) para manter seus consumidores e a identidade dos
callables compatíveis.

## Limites

Esta extração não muda payloads, codificação, fórmulas, entradas, prefixos,
digests persistidos, IDs, schema SQLite, transações, eventos, controller,
concorrência ou recuperação. Não migra responsabilidades adicionais do executor.
O módulo novo não importa store, runtime, subprocessos, approval/security ou
adaptadores.

## Critérios e verificação

- Vetores fixos cobrem JSON Unicode, digest de texto, digest de plano e IDs.
- Aliases antigos são os mesmos objetos callable dos nomes novos.
- O teste arquitetural exige que o módulo continue puro.
- Testes de execução/persistência existentes e suíte completa passam.
- Nenhuma exceção arquitetural nova é adicionada; a linha de base M2 permanece
  com zero ciclos e dez exceções explícitas.

## Resultado

Windows/Python 3.13: vetores e regressões focadas passaram (76 testes). A suíte
completa passou com **815 aprovados e 1 ignorado** em 344,56 segundos. O teste
arquitetural passou isoladamente (10 casos); o baseline segue com zero ciclos e
dez exceções. `git diff --check` não apontou erros de whitespace no código; o
aviso de fim de arquivo da arquitetura foi corrigido. Nenhuma dependência,
transação, schema ou operação real de agente foi introduzida.
