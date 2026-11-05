# ADR 0002: Contratos e estado da Fase 2

Status: aprovado para implementação da Fase 2, 2026-11-04.

## Decisões operacionais

- Apenas `.agentflow/spec.toml` e `.agentflow/tasks.toml` são versionáveis. Configuração, SQLite, WAL/SHM, backups e logs são locais e ignorados. `init` continua criando apenas `config.toml`.
- TOML v1 exige IDs ASCII `^[a-z][a-z0-9-]*$`, textos não vazios, critérios com IDs estáveis, dependências existentes e DAG. Chaves desconhecidas e versões futuras são erro. Digest SHA-256 usa JSON UTF-8 canônico (`sort_keys=True`, separadores compactos, `ensure_ascii=False`) dos modelos validados, com tarefas e dependências ordenadas por ID; mudanças de whitespace ou ordem não mudam o digest.
- Cada revisão de tarefa armazena snapshot canônico de título, descrição, critérios, dependências e digest da especificação. A revisão é imutável. Alteração em tarefa iniciada (`reserved`, `failed`, `blocked`, `completed`, ou com tentativas) requer revalidação explícita com autor e justificativa; conclusão anterior não é reinterpretada. IDs com histórico não são reutilizados.
- Evidência é registro estruturado ligado a tarefa, revisão e critério. `declared` contém fonte e payload; `verified` referencia uma declaração, método, autor e resultado. Na Fase 2, verificação humana é uma declaração de responsabilidade do autor fornecido pela chamada; não há autenticação implementada. Conclusão exige verificação positiva de cada critério ou exceção explícita auditada.
- Reserva lógica contém proprietário opaco não vazio e ID de operação. Não há lease nem processo executor na Fase 2: em reabertura, toda reserva é tratada como órfã e passa uma vez a `blocked` com `recovery_required`. Liberação exige decisão explícita. Nunca presumir sucesso ou repetir efeitos externos.
- SQLite usa WAL, `foreign_keys=ON`, `busy_timeout=5000` ms e `synchronous=FULL` em cada conexão. Escritas usam `BEGIN IMMEDIATE`; conflitos de revisão são erros controlados. Backup pré-migração usa a API SQLite `backup()` para arquivo temporário no mesmo diretório, `integrity_check` e publicação com `os.replace`; falha aborta migração. Versões TOML e SQLite evoluem separadamente; versão futura é recusada sem mutação.
- `operation_id` é único por banco, retido pelo tempo de vida do banco. A solicitação canônica inclui verbo, projeto, tarefa, revisão esperada e parâmetros. Mesmo ID/digest retorna resultado anterior, inclusive após reabertura; ID/digest diferente é conflito. Uma transação grava operação, estado, evidência, eventos e checkpoint juntos. Eventos e revisões nunca são apagados automaticamente.

## Consequências e limites

O núcleo depende de protocolos, sem importar SQLite. A CLI da Fase 2 valida e inspeciona, sem executar agentes. Corrupção, versão futura e divergência de plano impedem escrita e preservam os arquivos para diagnóstico. A restauração de backup é manual. Custos de sincronização e autoria não autenticada são riscos aceitos nesta fase.
