# Plano de implementação

## Resumo e regras de fase

Fase 1 estabelece um pacote `agentflow` instalável, comandos seguros `--version`, `init` e `doctor`, configuração TOML, testes pytest, CI e instruções de instalação. O protótipo `autonomous-development-runner/` permanece intacto. Nenhuma tarefa desta fase inicia agentes, cria contêineres, grava SQLite ou faz commits. Cada tarefa abaixo deve ser concluída e verificada antes das dependentes.

## Fase 1 — Fundação

| ID | Tarefa | Dependências | Critério de aceite |
| --- | --- | --- | --- |
| F1-01 | Definir layout `src/agentflow`, metadados de pacote, Python >=3.11 e entry point `agentflow`. | Nenhuma | Instalação local reproduzível; importação e `agentflow --version` funcionam sem Codex ou Docker. O pacote ADR permanece intacto. |
| F1-02 | Criar estrutura dos dez módulos e contratos mínimos de fronteira, sem comportamento de fases futuras. | F1-01 | Imports independentes; núcleo não importa adaptadores concretos; nenhum comando de execução é exposto. |
| F1-03 | Definir esquema versionado de TOML, valores padrão e validação com erros úteis. | F1-01 | Configuração válida carrega; tipos, chaves e versões inválidas falham com mensagem e código de saída consistentes; Docker não é obrigatório. |
| F1-04 | Implementar `agentflow init` não destrutivo e idempotente. | F1-03 | Primeira chamada cria somente arquivos previstos; segunda não altera bytes; conflitos e diretório sem permissão são informados sem sobrescrita; nenhuma execução de agente. |
| F1-05 | Implementar sondagens somente leitura para Python, Git, Codex CLI e Docker. | F1-02 | Resultados estruturados distinguem disponível, ausente e erro; ausência de Docker é opcional; testes usam sondagens simuladas. |
| F1-06 | Expor `agentflow doctor` e códigos de saída documentados. | F1-05 | Mostra versões/estado; funciona sem Docker; não instala, autentica ou executa agentes; status crítico tem saída previsível. |
| F1-07 | Adicionar pytest para instalação/CLI, TOML, `init` repetido e conflitos, e diagnóstico com dependências ausentes. | F1-03, F1-04, F1-06 | Testes passam em ambiente limpo sem Codex/Docker; testes verificam efeitos, não detalhes internos. |
| F1-08 | Adicionar workflow CI para Python 3.11+ e instalação/testes. | F1-07 | CI instala pacote e dependências de teste declaradas e executa pytest em sistema sem Codex/Docker. |
| F1-09 | Atualizar README e guia de instalação/uso para os comandos realmente entregues. | F1-04, F1-06, F1-08 | Exemplos executáveis, pré-requisitos, significado de `doctor` e limites da Fase 1 documentados; remover contradições com o protótipo. |

### Gate de conclusão da Fase 1

Instalação reproduzível com Python 3.11+; `agentflow --version`, `init` e `doctor` funcionais; `init` seguro e repetível; configuração TOML validada; pytest e CI passando; operação local sem Docker; nenhum agente executado e nenhuma dependência do Vibe Coding Toolkit.

## Fases seguintes, sem implementação antecipada

| Fase | Dependência | Entregas planejadas e gate |
| --- | --- | --- |
| 2 — Planejamento e estado | Fase 1 | Especificações e tarefas versionadas, dependências, esquema SQLite, transições e migrações; testes de transação e reabertura. |
| 3 — Execução local | Fase 2 | Adaptador Codex CLI, runtime local, políticas de tentativa e timeout, validação e evidências; execução de tarefa isolada por testes de contrato. |
| 4 — Recuperação e Git | Fase 3 | Reconciliação após interrupção, prevenção de duplicidade, branches e checkpoints explícitos; testes de falha entre transições. |
| 5 — Docker e entrega | Fase 4 | Runtime Docker opcional, isolamento configurável, builds e implantação sob política explícita; teste de paridade local/contêiner. |
| 6 — Ecossistema | Fase 5 | Adaptadores de outros agentes e eventual integração opcional com Vibe Coding Toolkit após revisão legal e técnica. |

## Questões e riscos a resolver antes de implementar

1. Resolvido: `.agentflow/config.toml` com `schema_version = 1`, `runtime = "local"` e `agent = "none"`.
2. Resolvido: `init` usa a raiz do repositório Git atual ou a raiz que contém `--path`.
3. Resolvido: `doctor` usa códigos 0, 1 e 2. Python e Git são obrigatórios; Codex e Docker tornam-se obrigatórios apenas quando selecionados.
4. Resolvido: `adr` permanece referência legada intacta, sem migração nesta fase.
5. Resolvido: CI em Ubuntu com Python 3.11, 3.12 e 3.13.

