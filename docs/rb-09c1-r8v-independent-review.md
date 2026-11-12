# RB-09C.1-R8-V — revisão independente da remediação R8

**Parecer: REJECT.** R8 melhora materialmente a proposta e resolve documentalmente a contradição aritmética original de C-20, mas deixa lacunas normativas em fencing/ancoragem, na separação dos failpoints de C-06, na projeção completa GER→ER e na precedência específica entre propostas. Este parecer rejeita R8 como remediação documental completa para deliberação de H-01–H-10; não rejeita a arquitetura-alvo B aprovada no ADR 0008 R2. Não é decisão sobre implementação ou homologação operacional.

## 1. Escopo e baseline

Revisados integralmente R7-B, R8, R6, R7-A.2 e ADR 0008. R3 e R5 foram consultados para precedência e conflitos. Os contratos Python atuais foram considerados como baseline de implementação: permanecem na forma local anterior e não implementam o protocolo COSE, o ECA, o StartPermit ou os receipts propostos em R8. A inspeção não executou serviços, testes operacionais, agentes ou providers.

O working tree continha alterações locais preexistentes, inclusive ADR 0008, documentação de fases anteriores, `autonomous-development-runner/` e os documentos R1/R3/R5/R6/R7-B. Índice vazio. Foram preservados; este relatório é o único arquivo criado nesta etapa. O `git diff --check` não encontrou erro de whitespace; Git emitiu avisos de conversão LF→CRLF para documentos já modificados no working tree.

### Precedência constatada

ADR 0008 R2 continua sendo a decisão arquitetural aprovada: Alternativa B como alvo; implementação e homologação da independência permanecem pendentes. A regra `kid` em CBOR `bstr` é uma correção estreita autorizada e verificada pela R7-A.2. R3, R5, R6 e R8 são propostas de contratos em diferentes revisões; R7-A/R7-A.1/R7-A.2 são evidências técnicas de escopos limitados; R7-B é um parecer independente. Nenhuma proposta posterior se torna norma apenas por ser mais recente ou ter `READY_FOR_REVIEW`.

## 2. Matriz B-01 e M-01–M-05

`CLOSED_DOCUMENTAL` significa suficiente para deliberação no aspecto específico, não implementação aprovada. `CONDITIONAL` significa proposta coerente com dependências ou escolhas explícitas. `OPEN` significa que resta lacuna documental verificável.

