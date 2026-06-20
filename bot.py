import asyncio
import io
import json
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Optional

import aiofiles  # Requiere: pip install aiofiles
import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
IDLE_TIMEOUT_SECONDS = int(os.getenv("VOICE_IDLE_TIMEOUT_SECONDS", "300"))
DATA_DIR = Path("data")
ECONOMY_FILE = DATA_DIR / "economy.json"
TTS_DIR = DATA_DIR / "tts"

# --- AJUSTE ROBUSTO PARA RUTAS DE FFMPEG EN WINDOWS ---
BASE_DIR = Path(__file__).resolve().parent
ENV_FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg.exe").strip()

if ENV_FFMPEG in ("ffmpeg.exe", "./ffmpeg.exe", "ffmpeg"):
    posible_path = BASE_DIR / "ffmpeg.exe"
    if posible_path.exists():
        FFMPEG_PATH = str(posible_path)
    else:
        FFMPEG_PATH = str(BASE_DIR / "ffmpeg.exe")
else:
    FFMPEG_PATH = ENV_FFMPEG

print(f"[DIAGNÓSTICO] Utilizando ejecutable FFmpeg en: {FFMPEG_PATH}")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
START_TIME = discord.utils.utcnow()

# Lock global para evitar corrupción al escribir el archivo de economía
economy_lock = asyncio.Lock()

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "noplaylist": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

@dataclass
class Song:
    title: str
    url: str  # Puede ser la URL de stream de YT o el path local al MP3 del TTS
    webpage_url: str
    requested_by: str
    is_tts: bool = False

music_queues: dict[int, Deque[Song]] = {}
now_playing: dict[int, Song] = {}
last_voice_activity: dict[int, datetime] = {}
voice_transcripts: dict[int, Deque[str]] = {}
voice_tts_enabled: dict[int, bool] = {}
idle_task_started = False

NUMBER_EMOJIS = [
    "\N{DIGIT ONE}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT TWO}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT THREE}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT FOUR}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT FIVE}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT SIX}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT SEVEN}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT EIGHT}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{DIGIT NINE}\N{VARIATION SELECTOR-16}\N{COMBINING ENCLOSING KEYCAP}",
    "\N{KEYCAP TEN}",
]

# --- SISTEMA DE ECONOMÍA ASÍNCRONO ---

async def load_economy() -> dict:
    if not ECONOMY_FILE.exists():
        return {"guilds": {}}
    async with aiofiles.open(ECONOMY_FILE, "r", encoding="utf-8") as file:
        content = await file.read()
        return json.loads(content) if content else {"guilds": {}}

