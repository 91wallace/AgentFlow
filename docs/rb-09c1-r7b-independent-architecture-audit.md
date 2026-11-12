# RB-09C.1-R7-B — auditoria arquitetural independente

**Parecer: REJECT.** A proposta R6 avançou na especificação e a correção R7-A.2 tornou `kid` compatível com o tipo COSE `bstr`, mas há uma contradição verificável na campanha de witnesses, ambiguidades de audiência/precedência entre propostas e lacunas de fencing, schema de receipts e evidência criptográfica. Este parecer não rejeita a arquitetura-alvo B, não é aprovação de implementação e não encerra RB-09C.1.

## 1. Escopo e baseline

Foram examinados integralmente ADR 0008; plano RB-09C.1/R1; R3; R5; R6; R7-A; R7-A.1; R7-A.2. Os contratos atuais foram consultados apenas para comparação: `approval.protocol`, `approval.contracts`, `teg.protocol`, `teg.contracts`, `teg.gateway` e as rotinas de recuperação do store TEG.

Baseline preservado: branch `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`; índice vazio. Já existiam modificações locais em documentos de arquitetura, ADRs 0002/0004/0005/0006/0008, plano e registros de progresso, além de `autonomous-development-runner/` e documentos R1/R3/R5/R6/R7-A/R7-A.1/R7-A.2 não rastreados. Nenhum desses arquivos foi alterado nesta auditoria. Somente este relatório foi criado.

**Evidência criptográfica delimitada:** R7-A reproduziu os quatro vetores antigos, mas usou decoder CBOR transitório e compartilhou OpenSSL; R7-A.1 bloqueou corretamente ao encontrar `kid` como `tstr`; R7-A.2 regenerou os vetores com `kid` `bstr` e os verificou usando `pycose 1.1.0`/`cbor2 5.6.5`. Essa é evidência de compatibilidade COSE/CBOR e de verificação de assinatura via a biblioteca, não independência do backend Ed25519: `pycose` usa `cryptography`, também usado na R7-A. As versões e limitações de proveniência/compatibilidade estão registradas em R7-A.2 §§2, 4–6. Não se trata de teste do AgentFlow nem de validação operacional.

ADR 0008 R2 aprova a Alternativa B como arquitetura-alvo, não sua implementação. Mantém Broker/TEG fail-closed e considera as propostas criptográficas, de revogação e ancoragem ainda abertas. A nota R6 atualiza R1.7/R1.8 sem apagar o histórico; isso está coerente com a decisão aprovada.

## 2. Matriz F1–F9

| Achado | Estado | Síntese independente |
|---|---|---|
| F1 — perfil criptográfico | **PARTIAL** | R6/R7-A.2 dão perfil COSE/CBOR e vetores corrigidos verificáveis por `pycose`; a independência do backend Ed25519 não foi demonstrada. A migração v1→v2 está proposta, não implementada. Também permanece conflito de audiência do `ExecutionBinding` entre R3 e o vetor R6. |
| F2 — raízes de confiança | **PARTIAL** | Bootstrap externo, epochs e recovery 3-de-4 são descritos, mas canal/registro, custódia, independência de trustees e recuperação de comprometimento não têm autoridade ou infraestrutura escolhida nem evidência externa. |
| F3 — StartPermit | **PARTIAL** | Campos, audiência, expiração e linearização no Broker estão descritos; faltam mecanismo demonstrável de fencing/instância única contra clone/restore e garantia contra corrida entre revogação confirmada e início físico. A limitação in-flight é reconhecida, mas a implementação não oferece permit. |
| F4 — ancoragem | **PARTIAL** | R6 define checkpoint pré-efeito incluindo autorização, consumo, permit e confirmação durável antes da chamada, além de checkpoint pós-efeito. Protocolo/quórum/âncora não estão aprovados nem implementados; falha/ACK ambíguo segue sem evidência operacional. |
| F5 — receipts | **PARTIAL** | GER primário, ER projetado 1:1, divergência em `unknown` e evidência externa são princípios úteis. O schema/vetor não fecha todos os campos de estado do provider nem prova externa específica; os contratos Python atuais são tipos distintos sem envelope ou projeção. |
| F6 — campanha C-01–C-22 | **FAIL** | C-20 manda quarentena “sem quorum” quando um witness está indisponível, mas com quatro witnesses e quorum 3/4 os três restantes ainda formam quorum, se consistentes. Falta também separar indisponibilidade simples de falha de quorum e provar os subcasos. |
| F7 — snapshots | **PARTIAL** | R6 reconcilia a autoridade inicial do Broker com ADR 0008 e propõe atestação de aceite, linhagem e consulta online pelo TEG. Emissor/status API e prova do evento humano ainda dependem de decisões/infraestrutura e não existem nos contratos atuais. |
| F8 — witnesses | **PARTIAL** | Quorum 3/4, continuidade e split view têm argumento de segurança sob até um Byzantine; independência real, pinning fora dos domínios, troca de heads e recuperação continuam propostas. A campanha C-20 contradiz a semântica do quorum. |
| F9 — histórico | **PASS** | Plano R1 §§R1.7/R1.8 identifica explicitamente que a escolha A/B foi supersedida pela aprovação da Alternativa B no ADR 0008 R2, mantendo a tabela e os critérios como histórico e preservando pendências independentes. |