## Progresso da Fase 1

| Tarefa | Estado | Evidência local |
| --- | --- | --- |
| F1-01 | Validada localmente | `pip install '.[test]'` construiu e instalou wheel `agentflow-0.1.0`; entry point instalado retornou `agentflow 0.1.0`. |
| F1-02 | Validada localmente | Imports de módulos cobertos pela suíte; `compileall` passou. |
| F1-03 | Validada localmente | Casos de configuração válida e inválida passaram no pytest. |
| F1-04 | Validada localmente | Pytest e teste de integração confirmaram criação, repetição sem alteração e conflito sem sobrescrita. |
| F1-05 | Validada localmente | Pytest confirmou obrigatoriedade por perfil e ausência de dependências opcionais. |
| F1-06 | Validada localmente | Teste com CLI instalada confirmou códigos 0, 1 e 2; ausência de Codex e Docker no PATH foi simulada. |
| F1-07 | Validada localmente | `python -m pytest -q`: 17 aprovados, 0 falhos. |
| F1-08 | Validada na CI remota | Workflow Ubuntu/Python 3.11, 3.12 e 3.13 passou no GitHub Actions para push e Pull Request #1. |
| F1-09 | Validada localmente | README e guia revisados contra comandos instalados e saídas observadas. |

## Homologação local — 2026-11-04

- Interpretador: Python 3.13.17 em `.venv` dentro do projeto. Python global não foi modificado.
- Dependências instaladas apenas no ambiente virtual: AgentFlow 0.1.0, pytest 9.2.0, colorama 0.4.6, iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0 e Pygments 2.21.0; pip 26.2.2 veio no ambiente.
- Empacotamento: wheel construído pela instalação PEP 517; metadados e entry point `agentflow` confirmados; `pip check` sem requisitos quebrados.
- Integração: em repositório Git temporário, `init` criou `config.toml`; segunda chamada preservou bytes; configuração inválida foi preservada com saída 2. `doctor` retornou 0 com Codex/Docker opcionais ausentes, 1 com Codex obrigatório ausente e 2 com TOML inválido. O repositório temporário foi descartado após o teste.
- `git diff --check` passou. O aviso de normalização LF/CRLF do README não é falha de whitespace.
- CI do GitHub: os jobs Python 3.11, 3.12 e 3.13 passaram nas execuções de push e Pull Request #1. A Fase 1 está pronta para revisão e merge, sem merge automático.

Revalidação antes da publicação: a suíte passou novamente com `--basetemp .pytest-run` (17 aprovados). Dois testes foram corrigidos para usar um caminho inexistente explícito; assim não dependem da localização do diretório temporário em relação a um repositório Git pai.

## Fase 2 — plano técnico proposto para revisão

A Fase 1 foi integrada à `main` no PR #1. A implementação local da Fase 2 foi autorizada, sem push ou merge. Esta seção delimita especificações, tarefas, dependências e estado operacional. Os contratos detalhados, o esquema de tabelas, as transições e a recuperação estão em [architecture.md](architecture.md#fase-2--planejamento-e-estado). O progresso verificável está em [phase-2-progress.md](phase-2-progress.md).

### Ordem de implementação e critérios de aceite

