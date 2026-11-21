# ADR 0018 — M1: fachadas sob demanda e isolamento de imports locais

Status: implementação M1 autorizada pelo proprietário em 2026-11-20, seguindo
o roadmap da [ADR 0017](0017-architecture-m0-baseline.md).

## Problema

Importar somente `SecurityProfile` na CLI executava `security/__init__.py`,
que carregava autorização, consentimento, `approval/__init__.py` e WebAuthn.
Assim, uma operação local dependia de `cryptography` mesmo sem usar criptografia.
`apl/__init__.py` também carregava pontes avançadas desnecessariamente.
Trocar apenas o import por `security.profile` não resolve a inicialização do pai.

## Decisão

- As fachadas `security`, `approval` e `apl` mantêm seus nomes públicos e
  `__all__`, mas resolvem cada símbolo pelo módulo proprietário somente no
  primeiro acesso, via `__getattr__` de módulo. O objeto resolvido fica em cache;
  identidade de classes, funções e aliases permanece a mesma. `dir()` inclui
  todos os exports sem carregar implementações. `import *` continua suportado
  e, por solicitar todos os símbolos, pode carregar a pilha avançada.
- Cada import sob demanda tem destino literal. A análise AST continua vendo
  as dependências reais; não há allowlist nova nem import dinâmico não resolvido.
- A CLI usa os dois valores históricos do parser de `profile select` diretamente,
  sem importar `SecurityProfile`. O comando continua inativo/não autorizador;
  flags, escolhas e resultados permanecem compatíveis. Alterações futuras dessas
  escolhas exigem manter a compatibilidade do parser legado.
- Se a resolução de um export exigir `cryptography` ausente, a fachada informa
  a dependência e encadeia a exceção original. Outros módulos ausentes não são
  mascarados. APIs que já importam criptografia no momento do uso mantêm esse
  comportamento e seus erros existentes; não se criam implementações substitutas.

## Fronteira e invariantes

Isso remove efeitos eager, sem afirmar que os ciclos estáticos foram resolvidos.
As pontes avançadas continuam disponíveis quando solicitadas explicitamente.
A única exceção eliminada do baseline é
`local-to-advanced|agentflow.cli|agentflow.security`: 29 → 28 violações aceitas.
As demais permanecem visíveis para M2. Metadados de proveniência M0 não são
reescritos para aparentar uma captura nova.

Sem movimentação de serviços, mudança de schema, divisão de transações,
alteração de hashes/grants/gates, reativação de autenticação ou mudança de
políticas de execução. Sem dependências novas. `pyproject.toml` continua exigindo
`cryptography`; tornar a instalação base independente e criar `[security]`
continua sendo M4. M1 prova o isolamento de execução/import, não uma instalação
base sem dependências.

## Critérios de saída e evidências

`tests/test_local_import_isolation.py` usa processos novos fora do checkout:

1. Carrega as três fachadas em ordens distintas sem importar implementações,
   SQLite ou tentar importar criptografia.
2. Carrega CLI, `--help` e `--version` sem carregar `security`, `approval` ou `teg`.
3. Bloqueia todas as tentativas de import de `cryptography` e reutiliza testes
   comportamentais de init idempotente, doctor, backlog, seleção/retomada,
   planejamento progressivo, execução simulada dos agentes, handoffs e gates
   independentes. Verifica também que a pilha avançada não foi carregada.
4. Confirma que contratos de segurança continuam acessíveis e que criptografia
   ausente impede o uso do assinador. Verifica diagnóstico de WebAuthn solicitado
   explicitamente e operação local posterior no mesmo processo.
5. Com a dependência instalada, compara todos os exports com o inventário anterior
   `tests/facade_exports.json`, incluindo aliases, identidade, cache e import star.

Os subprocessos usam o pacote instalado no interpretador e preservam o ambiente
da CI, inclusive seu guard contra imports acidentais de checkout. Não inserem
`src` no caminho Python. Testes simulados não homologam inferência real.

O próximo marco é M2: inverter dependências por contratos e reduzir os ciclos,
preservando serialização e unidades transacionais. Sua execução exige autorização
própria; as extrações maiores e empacotamento continuam em M3/M4.

## Resultado da validação M1

Windows/Python 3.13: 18 testes focados de isolamento/arquitetura aprovados;
suíte completa com **785 aprovados e 1 ignorado** em 379,38 segundos.
O worker foi também validado separadamente com configuração pytest vazia,
evitando o `pythonpath = ["src"]` do checkout durante a CI do wheel instalado.
Os 213 exports anteriores (71 APL, 64 approval, 78 security) preservam identidade,
aliases e import star. `git diff --check` passou. Não houve instalação de
dependências, inferência real, migração de banco ou push.

Antes de M1, a entrega funcional e M0 foram consolidados separadamente nos
commits `eab400b` e `fa61d5d`, conforme a sequência autorizada. As alterações
de M1 permanecem no working tree, separadas desses checkpoints.