`PASS` em F9 é apenas consistência documental histórica. Nenhuma classificação equivale a aprovação humana de H-01–H-10 ou homologação operacional.

## 3. Achados por severidade

### BLOCKER

**B-01 — C-20 contradiz o quorum declarado (F6/F8).**

- **Referência:** R6 §10 (“quatro witnesses, quorum 3-de-4”; dois indisponíveis deixam sem quorum) e R6 §9, cenário C-20 (“exatamente 5 com witness indisponível”; “sem quorum nos cinco: quarentena”).
- **Requisito violado:** campanha reproduzível deve ter resultado compatível com a política de quorum e critérios pass/fail objetivos.
- **Cenário mínimo:** existem quatro witnesses; um fica indisponível; os outros três, honestos e consistentes, assinam o mesmo root. O quorum 3/4 está alcançado, então a regra de ausência de quorum não se aplica. A linha C-20, contudo, exige negar ACK nesses cinco casos por indisponibilidade.
- **Consequência:** teste pode reprovar uma execução segura ou confundir falha de quorum com conflito de roots; resultados não validam o modelo 3/4 declarado.
- **Correção mínima:** separar (a) 1 witness indisponível + 3 assinaturas válidas, que deve atingir quorum, de (b) 2 indisponíveis ou assinaturas insuficientes/conflitantes, que deve bloquear. Declarar se um dos 15 casos de split roots deixa menos de três assinaturas válidas.
- **Aceitação:** executar contagens fixas dos dois grupos; em (a) a confirmação é aceita somente com três witnesses independentes e consistentes; em (b) nenhum ACK pré-efeito é emitido, serviço fica em quarentena e o evento é registrado fora do domínio testado.

### MAJOR

**M-01 — audiência do `ExecutionBinding` não está reconciliada entre contratos propostos (F1/F3).**

- **Referência:** R3 §3.2 define `ExecutionBinding` como mensagem TEG → Broker para autorização; R3 §4.1 o associa ao Broker. R6 §3.1 define audience como identidade exata do destinatário, mas o vetor R6 §4.2 fixa issuer `teg.example` e audience `teg:instance-7` (o próprio TEG). R6 §6 diz que o Broker emite StartPermit usando o binding; não esclarece se há envelope/cópia diferente para o Broker.
- **Requisito violado:** audiência literal deve corresponder ao receptor, sem reutilização cross-service.
- **Cenário mínimo:** o TEG envia ao Broker o envelope do vetor. Um verificador que aplica a regra de audience exata rejeita `teg:instance-7`; um verificador que aceita essa audience não está validando que a mensagem era destinada ao Broker.
- **Consequência:** interoperabilidade e prevenção de replay cross-service não podem ser implementadas de modo unívoco; R7-A.2 validou o valor contra a descrição do vetor, não contra o fluxo receptor.
- **Correção mínima:** definir envelope e audience por direção/cópia. Para mensagem TEG→Broker, audience deve ser o Broker; se a audience do vetor for para uso local do TEG, documentar que não é a mensagem submetida ao Broker e gerar vetores separados. Marcar expressamente que R6 substitui a semântica conflitante de R3, se essa for a decisão pretendida.
- **Aceitação:** vetor positivo para o destinatário correto e negativos assinados para Broker/TEG/ambiente diferentes; receptor rejeita envelope válido criptograficamente mas com audience incorreta.