async def save_economy(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    async with economy_lock:
        async with aiofiles.open(ECONOMY_FILE, "w", encoding="utf-8") as file:
            await file.write(json.dumps(data, indent=2))

async def get_account(guild_id: int, user_id: int) -> dict:
    data = await load_economy()
    guild = data["guilds"].setdefault(str(guild_id), {})
    account = guild.setdefault(
        str(user_id),
        {"coins": 0, "last_daily": None, "last_work": None},
    )
    await save_economy(data)
    return account

async def update_account(guild_id: int, user_id: int, account: dict) -> None:
    data = await load_economy()
    data["guilds"].setdefault(str(guild_id), {})[str(user_id)] = account
    await save_economy(data)

# --- REINGENIERÍA DEL SISTEMA TTS (DESDE 0) ---

def clean_tts_text(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ\s.,;:!?¿¡]', '', text)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 150:
        cleaned = cleaned[:147] + "..."
    return cleaned

async def synthesize_tts(text: str, output_path: Path) -> None:
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_text = text.replace("'", "''")
    safe_path = str(output_path).replace("/", "\\")

    script = (
        f"[System.Reflection.Assembly]::LoadWithPartialName('System.Speech') | Out-Null; "
        f"$sys = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$sys.SetOutputToWaveFile('{safe_path}'); "
        f"$sys.Speak('{safe_text}'); "
        f"$sys.Dispose(); "
        f"[System.GC]::Collect(); "
        f"[System.GC]::WaitForPendingFinalizers();"
    )

    try:
        process = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-Command",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
    except Exception as e:
        print(f"[CRITICAL] Error en la ejecución de subproceso de voz: {e}")
        raise e

async def convert_audio(input_path: Path, output_path: Path) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            FFMPEG_PATH,
            "-y",
            "-i",
            str(input_path),
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate()
    except Exception as e:
        print(f"[CRITICAL] Error al convertir audio con FFmpeg: {e}")
        raise e

async def queue_voice_tts(guild: discord.Guild, text: str, requested_by: str) -> None:
    cleaned = clean_tts_text(text)
    if not cleaned:
        return

    unique_id = f"tts-{guild.id}-{int(datetime.utcnow().timestamp() * 1000)}"
    wav_path = TTS_DIR / f"{unique_id}.wav"
    mp3_path = TTS_DIR / f"{unique_id}.mp3"

    try:
        await synthesize_tts(cleaned, wav_path)
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            return

        await convert_audio(wav_path, mp3_path)

        if wav_path.exists():
            wav_path.unlink()

        song = Song(
            title=f"TTS: {cleaned[:35]}...",
            url=str(mp3_path),
            webpage_url="",
            requested_by=requested_by,
            is_tts=True
        )

        queue = get_queue(guild.id)
        queue.append(song)

        voice_client = guild.voice_client
        if voice_client and not voice_client.is_playing() and not voice_client.is_paused():
            play_next(guild)

    except Exception as err:
        print(f"[TTS Pipeline] Ocurrió un error: {err}")
        try:
            if wav_path.exists(): wav_path.unlink()
            if mp3_path.exists(): mp3_path.unlink()
        except:
            pass

# --- MANEJO DE COLA Y RECOLECTOR DE BASURA ---

def mark_voice_activity(guild_id: int) -> None:
    last_voice_activity[guild_id] = discord.utils.utcnow()

def add_voice_transcript(guild_id: int, line: str) -> None:
    if guild_id not in voice_transcripts:
        voice_transcripts[guild_id] = deque(maxlen=200)
    voice_transcripts[guild_id].append(line)

def get_queue(guild_id: int) -> Deque[Song]:
    if guild_id not in music_queues:
        music_queues[guild_id] = deque()
    return music_queues[guild_id]

async def idle_voice_watcher() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                continue

            queue = get_queue(guild.id)
            is_busy = voice_client.is_playing() or voice_client.is_paused() or bool(queue)
            if is_busy:
                mark_voice_activity(guild.id)
                continue

            last_activity = last_voice_activity.get(guild.id, START_TIME)
            idle_seconds = (discord.utils.utcnow() - last_activity).total_seconds()
            if idle_seconds >= IDLE_TIMEOUT_SECONDS:
                await voice_client.disconnect()
                now_playing.pop(guild.id, None)
                queue.clear()

        await asyncio.sleep(30)

def play_next(guild: discord.Guild):
    queue = get_queue(guild.id)
    voice_client = guild.voice_client

    # --- RECOLECTOR DE BASURA ASÍNCRONO ---
    prev_song = now_playing.pop(guild.id, None)
    if prev_song and prev_song.is_tts:
        async def borrar_tts_residual(file_path: str):
            await asyncio.sleep(1.5)  # Tiempo de gracia para cerrar buffers
            target_file = Path(file_path)
            if target_file.exists():
                for intento in range(3):
                    try:
                        target_file.unlink()
                        break
                    except PermissionError:
                        await asyncio.sleep(2)
                    except Exception as ex:
                        print(f"[Limpieza] Error al eliminar {target_file.name}: {ex}")
                        break

        bot.loop.create_task(borrar_tts_residual(prev_song.url))

    if not voice_client or not queue:
        mark_voice_activity(guild.id)
        return

    song = queue.popleft()
    now_playing[guild.id] = song
    mark_voice_activity(guild.id)

    if song.is_tts:
        source = discord.FFmpegPCMAudio(song.url, executable=FFMPEG_PATH)
    else:
        source = discord.FFmpegPCMAudio(song.url, executable=FFMPEG_PATH, **FFMPEG_OPTIONS)

    def after_playing(error):
        if error:
            print(f"[Audio Event] Error reproduciendo: {error}")
        bot.loop.call_soon_threadsafe(play_next, guild)

    voice_client.play(source, after=after_playing)

async def search_song(query: str, requested_by: str) -> Song:
    loop = asyncio.get_running_loop()

    def extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            data = ydl.extract_info(query, download=False)
            if "entries" in data:
                data = data["entries"][0]
            return data

    data = await loop.run_in_executor(None, extract)
    return Song(
        title=data.get("title", "Canción sin título"),
        url=data["url"],
        webpage_url=data.get("webpage_url", query),
        requested_by=requested_by,
        is_tts=False
    )

async def ensure_voice(ctx: commands.Context) -> Optional[discord.VoiceClient]:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("Primero entra a un canal de voz.")
        return None

    voice_client = ctx.guild.voice_client
    channel = ctx.author.voice.channel

    if voice_client and voice_client.channel != channel:
        await voice_client.move_to(channel)
    elif not voice_client:
        voice_client = await channel.connect()

    mark_voice_activity(ctx.guild.id)
    return voice_client

# --- EVENTOS ---

@bot.event
async def on_ready():
    global idle_task_started
    print(f"Bot conectado como {bot.user} usando prefijo {PREFIX}")
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}help"))
    if not idle_task_started:
        bot.loop.create_task(idle_voice_watcher())
        idle_task_started = True

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    voice_client = message.guild.voice_client
    if voice_client and isinstance(message.channel, discord.VoiceChannel):
        if voice_client.channel and message.channel.id == voice_client.channel.id:
            mark_voice_activity(message.guild.id)
            
            timestamp = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M")
            add_voice_transcript(
                message.guild.id,
                f"[{timestamp}] {message.author.display_name}: {message.content}"
            )
            
            if voice_tts_enabled.get(message.guild.id, True) and not message.content.startswith(PREFIX):
                bot.loop.create_task(
                    queue_voice_tts(message.guild, message.content, message.author.display_name)
                )

    await bot.process_commands(message)

