"""
Bot de divulgação de lives (Twitch + YouTube) para Discord.

Como funciona:
- Você cadastra canais com /adicionar <link> (Twitch ou YouTube).
- A cada X minutos o bot verifica quem está ao vivo.
- Quando alguém entra ao vivo, o bot anuncia no canal definido com /canal.
- Vídeos novos do YouTube também são divulgados (configure com /videos).
- Cada live/vídeo é anunciado uma única vez (fica registrado em dados.json).
"""
import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
INTERVALO_MINUTOS = float(os.getenv("INTERVALO_MINUTOS", "2"))
ARQUIVO_DADOS = os.getenv("ARQUIVO_DADOS", "dados.json")

COR_TWITCH = 0x9146FF
COR_YOUTUBE = 0xFF0000

# Vídeos publicados há mais tempo que isso nunca são divulgados (evita spam de vídeos antigos)
IDADE_MAXIMA_VIDEO = timedelta(hours=24)
# Shorts têm até 3 minutos
DURACAO_MAXIMA_SHORT = 180

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("livebot")


# =====================================================================
# Armazenamento (arquivo JSON simples)
# =====================================================================
def carregar_dados():
    try:
        with open(ARQUIVO_DADOS, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"servidores": {}, "anunciados": [], "yt_vistos": {}}


def salvar_dados():
    tmp = ARQUIVO_DADOS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ARQUIVO_DADOS)


dados = carregar_dados()
dados.setdefault("yt_vistos", {})

PADRAO_SERVIDOR = {
    "canal": None,
    "cargo": None,
    "twitch": [],
    "youtube": {},
    # vídeos do YouTube
    "videos": True,
    "shorts": False,
    "canal_videos": None,  # None = usa o mesmo canal das lives
    "marcar_videos": False,
}


def config_servidor(guild_id):
    s = dados["servidores"].setdefault(str(guild_id), {})
    for chave, valor in PADRAO_SERVIDOR.items():  # completa configs antigas
        s.setdefault(chave, valor.copy() if isinstance(valor, (list, dict)) else valor)
    return s


def todos_servidores():
    return [(gid, config_servidor(gid)) for gid in list(dados["servidores"])]


def registrar_vistos(channel_id, ids):
    """Guarda os vídeos já conhecidos de um canal (até 50 por canal)."""
    vistos = dados["yt_vistos"].setdefault(channel_id, [])
    for v in ids:
        if v not in vistos:
            vistos.append(v)
    dados["yt_vistos"][channel_id] = vistos[-50:]


def ja_anunciado(chave):
    return chave in dados["anunciados"]


def marcar_anunciado(chave):
    dados["anunciados"].append(chave)
    dados["anunciados"] = dados["anunciados"][-500:]  # guarda só as últimas 500
    salvar_dados()


# =====================================================================
# Leitura dos links
# =====================================================================
def identificar_link(link: str):
    """Retorna (tipo, valor). Tipos: twitch, yt_id, yt_handle, yt_video."""
    link = link.strip()
    if m := re.search(r"twitch\.tv/([A-Za-z0-9_]{3,25})", link, re.I):
        return "twitch", m.group(1).lower()
    if m := re.search(r"youtube\.com/channel/(UC[\w-]{22})", link):
        return "yt_id", m.group(1)
    if m := re.search(r"youtube\.com/@([\w.\-]+)", link):
        return "yt_handle", m.group(1)
    if m := re.search(r"(?:youtu\.be/|[?&]v=|youtube\.com/live/)([\w-]{11})", link):
        return "yt_video", m.group(1)
    if m := re.fullmatch(r"@([\w.\-]+)", link):
        return "yt_handle", m.group(1)
    return None, None


# =====================================================================
# Twitch (API Helix)
# =====================================================================
class Twitch:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.token = None

    async def _gerar_token(self):
        async with self.s.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        ) as r:
            r.raise_for_status()
            self.token = (await r.json())["access_token"]

    async def _get(self, url, params):
        for _ in range(2):
            if not self.token:
                await self._gerar_token()
            headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {self.token}"}
            async with self.s.get(url, params=params, headers=headers) as r:
                if r.status == 401:  # token expirou: gera outro e tenta de novo
                    self.token = None
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError("Falha de autenticação na Twitch (confira Client ID/Secret)")

    async def usuario(self, login):
        d = await self._get("https://api.twitch.tv/helix/users", [("login", login)])
        return d["data"][0] if d["data"] else None

    async def lives(self, logins):
        resultado = []
        for i in range(0, len(logins), 100):  # a API aceita até 100 por chamada
            params = [("user_login", l) for l in logins[i : i + 100]]
            d = await self._get("https://api.twitch.tv/helix/streams", params)
            resultado += d["data"]
        return resultado


