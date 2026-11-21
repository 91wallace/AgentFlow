# ADR 0017 — M0: direção arquitetural e baseline progressivo

Status: M0 autorizado pelo proprietário em 2026-11-20; M1–M4 são roadmap,
sem autorização automática de implementação. Comportamento de produto permanece
definido pelas ADRs 0011–0016 e pelos contratos históricos não supersedidos.

## Estado de referência

A referência é o working tree da branch `feat/intelligent-model-routing-phase-7`,
incluindo entregas ainda não commitadas. HEAD de proveniência:
`272794fda44f6f08b65a70689a24772297a120ce`. HEAD isolado não reproduz essa referência.
Não se cria commit, stash, checkout ou worktree nesta etapa. Alterações funcionais
anteriores permanecem separadas logicamente da nova documentação/teste M0.
Na publicação posterior, consolide primeiro a entrega funcional das ADRs
0011–0016 e depois o M0, em commits distintos quando autorizado. Não publique
o baseline M0 sozinho sobre o HEAD antigo: ele depende dos fontes pendentes.

O [inventário M0](../architecture-m0-inventory.md) registra dependências,
compatibilidade e riscos. `tests/architecture_baseline.json` guarda exceções
identificáveis por regra/módulo/alvo e fingerprint dos fontes para proveniência.
O fingerprint não congela bytes do produto nem é uma condição do teste.

## Responsabilidades decididas

- `apl`: camada oficial de aplicação/coordenação de ciclos, início, retomada,
  limites e recuperação, progressivamente dependente de protocolos injetados.
- `core.git`: serviço específico existente para workspace/checkpoint; mantém
  caminho público durante a transição. Não recebe outro orquestrador geral.
- `cli` e fábricas: composição e interfaces de entrada. Selecionam implementações;
  não devem concentrar regras de domínio ou tornar optional features obrigatórias.
- Contratos de execução, orçamento, validação e autorização: dados/protocolos/erros
  de domínio sem import reverso de serviços ou persistência. Reutilizar conceitos
  existentes e preservar semântica; não criar um depósito genérico de DTOs.
- `state`: implementações de persistência operacional. Repositórios de aprovação
  e TEG ficam na pilha avançada, preservando suas unidades transacionais.
- `agents`, `runtime`, `git` e implementações de validação: bordas técnicas.
  Coordenação depende de interfaces, não de CLI nativo, subprocesso ou SQLite concreto.
- `approval`, `security`, `teg`: pilha avançada, sem dependência obrigatória no
  fluxo local. Isso é objetivo de M1/M4; hoje os imports eager ainda a carregam.

Não há migração em massa `apl → core`. A separação entre serviços, modelos e
persistência é feita por fatias posteriores. Fachadas antigas permanecem apenas
onde necessárias à compatibilidade; não devem carregar implementações eager.

## Baseline e direção das dependências

O teste M0 é um ratchet: nenhuma violação nova e nenhuma exceção obsoleta.
Uma correção exige remover sua entrada do baseline; assim ela não fica disponível
para permitir a reintrodução da dívida. Não existe atualização automática das
exceções durante pytest/CI. Ampliação requer decisão explícita e revisão própria.
Uma extração autorizada que apenas mude o endereço de dívida ainda existente
exige revisão explícita do mapeamento e evidência de que não houve ampliação;
não se deve substituir todo o baseline pelo relatório novo.

As regras atuais inventariam ciclos entre componentes de primeiro nível, imports
local → pilha avançada, criptografia direta no local, agentes concretos em
core/apl, stores concretos em core e dependências de implementações/IO nos módulos
de contrato designados. Wildcards e import amplo da fachada agents em core/apl
não podem esconder a dependência concreta. Reexports resolvíveis são examinados.

`TYPE_CHECKING` e aliases simples ficam separados; imports em funções contam
como runtime. Imports dinâmicos literais são resolvidos; dinâmicos não resolvidos
são registrados como dívida própria e novos casos falham no teste.

O mapa é estático e conservador, não uma prova de execução Python: não cobre
todo getattr, monkeypatch, import hook ou resolução dinâmica arbitrária, nem
prova ausência de IO dentro de funções. Também não afirma ausência de ciclos
internos a um componente. Fachadas/efeitos de inicialização exigem smoke tests
em processos novos, especialmente a prova sem cryptography prevista em M1.