**M-02 — uso único de permit não está cercado por fencing demonstrável entre clones/restores (F3/F4).**

- **Referência:** R6 §6 atrela permit a `gateway_instance_id`/`gateway_epoch` e registra emissão no Broker; R6 §§7–8 exige que `unknown` não seja repetido e que restore consulte estado. Não define autoridade monotônica que emita/invalide `gateway_epoch`, nem um redemption/fencing token atômico no início do efeito. R6 §10 anota que provider pode não oferecer idempotência.
- **Requisito violado:** impedir duas instâncias/restores de executar o mesmo slot após falha ambígua, sem pressupor transação distribuída nem idempotência do provider.
- **Cenário mínimo:** duas cópias restauráveis do mesmo TEG possuem a mesma identidade/epoch e permit. Uma copia está desatualizada e não observa o journal local da outra. Se ambas atravessarem a validação de permit antes de uma nova evidência compartilhada de consumo/início, o provider sem idempotency key pode receber duas chamadas. O anchor aceita checkpoint idêntico como idempotente, portanto a repetição do ACK por si só não é fencing de efeito.
- **Consequência:** o limite “uma invocação” depende de operação single-instance e disciplina de restore ainda não comprovadas; ausência de registro pós-efeito não distingue nunca iniciado de já comitado.
- **Correção mínima:** exigir lease/epoch monotônico e exclusão de instância sob autoridade independente, ou rejeitar execução em failover/clone até reconciliação que prove ausência de efeito; acrescentar protocolo de redemption one-use/fencing no ponto imediatamente anterior ao handler. Se provider não puder garantir idempotência/fencing, manter o slot `unknown` e bloqueado, sem retry.
- **Aceitação:** dois processos/VMs com mesmo snapshot de DB/permit, sincronizados por barreira, competem para obter fencing; no máximo um inicia handler. Restore antigo é rejeitado; falha após commit remoto fica `unknown`, sem segunda chamada, mesmo com resposta perdida.

**M-03 — contrato de revogação permite efeito iniciar após ACK (F3).**

- **Referência:** R6 §6 define permit antes de revogação como `in-flight`; §7 executa consulta de revogação antes de `effect_possible`, mas admite janela até provider; R3 §§5.2–5.4 declara que ACK bloqueia novos permits, não cancela permit já emitido; R6/H-04 deixa o aceite da janela pendente.
- **Requisito examinado:** verificar explicitamente corrida entre revogação confirmada e início físico. O contrato atual proposto não promete “nenhum efeito após ACK”; isso está assumido como limite, não pode ser apresentado como prevenção completa.
- **Cenário mínimo:** StartPermit lineariza; Broker confirma revogação e responde ACK; TEG, que já tinha permit e passou sua última consulta, chama o provider depois do ACK.
- **Consequência:** usuário/operador pode interpretar “revogado” como efeito impedido quando a operação in-flight ainda pode produzir efeito. Não é uma falha contra a semântica estreita “bloqueia novos permits”, mas é garantia mais fraca que bloqueio de início físico.
- **Correção mínima:** H-04 deve aprovar explicitamente a semântica in-flight e informar o ACK; para garantir nenhum início físico depois do ACK, exigir fencing/epoch validado atomicamente pelo provider/TEG no commit do efeito, ou declarar essa classe de operação inelegível. Não prometer cancelamento remoto genérico.
- **Aceitação:** teste injeta revogação entre permit/última consulta e chamada; resultado tem de ser ou recusa pelo fencing, ou status in-flight explicitamente permitido pela policy aprovada — jamais um ACK apresentado como cancelamento.

**M-04 — projeção de receipt não fecha o schema de status de efeito externo (F5).**