# =====================================================================
# YouTube (RSS gratuito + YouTube Data API v3)
# =====================================================================
class YouTube:
    API = "https://www.googleapis.com/youtube/v3"

    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def _get(self, rota, params):
        params = {**params, "key": YOUTUBE_API_KEY}
        async with self.s.get(f"{self.API}/{rota}", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def resolver_canal(self, tipo, valor):
        """Transforma qualquer link do YouTube em (channel_id, nome do canal)."""
        if tipo == "yt_video":
            d = await self._get("videos", {"part": "snippet", "id": valor})
            if not d.get("items"):
                return None
            sn = d["items"][0]["snippet"]
            return sn["channelId"], sn["channelTitle"]
        if tipo == "yt_id":
            d = await self._get("channels", {"part": "snippet", "id": valor})
        else:  # yt_handle
            d = await self._get("channels", {"part": "snippet", "forHandle": valor})
        itens = d.get("items") or []
        if not itens:
            return None
        return itens[0]["id"], itens[0]["snippet"]["title"]

    async def videos_recentes(self, channel_id, quantidade=5):
        """Lê o feed RSS do canal (não gasta cota da API)."""
        async with self.s.get(
            "https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}
        ) as r:
            if r.status != 200:
                return []
            xml = await r.text()
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        raiz = ET.fromstring(xml)
        return [e.findtext("yt:videoId", namespaces=ns) for e in raiz.findall("atom:entry", ns)[:quantidade]]

    async def detalhes(self, ids):
        """Consulta até 50 vídeos por chamada (1 unidade de cota cada chamada)."""
        itens = []
        for i in range(0, len(ids), 50):
            d = await self._get(
                "videos",
                {
                    "part": "snippet,liveStreamingDetails,contentDetails",
                    "id": ",".join(ids[i : i + 50]),
                },
            )
            itens += d.get("items", [])
        return itens

    async def eh_short(self, video):
        """Shorts têm até 3 min e abrem em youtube.com/shorts/ sem redirecionar."""
        if duracao_segundos(video) > DURACAO_MAXIMA_SHORT:
            return False
        try:
            async with self.s.head(
                f"https://www.youtube.com/shorts/{video['id']}", allow_redirects=False
            ) as r:
                return r.status == 200
        except aiohttp.ClientError:
            return False


def duracao_segundos(video):
    """Converte a duração ISO 8601 do YouTube (ex.: PT1H2M3S) em segundos."""
    texto = video.get("contentDetails", {}).get("duration", "")
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", texto)
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def formatar_duracao(segundos):
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def publicado_recentemente(video):
    texto = video["snippet"].get("publishedAt")
    if not texto:
        return False
    publicado = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return datetime.now(timezone.utc) - publicado <= IDADE_MAXIMA_VIDEO


# =====================================================================
# Embeds de anúncio
# =====================================================================
def embed_twitch(live):
    url = f"https://www.twitch.tv/{live['user_login']}"
    e = discord.Embed(title=live["title"][:256] or "Ao vivo", url=url, color=COR_TWITCH)
    e.set_author(name=f"{live['user_name']} • Twitch", url=url)
    if live.get("game_name"):
        e.add_field(name="Jogo", value=live["game_name"])
    e.add_field(name="Espectadores", value=str(live.get("viewer_count", 0)))
    thumb = live["thumbnail_url"].replace("{width}", "1280").replace("{height}", "720")
    e.set_image(url=f"{thumb}?t={int(time.time())}")  # evita imagem velha em cache
    return e, url, live["user_name"]


def embed_youtube(video):
    sn = video["snippet"]
    url = f"https://www.youtube.com/watch?v={video['id']}"
    e = discord.Embed(title=sn["title"][:256], url=url, color=COR_YOUTUBE)
    e.set_author(
        name=f"{sn['channelTitle']} • YouTube",
        url=f"https://www.youtube.com/channel/{sn['channelId']}",
    )
    espectadores = video.get("liveStreamingDetails", {}).get("concurrentViewers")
    if espectadores:
        e.add_field(name="Espectadores", value=espectadores)
    thumbs = sn.get("thumbnails", {})
    for qualidade in ("maxres", "standard", "high", "medium", "default"):
        if qualidade in thumbs:
            e.set_image(url=thumbs[qualidade]["url"])
            break
    return e, url, sn["channelTitle"]


def embed_video(video, short):
    e, url, nome = embed_youtube(video)
    e.clear_fields()
    if short:
        url = f"https://www.youtube.com/shorts/{video['id']}"
        e.url = url
    duracao = duracao_segundos(video)
    if duracao:
        e.add_field(name="Duração", value=formatar_duracao(duracao))
    return e, url, nome


# =====================================================================
# Bot
# =====================================================================
class LiveBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.sessao = None
        self.twitch = None
        self.youtube = None

    async def setup_hook(self):
        self.sessao = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
            self.twitch = Twitch(self.sessao)
        else:
            log.warning("Twitch desativada: preencha TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET no .env")
        if YOUTUBE_API_KEY:
            self.youtube = YouTube(self.sessao)
        else:
            log.warning("YouTube desativado: preencha YOUTUBE_API_KEY no .env")
        await self.tree.sync()
        self.verificar.start()

    async def close(self):
        if self.sessao:
            await self.sessao.close()
        await super().close()

    async def on_ready(self):
        log.info("Logado como %s (verificando a cada %s min)", self.user, INTERVALO_MINUTOS)

    # ---------- loop de verificação ----------
    @tasks.loop(minutes=INTERVALO_MINUTOS)
    async def verificar(self):
        try:
            await self.checar_twitch()
        except Exception:
            log.exception("Erro ao verificar Twitch")
        try:
            await self.checar_youtube()
        except Exception:
            log.exception("Erro ao verificar YouTube")

    @verificar.before_loop
    async def antes_de_verificar(self):
        await self.wait_until_ready()

    async def checar_twitch(self):
        if not self.twitch:
            return
        logins = sorted({l for s in dados["servidores"].values() for l in s["twitch"]})
        if not logins:
            return
        for live in await self.twitch.lives(logins):
            chave = f"tw:{live['id']}"
            if ja_anunciado(chave):
                continue
            login = live["user_login"].lower()
            e, url, nome = embed_twitch(live)
            await self.anunciar(
                lambda s, l=login: l in s["twitch"],
                e,
                f"🔴 **{discord.utils.escape_markdown(nome)}** está ao vivo! <{url}>",
            )
            marcar_anunciado(chave)

    async def checar_youtube(self):
        if not self.youtube:
            return
        canais = sorted({c for _, s in todos_servidores() for c in s["youtube"]})
        if not canais:
            return
        resultados = await asyncio.gather(
            *(self.youtube.videos_recentes(c) for c in canais), return_exceptions=True
        )
        ids = []
        novos = set()
        for cid, r in zip(canais, resultados):
            if not isinstance(r, list) or not r:
                continue  # feed falhou: pula o canal nesta rodada
            r = [v for v in r if v]
            if cid not in dados["yt_vistos"]:
                # primeira vez vendo o canal: os vídeos atuais servem de base e não são divulgados
                registrar_vistos(cid, r)
                salvar_dados()
            else:
                novos.update(v for v in r if v not in dados["yt_vistos"][cid])
            ids += r
        if not ids:
            return

        for video in await self.youtube.detalhes(ids):
            sn = video["snippet"]
            cid = sn["channelId"]
            estado = sn.get("liveBroadcastContent")

            # ----- live -----
            if estado == "live":
                chave = f"yt:{video['id']}"
                if not ja_anunciado(chave):
                    e, url, nome = embed_youtube(video)
                    await self.anunciar(
                        lambda s, c=cid: c in s["youtube"],
                        e,
                        f"🔴 **{discord.utils.escape_markdown(nome)}** está ao vivo! <{url}>",
                    )
                    marcar_anunciado(chave)
                continue

            # ----- vídeo novo -----
            if video["id"] not in novos or estado == "upcoming":
                continue  # já conhecido, ou live/estreia agendada (decide depois)
            registrar_vistos(cid, [video["id"]])
            salvar_dados()
            if "liveStreamingDetails" in video:
                continue  # gravação de uma live que já terminou
            if not publicado_recentemente(video):
                continue
            short = await self.youtube.eh_short(video)
            e, url, nome = embed_video(video, short)
            tipo = "um Short novo" if short else "um vídeo novo"
            await self.anunciar(
                lambda s, c=cid, sh=short: c in s["youtube"] and s["videos"] and (s["shorts"] or not sh),
                e,
                f"📺 **{discord.utils.escape_markdown(nome)}** postou {tipo}! <{url}>",
                video=True,
            )

    async def anunciar(self, filtro, embed, texto, video=False):
        for gid, s in todos_servidores():
            canal_id = (s["canal_videos"] or s["canal"]) if video else s["canal"]
            if not canal_id or not filtro(s):
                continue
            canal = self.get_channel(canal_id)
            if canal is None:
                log.warning("Canal %s do servidor %s não encontrado", canal_id, gid)
                continue
            marcar = s["cargo"] and (s["marcar_videos"] or not video)
            mencao = f"<@&{s['cargo']}> " if marcar else ""
            try:
                await canal.send(
                    f"{mencao}{texto}",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
                log.info("Anunciado no servidor %s: %s", gid, texto)
            except discord.HTTPException as erro:
                log.warning("Não consegui anunciar no servidor %s: %s", gid, erro)


bot = LiveBot()


# =====================================================================
# Comandos de barra (só quem tem "Gerenciar servidor" vê/usa)
# =====================================================================
@bot.tree.command(name="canal", description="Define o canal onde as lives serão anunciadas")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_canal(inter: discord.Interaction, canal: discord.TextChannel):
    perms = canal.permissions_for(inter.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await inter.response.send_message(
            f"Não tenho permissão para ver, enviar mensagens e embeds em {canal.mention}.", ephemeral=True
        )
    config_servidor(inter.guild_id)["canal"] = canal.id
    salvar_dados()
    await inter.response.send_message(f"✅ As lives serão anunciadas em {canal.mention}.", ephemeral=True)


@bot.tree.command(name="cargo", description="Cargo a ser marcado nos anúncios (deixe vazio para não marcar ninguém)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_cargo(inter: discord.Interaction, cargo: Optional[discord.Role] = None):
    config_servidor(inter.guild_id)["cargo"] = cargo.id if cargo else None
    salvar_dados()
    msg = f"✅ Vou marcar {cargo.mention} nos anúncios." if cargo else "✅ Não vou marcar nenhum cargo."
    await inter.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="adicionar", description="Adiciona um canal da Twitch ou do YouTube para monitorar")
@app_commands.describe(link="Link do canal (twitch.tv/nome, youtube.com/@nome) ou de uma live/vídeo do YouTube")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_adicionar(inter: discord.Interaction, link: str):
    await inter.response.defer(ephemeral=True)
    s = config_servidor(inter.guild_id)
    tipo, valor = identificar_link(link)
    try:
        if tipo == "twitch":
            if not bot.twitch:
                return await inter.followup.send("A Twitch não está configurada no .env do bot.")
            usuario = await bot.twitch.usuario(valor)
            if not usuario:
                return await inter.followup.send(f"Não achei o canal **{valor}** na Twitch.")
            login = usuario["login"]
            if login in s["twitch"]:
                return await inter.followup.send(f"**{usuario['display_name']}** já está na lista.")
            s["twitch"].append(login)
            nome = f"Twitch • {usuario['display_name']}"
        elif tipo:
            if not bot.youtube:
                return await inter.followup.send("O YouTube não está configurado no .env do bot.")
            resultado = await bot.youtube.resolver_canal(tipo, valor)
            if not resultado:
                return await inter.followup.send("Não achei esse canal no YouTube.")
            cid, titulo = resultado
            if cid in s["youtube"]:
                return await inter.followup.send(f"**{titulo}** já está na lista.")
            s["youtube"][cid] = titulo
            if cid not in dados["yt_vistos"]:
                # vídeos que já existem não são divulgados, só os que saírem daqui pra frente
                atuais = await bot.youtube.videos_recentes(cid)
                if atuais:
                    registrar_vistos(cid, [v for v in atuais if v])
            nome = f"YouTube • {titulo}"
        else:
            return await inter.followup.send(
                "Não reconheci esse link. Exemplos: `https://twitch.tv/gaules`, "
                "`https://youtube.com/@canal` ou o link de uma live do YouTube."
            )
    except (aiohttp.ClientError, RuntimeError) as erro:
        log.warning("Erro ao adicionar %s: %s", link, erro)
        return await inter.followup.send("Deu erro ao consultar a API. Confira as chaves no .env e tente de novo.")

    salvar_dados()
    aviso = "" if s["canal"] else "\n⚠️ Falta definir onde anunciar: use `/canal`."
    await inter.followup.send(f"✅ Adicionado: **{nome}**{aviso}")


async def autocompletar_remover(inter: discord.Interaction, atual: str):
    s = config_servidor(inter.guild_id)
    opcoes = [app_commands.Choice(name=f"Twitch • {l}"[:100], value=f"tw:{l}") for l in s["twitch"]]
    opcoes += [app_commands.Choice(name=f"YouTube • {n}"[:100], value=f"yt:{c}") for c, n in s["youtube"].items()]
    return [o for o in opcoes if atual.lower() in o.name.lower()][:25]


@bot.tree.command(name="remover", description="Para de monitorar um canal")
@app_commands.describe(canal="Escolha na lista")
@app_commands.autocomplete(canal=autocompletar_remover)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_remover(inter: discord.Interaction, canal: str):
    s = config_servidor(inter.guild_id)
    plataforma, _, valor = canal.partition(":")
    if plataforma == "tw" and valor in s["twitch"]:
        s["twitch"].remove(valor)
    elif plataforma == "yt" and valor in s["youtube"]:
        del s["youtube"][valor]
    else:
        return await inter.response.send_message("Esse canal não está na lista.", ephemeral=True)
    salvar_dados()
    await inter.response.send_message("🗑️ Removido.", ephemeral=True)


@bot.tree.command(name="lista", description="Mostra os canais monitorados e a configuração")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_lista(inter: discord.Interaction):
    s = config_servidor(inter.guild_id)
    linhas = [f"• Twitch: [{l}](https://twitch.tv/{l})" for l in s["twitch"]]
    linhas += [f"• YouTube: [{n}](https://youtube.com/channel/{c})" for c, n in s["youtube"].items()]
    e = discord.Embed(title="Canais monitorados", description="\n".join(linhas) or "Nenhum ainda. Use `/adicionar`.")
    e.add_field(name="Canal de anúncio", value=f"<#{s['canal']}>" if s["canal"] else "não definido")
    e.add_field(name="Cargo marcado", value=f"<@&{s['cargo']}>" if s["cargo"] else "nenhum")
    if s["videos"]:
        canal_v = f"<#{s['canal_videos']}>" if s["canal_videos"] else "mesmo das lives"
        extras = []
        if s["shorts"]:
            extras.append("com Shorts")
        if s["marcar_videos"] and s["cargo"]:
            extras.append("marcando o cargo")
        videos = f"ativados • {canal_v}" + (f" • {', '.join(extras)}" if extras else "")
    else:
        videos = "desativados"
    e.add_field(name="Vídeos do YouTube", value=videos, inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="videos", description="Configura a divulgação de vídeos novos do YouTube")
@app_commands.describe(
    ativar="Divulgar vídeos novos (não só lives)?",
    shorts="Divulgar também os Shorts?",
    canal="Canal para os vídeos (vazio = mesmo canal das lives)",
    marcar_cargo="Marcar o cargo definido em /cargo nos vídeos também?",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_videos(
    inter: discord.Interaction,
    ativar: bool,
    shorts: Optional[bool] = None,
    canal: Optional[discord.TextChannel] = None,
    marcar_cargo: Optional[bool] = None,
):
    s = config_servidor(inter.guild_id)
    if canal:
        perms = canal.permissions_for(inter.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await inter.response.send_message(
                f"Não tenho permissão para ver, enviar mensagens e embeds em {canal.mention}.", ephemeral=True
            )
        s["canal_videos"] = canal.id
    s["videos"] = ativar
    if shorts is not None:
        s["shorts"] = shorts
    if marcar_cargo is not None:
        s["marcar_videos"] = marcar_cargo
    salvar_dados()
    if not ativar:
        return await inter.response.send_message("✅ Vídeos desativados: só lives serão divulgadas.", ephemeral=True)
    destino = f"<#{s['canal_videos']}>" if s["canal_videos"] else "no mesmo canal das lives"
    msg = (
        f"✅ Vídeos novos do YouTube serão divulgados em {destino}."
        f"\nShorts: {'sim' if s['shorts'] else 'não'} • Marcar cargo: {'sim' if s['marcar_videos'] else 'não'}"
    )
    await inter.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="videos_canal_padrao", description="Volta a divulgar os vídeos no mesmo canal das lives")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_videos_canal_padrao(inter: discord.Interaction):
    config_servidor(inter.guild_id)["canal_videos"] = None
    salvar_dados()
    await inter.response.send_message("✅ Os vídeos voltam a sair no mesmo canal das lives.", ephemeral=True)


@bot.tree.command(name="testar", description="Envia uma mensagem de teste no canal de anúncio")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def cmd_testar(inter: discord.Interaction):
    s = config_servidor(inter.guild_id)
    canal = bot.get_channel(s["canal"]) if s["canal"] else None
    if not canal:
        return await inter.response.send_message("Defina o canal primeiro com `/canal`.", ephemeral=True)
    try:
        await canal.send("🧪 Teste do bot de lives: se você está vendo isso, os anúncios vão funcionar aqui.")
        await inter.response.send_message("✅ Mensagem de teste enviada.", ephemeral=True)
    except discord.HTTPException as erro:
        await inter.response.send_message(f"Não consegui enviar: {erro}", ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Preencha DISCORD_TOKEN no arquivo .env")
    bot.run(DISCORD_TOKEN, log_handler=None)