| Achado R7-B | Causa original e correção R8 | Critério/evidência revisados | Estado independente |
|---|---|---|---|
| **B-01 — C-20, R7-B §3; R8 §3** | A regra anterior dizia que não haveria quorum se um dos quatro witnesses estivesse indisponível, embora três ainda satisfizessem 3/4. R8 separa indisponibilidade, resposta inválida/Byzantine, split view, rollback e ausência de ACK durável. | A tabela fixa contagens, entradas e resultados esperados e distingue 3 votos possíveis de quorum impossível com 2. A soma dos subcasos é correta (§3). Fecha a inconsistência documental; a campanha e a independência dos witnesses ainda não foram executadas. | **CLOSED_DOCUMENTAL** |
| **M-01 — audience, R7-B §3; R8 §4** | Confusão entre audiência do envelope e identidade do TEG, incluindo audiência self-referential no vetor anterior. R8 propõe Binding TEG→Broker, Authorization para serviço TEG e StartPermit para instância/lease. | A direção proposta é coerente e reduz a reutilização entre destinatários. Ainda depende de H-01/H-03/H-08 e de vetores atualizados; identidade lógica, identidade de instância, ambiente e audiência exata precisam ser fixados no perfil wire antes da aprovação. | **CONDITIONAL** |
| **M-02 — clones/restores/fencing, R7-B §3; R8 §5** | Estado local/clonado não impede dois Gateways nem garante execução única externa. R8 introduz ECA, epoch, claim CAS e bloqueio quando provider não oferece fencing/idempotência/query. | A regra conservadora para `unknown` e inelegibilidade é adequada. Porém não há contrato verificável completo da resposta durável do ECA (`claim_start`/fence token), nem ligação obrigatória dessa decisão à confirmação pré-efeito da âncora. Também não há escolha/autoridade operacional do ECA. | **OPEN** |
| **M-03 — revogação em voo, R7-B §3; R8 §6** | ACK de revogação podia ser entendido como impedimento de qualquer efeito posterior, mesmo após permit/claim. R8 distingue pedido, bloqueio de novos starts e quiescência, sem prometer cancelamento retroativo. | Corridas estão descritas e fail-closed. O ponto de claim é lógico, não prova o início físico; `new_starts_blocked` depende de ACK durável do ECA e `confirmed_quiescent` de prova de todos os efeitos/provider. Sem protocolo/infraestrutura do ECA/provider, a semântica final e o limite da promessa continuam condicionais. | **CONDITIONAL** |
| **M-04 — GER→ER, R7-B §3; R8 §7** | R6 não distinguia adequadamente observação do TEG de efeito confirmado pelo provider. R8 propõe campos separados, projeção 1:1 e status `unknown`/quarentena. | A projeção é significativamente mais determinada e impede tratar receipt local como prova externa. Persistem ambiguidades: `started_at` não tem regra explícita para operação que falha antes de iniciar; a relação exata entre cada `provider_effect_status` e `evidence_class` não está tabelada; “verificável segundo policy” não fixa qual verificador/autoridade resolve a evidência. | **OPEN** |
| **M-05 — precedência, R7-B §3; R8 §8** | Leitor podia tratar proposta posterior como decisão ou não saber qual cláusula de R3/R5/R6/R8 prevalecia. R8 inclui uma tabela por artefato e mantém ADR como fonte aprovada. | A precedência por classe está clara, mas não há inventário cláusula a cláusula dos contratos que R8 pretende substituir, manter ou deixar em aberto. A própria §8 diz que Binding/Authorization/StartPermit “substitui a proposta anterior” somente se H-01 aprovar, sem enumerar todos os campos conflitantes de R3/R5/R6. O critério de “uma única proposta vigente por contrato” não é demonstrável só pela tabela atual. | **OPEN** |

## 3. Revisão F1–F9

| F | Avaliação R8 | Resultado |
|---|---|---|
| **F1 — COSE/CBOR e versões** | R7-A.2 `kid=bstr` permanece válido. R8 não afirma independência do backend Ed25519 e mantém perfil, migração, vetores completos e aprovação H-01 pendentes. A migração JSON v1→COSE v2, sobretudo operações pendentes/unknown, não foi demonstrada por R8 como protocolo implementável completo. | **CONDITIONAL** |
| **F2 — raízes de confiança** | R8 propõe congelamento, epoch, trustees/registro independentes e recuperação fora da raiz suspeita; não define custódia, canal de bootstrap real, operadores nem prova de independência. C-22 é plano, não evidência. | **CONDITIONAL** |
| **F3 — StartPermit** | Audience e uso único melhoram, mas fencing/claim ainda não está vinculado por contrato autenticado e checkpoint ao ponto do efeito. Não há garantia exatamente uma vez se o provider não suporta fencing/idempotência. | **OPEN** |
| **F4 — ancoragem** | R8 exige ACK durável pré-efeito e checkpoint posterior, mas não especifica que a decisão de claim/fence do ECA é parte do objeto exato ancorado antes do efeito. A janela entre ACK do checkpoint e chamada física também não pode ser eliminada por transação distribuída. | **OPEN** |
| **F5 — receipts** | Separação GER/ER e evidência provider é correta em princípio; permanecem lacunas de null/status e autoridade verificadora de evidência (M-04). | **OPEN** |
| **F6 — campanha adversarial** | C-20 corrigido aritmeticamente. C-06 ainda combina receipt persistido e checkpoint pós-efeito em um só boundary. C-13 e C-22 dependem de inventários e infraestrutura externos. | **OPEN** |
| **F7 — snapshots** | Mantém Broker como autoridade inicial, consistente com ADR 0008. A atestação do aceite humano, linhagem e consulta de status ao TEG continuam propostas e sem mecanismo operacional comprovado. | **CONDITIONAL** |
| **F8 — witnesses** | O quorum é coerente sob o limite declarado de no máximo um Byzantine e persistência monotônica honesta. Independência administrativa, pins, quorum em operação e prevenção real de split view/rollback dependem de H-06/H-08 e de implantação/testes. | **CONDITIONAL** |
| **F9 — histórico** | R8 preserva a aprovação da Alternativa B pelo ADR 0008 R2 e o caráter histórico/superado da recomendação A/B em R1, sem reabrir B. | **CLOSED_DOCUMENTAL** |