- **Referência:** R6 §8 projeta ER `external_effect_status` de GER `provider_effect_status` e exige campos/nulls fechados. Já o vetor R6 §4.3 enumera GER com `status=unknown`, `effect_state=possibly_started`, `effect_reference=null`, `provider_evidence=null`; não enumera campo distinto `provider_effect_status`/classe/status da evidência. R6 §8 também prevê GER como fonte primária, enquanto R6 §4.3 é apresentado como vetor completo.
- **Requisito violado:** projeção campo a campo verificável, distinguindo resultado observado pelo TEG de confirmação do provider.
- **Cenário mínimo:** um verificador recebe GER do vetor e tenta construir o ER. Não há campo declarado a partir do qual derivar `external_effect_status`; inferir a partir de `status` confunde resultado do handler com efeito remoto.
- **Consequência:** perfil não define se ER recebe `unknown`, `unavailable` ou outro estado quando o GER não tem evidência do provider; pode criar semântica de sucesso divergente.
- **Correção mínima:** publicar schema GER normativo com campo obrigatório `provider_effect_status` e `evidence_class` (valores e nullable definidos), atualizar vetor completo ou declarar explicitamente uma projeção de `unknown` sem inferência; fixar cardinalidade/IDs e assinante.
- **Aceitação:** vetores para todos os estados provider (`confirmed`, `rejected`, `unknown`, `unavailable`) mais divergência; o verificador reconstrói ER byte a byte e rejeita qualquer estado externo sem evidência independente requerida.

**M-05 — proposta R6 não delimita precedência normativa perante R3/R5 (F1–F8).**

- **Referência:** ADR 0008 R2 §§“Reconciliação com RB-09C.1-R1” mantém R1 como referência e diz que parâmetros continuam propostas; R3 §3.2 inclui `execution_binding_digest` em ExecutionAuthorization; R6 §4.1 exclui esse vínculo para evitar ciclo e passa a encadear o binding ao authorization. R5 propõe um só checkpoint pré-efeito após permit; R6 o detalha, mas mantém versões/propostas anteriores sem tabela de supersessão por contrato.
- **Requisito violado:** consumidor/revisor precisa saber qual versão de cada proposta está em deliberação sem converter silenciosamente proposta em decisão.
- **Cenário mínimo:** implementador segue R3 e exige binding digest dentro da Authorization, enquanto gerador do vetor R6 não o inclui; ambos citam ADR 0008 como prevalente e não encontram decisão que selecione uma forma.
- **Consequência:** versões incompatíveis podem ser implementadas ou testadas com critérios diferentes. O R6 ser mais recente sugere intenção, mas não é decisão humana aprovada nem matriz explícita de substituição.
- **Correção mínima:** adicionar tabela R3/R5→R6, por contrato/seção, indicando “substituída”, “mantida” ou “ainda aberta”; registrar que nada se torna aprovado e que ADR 0008 continua prevalente.
- **Aceitação:** revisão documental independente percorre cada contrato/etapa e obtém exatamente uma proposta vigente; testes/vetores citam essa versão e a aprovação humana continua pendente.

## 4. Inconsistências transversais

1. **Válida não significa executável.** R6 distingue assinatura/validade de uso: o verificador ainda precisa consultar expiração, revogação, snapshot status, reserva, audiência, anchor e consumo. Isso está conceitualmente correto; o protocolo de consulta e o ponto de consistência online permanecem pendentes de H-03/H-04/H-05/H-06.
2. **Revogação versus execução:** a semântica aceita permit in-flight após ACK. Não há contradição interna se ACK significar apenas “bloqueio de novos permits”; falta decisão explícita de risco H-04 e fencing para prometer bloqueio do início físico. Ver M-03.
3. **Unknown e repetição:** R6 preserva `unknown`, consulta/reconcilia e proíbe retry. É uma garantia de protocolo proposta. Porém, contra clones do TEG, sua observância depende de estado/lease não rollbackável e identidade de instância não clonável; ver M-02.
4. **Snapshot/consentimento/efeito:** a cadeia de digests vincula os objetos, mas assinatura do Broker demonstra apenas emissão; assinatura TEG demonstra relato; somente evidence provider externa pode afirmar commit remoto. O documento declara esse limite corretamente. Fonte SDD e evidence provider são decisões/integrações pendentes.
5. **Dependências circulares:** R6 evita ciclo Authorization↔Binding removendo binding digest de Authorization e põe ambos no StartPermit/checkpoint. Esse encadeamento é acíclico dentro de R6; o R3 ainda listado como referência faz a semântica ambígua (M-05). O checkpoint pré-efeito único após permit é possível sem ciclo: o digest do permit já existe antes do checkpoint, e o receipt pós-efeito fica num checkpoint separado.
6. **Âncora:** ACK pré-efeito confirma inclusão/ordem de intenção e autorização referenciada, não a verdade do evento nem efeito provider. ACK pós-efeito confirma inclusão do relato/prova anexada, não corrige ausência de evidence externa. A disponibilidade do anchor fail-closed antes do efeito e `unknown` depois está corretamente proposta, mas nenhum serviço, testemunha, retention ou teste real prova isso.

