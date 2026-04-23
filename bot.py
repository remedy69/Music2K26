import asyncio
import json
import os
import random
import re
import tempfile
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
import yt_dlp

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
PLAYLISTS_FILE = DATA_DIR / "playlists.json"
SETTINGS_FILE = DATA_DIR / "guild_settings.json"

PREFIX = os.getenv("COMMAND_PREFIX", "!")
TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is required.")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

URL_RE = re.compile(r"^https?://", re.I)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")


playlists = load_json(PLAYLISTS_FILE, {})
guild_settings = load_json(SETTINGS_FILE, {})


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: int = 0
    uploader: str = ""
    thumbnail: str = ""
    requester_id: int = 0
    source: str = "YouTube"

    @classmethod
    def from_info(cls, info: dict, requester_id: int) -> "Track":
        source = info.get("extractor_key") or info.get("extractor") or "Unknown"
        return cls(
            title=info.get("title") or "Unknown title",
            webpage_url=info.get("webpage_url") or info.get("original_url") or "",
            stream_url=info.get("url") or "",
            duration=info.get("duration") or 0,
            uploader=info.get("uploader") or info.get("channel") or "",
            thumbnail=info.get("thumbnail") or "",
            requester_id=requester_id,
            source=source,
        )


class GuildState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        settings = guild_settings.get(str(guild_id), {})
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.loop_mode: str = settings.get("loop_mode", "off")  # off, track, queue
        self.volume: float = float(settings.get("volume", 0.6))
        self.autoplay: bool = bool(settings.get("autoplay", True))
        self.filter_name: str = settings.get("filter_name", "off")
        self.stay_247: bool = bool(settings.get("stay_247", False))
        self.panel_message_id: Optional[int] = settings.get("panel_message_id")
        self.bound_text_channel_id: Optional[int] = settings.get("text_channel_id")
        self.bound_voice_channel_id: Optional[int] = settings.get("voice_channel_id")

    def persist(self):
        guild_settings[str(self.guild_id)] = {
            "loop_mode": self.loop_mode,
            "volume": self.volume,
            "autoplay": self.autoplay,
            "filter_name": self.filter_name,
            "stay_247": self.stay_247,
            "panel_message_id": self.panel_message_id,
            "text_channel_id": self.bound_text_channel_id,
            "voice_channel_id": self.bound_voice_channel_id,
        }
        save_json(SETTINGS_FILE, guild_settings)


guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState(guild_id)
    return guild_states[guild_id]


# --- CORRECTED DICTIONARY FORMAT FOR PYTHON API ---
ytdl_opts = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "ignoreerrors": False,
    "source_address": "0.0.0.0",
    "geo_bypass": True,
    "nocheckcertificate": True,
    "js_runtimes": {
        "deno": {},
        "nodejs": {}
    }
}

if YOUTUBE_COOKIES:
    cookie_fd, cookie_path = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
    with os.fdopen(cookie_fd, "w", encoding="utf-8") as handle:
        handle.write(YOUTUBE_COOKIES.replace("\\n", "\n"))
    ytdl_opts["cookiefile"] = cookie_path

ytdl = yt_dlp.YoutubeDL(ytdl_opts)

BASE_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FILTER_MAP = {
    "off": "",
    "bassboost": "bass=g=8",
    "nightcore": "asetrate=48000*1.15,aresample=48000,atempo=1.08",
    "vaporwave": "asetrate=48000*0.85,aresample=48000,atempo=0.95",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
}


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Live/Unknown"
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def track_embed(state: GuildState, track: Track, title: str = "Now Playing") -> discord.Embed:
    embed = discord.Embed(title=title, description=f"**[{track.title}]({track.webpage_url})**")
    embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
    embed.add_field(name="Source", value=track.source, inline=True)
    embed.add_field(name="Loop", value=state.loop_mode, inline=True)
    embed.add_field(name="Autoplay", value="On" if state.autoplay else "Off", inline=True)
    embed.add_field(name="Filter", value=state.filter_name, inline=True)
    embed.add_field(name="Volume", value=f"{int(state.volume * 100)}%", inline=True)
    if track.uploader:
        embed.add_field(name="Uploader", value=track.uploader, inline=False)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    if track.requester_id:
        embed.set_footer(text=f"Requested by user id {track.requester_id}")
    return embed