A validação ampla revelou uma expectativa instável no teste legado de consumo
concorrente: o perdedor pode receber BrokerConflict no CAS ou grant_consumed na
verificação anterior, conforme o escalonamento. M0 estabiliza apenas o teste,
exercitando ambos os interleavings com barreira/evento, aceitando somente esses
dois caminhos e mantendo um vencedor, um evento de transição e integridade do
journal. Não há mudança na implementação de autorização/persistência.

O teste lê `Path(agentflow.__file__).parent`, não presume checkout/src. Assim,
a CI existente também o aplica ao wheel instalado sob o guard contra imports
acidentais do checkout. Não se remove essa proteção nem se altera empacotamento M0.

## Invariantes de compatibilidade

- Flags, aliases de migração, entrypoint `agentflow.cli:main`, JSON, stderr e
  códigos de saída permanecem. Imports tardios legítimos nas bordas opcionais
  não são proibidos indiscriminadamente.
- Config v1/v2, TOMLs e snapshots históricos permanecem legíveis; contratos
  locais não passam a exigir autenticação. Não iniciar operações reais nesta refatoração.
- Digests/IDs, payloads assinados, JSON canônico, eventos, revisões, grants e
  evidências mantêm sua semântica. Helpers de nomes iguais não são necessariamente equivalentes.
- Extração mecânica não divide conexão/transação, altera schemas, relaxa guardas,
  renova autorização ou reinterpreta `unknown`. Preservar concorrência/restart.
- Handoff continua informativo; gates independentes continuam obrigatórios.
  Seleção de agente e padrões nativos, leitura progressiva e stdin responsivo
  permanecem cobertos pelas suítes existentes.
- Retenção separa artefatos descartáveis de dados essenciais à retomada/auditoria.
  M0 só inventaria o layout; não adiciona cleanup ou remoções.

## Roadmap e critérios de saída

| Marco | Escopo | Saída objetiva |
| --- | --- | --- |
| M0 | Esta ADR, AGENTS vigente, inventário, baseline e teste | Teste aceita dívida atual, rejeita violações injetadas e exige redução das exceções; suite existente preservada; nenhum código de produto movimentado. |
| M1 | Fachadas leves e fronteira local, primeira fatia CLI se necessária | Subprocesso limpo sem cryptography carrega CLI e executa init/doctor/planejamento/execução com adaptadores simulados; uso avançado informa dependência ausente. Hoje esse gate está pendente. |
| M2 | Contratos e remoção dos ciclos | Remover state/validation, approval/security, apl/security com seus imports reversos e exceções; protocolos/erros inferiores não importam serviços; regressões de serialização e transações passam. |
| M3 | Extrações verticais de CLI, APL, stores e providers | Uma responsabilidade por mudança; CLI/JSON/exit codes, hashes, histórico, restart e concorrência preservados; sem migração estrutural durante simples movimentações. |
| M4 | Extra security, CI/documentação/layout | Instalação base limpa sem crypto e instalação com security passam; inventário do wheel tem fonte única; links/âncoras e dados necessários à retomada permanecem válidos. |

M1 não pode limitar-se a trocar o endereço de um import: Python executa os
`__init__.py` dos pacotes intermediários. M2 deve inverter dependências usando
contratos, não apenas mascarar ciclos com novos imports tardios.
M3 começa pelo caminho local e segue por famílias de operações; não converte
toda a CLI em pacote num único diff. O diálogo plan_read/plan_index está em
`apl/codex_planner.py`; discovery/validação/materialização estão em initial_planning.

Ruff pode entrar gradualmente depois, com dependência de desenvolvimento explícita,
sem instalar ferramentas nesta etapa nem misturar reformatação geral com extrações.
Arquivar docs/experimentos e alterar retenção são tarefas próprias, precedidas
de inventário de referências, incluindo plan_sections e manifests existentes.

## Resultado da validação M0

Windows/Python 3.13: 10 testes arquiteturais passaram; suíte completa final com
777 aprovados e 1 ignorado. A primeira rodada ampla revelou o interleaving do
teste legado descrito acima; ambos os escalonamentos agora são exercitados.
`git diff --check` passou e o fingerprint confirma fontes de produção idênticos
à captura M0. Não houve commit/stash, movimentação de módulos, migração de banco,
instalação de dependências ou inferência real de agentes. M1–M4 permanecem pendentes.