## 5. Avaliação C-01–C-22

| Cenário | Avaliação independente | Lacuna concreta / condição |
|---|---|---|
| C-01 | PARTIAL | 1 lab/10 processos/100 intents e evidência externa estão definidos; “tentar cada rota/interface” não enumera as rotas, permitindo cobertura variável. Fixar inventário de interfaces/ACL/network endpoints antes da execução. |
| C-02 | PASS (plano) | Cinco classes e 50 tentativas estão nomeadas; zero efeito externo e evidência Broker/TEG/provider são critérios observáveis. Não cobre efeitos locais arbitrários, limitação assumida. |
| C-03 | PASS (plano) | A soma é consistente: 10 idênticas + 10 cross-instance + 10 expiradas + 80 mutações = 110 requests adicionais; critérios distinguem retorno idempotente das 100 recusas. Requer verificar contagem de efeitos em ledger externo. |
| C-04 | PASS (plano) | 10 IDs × (64 iguais + 16 divergentes) = 800 requests, barreira/processos independentes e restart especificados. Requer verificar que “um efeito” se afirma somente para provider que comprove idempotência. |
| C-05 | PASS (plano) | Cinco pontos × dez = 50; limites antes/depois de persistência distinguem zero efeito de unknown. A recuperação precisa comprovar que o ponto de injeção ocorreu após o commit descrito. |
| C-06 | PARTIAL | Cinco commits × dez = 50, mas a linha não fixa qual serviço é reiniciado em cada fronteira nem quais commits/ACKs remotos provam que a resposta foi perdida após persistência. Alinhar os 5 failpoints ao C-07 e registrar lado remetente/receptor. |
| C-07 | PASS (plano) | Seis pontos × dez = 60 e provider ledger/fault proxy permitem distinguir efeito desconhecido de duplicação. O “provider não aceitou” deve ter evidência forte de não commit; ausência eventual na busca não basta. |
| C-08 | PARTIAL | Oito campos/mutações e resultado 8/8 estão indicados, mas não são fixados explicitamente repetição, identidade da fonte independente e tentativas de recomputar assinatura/digest. Fixar 8 fixtures e prova de recusa pelo TEG antes do efeito. |
| C-09 | PASS (plano) | Quatro combinações de restore, teste de query/permit e quarentena definidos. Deve incluir clone concorrente do mesmo TEG/epoch; atualmente não fecha essa ameaça (M-02). |
| C-10 | PASS (plano) | Seis alterações e verificação externa contra anchor/witness estão descritas. Resultado para evento ainda não ancorado é reconhecido como janela residual/inconclusivo. |
| C-11 | PASS (plano) | Oito classes × dez = 80, decision journal e ausência de autorização positiva. Simulador não prova UI ou consentimento humano real, explicitamente reconhecido. |
| C-12 | PARTIAL | Oito campos × dez = 80 e capture/device+digest são previstos; falta condição objetiva para “conteúdo integral visível” em dispositivo/viewport e a evidência visual não prova compreensão. Incluir inspeção independente da UI implantada e bloquear em caso de truncamento. |
| C-13 | PARTIAL | 5 domínios × 4 pares dirigidos × 10 = 200 tentativas; porém “domínio” mistura witness e identidade do aprovador, que podem ter mais de uma autoridade/admin. Fixar principals, control planes e matriz de tentativas; tabletop sozinho não prova IAM. |
| C-14 | PASS (plano) | 30 tentativas/30 min, particionadas antes e depois da reserva; nenhum replay automático após reconexão. Falta DNS split parcial, mas limitação é explicitada. |
| C-15 | PARTIAL | Ensaio A tem 30 tentativas/30 min; B tem 10 efeitos e ACK pós-efeito perdido, sem duração e sem matriz de onde a âncora fica indisponível (somente resposta ou serviço). Fixar esses fatores e fonte autoritativa do efeito. |
| C-16 | PASS (plano) | 7 offsets × 3 fronteiras × 3 repetições = 63; igualdade/incerteza negam. Requer source de tempo real homologada, não NTP simulado, para alegar plataforma. |
| C-17 | PASS (plano) | 100 consume/revoke + 100 permit/revoke; 20 respostas perdidas em cada subgrupo, com barreira/processos e ordem do commit autoritativo. Correto como ensaio adicional ao C-05. |
| C-18 | PASS (plano) | 10 commits remotos e somente query após perda/crash; evidência independente e regra `unknown` sem prova estão especificadas. A validade depende do ledger/query forte do provider escolhido. |
| C-19 | PASS (plano) | 10 dimensões × 10 = 100, divergência resulta em unknown/quarentena; segunda implementação do verificador é requerida para evitar parser compartilhado. |
| C-20 | **FAIL** | 30 casos, incluindo 15 split roots/15 rollback, mais cinco com witness indisponível; a regra “sem quorum nos cinco” contradiz quorum 3/4 quando somente um dos quatro está indisponível e os outros três concordam. Não define se esses casos também introduzem conflito. Corrigir conforme B-01 e testar indisponibilidade de 1 e 2 witnesses separadamente. |
| C-21 | PASS (plano) | 40 antes do bound + 40 expiradas + 20 igualdade/incerteza = 100; revoke separado no C-17. A ordem deve vir do commit transacional e relógio homologado, não apenas timestamps. |
| C-22 | PARTIAL | 30 inválidos (10 manifestos da raiz antiga, 10 epochs em rollback, 10 recovery cert inválidos/replay) e 1 recovery 3/4 aceito são contagens claras. Não especifica o failpoint entre substituição da raiz e atualização do registry nem como a independência da autoridade externa será provada; precisa de passos e evidência para não aceitar o próprio registry como testemunha de si mesmo. |