## 4. C-20: quorum e ensaios

O quorum proposto é 3 assinaturas válidas de 4 witnesses distintos. A interseção mínima entre dois conjuntos de 3 dentre 4 é `3 + 3 − 4 = 2`. Sob a hipótese explicitada de no máximo um witness Byzantine, essa interseção contém pelo menos um witness honesto. A conclusão depende também de identidade/chave correta, estado monotônico desse witness e regra de não assinar heads incompatíveis; a aritmética, sozinha, não prova independência administrativa nem liveness.

Distribuição proposta em R8 §3.3:

| Subcaso | N | Resultado esperado |
|---|---:|---|
| Controle 4/4 | 5 | Quorum e ACK durável para o mesmo checkpoint. |
| 1 indisponível | 5 | Os 3 restantes podem formar quorum se válidos e consistentes. |
| 2 indisponíveis | 5 | Sem quorum; sem efeito. |
| 1 resposta inválida + 3 válidas | 5 | Excluir inválida; 3 válidas podem formar quorum. |
| Equivocation Byzantine | 5 | Quarentena, sem escolher automaticamente uma root. |
| Split view | 15 | Quarentena, sem ACK/efeito. |
| Rollback/continuidade | 15 | Quarentena, sem aceitar head abaixo do pin ou sem prova. |
| Timeout/sem ACK durável | 5 | Bloqueado; consulta à mesma sequência/digest. |
| 1 indisponível + 1 inválida | 5 | Restam 2 votos válidos; sem quorum. |
| **Total** | **65** | **5+5+5+5+5+15+15+5+5 = 65.** |

**Resultado da revisão:** quorum 3/4 e distribuição de 65 são matematicamente consistentes como proposta; um witness offline não impede por si só 3/4, e dois offline tornam quorum impossível. Resposta inválida não equivale a indisponibilidade para análise de segurança; equivocation, split view, rollback e falta de confirmação durável têm resultados conservadores distintos. Não aprovo a ampliação da campanha H-09: os 65 casos exigem decisão humana explícita, manifesto fechado, witnesses independentes e avaliador separado. O texto também deve assegurar que um witness considerado Byzantine não seja tratado como “um voto válido entre os três” sem a política de quarentena indicada.

## 5. Cenários afetados

