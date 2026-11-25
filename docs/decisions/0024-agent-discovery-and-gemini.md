# ADR 0024 — descoberta de agentes e integração inicial Gemini

Status: implementação incremental da solicitação A–M de 2026-11-23. Não altera
as responsabilidades arquiteturais da ADR 0017 nem autoriza commits/push.

## Decisão

Reutilizar `AgentRegistry`, `AdapterDescriptor`, `AgentAvailability`,
`AgentCapabilities` e `ModelDescriptor`. A composição de built-ins permanece em
`agents.factory`; `agents.discovery` apenas percorre o registro injetado. Entradas
lazy permitem informar instalações ausentes sem falhar a construção do catálogo
ou iniciar sessões. Nenhuma descoberta de entry points externos é adicionada.

O comando `agents` apresenta relatório humano ou JSON versionado. Observação de
versão não comprova login, acesso a modelos, isolamento ou homologação real.
Capacidades são identificadas como declarações do adaptador. Modelos configurados
são separados dos modelos observados; não há catálogo de nomes inventados.
Falhas esperadas de uma sondagem ficam locais àquela observação.

Adicionar Gemini pelo contrato de execução existente, usando stdin não interativo
e eventos JSONL. Reutilizar `SelectedAdapter`, leitura de resposta, planner APL,
runtime local, seleção persistida por ciclo e evidência de processo. Não criar
outro scheduler, store, transação, schema ou mecanismo de checkpoint.

## Protocolo Gemini e limites

Referências primárias consultadas em 2026-11-23:

- [Headless mode](https://geminicli.com/docs/cli/headless/).
- [Automação](https://geminicli.com/docs/cli/tutorials/automation/).
- [Tipos de saída](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/output/types.ts).
- [Estatísticas de stream](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/output/stream-json-formatter.ts).

O contrato observado fornece `init`, mensagens, eventos de ferramentas, erros e
resultado final. O adaptador exige abertura e término únicos, reconhece falhas
estruturadas e nunca converte cancelamento/timeout em sucesso por haver um log
anterior. Código zero do processo sozinho não basta. Falhas de ferramenta podem
ser corrigidas na mesma sessão; o resultado do processo continua separado dos
gates independentes que determinam o aceite da tarefa.

O AgentFlow não passa flags para liberar todas as ações, saltar trust ou mudar
sandbox. Permissões nativas podem impedir implementação/testes; sua ausência não
é contornada. Os controles de processo são os mesmos dos outros executores.

`init.model` pode ser um alias de roteamento. Apenas estatísticas com um único
modelo permitem registrar uma identidade observada; múltiplos modelos ou ausência
de estatísticas ficam desconhecidos. O protocolo não atesta provider efetivo.
Consequentemente, a fábrica recusa seleção manual Gemini de provider/modelo antes
da inferência; a execução inicial usa o padrão nativo. Essa limitação é explícita,
sem enfraquecer a confirmação exigida para seleção manual nos adaptadores atuais.

Sondagens usam versões e ajudam a verificar flags; não implicam homologação de
uma versão real. A consulta de autenticação e catálogos suportados ainda é trabalho
da Fase B. O [progresso A–M](../autonomous-platform-progress.md) diferencia as
entregas atuais das demais funcionalidades autorizadas e ainda pendentes.

## Verificação

Testes com adaptadores simulados verificam extensibilidade, ausência de instalação,
proveniência de catálogo, dados inválidos, preservação da configuração, descoberta
sem escrita, isolamento de falhas, flags/permissões Gemini, mensagens fragmentadas,
planner compartilhado, estados terminais e erros. O auditor inclui Gemini na
classificação de adaptadores concretos; baseline permanece com dez exceções e
zero ciclos. Suíte completa, resultado final e limitações ficam no documento
de progresso. O protótipo ADR permanece preservado.