Todas as linhas são planos, não execuções. “PASS (plano)” avalia somente a definição do ensaio e não sua execução, suficiência da infraestrutura ou aprovação do controle.

## 6. Decisões H-01–H-10 pendentes e dependências

O grafo R6 é acíclico em alto nível: H-01 precede os schemas; H-02/03/05 dependem do perfil; H-04 depende de chaves e tempo; H-06 depende de H-01–H-05; H-07 de receipts/âncora; H-08 de infraestrutura e custódia; H-09 de recursos e campanha; H-10 de tudo e de autoridade de risco. R6 não torna qualquer nó aprovado.

| H | Situação nesta auditoria | O que falta antes de aprovação verificável |
|---|---|---|
| H-01 | **Aberta; não pronta para aprovação integral** | Resolver audience/precedência M-01/M-05; vetor de schemas completos e teste independente; backend Ed25519 independente ou registrar formalmente que a evidência é só COSE/CBOR. Aprovação do perfil, limites, algoritmo e migração é humana. |
| H-02 | **Aberta; condicional** | Schema GER/ER fechado, campo provider status/evidence, projeção completa, emissor/audience, política de provider sem prova. |
| H-03 | **Aberta** | Autoridade de bootstrap, canais/operadores/trustees, custódia, KMS/HSM, epochs, recuperação após comprometimento e rollback demonstráveis. |
| H-04 | **Aberta; decisão de risco necessária** | Aceitar explicitamente que ACK de revogação apenas bloqueia novos permits, ou exigir fencing para impedir início físico posterior; definir tratamento de in-flight e semântica do permit. |
| H-05 | **Aberta** | Fonte de tempo, incerteza máxima, skew, TTL e comportamento de falha aprovados e testáveis em plataforma-alvo. |
| H-06 | **Aberta; bloqueada por B-01 e H-01–H-05** | Quorum coerente, âncora/witness, ACK durable, pinning independente, outage, retenção e recuperação. |
| H-07 | **Aberta** | Retenção numérica, privacidade, custo, backup/restore e legal hold. |
| H-08 | **Aberta** | Infraestrutura, operadores independentes, identidades, break-glass, ambiente e custos autorizados. |
| H-09 | **Aberta; bloqueada por C-20** | Campanha corrigida, evidências externas, evaluator e regras inconclusivas; fixar itens parciais listados em §5. |
| H-10 | **Aberta** | Só considerar após H-01–H-09, revisão independente sem MAJOR/BLOCKER aberto e autorização humana explícita. |