| ID | Tarefa | Dependências | Critério de aceite verificável |
| --- | --- | --- | --- |
| F2-01 | Registrar em ADR as decisões consolidadas na arquitetura: ignore seletivo, revisão imutável, evidências, reconciliação, WAL/durabilidade/backup e `operation_id`; fixar parâmetros operacionais e exemplos. | Fase 1 | ADR aprovado especifica `busy_timeout`, identidade de reserva, autoria de evidência e formato canônico de digest; mudanças restantes são risco explícito, não escolha implícita no código. |
| F2-02 | Definir `spec.toml` e `tasks.toml` v1, IDs de critérios, modelos imutáveis, normalização e evolução de versão. | F2-01 | Arquivos válidos produzem o mesmo plano/digest independentemente da ordem de leitura; versão futura/chave/tipo/ID/critério inválido falham com local preciso; originais não são editados. |
| F2-03 | Validar referências, unicidade e DAG de dependências; produzir ordem topológica estável. | F2-02 | Referência ausente, ID duplicado, autodependência e ciclos simples ou longos são rejeitados com ciclo identificável; permutação de entrada não muda a ordem canônica. |
| F2-04 | Definir protocolo do repositório, comandos de transição/evidência/revalidação e erros de domínio sem importar SQLite no núcleo. | F2-02, F2-03 | Implementação simulada e núcleo importam sem SQLite; guardas, revisão esperada e idempotência por ID+digest têm testes de contrato. |
| F2-05 | Criar esquema SQLite v1, FKs, índices, WAL, `busy_timeout`, `synchronous=FULL`, backup consistente e migração inicial. | F2-01, F2-04 | Banco novo abre com versão correta; FKs ativas por conexão; falha de backup impede migração; falha transacional preserva versão anterior; versão futura é recusada; `foreign_key_check` e `integrity_check` passam. |
| F2-06 | Persistir plano validado, revisões imutáveis e digests, com importação idempotente e revalidação explícita de definições alteradas. | F2-03, F2-05 | Duas importações iguais não duplicam registros; inserção parcial reverte tudo; plano inválido não toca banco; reabertura com tarefa iniciada alterada bloqueia transição até decisão auditável; histórico não é apagado. |
| F2-07 | Implementar máquina de estados, evidências estruturadas, operações idempotentes, eventos append-only, checkpoints lógicos e liberação transacional de dependentes. | F2-04, F2-06 | Status, evidência, operação, evento e checkpoint são atômicos; evidência declarada não satisfaz conclusão; ID repetido com mesmo digest retorna resultado anterior, digest divergente falha; concorrência não reserva tarefa duas vezes. |
| F2-08 | Implementar abertura/reabertura, integridade e reconciliação determinística de reservas sem efeitos externos. | F2-07 | Falha antes do commit não persiste efeito; após commit, estado/evento/checkpoint reaparecem; reserva órfã fica `blocked` com evento único; duas recuperações não duplicam eventos; corrupção/divergência bloqueia mutação e preserva dados. |
| F2-09 | Integrar validação e inspeção seguras na CLI, documentar versionamento seletivo e operação local. | F2-08 | CLI expõe somente leitura/inspeção aprovadas e erros estáveis; `init` e `doctor` da Fase 1 mantêm comportamento; Git versiona só `spec.toml` e `tasks.toml`; nenhum comando executa agentes, Docker ou Git mutável. |
| F2-10 | Completar testes unitários, de contrato, integração, migração, concorrência, reabertura e documentação de limite. | F2-02 a F2-09 | Suíte passa em Python 3.11, 3.12 e 3.13 sem Codex/Docker; cobre disputa entre processos, FKs, ciclos, mudanças de definição, evidência, idempotência, falha de migração/backup, interrupção e preservação do ADR. |

### Matriz mínima de verificação

| Camada | Cenários obrigatórios |
| --- | --- |
| Contratos TOML | Versão desconhecida/futura, encoding, chave/tipo incorretos, critérios vazios, IDs estáveis, digest canônico, arquivo malformado, evolução sem edição implícita. |
| Grafo | Referência ausente, duplicata, ciclo direto/indireto, ordem topológica determinística, desbloqueio com múltiplos pré-requisitos. |
| Estado | Transições permitidas/proibidas, revisão esperada, evidência declarada/verificada por critério, ID+digest divergente, eventos/checkpoints atômicos, FKs e rollback. |
| Migração | Banco vazio, reabertura, versão futura, falha de migração e backup, WAL ativo, timeout e durabilidade explícitos, integridade após interrupção. |
| Recuperação | Interrupção antes/depois do commit, reserva órfã bloqueada uma vez, reabertura repetida, definição alterada, banco corrompido em modo somente leitura. |
| Integração | CLI somente leitura onde prometido, apenas dois TOML versionáveis, dados operacionais fora do Git, ausência de Codex/Docker, protótipo ADR intacto. |

### Gate e riscos

Gate da Fase 2: contratos v1 documentados e testados; DAG válido; SQLite transacional com migrações reversíveis por falha, FKs e auditoria; recuperação determinística e idempotente; CI verde nas três versões; nenhuma execução de agentes. A Fase 3 só começa após revisão e autorização separadas.

Decisões consolidadas: somente `spec.toml` e `tasks.toml` versionáveis; revisões iniciadas imutáveis com revalidação explícita; evidência estruturada declarada/verificada por critério; reserva órfã bloqueada para reconciliação; SQLite em WAL com FKs, timeout, `synchronous=FULL`, migrações transacionais e backup consistente; `operation_id` global com digest de solicitação. F2-01 registra parâmetros concretos em ADR antes do código.

Riscos residuais: identidade confiável do proprietário de reserva e autoria de verificação; custo de I/O de durabilidade e backup; conflito quando TOML muda entre leitura e commit; restauração manual após corrupção física; compatibilidade de futuros esquemas TOML/SQLite. Migração do JSON do ADR, checkpoints Git, isolamento Docker e validação automática ficam fora da Fase 2.
