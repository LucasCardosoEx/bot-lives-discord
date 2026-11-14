# Bot de lives (Twitch + YouTube) para Discord

O bot verifica a cada 2 minutos se os canais cadastrados estão ao vivo e anuncia no Discord, com título, jogo, espectadores e thumbnail. Cada live é anunciada só uma vez.

## 1. Criar o bot no Discord
1. Acesse https://discord.com/developers/applications → **New Application** → dê um nome.
2. Menu **Bot** → **Reset Token** → copie o token (é o `DISCORD_TOKEN`). Não precisa ativar nenhuma "Privileged Gateway Intent".
3. Menu **OAuth2 → URL Generator**:
   - Scopes: `bot` e `applications.commands`
   - Bot Permissions: `View Channels`, `Send Messages`, `Embed Links` (e `Mention Everyone` se for marcar cargo que não é mencionável)
4. Abra a URL gerada e adicione o bot ao seu servidor.

## 2. Chaves da Twitch
1. Acesse https://dev.twitch.tv/console (a conta precisa ter verificação em duas etapas ativa).
2. **Register Your Application**: nome qualquer, OAuth Redirect URL `http://localhost`, categoria *Chat Bot* ou *Other*, tipo *Confidential*.
3. Abra a aplicação → copie o **Client ID** e clique em **New Secret** para gerar o **Client Secret**.

## 3. Chave do YouTube
1. Acesse https://console.cloud.google.com → crie um projeto.
2. **APIs e serviços → Biblioteca** → procure **YouTube Data API v3** → **Ativar**.
3. **Credenciais → Criar credenciais → Chave de API** → copie. (Recomendado: restringir a chave à YouTube Data API v3.)

O bot usa o RSS gratuito do YouTube para achar os vídeos recentes e só gasta ~1 unidade de cota a cada verificação (o limite diário é 10.000), então não estoura.

## 4. Configurar e rodar
Precisa de Python 3.10 ou mais novo.

```bash
pip install -r requirements.txt
```
Renomeie `.env.example` para `.env`, preencha as chaves e rode:
```bash
python bot.py
```
Se aparecer `Logado como ...` no terminal, está online.

## 5. Usar no Discord
Os comandos só aparecem para quem tem permissão de **Gerenciar servidor**.

| Comando | O que faz |
|---|---|
| `/canal #canal` | Define onde as lives serão anunciadas |
| `/cargo @cargo` | Cargo marcado nos anúncios (vazio = ninguém) |
| `/adicionar link` | Adiciona um canal: `twitch.tv/nome`, `youtube.com/@nome`, `youtube.com/channel/UC...` ou link de uma live/vídeo |
| `/remover` | Remove um canal (escolha na lista) |
| `/lista` | Mostra canais monitorados e configuração |
| `/testar` | Manda uma mensagem de teste no canal de anúncio |

## Observações
- O bot só anuncia enquanto estiver rodando. Para ficar 24h, deixe num PC ligado ou numa hospedagem (VPS ou um serviço de hospedagem de bots).
- No YouTube o anúncio pode atrasar alguns minutos em relação ao início da live, porque depende do feed do canal atualizar.
- Os dados ficam em `dados.json`, criado automaticamente. Não compartilhe o `.env`.
- Para mudar o intervalo de verificação, altere `INTERVALO_MINUTOS` no `.env`.