# --- COMANDOS GENERALES Y UTILIDAD ---

@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(title="Comandos del bot", description=f"Prefijo: `{PREFIX}`", color=discord.Color.blurple())
    embed.add_field(
        name="Música / TTS Unificado",
        value="`play <URL/Texto>`\n`join`\n`now`\n`pause`\n`resume`\n`skip`\n`queue`\n`shuffle`\n`remove <n°>`\n`stop`\n`leave`\n`idle`",
        inline=True
    )
    embed.add_field(
        name="Utilidad Voz",
        value="`transcript [cantidad]`\n`cleartranscript`\n`vozchat on/off`\n`probarvoz [texto]`",
        inline=True
    )
    embed.add_field(
        name="Economía",
        value="`balance [@user]`\n`daily`\n`work`\n`pay @user <cant>`\n`leaderboard`",
        inline=False
    )
    embed.add_field(
        name="Moderación",
        value="`ban @user`\n`unban <user>`\n`kick @user`\n`clear <cant>`\n`mute @user`\n`unmute @user`\n`lock`\n`unlock`",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong: `{round(bot.latency * 1000)} ms`")

@bot.command()
async def uptime(ctx: commands.Context):
    elapsed = discord.utils.utcnow() - START_TIME
    days = elapsed.days
    hours, remainder = divmod(elapsed.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m" if minutes else "", f"{seconds}s"]
    await ctx.send(f"Llevo encendido: `{ ' '.join(filter(None, parts)) }`")

@bot.command()
async def serverinfo(ctx: commands.Context):
    guild = ctx.guild
    embed = discord.Embed(title=guild.name, color=discord.Color.green())
    embed.add_field(name="Miembros", value=guild.member_count)
    embed.add_field(name="Canales", value=len(guild.channels))
    embed.add_field(name="Dueño", value=guild.owner.mention if guild.owner else "No disponible")
    if guild.icon: embed.set_thumbnail(url=guild.icon.url)
    await ctx.send(embed=embed)

@bot.command()
async def userinfo(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=str(member), color=member.color or discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=False)
    embed.add_field(name="Cuenta creada", value=discord.utils.format_dt(member.created_at, "F"), inline=False)
    roles = [role.mention for role in member.roles if role != ctx.guild.default_role]
    embed.add_field(name="Roles", value=", ".join(roles[-10:]) if roles else "Sin roles", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def avatar(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"Avatar de {member.display_name}", color=discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command()
async def coin(ctx: commands.Context):
    await ctx.send(random.choice(["Cara", "Sello"]))

@bot.command()
async def dice(ctx: commands.Context, sides: int = 6):
    if sides < 2 or sides > 1000:
        await ctx.send("El dado debe tener entre 2 y 1000 caras.")
        return
    await ctx.send(f"Salió: `{random.randint(1, sides)}`")

@bot.command()
async def choose(ctx: commands.Context, *, options: str):
    choices = [opt.strip() for opt in options.replace(",", "|").split("|") if opt.strip()]
    if len(choices) < 2:
        await ctx.send("Escribe al menos 2 opciones separadas por `|`.")
        return
    await ctx.send(f"Elijo: **{random.choice(choices)}**")

@bot.command()
async def poll(ctx: commands.Context, *, text: str):
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if len(parts) < 3:
        await ctx.send("Formato: `!poll pregunta | opción 1 | opción 2`")
        return
    question, options = parts[0], parts[1:11]
    embed = discord.Embed(title=question, color=discord.Color.gold())
    embed.description = "\n".join(f"{NUMBER_EMOJIS[idx]} {opt}" for idx, opt in enumerate(options))
    message = await ctx.send(embed=embed)
    for idx in range(len(options)): await message.add_reaction(NUMBER_EMOJIS[idx])

@bot.command()
async def remind(ctx: commands.Context, minutes: int, *, text: str):
    if minutes < 1 or minutes > 1440: return
    await ctx.send(f"Te recordaré en {minutes} minuto(s).")
    await asyncio.sleep(minutes * 60)
    await ctx.send(f"{ctx.author.mention} recordatorio: {text}")

# --- COMANDOS TTS Y DE CANAL DE VOZ ---

@bot.command()
async def transcript(ctx: commands.Context, amount: int = 50):
    amount = max(1, min(amount, 200))
    lines = list(voice_transcripts.get(ctx.guild.id, []))[-amount:]
    if not lines:
        await ctx.send("No hay historial de chat de voz disponible.")
        return
    file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="transcripcion.txt")
    await ctx.send(file=file)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def cleartranscript(ctx: commands.Context):
    voice_transcripts.pop(ctx.guild.id, None)
    await ctx.send("Transcripción de voz eliminada.")

@bot.command()
async def vozchat(ctx: commands.Context, state: Optional[str] = None):
    if state is None:
        status = "activada" if voice_tts_enabled.get(ctx.guild.id, True) else "desactivada"
        await ctx.send(f"Lectura del chat de voz está **{status}**.")
        return
    state = state.lower()
    if state in ("on", "activar", "si", "sí"):
        voice_tts_enabled[ctx.guild.id] = True
        await ctx.send("Lector automático activado.")
    elif state in ("off", "desactivar", "no"):
        voice_tts_enabled[ctx.guild.id] = False
        await ctx.send("Lector automático desactivado.")

@bot.command()
async def probarvoz(ctx: commands.Context, *, text: str = "Probando sistema de audio unificado."):
    voice_client = await ensure_voice(ctx)
    if not voice_client: return
    await queue_voice_tts(ctx.guild, text, ctx.author.display_name)
    await ctx.send("Texto encolado en el pipeline de voz.")

# --- COMANDOS DE MÚSICA ---

@bot.command()
async def join(ctx: commands.Context):
    vc = await ensure_voice(ctx)
    if vc: await ctx.send(f"Conectado a **{vc.channel.name}**.")

@bot.command()
async def play(ctx: commands.Context, *, query: str):
    vc = await ensure_voice(ctx)
    if not vc: return
    msg = await ctx.send("Buscando audio...")
    try:
        song = await search_song(query, str(ctx.author))
        get_queue(ctx.guild.id).append(song)
        if not vc.is_playing() and not vc.is_paused():
            play_next(ctx.guild)
            await msg.edit(content=f"Reproduciendo: **{song.title}**")
        else:
            await msg.edit(content=f"Encolada: **{song.title}**")
    except Exception as e:
        await msg.edit(content=f"Error al cargar el video: `{e}`")

@bot.command(name="queue")
async def queue_command(ctx: commands.Context):
    q = get_queue(ctx.guild.id)
    curr = now_playing.get(ctx.guild.id)
    lines = [f"Sonando ahora: **{curr.title}**"] if curr else []
    if q:
        for idx, song in enumerate(list(q)[:10], start=1):
            lines.append(f"{idx}. {song.title} (por {song.requested_by})")
    if not lines: lines.append("Cola vacía.")
    await ctx.send("\n".join(lines))

@bot.command(aliases=["np"])
async def now(ctx: commands.Context):
    curr = now_playing.get(ctx.guild.id)
    if curr: await ctx.send(f"Ahora suena: **{curr.title}** | Pedido por: `{curr.requested_by}`")
    else: await ctx.send("Silencio total.")

@bot.command()
async def idle(ctx: commands.Context):
    vc = ctx.guild.voice_client
    if not vc or not vc.is_connected(): return
    last_act = last_voice_activity.get(ctx.guild.id, START_TIME)
    rem = max(0, IDLE_TIMEOUT_SECONDS - int((discord.utils.utcnow() - last_act).total_seconds()))
    await ctx.send(f"Auto-desconexión en: {rem} segundos.")

@bot.command()
async def shuffle(ctx: commands.Context):
    q = get_queue(ctx.guild.id)
    if len(q) < 2: return
    s = list(q); random.shuffle(s); q.clear(); q.extend(s)
    await ctx.send("Cola mezclada.")

@bot.command()
async def remove(ctx: commands.Context, index: int):
    q = get_queue(ctx.guild.id)
    if index < 1 or index > len(q): return
    s = list(q); rm = s.pop(index - 1); q.clear(); q.extend(s)
    if rm.is_tts:
        try: Path(rm.url).unlink()
        except: pass
    await ctx.send(f"Removido: **{rm.title}**")

@bot.command()
async def pause(ctx: commands.Context):
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        ctx.guild.voice_client.pause(); await ctx.send("Pausado.")

@bot.command()
async def resume(ctx: commands.Context):
    if ctx.guild.voice_client and ctx.guild.voice_client.is_paused():
        ctx.guild.voice_client.resume(); await ctx.send("Reanudado.")

@bot.command()
async def skip(ctx: commands.Context):
    if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
        ctx.guild.voice_client.stop(); await ctx.send("Saltado.")

@bot.command()
async def stop(ctx: commands.Context):
    q = get_queue(ctx.guild.id)
    for s in q:
        if s.is_tts:
            try: Path(s.url).unlink()
            except: pass
    q.clear()
    if ctx.guild.voice_client: ctx.guild.voice_client.stop()
    await ctx.send("Cola limpiada y audio detenido.")

@bot.command()
async def leave(ctx: commands.Context):
    await stop(ctx)
    if ctx.guild.voice_client: await ctx.guild.voice_client.disconnect()

# --- COMANDOS DE ECONOMÍA ---

@bot.command(aliases=["bal"])
async def balance(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    acc = await get_account(ctx.guild.id, member.id)
    await ctx.send(f"{member.mention} balance: **{acc['coins']}** monedas.")

@bot.command()
async def daily(ctx: commands.Context):
    acc = await get_account(ctx.guild.id, ctx.author.id)
    now = discord.utils.utcnow()
    if acc.get("last_daily"):
        rem = timedelta(hours=20) - (now - datetime.fromisoformat(acc["last_daily"]))
        if rem.total_seconds() > 0:
            await ctx.send(f"Vuelve en {int(rem.total_seconds()//3600)}h {int((rem.total_seconds()%3600)//60)}m.")
            return
    r = random.randint(150, 300); acc["coins"] += r; acc["last_daily"] = now.isoformat()
    await update_account(ctx.guild.id, ctx.author.id, acc)
    await ctx.send(f"Diario reclamado: **+{r}** monedas.")

@bot.command()
async def work(ctx: commands.Context):
    acc = await get_account(ctx.guild.id, ctx.author.id)
    now = discord.utils.utcnow()
    if acc.get("last_work"):
        rem = timedelta(hours=1) - (now - datetime.fromisoformat(acc["last_work"]))
        if rem.total_seconds() > 0:
            await ctx.send(f"Descansa, vuelve en {int(rem.total_seconds()//60)} minutos.")
            return
    jobs = ["Escribiste código", "Arreglaste bugs", "Limpiaste la base de datos", "Configuraste un VPS"]
    r = random.randint(50, 150); acc["coins"] += r; acc["last_work"] = now.isoformat()
    await update_account(ctx.guild.id, ctx.author.id, acc)
    await ctx.send(f"{random.choice(jobs)} y ganaste **{r}** monedas.")

@bot.command()
async def pay(ctx: commands.Context, member: discord.Member, amount: int):
    if member.bot or member.id == ctx.author.id or amount < 1: return
    snd = await get_account(ctx.guild.id, ctx.author.id)
    if snd["coins"] < amount:
        await ctx.send("No te alcanza.")
        return
    rcv = await get_account(ctx.guild.id, member.id)
    snd["coins"] -= amount; rcv["coins"] += amount
    await update_account(ctx.guild.id, ctx.author.id, snd)
    await update_account(ctx.guild.id, member.id, rcv)
    await ctx.send(f"Transferencia exitosa de **{amount}** monedas a {member.mention}.")

@bot.command(aliases=["lb"])
async def leaderboard(ctx: commands.Context):
    data = await load_economy()
    g_data = data["guilds"].get(str(ctx.guild.id), {})
    rk = sorted(g_data.items(), key=lambda x: x[1].get("coins", 0), reverse=True)[:10]
    lines = [f"{i}. **{ctx.guild.get_member(int(uid)).display_name if ctx.guild.get_member(int(uid)) else uid}** - {acc.get('coins',0)} coins" for i, (uid, acc) in enumerate(rk,1)]
    await ctx.send(embed=discord.Embed(title="Top Economía", description="\n".join(lines) or "Sin datos", color=discord.Color.gold()))

# --- MODERACIÓN ---

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razón"):
    await member.ban(reason=reason); await ctx.send(f"{member} baneado.")

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, *, username: str):
    async for entry in ctx.guild.bans():
        if entry.user.name.lower() == username.lower() or str(entry.user.id) == username:
            await ctx.guild.unban(entry.user); await ctx.send(f"{entry.user} desbaneado.")
            return
    await ctx.send("No encontrado.")

@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razón"):
    await member.kick(reason=reason); await ctx.send(f"{member} expulsado.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int):
    if 0 < amount <= 100:
        del_msgs = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"Borrados {len(del_msgs)-1} mensajes.", delete_after=4)

@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx: commands.Context, member: discord.Member, minutes: int = 10, *, reason: str = "Sin razón"):
    await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason=reason)
    await ctx.send(f"{member.mention} muteado por {minutes}m.")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member):
    await member.timeout(None); await ctx.send(f"{member.mention} desmuteado.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("Canal bloqueado.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("Canal desbloqueado.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.message.delete(); await ctx.send(message)

# --- MANEJO DE ERRORES GLOBALES ---

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("No tienes permisos suficientes para usar ese comando.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Faltan argumentos. Revisa el uso del comando con `!help`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Dato o usuario no válido.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignora silenciosamente si ponen un comando que no existe
    else:
        print(f"[CMD ERROR] {error}")
        await ctx.send("Ocurrió un error inesperado ejecutando el comando.")

if not TOKEN: raise RuntimeError("Falta DISCORD_TOKEN en el .env")
bot.run(TOKEN)