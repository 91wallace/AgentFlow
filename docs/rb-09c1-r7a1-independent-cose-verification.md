# RB-09C.1-R7-A.1 — verificação COSE independente

**Resultado: BLOCKED.** Foi encontrada uma biblioteca COSE consolidada e ela foi instalada em ambiente temporário isolado, mas não consegue decodificar os vetores R6 porque o valor protegido do cabeçalho COSE `kid` está codificado como texto CBOR, enquanto a biblioteca valida o tipo normativo como byte string. A verificação foi interrompida nesse ponto, conforme instruído. Nenhum parser/encoder manual ou alternativa improvisada foi usado para contornar a rejeição. Não foi possível reproduzir assinaturas, digests e vínculos por essa biblioteca.

Este resultado identifica uma incompatibilidade a deliberar; não corrige nem altera os vetores. Não é homologação operacional, aprovação de H-01, encerramento de RB-09C.1 ou autorização para RB-09C.2.

## 1. Escopo e preservação

Foram lidos integralmente `docs/rb-09c1-r6-contract-closure.md` e `docs/rb-09c1-r7a-crypto-verification.md`. O estado Git observado antes da verificação era branch `feat/intelligent-model-routing-phase-7`, HEAD `b9f7ebeab3fe2a7019ac98fffb79dee6c1225138`, índice vazio. Já havia alterações rastreadas e arquivos não rastreados, incluindo `autonomous-development-runner/`, R1, R3, R5, R6 e R7-A; todos foram preservados.

Somente este relatório foi criado. Não houve alteração a R6, R7-A, ADR 0008, código, testes ou workflows; nenhum serviço, provider, agente, Broker ou TEG foi executado. Não houve operação Git de escrita, sincronização ou instalação no ambiente do AgentFlow.

## 2. Biblioteca e procedência

Foi selecionada `pycose` 1.1.0, projeto Python de COSE que documenta `Sign1Message`, decodificação COSE_Sign1, EdDSA e verificação de assinatura. A publicação informa Python `>=3.7`, release de 15-12-2023, licença BSD-3-Clause e wheel `py3-none-any`; seus classifiers publicados vão até Python 3.10, portanto não certificam formalmente Python 3.13. O código importou em Python 3.13.17, mas a decodificação dos vetores falhou antes de qualquer verificação de assinatura. A documentação do projeto informa que as operações criptográficas usam `cryptography`; logo, mesmo se os envelopes fossem aceitos, esse backend compartilhado com a rota R7-A limitaria independência criptográfica de baixo nível.

O wheel foi obtido do PyPI em uma venv temporária fora do repositório e seu SHA-256 local foi comparado com o checksum publicado pelo PyPI:

| Distribuição | Versão/artefato usado | SHA-256 local | Referência publicada | Procedência declarada |
|---|---|---|---|---|
| `pycose` | 1.1.0, `pycose-1.1.0-py3-none-any.whl` | `52b524e9d314d6ec89462a7666afdb398a6e7beeede26104617d8246b8c79692` | Igual | PyPI registra **sem provenance/attestation**; upload não via Trusted Publishing. |
| `cbor2` | 5.6.5, `cbor2-5.6.5-cp313-cp313-win_amd64.whl` | `54c72a3207bb2d4480c2c39dad12d7971ce0853a99e3f9b8d559ce6eac84f66f` | Igual | PyPI registra Trusted Publishing para esse wheel. |

