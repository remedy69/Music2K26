import asyncio
import os
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands
import yt_dlp

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("COMMAND_PREFIX", "!").strip() or "!"
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


@dataclass
class QueuedTrack:
    title: str
    webpage_url: str
    requested_by: str


class GuildState:
    def __init__(self) -> None:
        self.queue: deque[QueuedTrack] = deque()
        self.current: Optional[QueuedTrack] = None
        self.loop: bool = False
        self.volume: float = 0.5
        self.cookies_file: Optional[str] = None


states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in states:
        states[guild_id] = GuildState()
    return states[guild_id]


YDL_OPTIONS = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

if YOUTUBE_COOKIES:
    fd, cookie_path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(YOUTUBE_COOKIES.replace("\\n", "\n"))
    YDL_OPTIONS["cookiefile"] = cookie_path

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def make_ydl() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL(YDL_OPTIONS)


async def extract_info(query: str) -> dict:
    def _extract() -> dict:
        with make_ydl() as ydl:
            data = ydl.extract_info(query, download=False)
            if "entries" in data and data["entries"]:
                data = next((entry for entry in data["entries"] if entry), None)
            if not data:
                raise RuntimeError("No results found.")
            return data

    return await asyncio.to_thread(_extract)


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("Join a voice channel first.")

    vc = ctx.guild.voice_client
    if vc is None:
        return await ctx.author.voice.channel.connect()
    if vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)
    return vc


async def play_next(ctx: commands.Context) -> None:
    state = get_state(ctx.guild.id)
    vc = ctx.guild.voice_client

    if vc is None:
        state.current = None
        return

    if state.loop and state.current is not None:
        next_track = state.current
    elif state.queue:
        next_track = state.queue.popleft()
    else:
        state.current = None
        await vc.disconnect()
        return

    state.current = next_track

    try:
        info = await extract_info(next_track.webpage_url)
        stream_url = info.get("url")
        title = info.get("title") or next_track.title
        if not stream_url:
            raise RuntimeError("Could not get a playable stream URL.")

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS),
            volume=state.volume,
        )

        def _after_play(error: Optional[Exception]) -> None:
            if error:
                print(f"Playback error: {error}")
            future = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
            try:
                future.result()
            except Exception as exc:
                print(f"Queue continuation error: {exc}")

        vc.play(source, after=_after_play)
        await ctx.send(f"🎶 Now playing: **{title}**")
    except Exception as exc:
        await ctx.send(f"❌ Audio error: `{exc}`")
        await play_next(ctx)


@bot.command(name="play")
async def play(ctx: commands.Context, *, query: str) -> None:
    state = get_state(ctx.guild.id)
    try:
        vc = await ensure_voice(ctx)
        info = await extract_info(query)
        track = QueuedTrack(
            title=info.get("title") or query,
            webpage_url=info.get("webpage_url") or query,
            requested_by=str(ctx.author),
        )
        state.queue.append(track)
        await ctx.send(f"✅ Added: **{track.title}**")
        if not vc.is_playing() and not vc.is_paused() and state.current is None:
            await play_next(ctx)
    except Exception as exc:
        await ctx.send(f"❌ {exc}")


@bot.command(name="skip")
async def skip(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("⏭ Skipped.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="stop")
async def stop(ctx: commands.Context) -> None:
    state = get_state(ctx.guild.id)
    state.queue.clear()
    state.current = None
    vc = ctx.guild.voice_client
    if vc:
        await vc.disconnect()
    await ctx.send("⏹ Stopped and disconnected.")


@bot.command(name="pause")
async def pause(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸ Paused.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="resume")
async def resume(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("Nothing is paused.")


@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context) -> None:
    state = get_state(ctx.guild.id)
    lines = []
    if state.current:
        lines.append(f"**Now:** {state.current.title}")
    for index, item in enumerate(list(state.queue)[:10], start=1):
        lines.append(f"`{index}.` {item.title}")
    if not lines:
        await ctx.send("Queue is empty.")
        return
    await ctx.send("\n".join(lines))


@bot.command(name="loop")
async def loop_cmd(ctx: commands.Context, mode: str) -> None:
    mode = mode.lower().strip()
    if mode not in {"on", "off"}:
        await ctx.send("Use `!loop on` or `!loop off`.")
        return
    state = get_state(ctx.guild.id)
    state.loop = mode == "on"
    await ctx.send(f"🔁 Loop {'enabled' if state.loop else 'disabled' }.")


@bot.command(name="volume")
async def volume(ctx: commands.Context, amount: int) -> None:
    if amount < 0 or amount > 200:
        await ctx.send("Volume must be between 0 and 200.")
        return
    state = get_state(ctx.guild.id)
    state.volume = amount / 100.0
    vc = ctx.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume
    await ctx.send(f"🔊 Volume set to {amount}%.")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "Commands: `!play <url or search>`, `!pause`, `!resume`, `!skip`, `!stop`, `!queue`, `!loop on|off`, `!volume <0-200>`"
    )


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
