#!/usr/bin/env python3
"""moonfall_client.py -- interactive player terminal. Uses wolfnet.Client only."""
import argparse
import asyncio
import sys

from wolfnet import Client

C = {"system": "\033[36m", "chat": "\033[0m", "wolf": "\033[31m", "ghost": "\033[90m",
     "whisper": "\033[35m", "private": "\033[33m", "role": "\033[1;33m", "phase": "\033[1;34m",
     "death": "\033[1;31m", "vote": "\033[32m", "gameover": "\033[1;32m", "error": "\033[91m"}
RST = "\033[0m"
HELP = """Commands:
  <text>            talk (day: everyone, night: wolf pack only, dead: ghosts only)
  /w <name> <text>  whisper privately (day only; others see that you whispered)
  /vote <name>      vote to banish (day)
  /kill <name>      werewolf night attack     /see <name>  seer vision
  /save <name>      doctor protection         /who         list players
  /start            start the game (lobby)    /quit        leave"""


def render(m):
    kind = m.get("kind", m["type"])
    sender = m.get("sender")
    prefix = {"wolf": "[wolves] ", "ghost": "[ghosts] ", "whisper": "[whisper] "}.get(kind, "")
    who = f"{sender}: " if sender and kind in ("chat", "wolf", "ghost", "whisper") else ""
    print(f"\r{C.get(kind, '')}{prefix}{who}{m.get('text', '')}{RST}")
    print("> ", end="", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/tmp/moonfall/game.sock")
    ap.add_argument("--nick", help="nickname (only honoured if server runs --dev)")
    a = ap.parse_args()

    c = Client(a.socket)
    try:
        name = await c.connect(a.nick)
    except (OSError, ConnectionError) as e:
        sys.exit(f"Could not join: {e}")
    print(f"Joined Moonfall as {name}. Type /help for commands.")

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()
    loop.add_reader(sys.stdin.fileno(), lambda: lines.put_nowait(sys.stdin.readline()))

    async def receiver():                    # concurrent task 1: network -> screen
        while (m := await c.recv()) is not None:
            if m["type"] == "bye":
                break
            render(m)
        print("\nServer closed the connection.")

    async def sender():                      # concurrent task 2: keyboard -> network
        while True:
            line = await lines.get()
            if line == "":
                return
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()
            if cmd == "/help":
                print(HELP)
            elif cmd == "/quit":
                return
            elif cmd in ("/start", "/who"):
                await c.send(cmd[1:])
            elif cmd in ("/vote", "/kill", "/see", "/save") and len(parts) >= 2:
                await c.send("act", verb=cmd[1:], target=parts[1])
            elif cmd == "/w" and len(parts) == 3:
                await c.send("whisper", to=parts[1], text=parts[2])
            elif cmd.startswith("/"):
                print("Bad command. /help for help.")
            else:
                await c.send("chat", text=line)

    tasks = [asyncio.create_task(receiver()), asyncio.create_task(sender())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    loop.remove_reader(sys.stdin.fileno())
    await c.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