Fontes primárias: [repositório/documentação pycose](https://github.com/TimothyClaeys/pycose), [release pycose 1.1.0 no PyPI](https://pypi.org/project/pycose/1.1.0/), [release cbor2 5.6.5 no PyPI](https://pypi.org/project/cbor2/5.6.5/) e [RFC 9052](https://www.rfc-editor.org/rfc/rfc9052.html).

## 3. Ambiente e tentativas de compatibilidade

- Windows x86-64, Python 3.13.17.
- Venv descartável em `%LOCALAPPDATA%\Temp\agentflow-r7a1-5ae2828375a1497dbb41169401abd9ee\venv`; downloads e pacotes ficaram no mesmo diretório temporário, fora do projeto.
- `pycose==1.1.0`; dependências foram instaladas somente nessa venv. Nenhum pacote global ou do AgentFlow foi alterado.
- O resolvedor instalou inicialmente `cbor2==6.2.1`. Com essa versão, `cbor2.loads()` representou arrays CBOR como tuplas; o decoder pycose espera uma lista e `Sign1Message.decode()` recusou o objeto com `TypeError: Bytes cannot be decoded as COSE message`.
- Para testar a compatibilidade da biblioteca sem criar decoder alternativo, foi baixado e instalado na mesma venv `cbor2==5.6.5`. O wheel CPython 3.13 Windows foi confirmado pelo hash listado acima; `cbor2.loads()` passou a representar arrays como listas, permitindo pycose avançar até a validação dos cabeçalhos.
- O pacote pycose documenta suporte a Sign1/EdDSA, mas a compatibilidade do par pycose 1.1.0/Python 3.13 não é atestada pelos classifiers publicados. A tentativa demonstra apenas que foi possível importar a biblioteca e exercitar a rotina de decodificação nesse ambiente.

## 4. Falha observada nos vetores

Ao chamar `Sign1Message.decode()` diretamente sobre o primeiro envelope COSE_Sign1 de R6, com `cbor2==5.6.5`, pycose terminou com:

```text
ValueError: KID should be a byte string
```

O protected header dos quatro vetores codifica o label COSE `4` (`kid`) como CBOR text string. Por exemplo, os bytes de R6 contêm label `04`, seguido do major type de texto e do valor `test-rfc8032-v1`. A seção 3.1 de R6 descreve “kid ASCII”, mas os bytes publicados representam texto, não um CBOR byte string. A tabela de parâmetros do RFC 9052 define o tipo de `kid` como `bstr`; pycose rejeita a representação incompatível antes de entregar qualquer assinatura para verificação.

Isso é um defeito ou ambiguidade material no contrato/vetor a ser resolvido pelos responsáveis arquiteturais. Este relatório não decide se o perfil deveria usar `kid` byte string ou um cabeçalho privado textual diferente, nem altera bytes, protected header, assinatura ou digest. Recalcular ou substituir os vetores sem decisão explícita seria inadequado.

## 5. Resultado por verificação solicitada

| Verificação | Resultado | Evidência/limite |
|---|---|---|
| Descoberta, identidade e versão da biblioteca | PASS | `pycose` 1.1.0 importado na venv isolada. |
| Decodificação COSE dos quatro envelopes | BLOCKED | A biblioteca rejeitou o primeiro envelope em `kid`; os demais não foram tentados após o bloqueio reproduzido no cabeçalho comum. |
| Cabeçalhos protegidos, algoritmo, tipo COSE | BLOCKED | Não foi obtido objeto COSE decodificado para comparação dos quatro vetores. |
| Assinaturas Ed25519 | NOT RUN | Não foi alcançada a chamada de verificação de assinatura. |
| Payload/envelope digests e issuer/audience | NOT RUN | Não foi feita análise posterior ao decode com pycose. |
| Vínculos cruzados entre contratos | NOT RUN | Depende da decodificação e validação semântica, que não ocorreram. |
| Testes negativos compatíveis com a biblioteca | NOT RUN | O perfil positivo não decodifica; não foram construídas rotas de contorno ou objetos de COSE manualmente. |

Não há resultado criptográfico positivo nesta etapa. O erro é uma incompatibilidade de codificação/perfil detectada pela biblioteca, não uma assinatura inválida. Não se deve descrever como assinatura aprovada nem como falha criptográfica de Ed25519.

## 6. Diferenças em relação à R7-A e próximos critérios

R7-A usou decoder/encoder CBOR específico transitório e `cryptography`; conseguiu interpretar `kid` textual e relatou assinatura, digests e vínculos como válidos. R7-A.1 tentou um decoder COSE publicado (pycose) e encontrou a rejeição normativa de tipo no `kid`, impedindo repetir os resultados. A biblioteca usa `cryptography` para operações criptográficas, então esta seleção melhora a independência do tratamento COSE/CBOR, mas não forneceria independência completa do backend Ed25519 em relação à R7-A.

Para retomar a reprodução sem contornar validações:

1. Deliberar formalmente a representação do `kid` conforme o perfil COSE e o RFC aplicável.
2. Se os bytes dos vetores forem alterados, gerar nova versão explícita dos vetores e atualizar assinaturas e digests apenas por processo autorizado; preservar os antigos como histórico.
3. Selecionar biblioteca e versões com compatibilidade documentada entre COSE, CBOR e Python alvo; registrar hashes e provenance de cada artefato.
4. Reexecutar os quatro vetores e negativos usando somente APIs da biblioteca consolidada para COSE/CBOR, separando a validação criptográfica da semântica AgentFlow.
5. Registrar que `pycose` depende de `cryptography`; para independência também no backend Ed25519, selecionar outra implementação independente e revisar sua procedência antes de alegar tal garantia.

## 7. Estado final

Resultado desta etapa: **BLOCKED**. A biblioteca adequada foi encontrada e isolada, mas não pôde consumir os vetores existentes devido ao tipo do `kid`. A divergência deve ser deliberada; nenhum vetor foi corrigido silenciosamente. R6 e R7-A permanecem inalterados, todos os arquivos locais preexistentes foram preservados e RB-09C.2 não foi iniciada.
