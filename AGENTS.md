# Instruções para agentes no AgentFlow

## Escopo e estado atual

O produto é um orquestrador local com CLI, planejamento versionado, estado operacional SQLite e Git para versionamento. O fluxo local oferece execução explícita por Codex, Antigravity, OpenCode ou Claude Code via adaptadores; a homologação real de cada CLI é distinta dos testes sintéticos. Docker é opcional. Use Python 3.11+, pytest, TOML e Apache 2.0.

As ADRs 0011–0016 descrevem o comportamento local vigente; a ADR 0017 define a reorganização arquitetural M0–M4. Restrições de fases históricas não são proibições globais do produto atual. Implemente apenas a fatia autorizada na solicitação: a autorização atual de M0 não inclui executar M1–M4 automaticamente.

O diretório `autonomous-development-runner/` contém o protótipo ADR 0.1 extraído do ZIP versionado. Preserve seus arquivos e comportamento enquanto uma migração explícita não for aprovada. O README da raiz descreve uma visão mais ampla que o código atual.

## Regras de trabalho

- Leia `docs/architecture.md`, `docs/implementation-plan.md` e as decisões em `docs/decisions/` antes de alterar interfaces.
- Implemente somente o escopo autorizado e seus critérios de aceite. Testes do próprio AgentFlow, com adaptadores simulados e subprocessos locais de teste, fazem parte da validação. Inferência real, testes de projetos de terceiros, Docker e pilotos operacionais exigem que essas ações estejam no escopo autorizado; não os inicie apenas para concluir uma refatoração.
- Preserve alterações existentes; não sobrescreva arquivos de usuário. Não faça commit ou push sem instrução explícita.
- Mantenha interfaces entre módulos explícitas, substituíveis e testáveis. Não importe adaptadores concretos para dentro do núcleo.
- `apl` é a camada oficial de coordenação; `core.git` permanece um serviço específico de compatibilidade. Não migre serviços em massa entre esses pacotes nem amplie `core` como segundo orquestrador. Serviços dependem de protocolos e contratos; montagem de implementações fica nas bordas.
- O baseline `tests/architecture_baseline.json` registra dívida existente, não arquitetura desejada. Imports em funções continuam sendo dependências de runtime; `TYPE_CHECKING` é inventariado separadamente. Não introduza violações novas nem regenere o baseline para silenciar o teste. Ao remover dívida, retire também sua exceção. Novas exceções exigem decisão arquitetural explícita.
- `init` deve ser repetível, não destrutivo e informar conflitos sem editar arquivos existentes. `doctor` é somente leitura e Docker é opcional.
- Evite dependências de runtime quando a biblioteca padrão atender. Não instale dependências ou execute scripts obtidos da internet sem autorização.
- Cubra comportamentos e falhas relevantes com pytest; execute os testes disponíveis antes de concluir uma implementação.
- Registre mudanças de escopo, decisões e limitações na documentação.

## Invariantes do fluxo local

- `init` preserva configuração existente; `doctor` não altera estado. Perfil de segurança, senha AgentFlow e WebAuthn não são pré-requisitos para jobs locais comuns. Não reative comandos legados de enrollment como caminho obrigatório.
- Seleção segue agente explícito, integração chamadora, padrão do projeto e Codex. Não adivinhe o chamador pelo processo pai. Provider/modelo são opcionais; sem seleção configurada, use o padrão nativo do CLI. Retomada preserva agente/versão e modo de seleção; `cli-default` não congela um modelo nativo não selecionado.
- Plano Mestre Markdown e fila executável são distintos. Detalhe apenas o marco ativo. O Markdown não tem teto de tamanho; contexto usa recortes/índice e recuperação por linhas vinculada ao SHA-256.
- Handoffs JSON minificados são contexto informativo das dependências diretas, sem corte artificial por tamanho. Não substituem arquivos reais nem gates independentes.
- Preserve serialização canônica, semântica dos digests/IDs, schemas, journals append-only, idempotência, fencing, quotas, cancelamento e recuperação conservadora de `unknown`. Extrações mecânicas não autorizam migrações nem divisão de transações SQLite.
- Manifeste limitações comprovadas: configuração de cwd/pathset não prova isolamento; testes sintéticos não homologam agentes reais; estimativa de custo não é teto financeiro imposto pelo provider.
- Logs e temporários têm retenção distinta de planos, checkpoints, evidências e manifestos necessários à retomada/auditoria. Não apague artefatos essenciais por uma regra genérica de idade.

## Integrações externas

O Vibe Coding Toolkit é uma referência potencial, não uma dependência. Qualquer integração futura deve ser opcional, isolada por adaptador e revisada quanto a licença, versão, segurança e proveniência. Não copie seu código na Fase 1.
