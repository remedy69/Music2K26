import asyncio
import os
import random
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands
import yt_dlp

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("COMMAND_PREFIX", "!").strip() or "!"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

FILTERS = {
    "off": "",
    "bassboost": "bass=g=8",
    "nightcore": "asetrate=48000*1.25,aresample=48000,atempo=1.1",
    "vaporwave": "asetrate=48000*0.8,aresample=48000,atempo=1.0",
    "karaoke": "pan=stereo|c0=c0-c1|c1=c1-c0",
}


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requested_by: str


class GuildPlayer:
    def __init__(self) -> None:
        self.queue: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume: int = 100
        self.loop_mode: str = "off"  # off / track / queue
        self.filter_name: str = "off"


players: dict[int, GuildPlayer] = {}


def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]


async def extract_track(query: str, requested_by: str) -> Track:
    def _extract() -> Track:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)

            if "entries" in info and info["entries"]:
                info = next((e for e in info["entries"] if e), None)

            if not info:
                raise RuntimeError("No results found.")

            stream_url = info.get("url")
            title = info.get("title") or "Unknown title"
            webpage_url = info.get("webpage_url") or query

            if not stream_url:
                raise RuntimeError("Could not get a playable stream URL.")

            return Track(
                title=title,
                webpage_url=webpage_url,
                stream_url=stream_url,
                requested_by=requested_by,
            )

    return await asyncio.to_thread(_extract)


def build_source(stream_url: str, volume: int, filter_name: str) -> discord.PCMVolumeTransformer:
    audio_filter = FILTERS.get(filter_name, "")
    options = FFMPEG_OPTIONS
    if audio_filter:
        options += f' -af "{audio_filter}"'

    source = discord.FFmpegPCMAudio(
        stream_url,
        before_options=FFMPEG_BEFORE_OPTIONS,
        options=options,
    )
    return discord.PCMVolumeTransformer(source, volume=max(0.0, min(volume / 100.0, 2.0)))


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("Join a voice channel first.")

    vc = ctx.guild.voice_client
    if vc is None:
        return await ctx.author.voice.channel.connect()

    if vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)

    return vc


async def start_next(guild: discord.Guild, text_channel: discord.TextChannel | discord.Thread | None = None) -> None:
    player = get_player(guild.id)
    vc = guild.voice_client

    if vc is None:
        player.current = None
        return

    next_track: Optional[Track] = None

    if player.loop_mode == "track" and player.current is not None:
        next_track = player.current
    elif player.queue:
        if player.loop_mode == "queue" and player.current is not None:
            player.queue.append(player.current)
        next_track = player.queue.popleft()

    if next_track is None:
        player.current = None
        try:
            await vc.disconnect()
        except Exception:
            pass
        return

    player.current = next_track
    source = build_source(next_track.stream_url, player.volume, player.filter_name)

    def after_play(error: Optional[Exception]) -> None:
        if error:
            print(f"Playback error: {error}")
        fut = asyncio.run_coroutine_threadsafe(start_next(guild, text_channel), bot.loop)
        try:
            fut.result()
        except Exception as exc:
            print(f"Queue continuation error: {exc}")

    vc.play(source, after=after_play)

    if text_channel:
        try:
            await text_channel.send(
                f"🎶 **Now playing:** {next_track.title}\nRequested by: {next_track.requested_by}"
            )
        except discord.HTTPException:
            pass


@bot.event
async def on_ready() -> None:
    print(f"Bot ready as {bot.user} ({bot.user.id})")


@bot.command(name="play")
async def play_cmd(ctx: commands.Context, *, query: str) -> None:
    try:
        vc = await ensure_voice(ctx)
        track = await extract_track(query, str(ctx.author))
        player = get_player(ctx.guild.id)
        player.queue.append(track)

        if vc.is_playing() or vc.is_paused():
            await ctx.send(f"➕ Added to queue: **{track.title}**")
        else:
            await start_next(ctx.guild, ctx.channel)
    except Exception as exc:
        await ctx.send(f"❌ {exc}")


@bot.command(name="pause")
async def pause_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸ Paused.")
    else:
        await ctx.send("Nothing is playing.")


@bot.command(name="resume")
async def resume_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("Nothing is paused.")


@bot.command(name="skip")
async def skip_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("⏭ Skipped.")
    else:
        await ctx.send("Nothing to skip.")


@bot.command(name="stop")
async def stop_cmd(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client
    player = get_player(ctx.guild.id)
    player.queue.clear()
    player.current = None
    if vc:
        await vc.disconnect()
        await ctx.send("⏹ Stopped and disconnected.")
    else:
        await ctx.send("Not connected.")


@bot.command(name="queue")
async def queue_cmd(ctx: commands.Context) -> None:
    player = get_player(ctx.guild.id)
    lines = []

    if player.current:
        lines.append(f"**Now:** {player.current.title}")

    if player.queue:
        for idx, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"`{idx}.` {track.title}")

    if not lines:
        await ctx.send("Queue is empty.")
        return

    await ctx.send("\n".join(lines))


@bot.command(name="loop")
async def loop_cmd(ctx: commands.Context, mode: str) -> None:
    mode = mode.lower().strip()
    if mode not in {"off", "track", "queue"}:
        await ctx.send("Use: `off`, `track`, or `queue`.")
        return

    player = get_player(ctx.guild.id)
    player.loop_mode = mode
    await ctx.send(f"🔁 Loop mode set to **{mode}**.")


@bot.command(name="volume")
async def volume_cmd(ctx: commands.Context, amount: int) -> None:
    if amount < 0 or amount > 200:
        await ctx.send("Volume must be between 0 and 200.")
        return

    player = get_player(ctx.guild.id)
    player.volume = amount

    vc = ctx.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = max(0.0, min(amount / 100.0, 2.0))

    await ctx.send(f"🔊 Volume set to **{amount}%**.")


@bot.command(name="filter")
async def filter_cmd(ctx: commands.Context, name: str) -> None:
    name = name.lower().strip()
    if name not in FILTERS:
        await ctx.send(f"Available filters: {', '.join(FILTERS.keys())}")
        return

    player = get_player(ctx.guild.id)
    player.filter_name = name
    vc = ctx.guild.voice_client

    if vc and player.current and (vc.is_playing() or vc.is_paused()):
        current = player.current
        vc.stop()
        player.current = current
        if player.loop_mode != "track":
            player.queue.appendleft(current)

    await ctx.send(f"🎛 Filter set to **{name}**.")


@bot.command(name="helpmusic")
async def helpmusic_cmd(ctx: commands.Context) -> None:
    await ctx.send(
        "**Music commands**\n"
        "`!play <url or search>`\n"
        "`!pause`\n"
        "`!resume`\n"
        "`!skip`\n"
        "`!stop`\n"
        "`!queue`\n"
        "`!loop off|track|queue`\n"
        "`!volume 0-200`\n"
        "`!filter off|bassboost|nightcore|vaporwave|karaoke`"
    )


bot.run(TOKEN)