async def safe_send(target, *args, **kwargs):
    try:
        return await target.send(*args, **kwargs)
    except Exception:
        return None


async def extract_tracks(query: str, requester_id: int) -> list[Track]:
    loop = asyncio.get_running_loop()

    def _extract():
        search = query if URL_RE.match(query) else f"ytsearch1:{query}"
        return ytdl.extract_info(search, download=False)

    data = await loop.run_in_executor(None, _extract)
    if not data:
        raise RuntimeError("No results found.")
    entries = []
    if "entries" in data:
        entries = [entry for entry in data["entries"] if entry]
    else:
        entries = [data]

    tracks: list[Track] = []
    for entry in entries:
        if entry.get("_type") == "playlist":
            for sub in entry.get("entries", []):
                if sub:
                    tracks.append(Track.from_info(sub, requester_id))
        else:
            tracks.append(Track.from_info(entry, requester_id))
    return [t for t in tracks if t.stream_url or t.webpage_url]


def make_audio_source(track: Track, state: GuildState):
    filter_args = FILTER_MAP.get(state.filter_name, "")
    options = "-vn"
    if filter_args:
        options += f' -af "{filter_args}"'
    return discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(track.stream_url, before_options=BASE_BEFORE, options=options),
        volume=state.volume,
    )


async def ensure_voice(ctx_or_interaction) -> discord.VoiceClient:
    guild = ctx_or_interaction.guild
    user = getattr(ctx_or_interaction, "author", None) or getattr(ctx_or_interaction, "user", None)
    if not user or not user.voice or not user.voice.channel:
        raise RuntimeError("You need to be in a voice channel first.")

    voice_client = guild.voice_client
    if voice_client and voice_client.channel != user.voice.channel:
        await voice_client.move_to(user.voice.channel)
    elif not voice_client:
        voice_client = await user.voice.channel.connect()

    state = get_state(guild.id)
    state.bound_voice_channel_id = user.voice.channel.id
    if getattr(ctx_or_interaction, "channel", None):
        state.bound_text_channel_id = ctx_or_interaction.channel.id
    state.persist()
    return voice_client