| Cenário | Revisão independente | Lacuna/condição remanescente |
|---|---|---|
| **C-01** | R8 fixa inventário prévio de rotas e exige manifesto; evita definir “cada rota” retrospectivamente. | O inventário de interfaces, credenciais alternativas, endpoints e processos é específico do ambiente. Sem assinatura/versão do manifesto e evidência externa, campanha pode produzir falso pass por rota omitida. **CONDITIONAL.** |
| **C-06** | R8 fixa 5 grupos ×10 = 50 injeções e exige IDs de commit e consulta da mesma operação. | Grupo 5 combina “receipt/anchor post-effect”; receipt TEG persistido e checkpoint pós-efeito são dois commits/efeitos de persistência distintos. Falha entre ambos não é injetada de forma independente. Deve separar os boundaries (por exemplo, 6×10) ou provar que não existe fronteira persistente entre eles. **OPEN.** |
| **C-08** | 8 classes ×10 = 80 fixtures; inclui mutação e tentativa de recomputar assinatura não autorizada. | Não fixa plenamente corpus/gerador, versão de verificador, independência de quem prepara fixture e evidência de “zero chamada” em provider externo. A execução requer H-01/H-03/H-09. **CONDITIONAL.** |
| **C-12** | 8 campos ×10 = 80 e fixa viewport/idioma/escala/dispositivo, sem alegar que screenshot prova compreensão. | Teste depende de dispositivo/UI confiáveis fora do host agente; aceite humano não pode ser inferido só de captura. R8 reconhece parte da limitação. **CONDITIONAL.** |
| **C-13** | Base `5 categorias × 4 categorias-destino × 10 = 200` está correta para pares dirigidos entre categorias distintas. | A contagem adicional por principal privilegiado depende de inventário IAM real e completo; categorias podem esconder principals/grupos e control planes. Manifesto ausente/incompleto torna resultado inconclusivo. **CONDITIONAL.** |
| **C-15** | Ensaio A fixa 30 minutos/30 tentativas e zero efeitos; B fixa 10 (5 ACK post-effect com resposta perdida, 5 sem ACK), só query/reconcile e zero retry. | Evidência de efeito/ledger precisa vir de provider independente escolhido. Casos sem autoridade externa de consulta não distinguem “sem efeito” de “efeito desconhecido”; devem ficar `inconclusive/unknown`, como o próprio desenho sugere. **CONDITIONAL.** |
| **C-20** | Subcasos e total 65 coerentes (§4 deste parecer). | Expansão H-09 ainda carece de aprovação e execução independente; não é homologação. **CLOSED_DOCUMENTAL** quanto à correção do desenho. |
| **C-22** | R8 define 46 (30 rejeições + 15 em 3 failpoints + 1 recuperação válida 3/4), aritmeticamente correto. | O failpoint pós-publicação/antes de pins exige especificar qual registry/pin é autoritativo e uma fonte externa para provar recuperação sem confiar na raiz comprometida. Canal e operadores ainda pendentes. **CONDITIONAL.** |

### Independência e falsos positivos

R8 frequentemente exige “evidência externa”, “avaliador independente” e “inventário congelado”, mas não há ainda uma autoridade/identidade e protocolo escolhidos para atestar o manifesto, o avaliador, o provider ledger ou os pins. Esses requisitos são bons critérios de entrada, não evidência já disponível. Testes simulados, evidência produzida pelo próprio domínio sob teste, screenshots e logs locais não comprovam isolamento administrativo, ausência de bypass ou efeito externo.

## 6. Contradições e bloqueadores remanescentes

### B-1 — claim/fence não compõe o checkpoint de intenção pré-efeito (**MAJOR**)

- **Referências:** R8 §§5.1–5.2, §6 e §3.1/C-20; R8 §5.1 define estado de claim no ECA, enquanto R8 §3.1 descreve o checkpoint pré-efeito sem um contrato de recibo/fence do ECA e sem exigir seu digest nos campos do checkpoint.
- **Requisito:** efeito somente após autorização exata, consumo único, confirmação durável pré-efeito e proteção contra clone/restore/replay.
- **Cenário mínimo:** ECA faz CAS de `permit_issued→permit_claimed`, resposta se perde; TEG consulta/reconcilia e vê o checkpoint pré-efeito que só referencia permit/consumo, não a geração/lease/fence claimada; um clone restaurado apresenta estado antigo e tenta iniciar o efeito.
- **Impacto:** não é possível verificar, a partir da intenção ancorada, qual instância/generation venceu nem se o claim foi consumido uma vez. O bloqueio genérico em unknown ajuda, mas não especifica uma prova verificável que impeça outro caminho de efeito.
- **Correção mínima:** definir recibo autenticado e idempotente do ECA com operation/effect slot, binding/auth digests, service/instance, generation, lease/fencing epoch, claim nonce, revocation epoch e estado; incluir o digest/receipt desse claim no checkpoint pré-efeito e exigir confirmação durável antes do handler. Definir recuperação quando ACK do claim ou da âncora se perde.
- **Aceitação:** teste de corrida/restore mostra apenas uma claim válida; checkpoint ancorado referencia exatamente a claim; sem confirmação, zero efeito; estado ambíguo permanece unknown sem nova generation.

