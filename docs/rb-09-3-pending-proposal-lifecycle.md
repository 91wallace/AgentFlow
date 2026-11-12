# RB-09.3 — Ciclo de vida de propostas pendentes

## Resultado

O Broker agora persiste e consulta o ciclo de vida de propostas, com expiração, cancelamento do autor e invalidação por uma revisão sucessora vinculada. As transições são terminais e auditáveis. Nenhuma rota aprova ou executa propostas.

## Estados e transições

| Estado atual | Próximo estado | Condição |
| --- | --- | --- |
| `pending` | `expired` | TTL fixo de 7 dias desde o timestamp de recebimento gravado no evento `snapshot.proposed`; aplicado ao iniciar o serviço, ao submeter/repetir e antes da consulta. |
| `pending` | `cancelled` | Cliente mTLS ativo, escopos `execution:propose` e `project:<id>`, principal igual ao autor da proposta. O principal vem do certificado cadastrado e não é um campo do pedido. |
| `pending` | `obsolete` | Mesmo autor, projeto e tipo; revisão sucessora pendente, revisão diferente e `parent_digest` da sucessora igual ao digest da proposta anterior. |
| `pending` | `accepted` | Continua somente na primitiva interna existente, fora do serviço mTLS; esta etapa não verifica nem produz prova de autoridade e não expõe essa transição. |

`cancelled`, `expired`, `obsolete`, `rejected`, `revoked` e `accepted` não retornam a `pending`. `snapshot_get` continua compatível para propostas pendentes e retorna metadados de estado terminal sem conteúdo. Propostas ausentes ou fora de escopo têm resposta indistinguível.

## Persistência e idempotência

- O schema SQLite evoluiu de v3 para v4; a migração recria somente a tabela de projeção `snapshot_state`, preservando propostas, journal e eventos existentes.
- Cada cancelamento/invalidação usa `operation_id`; o mesmo pedido retorna a transição já persistida. Reuso com parâmetros divergentes ou uma nova tentativa após estado terminal é conflito.
- Expiração usa identidade de ator interna fixa (`broker-expiry-scheduler`) e `operation_id` determinístico por snapshot. O relógio é o tempo de recebimento persistido pelo Broker, não `proposed_at` controlado pelo cliente.
- Estado materializado é verificado contra o evento de journal. Falha de gravação reverte a transação e não confirma a transição. Locks do store serializam concorrência.
- Eventos terminais guardam ator, origem/destino e identificador da operação; obsolescência também guarda a referência do snapshot sucessor. O verificador de integridade confere autoria e vínculo de revisão.

## Contratos e segurança

Novos contratos tipados: `PendingProposalCancellation`, `PendingProposalInvalidation` e `ProposalLifecycleSnapshot`. O transporte continua loopback mTLS no protocolo v1. A identidade do cliente é autenticada antes de ler escopos ou acionar transições.

O cancelamento está restrito ao autor. A invalidação também exige que a sucessora pertença ao mesmo autor e esteja no mesmo projeto. Nenhum pedido transporta `actor_id`, principal, autoridade ou estado destino arbitrário. `authority_status` permanece `unavailable`; `dispatch_allowed` é sempre `false`. Não são criados grants, decisões humanas ou jobs. `UnavailableGatewayAuthority`, Production Mode e os contratos RB-09C permanecem inalterados.

## Testes e evidências

Testes cobrem expiração determinística e repetida, cancelamento autorizado/negado, invalidação com e sem vínculo válido de revisão, consulta terminal, replay após reinício, falha parcial com rollback, concorrência, migração de schema v3 e verificação do journal. Testes de transporte usam certificados sintéticos e SQLite descartável.

Resultados registrados nesta implementação:

- `tests/test_approval_persistence.py tests/test_approval_transport.py tests/test_approval_protocol.py -q`: **68 passed** na última execução focada.
- `.venv\Scripts\python.exe -m pytest -q`: **472 passed**, 0 falhas, 275,08 s.
- `git diff --check`: passou sem whitespace inválido; o Git pode emitir avisos de conversão LF/CRLF de arquivos preexistentes modificados localmente.

## Limitações

- O TTL é fixo em sete dias nesta versão; não há configuração operacional pública.
- A expiração é lazy (na inicialização e em pedidos), não usa daemon ou worker separado.
- Cancelamento e invalidação estão limitados ao principal autor da proposta; não há cancelamento por gerente de projeto.
- A obsolescência exige vínculo explícito por `parent_digest`; não há comparação semântica automática de revisões.
- O serviço é local e não constitui uma autoridade independente, serviço remoto ou fronteira de segurança de produção.
