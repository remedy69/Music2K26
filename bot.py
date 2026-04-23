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

# --- THE FIX: Aggressive Format Fallback & Mobile Spoofing ---
YDL_OPTIONS = {
    "format": "ba/b",
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
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
    "vaporwave": "asetrate=48000*0.8,aresample=48000,atempo=1.2",
}

@dataclass
class Song:
    title: str
    url: str
    duration: int

class MusicPlayer:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue = deque()
        self.current = None
        self.volume = 100
        self.filter_name = "off"
        self.loop_mode = "off" # off, track

players = {}

def get_player(guild_id: int) -> MusicPlayer:
    if guild_id not in players:
        players[guild_id] = MusicPlayer(guild_id)
    return players[guild_id]

async def play_next(ctx: commands.Context):
    player = get_player(ctx.guild.id)
    if player.loop_mode == "track" and player.current:
        song = player.current
    elif player.queue:
        song = player.queue.popleft()
        player.current = song
    else:
        player.current = None
        return

    vc = ctx.guild.voice_client
    if not vc: return

    # Clear old source if it exists
    if vc.is_playing():
        vc.stop()

    ytdl = yt_dlp.YoutubeDL(YDL_OPTIONS)
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(song.url, download=False))
    
    stream_url = data['url']
    
    ffmpeg_opts = FFMPEG_OPTIONS
    if player.filter_name != "off":
        ffmpeg_opts += f' -af "{FILTERS[player.filter_name]}"'

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(stream_url, before_options=FFMPEG_BEFORE_OPTIONS, options=ffmpeg_opts),
        volume=player.volume / 100.0
    )

    def after_playing(error):
        coro = play_next(ctx)
        fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
        try:
            fut.result()
        except:
            pass

    vc.play(source, after=after_playing)
    await ctx.send(f"🎶 Now playing: **{song.title}**")

@bot.command(name="play")
async def play(ctx: commands.Context, *, search: str):
    if not ctx.author.voice:
        return await ctx.send("Join a voice channel first!")

    if not ctx.guild.voice_client:
        await ctx.author.voice.channel.connect()

    await ctx.send(f"🔍 Searching for `{search}`...")
    
    ytdl = yt_dlp.YoutubeDL(YDL_OPTIONS)
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=False))

    if 'entries' in data:
        data = data['entries'][0]

    song = Song(title=data['title'], url=data['webpage_url'], duration=data.get('duration', 0))
    player = get_player(ctx.guild.id)
    
    vc = ctx.guild.voice_client
    if vc.is_playing() or vc.is_paused():
        player.queue.append(song)
        await ctx.send(f"✅ Added to queue: **{song.title}**")
    else:
        player.queue.append(song)
        await play_next(ctx)

@bot.command(name="skip")
async def skip(ctx: commands.Context):
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        ctx.guild.voice_client.stop()
        await ctx.send("⏭ Skipped!")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)