### B-2 — C-06 não separa receipt de checkpoint pós-efeito (**MAJOR**)

- **Referências:** R8 §2 C-06 e R8 §7.3.
- **Requisito:** testar as janelas de persistência/recuperação e detectar falhas entre commits distintos.
- **Cenário mínimo:** efeito confirmado e GER persistido; crash antes de gravar/ancorar o checkpoint pós-efeito. O grupo único de C-06 não define qual dos dois atos foi interrompido nem uma tentativa correspondente a cada boundary.
- **Impacto:** cobertura subcontada; recovery/audit chain pode divergir apesar de C-06 passar.
- **Correção mínima:** separar persistência de GER, publicação/ACK do checkpoint pós-efeito e materialização ER em failpoints/linhas próprias, ou demonstrar que algum par é atomicamente inseparável por contrato. Fixar quantidades e evidência por boundary.
- **Aceitação:** falhas injetadas individualmente antes/depois de cada commit; mesma operation/receipt consultados; não há novo efeito; divergência observada fica unknown/quarantine.

### B-3 — envelope de receipt ainda tem semântica incompleta (**MAJOR**)

- **Referências:** R8 §7.2 (“`completed_at=null` se completion não observada”; `started_at` copiado) e §7.3.
- **Requisito:** projeção determinística, cardinalidade, optional/null/status/evidência e tratamento de divergência inequívocos.
- **Cenário mínimo:** `handler_status=failed_before_effect`, sem claim nem início físico; GER não pode dizer se `started_at` é ausente, null ou timestamp do início do handler. Ou provider status `rejected` aparece com `evidence_class=local_observation` sem regra explícita de incompatibilidade.
- **Impacto:** implementações distintas podem produzir ERs diferentes ou atribuir força probatória indevida a status externo.
- **Correção mínima:** tabela de validade cruzada `handler_status × provider_effect_status × evidence_class`, regra de nullable/null para cada timestamp e campo, definição de quem valida cada classe e prova mínima para `confirmed`, `rejected`, `not_observed`.
- **Aceitação:** vetores para todas as combinações permitidas e negativas para proibidas, com projeção byte a byte determinística e status unknown quando a evidência não satisfaz a classe.

### B-4 — precedência ainda exige interpretação do leitor (**MAJOR**)

- **Referências:** R8 §8, especialmente linhas de R3/R5/R6/R8; R7-B §3/M-05.
- **Requisito:** um contrato/proposta vigente identificável sem apagar decisões humanas nem promover propostas.
- **Cenário mínimo:** leitor compara R3 Authorization/audience com R6 e a cadeia Binding→Authorization→StartPermit de R8; a tabela por artefato não identifica, campo a campo, qual texto de R3/R5/R6 permanece histórico, é proposto para substituição, ou segue em aberto.
- **Impacto:** duas equipes podem implementar perfis incompatíveis, embora ambas afirmem seguir a precedência documental.
- **Correção mínima:** matriz por contrato e seção/campo: baseline implementado, decisão ADR aplicável, proposta anterior, delta R8, status e aprovador necessário; incluir campos removidos/renomeados e migração.
- **Aceitação:** revisão cruzada não encontra dois campos normativos concorrentes; cada campo proposto aponta para uma única origem e status.

### Outros condicionantes importantes

- O ECA é uma proposta, não uma autoridade existente ou homologada. Identidade, protocolo autenticado, durabilidade, fencing e independência do host agente dependem de H-03/H-08.
- Uma assinatura de witness e quorum matemático não provam diversidade administrativa, resistência a comprometimento comum ou split-view detection sem protocolo de comunicação/pins e operadores selecionados.
- Provider fencing/idempotency/query é requisito por operação. Para providers que não o oferecem, o desenho corretamente conclui inelegibilidade/unknown; não há garantia geral de exactly-once.
- Broker como autoridade inicial de snapshots concorda com ADR 0008, mas não resolve por si só atestação verificável do evento humano de aceite nem revogação/status remoto para o TEG.

