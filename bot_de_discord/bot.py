import asyncio
import io
import json
import os
import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Optional

import discord
import yt_dlp
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
IDLE_TIMEOUT_SECONDS = int(os.getenv("VOICE_IDLE_TIMEOUT_SECONDS", "300"))
DATA_DIR = Path("data")
ECONOMY_FILE = DATA_DIR / "economy.json"
TTS_DIR = DATA_DIR / "tts"

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
START_TIME = discord.utils.utcnow()


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
    url: str
    webpage_url: str
    requested_by: str


music_queues: dict[int, Deque[Song]] = {}
now_playing: dict[int, Song] = {}
last_voice_activity: dict[int, datetime] = {}
voice_transcripts: dict[int, Deque[str]] = {}
voice_tts_enabled: dict[int, bool] = {}
voice_tts_queues: dict[int, Deque[str]] = {}
voice_tts_running: set[int] = set()
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


def load_economy() -> dict:
    if not ECONOMY_FILE.exists():
        return {"guilds": {}}
    with ECONOMY_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_economy(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with ECONOMY_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def get_account(guild_id: int, user_id: int) -> dict:
    data = load_economy()
    guild = data["guilds"].setdefault(str(guild_id), {})
    account = guild.setdefault(
        str(user_id),
        {"coins": 0, "last_daily": None, "last_work": None},
    )
    save_economy(data)
    return account


def update_account(guild_id: int, user_id: int, account: dict) -> None:
    data = load_economy()
    data["guilds"].setdefault(str(guild_id), {})[str(user_id)] = account
    save_economy(data)


def mark_voice_activity(guild_id: int) -> None:
    last_voice_activity[guild_id] = discord.utils.utcnow()


def add_voice_transcript(guild_id: int, line: str) -> None:
    if guild_id not in voice_transcripts:
        voice_transcripts[guild_id] = deque(maxlen=200)
    voice_transcripts[guild_id].append(line)


def clean_tts_text(text: str) -> str:
    cleaned = " ".join(text.replace("\n", " ").split())
    if len(cleaned) > 180:
        cleaned = cleaned[:177] + "..."
    return cleaned


async def synthesize_tts(text: str, output_path: Path) -> None:
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    script = (
        "& { param($Text,$Path) "
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 0; "
        "$s.Volume = 100; "
        "$s.SetOutputToWaveFile($Path); "
        "$s.Speak($Text); "
        "$s.Dispose() }"
    )
    process = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
        text,
        str(output_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.communicate()
    if process.returncode != 0:
        raise RuntimeError("No pude generar el audio TTS.")


async def play_audio_file(voice_client: discord.VoiceClient, path: Path) -> None:
    finished = asyncio.Event()

    def after_playing(error):
        if error:
            print(f"Error reproduciendo TTS: {error}")
        bot.loop.call_soon_threadsafe(finished.set)

    source = discord.FFmpegPCMAudio(str(path), executable=FFMPEG_PATH)
    voice_client.play(source, after=after_playing)
    await finished.wait()


async def convert_audio(input_path: Path, output_path: Path) -> None:
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
    if process.returncode != 0:
        raise RuntimeError("No pude convertir el audio para Discord.")


async def voice_tts_worker(guild: discord.Guild) -> None:
    guild_id = guild.id
    voice_tts_running.add(guild_id)

    try:
        while voice_tts_queues.get(guild_id):
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                break

            while (
                voice_client.is_playing()
                or voice_client.is_paused()
                or get_queue(guild_id)
            ):
                await asyncio.sleep(1)
                voice_client = guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    return

            text = voice_tts_queues[guild_id].popleft()
            mark_voice_activity(guild_id)
            wav_path = TTS_DIR / f"voice-chat-{guild_id}.wav"
            mp3_path = TTS_DIR / f"voice-chat-{guild_id}.mp3"
            try:
                await synthesize_tts(text, wav_path)
                await convert_audio(wav_path, mp3_path)
                await play_audio_file(voice_client, mp3_path)
            except Exception as error:
                print(f"No pude leer mensaje del canal de voz: {error}")

        if guild.voice_client and get_queue(guild_id):
            play_next(guild)
    finally:
        voice_tts_running.discard(guild_id)


def queue_voice_tts(guild: discord.Guild, text: str) -> None:
    cleaned = clean_tts_text(text)
    if not cleaned:
        return
    if guild.id not in voice_tts_queues:
        voice_tts_queues[guild.id] = deque(maxlen=20)
    voice_tts_queues[guild.id].append(cleaned)
    if guild.id not in voice_tts_running:
        bot.loop.create_task(voice_tts_worker(guild))


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


def get_queue(guild_id: int) -> Deque[Song]:
    if guild_id not in music_queues:
        music_queues[guild_id] = deque()
    return music_queues[guild_id]


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
        title=data.get("title", "Cancion sin titulo"),
        url=data["url"],
        webpage_url=data.get("webpage_url", query),
        requested_by=requested_by,
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


def play_next(guild: discord.Guild):
    queue = get_queue(guild.id)
    voice_client = guild.voice_client

    if not voice_client or not queue:
        now_playing.pop(guild.id, None)
        mark_voice_activity(guild.id)
        return

    song = queue.popleft()
    now_playing[guild.id] = song
    mark_voice_activity(guild.id)
    source = discord.FFmpegPCMAudio(song.url, executable=FFMPEG_PATH, **FFMPEG_OPTIONS)

    def after_playing(error):
        if error:
            print(f"Error reproduciendo audio: {error}")
        bot.loop.call_soon_threadsafe(play_next, guild)

    voice_client.play(source, after=after_playing)


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
                f"[{timestamp}] {message.author.display_name}: {message.content}",
            )
            if voice_tts_enabled.get(message.guild.id, True) and not message.content.startswith(PREFIX):
                queue_voice_tts(message.guild, f"{message.author.display_name} dice: {message.content}")

    await bot.process_commands(message)


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="Comandos del bot",
        description=f"Prefijo actual: `{PREFIX}`",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Musica",
        value=(
            "`play <nombre o URL>` reproduce o agrega a cola\n"
            "`join` conecta el bot a tu canal de voz\n"
            "`now` muestra la cancion actual\n"
            "`pause` pausa\n"
            "`resume` continua\n"
            "`skip` salta la cancion\n"
            "`queue` muestra la cola\n"
            "`shuffle` mezcla la cola\n"
            "`remove <numero>` quita una cancion de la cola\n"
            "`stop` detiene y limpia la cola\n"
            "`leave` desconecta el bot\n"
            "`idle` muestra el tiempo de auto-salida"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderacion",
        value=(
            "`ban @usuario [razon]`\n"
            "`unban nombre#0000 [razon]`\n"
            "`kick @usuario [razon]`\n"
            "`clear <cantidad>`\n"
            "`mute @usuario [minutos] [razon]`\n"
            "`unmute @usuario [razon]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Utilidad",
        value=(
            "`ping` muestra la latencia\n"
            "`uptime` muestra cuanto tiempo lleva encendido\n"
            "`serverinfo` informacion del servidor\n"
            "`userinfo [@usuario]` informacion de un usuario\n"
            "`avatar [@usuario]` muestra el avatar\n"
            "`remind <minutos> <texto>` crea un recordatorio\n"
            "`transcript [cantidad]` guarda el chat escrito del canal de voz\n"
            "`cleartranscript` limpia esa transcripcion\n"
            "`vozchat on/off` lee en voz alta el chat escrito del canal de voz\n"
            "`probarvoz <texto>` prueba la voz automatica\n"
            "`decirvoz <texto>` prueba directa con diagnostico"
        ),
        inline=False,
    )
    embed.add_field(
        name="Diversion",
        value=(
            "`coin` tira una moneda\n"
            "`dice [caras]` tira un dado\n"
            "`choose opcion1 | opcion2 | opcion3` elige una opcion\n"
            "`poll pregunta | opcion1 | opcion2` crea una encuesta"
        ),
        inline=False,
    )
    embed.add_field(
        name="Economia",
        value=(
            "`balance [@usuario]` mira monedas\n"
            "`daily` reclama premio diario\n"
            "`work` trabaja por monedas\n"
            "`pay @usuario cantidad` paga a alguien\n"
            "`leaderboard` ranking de economia"
        ),
        inline=False,
    )
    embed.add_field(
        name="Extra admin",
        value=(
            "`say <mensaje>` hace que el bot diga algo\n"
            "`slowmode <segundos>` cambia el modo lento del canal\n"
            "`lock` bloquea el canal actual\n"
            "`unlock` desbloquea el canal actual"
        ),
        inline=False,
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
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    await ctx.send(f"Llevo encendido: `{ ' '.join(parts) }`")


@bot.command()
async def serverinfo(ctx: commands.Context):
    guild = ctx.guild
    embed = discord.Embed(title=guild.name, color=discord.Color.green())
    embed.add_field(name="Miembros", value=guild.member_count)
    embed.add_field(name="Canales", value=len(guild.channels))
    embed.add_field(name="Roles", value=len(guild.roles))
    embed.add_field(name="Dueno", value=guild.owner.mention if guild.owner else "No disponible")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await ctx.send(embed=embed)


@bot.command()
async def userinfo(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=str(member), color=member.color or discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=False)
    embed.add_field(name="Cuenta creada", value=discord.utils.format_dt(member.created_at, "F"), inline=False)
    if member.joined_at:
        embed.add_field(name="Entro al servidor", value=discord.utils.format_dt(member.joined_at, "F"), inline=False)
    roles = [role.mention for role in member.roles if role != ctx.guild.default_role]
    embed.add_field(name="Roles", value=", ".join(roles[-10:]) if roles else "Sin roles", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def avatar(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"Avatar de {member.display_name}", color=discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(aliases=["moneda"])
async def coin(ctx: commands.Context):
    await ctx.send(random.choice(["Cara", "Sello"]))


@bot.command(aliases=["dado"])
async def dice(ctx: commands.Context, sides: int = 6):
    if sides < 2 or sides > 1000:
        await ctx.send("El dado debe tener entre 2 y 1000 caras.")
        return
    await ctx.send(f"Salio: `{random.randint(1, sides)}`")


@bot.command(aliases=["elige"])
async def choose(ctx: commands.Context, *, options: str):
    choices = [option.strip() for option in options.replace(",", "|").split("|") if option.strip()]
    if len(choices) < 2:
        await ctx.send("Escribe al menos 2 opciones separadas por `|`. Ejemplo: `!choose pizza | tacos`")
        return
    await ctx.send(f"Elijo: **{random.choice(choices)}**")


@bot.command()
async def poll(ctx: commands.Context, *, text: str):
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if len(parts) < 3:
        await ctx.send("Formato: `!poll pregunta | opcion 1 | opcion 2`")
        return
    question, options = parts[0], parts[1:11]
    embed = discord.Embed(title=question, color=discord.Color.gold())
    description = "\n".join(f"{NUMBER_EMOJIS[index]} {option}" for index, option in enumerate(options))
    embed.description = description
    embed.set_footer(text=f"Encuesta creada por {ctx.author.display_name}")
    message = await ctx.send(embed=embed)
    for index in range(len(options)):
        await message.add_reaction(NUMBER_EMOJIS[index])


@bot.command()
async def remind(ctx: commands.Context, minutes: int, *, text: str):
    if minutes < 1 or minutes > 1440:
        await ctx.send("El recordatorio debe ser entre 1 y 1440 minutos.")
        return

    await ctx.send(f"Listo, te recordare en {minutes} minuto(s).")

    async def reminder():
        await asyncio.sleep(minutes * 60)
        await ctx.send(f"{ctx.author.mention} recordatorio: {text}")

    bot.loop.create_task(reminder())


@bot.command()
async def transcript(ctx: commands.Context, amount: int = 50):
    amount = max(1, min(amount, 200))
    lines = list(voice_transcripts.get(ctx.guild.id, []))[-amount:]
    if not lines:
        await ctx.send("Todavia no tengo mensajes del chat escrito del canal de voz.")
        return

    text = "\n".join(lines)
    file = discord.File(
        io.BytesIO(text.encode("utf-8")),
        filename=f"transcripcion-voz-{ctx.guild.id}.txt",
    )
    await ctx.send("Aqui esta la transcripcion del chat escrito del canal de voz.", file=file)


@bot.command()
@commands.has_permissions(manage_messages=True)
async def cleartranscript(ctx: commands.Context):
    voice_transcripts.pop(ctx.guild.id, None)
    await ctx.send("Transcripcion del chat de voz limpiada.")


@bot.command()
async def vozchat(ctx: commands.Context, state: Optional[str] = None):
    if state is None:
        status = "activada" if voice_tts_enabled.get(ctx.guild.id, True) else "desactivada"
        await ctx.send(f"La lectura automatica del chat de voz esta **{status}**.")
        return

    state = state.lower()
    if state in ("on", "activar", "activado", "si", "sí"):
        voice_tts_enabled[ctx.guild.id] = True
        await ctx.send("Lectura automatica del chat de voz activada.")
    elif state in ("off", "apagar", "desactivar", "desactivado", "no"):
        voice_tts_enabled[ctx.guild.id] = False
        voice_tts_queues.pop(ctx.guild.id, None)
        await ctx.send("Lectura automatica del chat de voz desactivada.")
    else:
        await ctx.send("Usa `!vozchat on` o `!vozchat off`.")


@bot.command()
async def probarvoz(ctx: commands.Context, *, text: str = "Esto es una prueba de voz."):
    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return
    voice_tts_enabled[ctx.guild.id] = True
    queue_voice_tts(ctx.guild, f"{ctx.author.display_name} dice: {text}")
    await ctx.send("Prueba de voz enviada al canal.")


@bot.command()
async def decirvoz(ctx: commands.Context, *, text: str = "Esto es una prueba directa de voz."):
    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return

    if voice_client.is_playing() or voice_client.is_paused():
        await ctx.send("Ahora mismo ya hay audio sonando o pausado. Usa `!stop` y vuelve a probar.")
        return

    status = await ctx.send("Generando audio de prueba...")
    wav_path = TTS_DIR / f"direct-test-{ctx.guild.id}.wav"
    mp3_path = TTS_DIR / f"direct-test-{ctx.guild.id}.mp3"

    try:
        await synthesize_tts(clean_tts_text(text), wav_path)
        if not wav_path.exists() or wav_path.stat().st_size == 0:
            await status.edit(content="Fallo: no se genero el archivo WAV.")
            return

        await convert_audio(wav_path, mp3_path)
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            await status.edit(content="Fallo: no se genero el archivo MP3.")
            return

        await status.edit(content="Reproduciendo prueba de voz...")
        await play_audio_file(voice_client, mp3_path)
        await status.edit(content="Prueba terminada. Si no escuchaste nada, revisa permisos/volumen del bot en Discord.")
    except Exception as error:
        await status.edit(content=f"Fallo la prueba de voz: `{error}`")


@bot.command(aliases=["bal", "saldo"])
async def balance(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    account = get_account(ctx.guild.id, member.id)
    await ctx.send(f"{member.mention} tiene **{account['coins']}** monedas.")


@bot.command()
async def daily(ctx: commands.Context):
    account = get_account(ctx.guild.id, ctx.author.id)
    now = discord.utils.utcnow()
    if account.get("last_daily"):
        last_daily = datetime.fromisoformat(account["last_daily"])
        remaining = timedelta(hours=20) - (now - last_daily)
        if remaining.total_seconds() > 0:
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes = remainder // 60
            await ctx.send(f"Ya reclamaste tu diario. Vuelve en {hours}h {minutes}m.")
            return

    reward = random.randint(150, 300)
    account["coins"] += reward
    account["last_daily"] = now.isoformat()
    update_account(ctx.guild.id, ctx.author.id, account)
    await ctx.send(f"Reclamaste tu diario: **+{reward}** monedas.")


@bot.command()
async def work(ctx: commands.Context):
    account = get_account(ctx.guild.id, ctx.author.id)
    now = discord.utils.utcnow()
    if account.get("last_work"):
        last_work = datetime.fromisoformat(account["last_work"])
        remaining = timedelta(hours=1) - (now - last_work)
        if remaining.total_seconds() > 0:
            minutes = int(remaining.total_seconds()) // 60
            await ctx.send(f"Necesitas descansar. Vuelve en {minutes} minuto(s).")
            return

    jobs = [
        "arreglaste cables del servidor",
        "vendiste pan virtual",
        "limpiaste el canal general",
        "programaste un mini bot",
        "organizaste una playlist",
    ]
    reward = random.randint(40, 120)
    account["coins"] += reward
    account["last_work"] = now.isoformat()
    update_account(ctx.guild.id, ctx.author.id, account)
    await ctx.send(f"{random.choice(jobs)} y ganaste **{reward}** monedas.")


@bot.command()
async def pay(ctx: commands.Context, member: discord.Member, amount: int):
    if member.bot:
        await ctx.send("No puedes pagarle a bots.")
        return
    if member.id == ctx.author.id:
        await ctx.send("No puedes pagarte a ti mismo.")
        return
    if amount < 1:
        await ctx.send("La cantidad debe ser mayor que 0.")
        return

    sender = get_account(ctx.guild.id, ctx.author.id)
    receiver = get_account(ctx.guild.id, member.id)
    if sender["coins"] < amount:
        await ctx.send("No tienes suficientes monedas.")
        return

    sender["coins"] -= amount
    receiver["coins"] += amount
    update_account(ctx.guild.id, ctx.author.id, sender)
    update_account(ctx.guild.id, member.id, receiver)
    await ctx.send(f"{ctx.author.mention} le pago **{amount}** monedas a {member.mention}.")


@bot.command(aliases=["lb", "top"])
async def leaderboard(ctx: commands.Context):
    data = load_economy()
    guild_data = data["guilds"].get(str(ctx.guild.id), {})
    ranking = sorted(
        guild_data.items(),
        key=lambda item: item[1].get("coins", 0),
        reverse=True,
    )[:10]
    if not ranking:
        await ctx.send("Todavia no hay economia en este servidor.")
        return

    lines = []
    for index, (user_id, account) in enumerate(ranking, start=1):
        member = ctx.guild.get_member(int(user_id))
        name = member.display_name if member else f"Usuario {user_id}"
        lines.append(f"{index}. **{name}** - {account.get('coins', 0)} monedas")

    embed = discord.Embed(title="Ranking de economia", description="\n".join(lines), color=discord.Color.gold())
    await ctx.send(embed=embed)


@bot.command()
async def join(ctx: commands.Context):
    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return
    await ctx.send(f"Conectado a **{voice_client.channel.name}**.")


@bot.command()
async def play(ctx: commands.Context, *, query: str):
    voice_client = await ensure_voice(ctx)
    if not voice_client:
        return

    message = await ctx.send("Buscando cancion...")
    song = await search_song(query, str(ctx.author))
    queue = get_queue(ctx.guild.id)
    queue.append(song)

    if not voice_client.is_playing() and not voice_client.is_paused():
        play_next(ctx.guild)
        await message.edit(content=f"Reproduciendo: **{song.title}**")
    else:
        await message.edit(content=f"Agregada a la cola: **{song.title}**")


@bot.command(name="queue")
async def queue_command(ctx: commands.Context):
    queue = get_queue(ctx.guild.id)
    current = now_playing.get(ctx.guild.id)

    lines = []
    if current:
        lines.append(f"Ahora: **{current.title}**")

    if queue:
        for index, song in enumerate(list(queue)[:10], start=1):
            lines.append(f"{index}. {song.title} - pedido por {song.requested_by}")
    else:
        lines.append("No hay canciones en cola.")

    await ctx.send("\n".join(lines))


@bot.command(aliases=["np", "actual"])
async def now(ctx: commands.Context):
    current = now_playing.get(ctx.guild.id)
    if not current:
        await ctx.send("No hay ninguna cancion reproduciendose.")
        return
    await ctx.send(f"Ahora suena: **{current.title}**\nPedida por: `{current.requested_by}`")


@bot.command()
async def idle(ctx: commands.Context):
    voice_client = ctx.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await ctx.send("No estoy conectado a un canal de voz.")
        return

    if voice_client.is_playing() or voice_client.is_paused() or get_queue(ctx.guild.id):
        await ctx.send("No me saldre mientras haya musica activa, pausada o en cola.")
        return

    last_activity = last_voice_activity.get(ctx.guild.id, START_TIME)
    idle_seconds = (discord.utils.utcnow() - last_activity).total_seconds()
    remaining = max(0, IDLE_TIMEOUT_SECONDS - int(idle_seconds))
    await ctx.send(f"Me saldre automaticamente en {remaining} segundo(s) si no hay actividad.")


@bot.command()
async def shuffle(ctx: commands.Context):
    queue = get_queue(ctx.guild.id)
    if len(queue) < 2:
        await ctx.send("Necesito al menos 2 canciones en cola para mezclarlas.")
        return
    songs = list(queue)
    random.shuffle(songs)
    queue.clear()
    queue.extend(songs)
    await ctx.send("Cola mezclada.")


@bot.command()
async def remove(ctx: commands.Context, index: int):
    queue = get_queue(ctx.guild.id)
    if index < 1 or index > len(queue):
        await ctx.send("Ese numero no existe en la cola.")
        return
    songs = list(queue)
    removed = songs.pop(index - 1)
    queue.clear()
    queue.extend(songs)
    await ctx.send(f"Quite de la cola: **{removed.title}**")


@bot.command()
async def pause(ctx: commands.Context):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.send("Musica pausada.")
    else:
        await ctx.send("No hay musica reproduciendose.")


@bot.command()
async def resume(ctx: commands.Context):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.send("Musica reanudada.")
    else:
        await ctx.send("La musica no esta pausada.")


@bot.command()
async def skip(ctx: commands.Context):
    voice_client = ctx.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await ctx.send("Cancion saltada.")
    else:
        await ctx.send("No hay cancion para saltar.")


@bot.command()
async def stop(ctx: commands.Context):
    get_queue(ctx.guild.id).clear()
    voice_client = ctx.guild.voice_client
    if voice_client:
        voice_client.stop()
    now_playing.pop(ctx.guild.id, None)
    await ctx.send("Musica detenida y cola limpiada.")


@bot.command()
async def leave(ctx: commands.Context):
    voice_client = ctx.guild.voice_client
    get_queue(ctx.guild.id).clear()
    now_playing.pop(ctx.guild.id, None)

    if voice_client:
        await voice_client.disconnect()
        await ctx.send("Me desconecte del canal de voz.")
    else:
        await ctx.send("No estoy conectado a un canal de voz.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razon"):
    await member.ban(reason=reason)
    await ctx.send(f"{member.mention} fue baneado. Razon: {reason}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, *, user_tag: str):
    banned_users = [entry async for entry in ctx.guild.bans()]
    user_name, _, user_discriminator = user_tag.partition("#")

    for ban_entry in banned_users:
        user = ban_entry.user
        if user.name == user_name and user.discriminator == user_discriminator:
            await ctx.guild.unban(user)
            await ctx.send(f"{user_tag} fue desbaneado.")
            return

    await ctx.send("No encontre ese usuario en la lista de baneados.")


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razon"):
    await member.kick(reason=reason)
    await ctx.send(f"{member.mention} fue expulsado. Razon: {reason}")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx: commands.Context, amount: int):
    if amount < 1 or amount > 100:
        await ctx.send("Elige una cantidad entre 1 y 100.")
        return

    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"Se borraron {len(deleted) - 1} mensajes.", delete_after=5)


@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(
    ctx: commands.Context,
    member: discord.Member,
    minutes: int = 10,
    *,
    reason: str = "Sin razon",
):
    duration = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"{member.mention} fue silenciado por {minutes} minutos. Razon: {reason}")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member, *, reason: str = "Sin razon"):
    await member.timeout(None, reason=reason)
    await ctx.send(f"{member.mention} ya no esta silenciado.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, message: str):
    await ctx.message.delete()
    await ctx.send(message)


@bot.command()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
    if seconds < 0 or seconds > 21600:
        await ctx.send("El modo lento debe estar entre 0 y 21600 segundos.")
        return
    await ctx.channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await ctx.send("Modo lento desactivado.")
    else:
        await ctx.send(f"Modo lento configurado en {seconds} segundo(s).")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send("Canal bloqueado.")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send("Canal desbloqueado.")


@ban.error
@unban.error
@kick.error
@clear.error
@mute.error
@unmute.error
@say.error
@slowmode.error
@lock.error
@unlock.error
@cleartranscript.error
async def moderation_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("No tienes permisos para usar ese comando.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Faltan argumentos. Usa `!help` para ver ejemplos.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("No pude encontrar ese usuario o el dato enviado no es valido.")
    else:
        await ctx.send("Ocurrio un error ejecutando el comando.")
        raise error


@play.error
@choose.error
@poll.error
@remind.error
@dice.error
@remove.error
@transcript.error
@daily.error
@work.error
@pay.error
@leaderboard.error
async def play_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Faltan argumentos. Usa `!help` para ver ejemplos.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Uno de los datos enviados no es valido.")
    else:
        await ctx.send("Ocurrio un error ejecutando ese comando.")
        raise error


if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN. Crea un archivo .env con tu token.")

bot.run(TOKEN)