async def update_panel(guild: discord.Guild):
    state = get_state(guild.id)
    if not state.bound_text_channel_id or not state.panel_message_id:
        return
    channel = guild.get_channel(state.bound_text_channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(state.panel_message_id)
    except Exception:
        return

    if state.current:
        embed = track_embed(state, state.current, "Music Panel")
    else:
        embed = discord.Embed(title="Music Panel", description="Nothing is playing right now.")
        embed.add_field(name="Queue", value=str(len(state.queue)), inline=True)
        embed.add_field(name="Autoplay", value="On" if state.autoplay else "Off", inline=True)
        embed.add_field(name="24/7", value="On" if state.stay_247 else "Off", inline=True)

    try:
        await message.edit(embed=embed, view=MusicPanelView())
    except Exception:
        pass


async def maybe_autoplay(guild: discord.Guild):
    state = get_state(guild.id)
    if not state.autoplay or not state.current:
        return
    seed = state.current.title
    try:
        related = await extract_tracks(f"{seed} audio", requester_id=0)
        for candidate in related:
            if candidate.webpage_url and candidate.webpage_url != state.current.webpage_url:
                state.queue.append(candidate)
                return
    except Exception:
        return


async def start_next_song(guild: discord.Guild, announce: bool = True):
    state = get_state(guild.id)
    voice = guild.voice_client
    if not voice:
        return

    if state.loop_mode == "track" and state.current:
        next_track = state.current
    else:
        if state.loop_mode == "queue" and state.current:
            state.queue.append(state.current)
        if not state.queue:
            await maybe_autoplay(guild)
        if not state.queue:
            state.current = None
            state.persist()
            await update_panel(guild)
            if not state.stay_247 and voice.is_connected():
                try:
                    await voice.disconnect()
                except Exception:
                    pass
            return
        next_track = state.queue.popleft()
        state.current = next_track

    source = make_audio_source(next_track, state)

    def after_playback(error):
        if error:
            print(f"Playback error in guild {guild.id}: {error}")
        asyncio.run_coroutine_threadsafe(start_next_song(guild), bot.loop)

    voice.play(source, after=after_playback)

    if announce and state.bound_text_channel_id:
        channel = guild.get_channel(state.bound_text_channel_id)
        if channel:
            await safe_send(channel, embed=track_embed(state, next_track))
    state.persist()
    await update_panel(guild)


class MusicPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.blurple, custom_id="music_panel_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = interaction.guild.voice_client
        if not voice:
            return await interaction.response.send_message("Not connected.", ephemeral=True)
        if voice.is_playing():
            voice.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)
        elif voice.is_paused():
            voice.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.green, custom_id="music_panel_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = interaction.guild.voice_client
        if not voice or not (voice.is_playing() or voice.is_paused()):
            return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
        voice.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.gray, custom_id="music_panel_loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        state.loop_mode = {"off": "track", "track": "queue", "queue": "off"}[state.loop_mode]
        state.persist()
        await update_panel(interaction.guild)
        await interaction.response.send_message(f"Loop mode: `{state.loop_mode}`", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.gray, custom_id="music_panel_queue")
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        if not state.queue:
            return await interaction.response.send_message("Queue is empty.", ephemeral=True)
        lines = [f"{idx}. {track.title}" for idx, track in enumerate(list(state.queue)[:10], start=1)]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.red, custom_id="music_panel_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        state.queue.clear()
        state.current = None
        state.persist()
        voice = interaction.guild.voice_client
        if voice:
            await voice.disconnect()
        await update_panel(interaction.guild)
        await interaction.response.send_message("Stopped and disconnected.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    bot.add_view(MusicPanelView())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} application commands.")
    except Exception as exc:
        print(f"Command sync failed: {exc}")

    for guild in bot.guilds:
        state = get_state(guild.id)
        if state.stay_247 and state.bound_voice_channel_id:
            channel = guild.get_channel(state.bound_voice_channel_id)
            if isinstance(channel, discord.VoiceChannel):
                try:
                    if not guild.voice_client:
                        await channel.connect()
                except Exception as exc:
                    print(f"24/7 reconnect failed in guild {guild.id}: {exc}")
        await update_panel(guild)


@bot.event
async def on_voice_state_update(member, before, after):
    if not bot.user or member.id != bot.user.id:
        return
    state = get_state(member.guild.id)
    if state.stay_247 and before.channel and after.channel is None and state.bound_voice_channel_id:
        await asyncio.sleep(3)
        channel = member.guild.get_channel(state.bound_voice_channel_id)
        if isinstance(channel, discord.VoiceChannel):
            try:
                if not member.guild.voice_client:
                    await channel.connect()
            except Exception as exc:
                print(f"Auto-reconnect failed in guild {member.guild.id}: {exc}")


@bot.hybrid_command(description="Play a song or search term.")
async def play(ctx: commands.Context, *, query: str):
    await ctx.defer()
    try:
        voice = await ensure_voice(ctx)
        state = get_state(ctx.guild.id)
        tracks = await extract_tracks(query, ctx.author.id)
        for track in tracks:
            state.queue.append(track)
        state.bound_text_channel_id = ctx.channel.id
        state.persist()

        if len(tracks) == 1:
            await ctx.reply(f"Queued: **{tracks[0].title}**")
        else:
            await ctx.reply(f"Queued **{len(tracks)}** tracks.")

        if not voice.is_playing() and not voice.is_paused():
            await start_next_song(ctx.guild, announce=True)
        else:
            await update_panel(ctx.guild)
    except Exception as exc:
        await ctx.reply(f"❌ {exc}")


@bot.hybrid_command(description="Pause the current track.")
async def pause(ctx):
    voice = ctx.guild.voice_client
    if not voice or not voice.is_playing():
        return await ctx.reply("Nothing is playing.")
    voice.pause()
    await ctx.reply("Paused.")