## 7. Matriz H-01–H-10

Nenhuma decisão H é aprovada por este relatório. Recomendações abaixo são técnicas e não substituem o aprovador humano.

| H | Alternativas sob deliberação | Recomendação técnica | Dependências e risco residual | Evidência necessária | Decisão humana exigida |
|---|---|---|---|---|---|
| **H-01 — wire/crypto** | Perfil/canonicalização e migração JSON→COSE; backend Ed25519 compartilhado vs implementação independente. | Fixar perfil COSE/CBOR estrito com rejeição de formas alternativas; manter `kid=bstr`; não alegar independência enquanto backend for compartilhado. | Depende M-01/M-04; risco de interop e migração de operações pendentes/unknown. | Vetores positivos/negativos independentes, migração e rollback sem reabrir operação consumida. | Aprovar perfil, limites, backend e política v1/v2. |
| **H-02 — receipts** | GER/ER como propostas e nível de evidência externa exigido. | Fechar matriz status×evidence e timestamps antes do wire; provider receipt separado da atestação TEG. | B-3 e disponibilidade/semântica do provider. | Vetores completos e validação com ledger/provider selecionado; casos desconhecidos permanecem unknown. | Aprovar schema, emissores e critérios para confirmação. |
| **H-03 — keys/ECA/snapshot authority** | Autoridades e custódia fora do host agente; mecanismo de bootstrap/recovery. | Autoridades independentes por função, recuperação fora da raiz suspeita e ECA não controlável pelo agente; operacionalmente fail-closed até seleção/prova. | B-1; F2/F7; infraestrutura e trustees não escolhidos. | Bootstrap, rotação/revogação/recovery adversarial, prova de independência e claim durável. | Designar custodiante/operadores e aprovar autoridade, canais e recovery. |
| **H-04 — revogação** | ACK significa novos starts bloqueados vs quiescência total. | Expor distinção: `new_starts_blocked` não significa que efeito claimado foi cancelado; reservar `confirmed_quiescent` para prova forte. | Provider pode não suportar fencing/cancelamento; corrida física não eliminável. | Barreira/claim concorrentes, provider fencing e receipt de cancelamento, timeout em unknown. | Escolher semântica do produto/UI/API e risco aceito. |
| **H-05 — tempo/TTL** | Fontes e política de skew/uncertainty. | Fonte autoritativa e fail-closed quando indisponível ou ambígua; nunca relógio do cliente como fallback. | Implementação depende de ambiente operacional e leases do ECA. | Testes de relógios divergentes, atraso, expiry nas fronteiras e reinício. | Fixar fonte, tolerância, TTL e comportamento de indisponibilidade. |
| **H-06 — anchor/witness** | Manter 4/3 ou propor alternativa; protocolo de checkpoint e continuidade. | R8 mantém 4 witnesses/3 quorum; não alterar limiar nesta etapa. Exigir ACK durável, mesma root/size/epoch e evidência de continuidade. | C-20; dependente de witnesses e pins externos realmente independentes. | C-20 aprovado/executado, split-view/rollback, recuperação e ACK perdido. | Aprovar quorum, retenção, quorum semantics e confiança nos operadores. |
| **H-07 — retenção/custos** | Retenção/quotas/privacy/legal hold. | Retenção mínima suficiente para auditoria/recovery, com segregação de dados sensíveis; não fixável sem requisitos legais/operacionais. | Anchor e receipts podem conter metadados de operação. | Modelo de dados, custo, exclusão e legal hold verificáveis. | Definir retenção, limites e responsável por exceções. |
| **H-08 — infra/operadores** | Domínios, control planes, principals, break-glass e UI confiável. | Preservar Alternativa B; comprovar separação administrativa real entre Broker/TEG e independência apropriada do anchor/aprovador. | C-01/C-13/C-22; nenhuma infraestrutura/identidade foi selecionada. | Inventário IAM assinado, testes de acesso cruzado, recovery tabletop + execução adversarial. | Selecionar infraestrutura/operadores, device do aprovador e política break-glass. |
| **H-09 — campanha/evaluator** | C-20=65 e contagens/failpoints de outros cenários; avaliador próprio vs independente. | Aprovar só após fechar B-2 e congelar manifesto; usar avaliador independente e não considerar simulações como homologação. | Custos/test resources, provider, witnesses, inventários; C-20 é proposta ampliada. | Plano preregistrado, sementes/fixtures, quantidades, evidências e critérios pass/fail. | Aprovar orçamento, contagens (incluindo aumento de C-20) e avaliador. |
| **H-10 — go/no-go** | Prosseguir para RB-09C.2 ou permanecer bloqueado. | Não autorizar ainda; exigir revisão independente sem blocker documental e aprovação operacional específica posterior. | Depende de H-01–H-09 e provas de infraestrutura. | Fechamento de blockers, campanha, threat review e evidência operacional conforme escopo futuro. | Aprovar explicitamente qualquer etapa seguinte; este parecer não dá autorização. |