Decisões de princípio prontas para reafirmação, sem equivaler a aprovação de H: Alternativa B continua aprovada; `kid` é `bstr` conforme R7-A.2; não há fallback/retry de efeito `unknown`; Broker e TEG ficam fail-closed. Nenhuma escolha de infraestrutura, operadores, chaves, retenção, custos ou operação é presumida.

## 7. Bloqueadores objetivos e critérios de correção

RB-09C.1 não deve avançar a decisão de encerramento até que:

1. B-01 seja corrigido e C-20 avaliado por revisor independente contra o modelo 3/4, incluindo 1 witness indisponível (3 assinaturas), 2 indisponíveis (2 assinaturas, sem quorum) e roots conflitantes.
2. M-01 e M-05 fechem a semântica de audiência e a precedência por contrato, sem aprovar silenciosamente proposta.
3. M-02 defina e teste fencing/lease/epoch ou uma política operacional que mantenha clone/restore com permit potencialmente usado em quarantine até evidência autoritativa; nenhuma suposição de idempotência de provider pode substituí-la.
4. H-04 declare, com UX/receipt compatíveis, a semântica de revogação in-flight; se exigir não iniciar após ACK, demonstrar fencing no ponto do efeito.
5. M-04 feche os schemas e a projeção GER→ER com todos os estados e evidências externos.
6. C-01, C-06, C-08, C-12, C-13, C-15 e C-22 tenham quantidades, atores, failpoints, fontes independentes e resultados pass/fail sem interpretação variável.
7. Evidência R7 de duas bibliotecas continue classificada corretamente: `pycose` confirma COSE/CBOR e assinatura via backend compartilhado; não declarar independência Ed25519 até uma implementação/backend distinto ser reproduzido, ou uma decisão humana delimitar formalmente a garantia aceita para H-01.

## 8. Parecer final

**REJECT** para aceitar as correções F1–F9 ou encerrar RB-09C.1 neste estado. O bloqueio demonstrável é a contradição C-20/3-de-4; há achados MAJOR sobre destinatário de `ExecutionBinding`, fencing de permit/restore, semântica in-flight de revogação, projeção de receipts e precedência documental. Os demais itens são parciais por propostas plausíveis ainda sem evidência ou decisões operacionais, não por suposta implementação.

O parecer não afirma que Broker/TEG estão ativados. Os contratos Python atuais são JSON/digest v1, sem assinaturas COSE, StartPermit ou anchor. `GatewayAuthority` não define StartPermit/anchor; `UnavailableGatewayAuthority` falha fechado. O fluxo `TrustedExecutionGateway.submit` chama o handler após confirmação de consumo (`src/agentflow/teg/gateway.py:258–371`), de modo que esse fluxo não implementa as barreiras futuras R6. Isto é um limite/incompatibilidade para implementação futura, não evidência de serviço operacional: não se deve conectar uma autoridade/handler operacional antes de integrar esses gates. `AcceptedSnapshot`, `ExecutionAuthorization` e `ExecutionReceipt` atuais permanecem contratos JSON v1 com digests (`src/agentflow/approval/protocol.py:17,189,381,418`); `ExecutionBinding` e GER atuais são protocolo JSON v1 sem assinatura (`src/agentflow/teg/protocol.py:70,393`).

Nenhum documento prévio, código ou teste foi alterado; RB-09C.2 e RB-10 não foram iniciadas. H-01–H-10 permanecem sem aprovação automática.
