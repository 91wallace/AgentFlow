# MVP-19.4 — Autenticação local multiplataforma

## Estado

**PARTIAL** — a implementação e os testes automatizados do contrato local estão disponíveis. Não houve cadastro real de operador nem aprovação operacional de uma proposta nesta execução. O perfil `LOCAL_SUPERVISED` permanece não homologado e o dispatch continua desabilitado.

## Mecanismo

O autenticador padrão preparado para Windows e Linux é uma senha local de operador digitada em prompt oculto. O processo deriva uma chave Ed25519 efêmera a partir da senha com `hashlib.scrypt` (N=32768, r=8, p=1, sal aleatório de 16 bytes). A chave privada é usada somente em memória para assinar o nonce aleatório persistido para uma única decisão. O SQLite guarda apenas sal, parâmetros do KDF, chave pública e eventos append-only. Senhas e chaves privadas não são gravadas nem incluídas na prova, nos logs ou na representação dos objetos.

A prova assina o desafio que o State Store já vinculou ao digest da solicitação e ao digest da decisão explícita. Antes do prompt de senha, a interface mostra ID, digest, revisão, workspace, operações, limites, validade e pede a digitação de `APROVAR <12 caracteres do digest>` ou `REJEITAR <12 caracteres do digest>`. Essa confirmação é uma ação explícita adicional e não vale como autenticação sem a assinatura da credencial. A autoridade valida a proposta persistida, o digest, revisão, workspace, operações, limites e validade antes de verificar a assinatura. O principal não vem de argumento do agente: no Windows deriva do SID do token do processo; no Linux deriva do UID efetivo. A prova expirada, inválida, repetida, de outra credencial ou para outro nonce falha fechada.

No Linux, após o cadastro, o arquivo SQLite operacional é restringido para modo `0600`; o diretório pai não é alterado. No Windows, a proteção de acesso ao banco depende das ACLs NTFS herdadas do local escolhido. A tabela não contém segredo privado que exija DPAPI. Essa proteção não impede que processos executados pela mesma conta leiam ou alterem o banco.

WebAuthn/Windows Hello permanece opcional e separado. A falta de Windows Hello não impede o uso do novo backend.

## Comandos de credencial

O perfil deve ter sido selecionado explicitamente como `LOCAL_SUPERVISED`, e o State Store já deve existir:

```powershell
agentflow profile select --profile LOCAL_SUPERVISED --confirm
agentflow local-auth status
agentflow local-auth enroll --confirm-bootstrap
agentflow local-auth revoke --confirm-revocation
```

Os comandos `enroll` e `revoke` exigem terminal interativo. O enrollment solicita a senha duas vezes com entrada oculta e exige no mínimo 12 caracteres. A identidade é derivada da conta do sistema. O `status` não autentica ninguém. A CLI não aceita senha por argumento, arquivo, variável de ambiente ou entrada não interativa.

O adaptador `LocalPassphraseProofProvider` pode ser fornecido ao `LocalConsentReviewService`; o consentimento exige a ação explícita da interface, a confirmação do digest, seguida da senha oculta. O `PassphraseLocalOperatorAuthenticator` valida a assinatura Ed25519 contra a chave pública persistida. O adaptador não cria uma autoridade por padrão, não altera `INDEPENDENT_AUTHORITY` e não conecta dispatch.

## Limites de confiança

`LOCAL_SUPERVISED` confia no sistema operacional, na conta local do proprietário e no processo da interface. Ele **não** protege contra administrador local comprometido, malware executado sob a mesma conta, adulteração do executável AgentFlow, controle da sessão terminal ou alteração direta do SQLite/ACL. A derivação lenta reduz tentativas offline, mas não transforma senha em hardware-backed identity. Recomenda-se senha longa e exclusiva.

O registro criptográfico prova posse da senha para o desafio; por si só, não demonstra que um operador compreendeu a proposta. A interface existente mostra campos de escopo e exige `approve`/`reject`, mas ainda não existe um comando CLI completo que componha a resolução autoritativa APL/Broker, a política local e a tela de revisão em uma cerimônia utilizável. Também não foi concluída validação de ACL Windows nem homologação Linux neste host.

Grants, quando a autoridade explícita MVP-17 é montada por um chamador com resolver, política e signer, continuam vinculados à proposta e não disparam trabalho. O mecanismo novo não os instancia automaticamente. Nenhum executor ou job foi chamado nesta etapa.

## Cobertura e evidências

Testes automatizados cobrem principal por UID/SID (com provedores simulados), policy de senha, confirmação, persistência somente da chave pública, assinatura válida e inválida, nonce divergente, replay, revogação, recuperação do banco, permissões POSIX, e fluxo sintético da interface de consentimento pela autoridade MVP-17. Esses testes são fixtures e não contam como autenticação humana operacional.

O probe de capacidade agora separa `local_passphrase_available` de `authentication_operational`. Cadastro, prompt oculto e revisão real só serão considerados operacionais depois de uma cerimônia manual no host suportado, com a senha digitada pelo proprietário e uma proposta persistida inspecionada e aprovada.

## Homologação pendente

1. Selecionar `LOCAL_SUPERVISED` em um projeto de teste e confirmar a persistência do perfil.
2. Executar `agentflow local-auth enroll --confirm-bootstrap` num terminal interativo; digitar a senha sem compartilhá-la no chat ou em logs.
3. Verificar `agentflow local-auth status` e a ACL do `state.sqlite3` (Linux: modo `0600`; Windows: `icacls` no arquivo e diretório pai).
4. Revisar uma proposta APL real já persistida pela interface autoritativa, digitar a ação explícita e a senha no prompt oculto.
5. Confirmar a decisão append-only ligada a proposal ID/digest/revisão/workspace/escopo/limites/validade, com `dispatch_enabled=false` e nenhuma tentativa criada.
6. Repetir após reinício e testar senha errada, replay, expiração e revogação.
7. Executar os mesmos passos no Linux antes de declarar suporte operacional multiplataforma.

## Próximos limites

- Criar o comando de revisão que compõe APL Store, Broker Store, `AuthoritativeConsentResolver`, `LocalAuthorizationAuthority` e a interface; deve receber configuração de projeto/política sem aceitar claims do agente.
- Homologar cadastro e autenticação reais separadamente em Windows e Linux.
- Revisar as ACLs do State Store e o risco de agente sob a mesma identidade.
- Manter dispatch bloqueado até revisão posterior específica.