## 8. Bloqueadores documentais e condições de nova revisão

Antes de reconsiderar R8 como remediação documental suficiente, são necessários:

1. Contrato verificável de claim/fence ECA e sua ligação criptográfica ao checkpoint pré-efeito, ou declaração ainda mais estrita de que nenhuma operação será elegível enquanto isso não existir.
2. C-06 separado por boundary persistente e confirmável, inclusive GER e checkpoint pós-efeito.
3. Projeção GER→ER com nulls/timestamps, matriz de compatibilidade status/evidence e autoridade verificadora definidos.
4. Precedência por contrato/campo entre R3, R5, R6, R7-A.2 e R8, mantendo ADR 0008 como decisão superior e `kid=bstr` como decisão estreita já confirmada.
5. Manifestos reproduzíveis e critérios de inconclusivo para ensaios dependentes de infraestrutura; nenhuma ausência de evidência pode virar pass.

Mesmo após resolver os pontos documentais, independência administrativa, segurança do ECA, proteção contra bypass direto ao provider, comportamento do provider e ancoragem externa só podem ser aprovados com infraestrutura, operadores, identidades e evidência adversarial reais selecionados e autorizados. Proposta documental suficiente não equivale a teste, homologação ou ativação.

## 9. Validação e resultado

- Conferida a matriz B-01/M-01–M-05 contra os achados R7-B e o texto R8.
- Conferida F1–F9, preservando a correção `kid=bstr` e a aprovação da Alternativa B, sem promover outras propostas.
- Recalculado C-20: **65**; quorum: **3/4**; 1 witness indisponível deixa 3 disponíveis; 2 indisponíveis deixam 2 e não formam quorum; 1 resposta inválida pode ser excluída e deixar exatamente 3 válidas; combinação 1 indisponível + 1 inválida deixa 2 válidas.
- Conferidas as quantidades afetadas: C-01 10 processos/100 intents conforme R6; C-06 50 declaradas, mas com boundary agregado; C-08 80; C-12 80; C-13 base 200; C-15 A=30 tentativas/30 min e B=10; C-20=65; C-22=46.
- `git diff --check` executado; sem erros de whitespace. Avisos LF→CRLF referem-se a arquivos preexistentes modificados.
- Nenhuma validação operacional, de serviço, agente ou provider foi executada.

**Resultado final: REJECT da R8 como fechamento independente completo.** B-01/C-20 e F9 estão fechados documentalmente; os demais resultados e condicionantes estão discriminados acima. R8 deve permanecer proposta. H-01–H-10 não foram aprovadas, RB-09C.1 não está encerrada e RB-09C.2/RB-10 não foram iniciadas nem autorizadas por esta revisão.