@bot.hybrid_command(description="Resume the current track.")
async def resume(ctx):
    voice = ctx.guild.voice_client
    if not voice or not voice.is_paused():
        return await ctx.reply("Nothing is paused.")
    voice.resume()
    await ctx.reply("Resumed.")


@bot.hybrid_command(description="Skip the current track.")
async def skip(ctx):
    voice = ctx.guild.voice_client
    if not voice or not (voice.is_playing() or voice.is_paused()):
        return await ctx.reply("Nothing to skip.")
    voice.stop()
    await ctx.reply("Skipped.")


@bot.hybrid_command(description="Stop playback and clear the queue.")
async def stop(ctx):
    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None
    state.persist()
    voice = ctx.guild.voice_client
    if voice:
        await voice.disconnect()
    await update_panel(ctx.guild)
    await ctx.reply("Stopped and disconnected.")


@bot.hybrid_command(description="Show the current queue.")
async def queue(ctx):
    state = get_state(ctx.guild.id)
    if not state.current and not state.queue:
        return await ctx.reply("Queue is empty.")
    lines = []
    if state.current:
        lines.append(f"**Now:** {state.current.title}")
    for idx, track in enumerate(list(state.queue)[:15], start=1):
        lines.append(f"`{idx}` {track.title}")
    await ctx.reply("\n".join(lines))


@bot.hybrid_command(description="Show the currently playing track.")
async def nowplaying(ctx):
    state = get_state(ctx.guild.id)
    if not state.current:
        return await ctx.reply("Nothing is playing.")
    await ctx.reply(embed=track_embed(state, state.current))


@bot.hybrid_command(description="Set playback volume from 0 to 150.")
async def volume(ctx, percent: commands.Range[int, 0, 150]):
    state = get_state(ctx.guild.id)
    state.volume = percent / 100
    state.persist()
    voice = ctx.guild.voice_client
    if voice and voice.source:
        voice.source.volume = state.volume
    await update_panel(ctx.guild)
    await ctx.reply(f"Volume set to {percent}%.")


@bot.hybrid_command(description="Change loop mode: off, track, or queue.")
async def loop(ctx, mode: str):
    mode = mode.lower()
    if mode not in {"off", "track", "queue"}:
        return await ctx.reply("Use one of: off, track, queue")
    state = get_state(ctx.guild.id)
    state.loop_mode = mode
    state.persist()
    await update_panel(ctx.guild)
    await ctx.reply(f"Loop mode set to `{mode}`.")


@bot.hybrid_command(description="Toggle autoplay.")
async def autoplay(ctx):
    state = get_state(ctx.guild.id)
    state.autoplay = not state.autoplay
    state.persist()
    await update_panel(ctx.guild)
    await ctx.reply(f"Autoplay is now {'on' if state.autoplay else 'off'}.")


@bot.hybrid_command(description="Toggle 24/7 mode.")
async def mode247(ctx):
    state = get_state(ctx.guild.id)
    state.stay_247 = not state.stay_247
    if ctx.author.voice and ctx.author.voice.channel:
        state.bound_voice_channel_id = ctx.author.voice.channel.id
    state.bound_text_channel_id = ctx.channel.id
    state.persist()
    await update_panel(ctx.guild)
    await ctx.reply(f"24/7 mode is now {'on' if state.stay_247 else 'off'}.")


@bot.hybrid_command(description="Set a filter: off, bassboost, nightcore, vaporwave, karaoke.")
async def filter(ctx, name: str):
    name = name.lower()
    if name not in FILTER_MAP:
        return await ctx.reply(f"Available filters: {', '.join(FILTER_MAP)}")
    state = get_state(ctx.guild.id)
    state.filter_name = name
    state.persist()

    voice = ctx.guild.voice_client
    if voice and state.current and (voice.is_playing() or voice.is_paused()):
        position_note = "Filter saved. Use /skip or replay the song to hear it immediately."
    else:
        position_note = "Filter saved."
    await update_panel(ctx.guild)
    await ctx.reply(position_note)


