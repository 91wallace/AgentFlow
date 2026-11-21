# ADR 0022 — sondagem de identidade após crash com WAL

Status: correção pontual encontrada durante a validação de M2.3 em 2026-11-21.
Complementa a ADR 0021 para preservar seu critério de recuperação; não constitui
extração de store nem mudança de schema ou de unidade transacional.

## Evidência

A primeira suíte M2.3 terminou com 806 testes aprovados, 1 ignorado e uma falha
no teste de reinício do serviço após terminar seu subprocesso à força.
O erro ocorreu em `PRAGMA application_id` da conexão `mode=ro`, antes da aquisição
do lock do Broker: `SQLITE_IOERR_TRUNCATE` (1546).

O mesmo teste falhou em uma cópia dos fontes anteriores a M2.3, incluindo o
subprocesso usando aquela cópia. Portanto não é regressão dos contratos ou da
remoção do ciclo. Um experimento nessa referência passou ao sondar cópias privadas
do banco e do WAL, sem tornar o banco de origem gravável para reconhecer sua
identidade. Diagnósticos locais ficam em `.pytest_cache/agentflow-m23-*.log`.

## Decisão e limites

O caminho normal permanece a leitura de identidade via URI `mode=ro`. Somente
o erro estendido `SQLITE_IOERR_TRUNCATE` aciona a sondagem isolada:

1. Registrar identidade de arquivo, tamanho e timestamps do banco e do WAL.
2. Copiar ambos para diretório temporário privado; não copiar SHM ou lock.
3. Recusar se os metadados das origens mudarem durante a cópia.
4. Abrir somente a cópia com permissão de escrita, permitindo que SQLite
   reconstrua seu estado auxiliar naquele diretório. Aplicar as mesmas verificações
   de application_id e tabelas usadas no caminho normal, considerando o WAL.
5. Fechar/remover a cópia. Só então prosseguir pelo caminho original: adquirir
   o lock e validar novamente a identidade na conexão real, antes das mutações
   normais, migração, verificação de integridade e recuperação.

Nenhuma cópia é restaurada na origem. Ela não fornece evidência de execução,
autoridade ou conteúdo operacional a serviços. A checagem de metadados não é um
backup atômico nem prova contra um atacante controlando o filesystem; serve para
recusar mudanças detectadas durante essa sondagem. As verificações da conexão
real e o lock continuam obrigatórios.

Erros diferentes de `SQLITE_IOERR_TRUNCATE` não acionam fallback. Falta de espaço,
permissão, cópia ilegível ou origem alterada causam erro conservador, sem tentar
uma abertura gravável de banco ainda não identificado. A regra de recusar banco
estrangeiro antes de criar `.lock` permanece, incluindo tabelas presentes só no WAL.

O fallback usa biblioteca padrão e espaço temporário proporcional ao banco/WAL,
somente nessa condição excepcional. Não altera formatos, digests, assinaturas,
eventos, estados ou transações. A recuperação não presume sucesso de jobs ou gates.

## Verificação

O teste existente de reinício após crash passou sem remover WAL, inserir sleeps,
fazer checkpoint antecipado ou enfraquecer assertions. Os testes existentes de
persistência também passaram: 27 casos no conjunto focado.

Seis casos adicionais em `tests/test_broker_identity_probe.py` cobrem preservação
de bytes de origem/limpeza temporária, rejeição de banco estrangeiro com/sem WAL,
recusa de alteração durante cópia, falha de permissão e ausência de fallback para
outro erro SQLite. A suíte completa é repetida após esta correção.

Resultado final em Windows/Python 3.13: **813 aprovados e 1 ignorado** em
361,85 segundos. O teste de crash original permanece inalterado e passou também
no conjunto focado de 27 casos. Os seis novos casos passaram; `git diff --check`
passou. Nenhum schema, migração, estado de execução ou transação foi alterado.
