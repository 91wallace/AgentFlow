# RB-09.2 — Propostas pendentes e snapshots

## Resultado

**PENDING_PROPOSALS_IMPLEMENTED** para o transporte Broker local existente. A rota aceita e consulta somente propostas em estado `pending`; não aceita, rejeita ou expira snapshots, não produz grants e não inicia jobs.

## Implementação

- `LocalBrokerService` reutiliza o listener loopback mTLS, ALPN `agentflow-broker/1`, envelope v1, autenticação de certificados e `BrokerStore` do RB-09.1.
- `snapshot_submit` recebe um `PendingProposalSubmission` tipado, contendo `SnapshotProposal` serializada. Valida versão, envelope, contrato estrito, digest, vínculo entre `operation_id` e envelope, identidade autenticada e escopos `execution:propose` e `project:<project_id>`.
- O campo `proposed_by` precisa corresponder ao principal autenticado. A proposta é persistida pelo `BrokerStore` antes de qualquer resposta de sucesso. O estado de retorno é obrigatoriamente `pending`.
- Repetição idêntica do mesmo `operation_id` retorna o mesmo snapshot e sequência do journal. Reuso com qualquer conteúdo contratual divergente é conflito. A comparação do store cobre o contrato completo, não somente ID e digest.
- `snapshot_get` consulta apenas uma proposta pendente pertencente ao principal autenticado e a um projeto presente no escopo da identidade. Ausência, estado não pendente, identidade divergente ou falta de escopo não revela o conteúdo.
- A resposta `PendingProposalSnapshot` contém apenas ID, revisão, digest, estado, identidade cliente/principal, sequência do evento `snapshot.proposed`, `authority_status=unavailable` e `dispatch_allowed=false`.
- A leitura confere vínculo entre projeção SQLite e evento de journal. Divergência falha fechada.
- Falhas de escrita retornam erro sem resposta positiva; as transações SQLite revertem escrita parcial. Eventos de rejeição registram códigos controlados, sem incluir corpo da proposta.

## Contratos e fronteiras preservadas

Foram reutilizados `SnapshotProposal`, `BrokerEnvelope`, protocolo v1, `authenticate_tls_peer`, `BrokerStore.propose_snapshot` e o journal append-only. Foram adicionados `PendingProposalSubmission`, `PendingProposalQuery` e `PendingProposalSnapshot`.

A identidade precisa estar cadastrada e ativa, com certificado válido e escopos explícitos. O escopo de projeto usa a forma `project:<project_id>`. Esses escopos permitem somente submeter/consultar propostas pendentes. A autenticação mTLS não representa consentimento humano nem autorização de execução.

`UnavailableGatewayAuthority`/`UnavailableApprovalAuthority` continuam indisponíveis; `LocalBrokerService.authority_available` continua `False`. Nenhum grant, decisão, dispatch, provider ou executor foi conectado. Production Mode, Broker/TEG fora desta fatia e RB-09C não foram alterados.

## Evidências e testes

Testes reais de transporte local com certificados sintéticos descartáveis e persistência SQLite cobrem:

- submissão mTLS, vínculo de identidade, resposta mínima e sequência do journal;
- replay idempotente sem evento duplicado;
- consulta após encerrar e reiniciar o Broker;
- escopo de projeto negado e tentativa de falsificar `proposed_by`;
- replay conflitante;
- consulta fora de escopo sem revelar dados;
- estado de projeção inconsistente retornando indisponível;
- falha de persistência sem confirmação;
- falha após escrita parcial do evento com rollback SQLite;
- contrato malformado e integridade do store.

Resultados nesta execução:

- `tests/test_approval_transport.py tests/test_approval_persistence.py tests/test_approval_protocol.py -q`: **58 passed**.
- `.venv\Scripts\python.exe -m pytest -q`: **462 passed**, 0 falhas, 281,98 s.
- `git diff --check`: executado após as alterações; não reportou whitespace inválido (Git exibiu apenas avisos de conversão LF/CRLF em arquivos já modificados localmente).

## Limitações

- O serviço continua preso a loopback e é uma fundação local, não um domínio administrativo independente nem um serviço pronto para rede externa.
- Os escopos são fornecidos pela identidade cadastrada; esta etapa não implementa uma interface de consentimento humano nem revisão/aceite da proposta.
- Consultas são individuais por ID; não há listagem paginada de propostas.
- Uma resposta perdida após commit exige retry com o mesmo envelope/contrato imutável; reuso com timestamp ou conteúdo alterado é conflito. O cliente deve tratar timeout como resultado potencialmente desconhecido e consultar pelo ID.
- Certificados e fixtures dos testes são sintéticos; não foram usados providers ou credenciais operacionais.
- Esta implementação não homologa despacho, aprovação, autoridade independente ou isolamento de segurança para produção.

## Próxima fatia

Adicionar, em etapa separada e sob os contratos aprovados, uma consulta paginada por projeto e eventual fluxo humano de decisão. Nenhuma proposta pode ser aceita ou executada por esta interface.