@bot.hybrid_command(description="Post a persistent music control panel.")
async def panel(ctx):
    state = get_state(ctx.guild.id)
    state.bound_text_channel_id = ctx.channel.id
    embed = discord.Embed(title="Music Panel", description="Use the buttons below to control playback.")
    message = await ctx.reply(embed=embed, view=MusicPanelView())
    state.panel_message_id = message.id
    state.persist()


@bot.hybrid_command(description="Save the current queue as a playlist.")
async def saveplaylist(ctx, name: str):
    state = get_state(ctx.guild.id)
    tracks = []
    if state.current:
        tracks.append(asdict(state.current))
    tracks.extend(asdict(t) for t in state.queue)
    if not tracks:
        return await ctx.reply("There is nothing in the queue to save.")
    guild_map = playlists.setdefault(str(ctx.guild.id), {})
    guild_map[name] = tracks
    save_json(PLAYLISTS_FILE, playlists)
    await ctx.reply(f"Saved playlist `{name}` with {len(tracks)} tracks.")


@bot.hybrid_command(description="Load a saved playlist into the queue.")
async def loadplaylist(ctx, name: str):
    guild_map = playlists.get(str(ctx.guild.id), {})
    if name not in guild_map:
        return await ctx.reply("Playlist not found.")
    state = get_state(ctx.guild.id)
    for item in guild_map[name]:
        state.queue.append(Track(**item))
    state.bound_text_channel_id = ctx.channel.id
    state.persist()
    await ctx.reply(f"Loaded playlist `{name}` with {len(guild_map[name])} tracks.")

    try:
        voice = await ensure_voice(ctx)
        if not voice.is_playing() and not voice.is_paused():
            await start_next_song(ctx.guild, announce=True)
    except Exception:
        pass


@bot.hybrid_command(description="Delete a saved playlist.")
async def deleteplaylist(ctx, name: str):
    guild_map = playlists.get(str(ctx.guild.id), {})
    if name not in guild_map:
        return await ctx.reply("Playlist not found.")
    del guild_map[name]
    save_json(PLAYLISTS_FILE, playlists)
    await ctx.reply(f"Deleted playlist `{name}`.")


@bot.hybrid_command(description="List saved playlists.")
async def playlists_cmd(ctx):
    guild_map = playlists.get(str(ctx.guild.id), {})
    if not guild_map:
        return await ctx.reply("No saved playlists yet.")
    lines = [f"`{name}` - {len(items)} tracks" for name, items in guild_map.items()]
    await ctx.reply("\n".join(lines[:20]))


@bot.hybrid_command(description="Shuffle the queue.")
async def shuffle(ctx):
    state = get_state(ctx.guild.id)
    items = list(state.queue)
    if len(items) < 2:
        return await ctx.reply("Need at least 2 queued tracks to shuffle.")
    random.shuffle(items)
    state.queue = deque(items)
    await ctx.reply("Queue shuffled.")
    await update_panel(ctx.guild)


@bot.hybrid_command(description="Remove a queued track by position.")
async def remove(ctx, position: int):
    state = get_state(ctx.guild.id)
    items = list(state.queue)
    if position < 1 or position > len(items):
        return await ctx.reply("Invalid queue position.")
    removed = items.pop(position - 1)
    state.queue = deque(items)
    await ctx.reply(f"Removed **{removed.title}**.")
    await update_panel(ctx.guild)


@bot.hybrid_command(description="Clear the queue.")
async def clear(ctx):
    state = get_state(ctx.guild.id)
    state.queue.clear()
    await ctx.reply("Queue cleared.")
    await update_panel(ctx.guild)


@bot.hybrid_command(description="Show help for music commands.")
async def helpmusic(ctx):
    commands_text = (
        "`/play` `/pause` `/resume` `/skip` `/stop` `/queue` `/nowplaying`\n"
        "`/volume` `/loop` `/autoplay` `/mode247` `/filter` `/panel`\n"
        "`/saveplaylist` `/loadplaylist` `/deleteplaylist` `/playlists_cmd` `/shuffle` `/remove` `/clear`"
    )
    await ctx.reply(commands_text)


bot.run(TOKEN)
