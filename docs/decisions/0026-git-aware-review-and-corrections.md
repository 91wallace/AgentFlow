# ADR 0026 — revisão Git incremental e correções no APL

Status: implementada na fatia autorizada de revisão/correção após a ADR 0025.
Não autoriza commits/push, worker independente, MCP ou reorganização M3/M4.

## Composição e compatibilidade

O APL permanece a autoridade de execução e conclusão. A CLI compõe
`ReviewedJobExecutor` sobre o executor autorizado existente e
`ReviewAcceptanceValidator` sobre os gates independentes. Provedores, clientes
de modelos, rastreador e journals são injetados; APL não importa adaptadores
concretos. Não há outra fila, scheduler ou banco. O runtime e planejador
headless existentes atendem o revisor selecionado e o executor corretor em
sessões separadas. A aprovação do modelo não substitui os gates.

Novos pedidos configurados por papéis usam revisão automática por tarefa.
`--review-mode off` é explícito. Pedidos v8 sem política de revisão conservam
identidade, digest e comportamento original na retomada. Política e identidade
do código do motor entram no pedido/contrato; uma versão diferente não reutiliza
aprovação antiga silenciosamente. Marco, sob demanda e adaptativo continuam
reservados: nenhum deles é anunciado como implementado.

## Baseline, autoria e publicação

`git.change_tracker` reutiliza o provedor Git e o lease de repositório existentes.
Captura HEAD/ref, identidade da raiz/common-dir, hash bruto do índice, entradas
staged e conteúdo/hash/modo dos arquivos tracked e untracked. Duas capturas
consecutivas devem concordar. Arquivos operacionais `.agentflow` e caches locais
não entram como código. Nomes sensíveis conhecidos ficam somente com hash.
Conflitos, submódulos, links e caminhos inseguros recusam a captura.

Antes do executor, alterações preexistentes que cruzam o escopo bloqueiam o
dispatch, preservando os bytes. Fonte ignorada pelo Git dentro do escopo também
bloqueia, pois sua preimagem não está na baseline normal; caches operacionais
conhecidos são excluídos dessa checagem. Arquivos de tarefas anteriores só contam como
pertencentes à execução quando tarefa concluída e revisão aprovada da mesma
execução possuem conteúdo/índice/HEAD/identidade ainda exatos. Mudança externa
invalida esse reconhecimento. Diffs distinguem working tree e índice; renomeações
de conteúdo idêntico são vinculadas quando a correspondência é única. Outras
renomeações aparecem conservadoramente como remoção e adição.

Mudanças fora do escopo, sobre conteúdo preexistente ou acompanhadas de mudança
de identidade são ambíguas. O motor não restaura, faz reset, checkout ou limpa
arquivos. Correções automáticas só editam arquivos existentes atribuíveis à
tarefa e citados nos findings. O modelo devolve JSON com conteúdo UTF-8 e hash
anterior; publicação exige fingerprint completo e preimagem atuais, revalida
autorização/cancelamento e usa substituição atômica por arquivo com fsync.
Índice, commits e referências não são alterados pelo corretor. Findings sobre
índice/remoções exigem reconciliação, sem inventar uma correção destrutiva.

O lease é cooperativo: não equivale a sandbox contra processos externos. Há
checagens antes/depois de escrita, mas não uma transação atômica envolvendo
Git/SQLite/filesystem inteiro. Se uma publicação parcial for observada após
falha ou concorrência, preservar e bloquear é preferível a desfazer conteúdo
de autoria incerta. ACLs e isolamento OS não foram homologados nesta fatia.

## Contexto, findings e limites

O pacote inclui diffs, arquivos alterados, funções/classes Python modificadas,
imports locais, contratos e testes relacionados, critérios e gates. Arquivos
adicionais podem ser pedidos por `INCOMPLETE`, com até duas expansões. Não há
truncamento silencioso de diff: limites de arquivos, texto ou payload bloqueiam.
Não se afirma análise semântica completa para todas as linguagens.

Resposta v1 exige estado, findings e pedidos de contexto. Cada finding contém
ID, arquivo, linhas, severidade, evidência literal, descrição, impacto, sugestão,
confiança e resolução. O Core verifica localização e trecho no snapshot,
confiança e schema, e atribui ID estável. `source_anchor_verified` comprova a
origem da evidência, não prova formalmente a interpretação semântica do modelo.
Findings inventados ou sem âncora válida não geram correção confirmada.

Cada correção é seguida de gates e revisão nova. Limites configuráveis incluem
correções, chamadas, segundos, bytes de payload e ausência de progresso; o grant
também limita duração e tentativas. Bytes são medição local, não tokens nem
hard cap financeiro. Não há troca silenciosa de agente/modelo. Ao bloquear,
findings pendentes, recibos e motivos seguem ao observador/orquestrador existente.

## Durabilidade e recuperação

Migração aditiva SQLite v9, backup-first no migrador existente, acrescenta
sessões/eventos imutáveis ligados ao run, ciclo, job e operação oficiais.
Baseline, binding, snapshots, contexto/digest, findings, publicações, gates,
resolução, uso local e resultado final são persistidos. Não duplicam a máquina
de estados de tarefas. `RoleCallJournal` registra intenções antes de inferência
e publicação; resultados completos reutilizam o mesmo ID. Eventos têm chave
idempotente e digest, e triggers impedem update/delete.

Cache é restrito à operação e às identidades exatas de conteúdo, contexto,
papéis, política e implementação. Mudanças invalidam o reaproveitamento;
o fingerprint de todo o inventário torna a invalidação conservadora. Não há
cache semântico entre tarefas.

Recuperação preserva a regra anterior de `unknown`: intenção sem resultado ou
interrupção entre publicação e confirmação não dispara executor, modelo ou
publicação novamente. Não existe reconciliação automática de toda janela de
crash nesta fatia. Aprovação durável só satisfaz gates com estado atual exato;
retomada após tarefa já concluída usa os checkpoints existentes. `review-status`
consulta eventos e operações pendentes sem migrar/escrever o banco.

Evidências, comandos e limitações operacionais estão no
[progresso persistente](../autonomous-platform-progress.md).
