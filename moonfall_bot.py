#!/usr/bin/env python3
"""moonfall_bot.py -- a simple AI player (random but legal moves). Uses wolfnet.Client."""
import argparse
import asyncio
import random

from wolfnet import Client


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/tmp/moonfall/game.sock")
    ap.add_argument("--nick")
    ap.add_argument("--start", action="store_true", help="send /start after joining")
    ap.add_argument("--start-after", type=int, default=4, help="players needed before /start")
    a = ap.parse_args()

    c = Client(a.socket)
    me = await c.connect(a.nick)
    role, pack, seen = None, [], set()

    async def act_later(verb, choices):
        await asyncio.sleep(random.uniform(0.5, 2.0))
        if choices:
            await c.send("act", verb=verb, target=random.choice(choices))

    joined = 0
    while (m := await c.recv()) is not None:
        if m["type"] == "bye":
            break
        kind = m.get("kind")
        if kind == "system" and "entered the village" in m["text"]:
            joined += 1
            if a.start and joined >= a.start_after:
                await c.send("start")
        elif kind == "role":
            role, pack = m["role"], m.get("pack", [])
        elif kind == "phase":
            others = [p for p in m["alive"] if p != me]
            if me not in m["alive"]:
                continue
            if m["phase"] == "night":
                if role == "werewolf":
                    asyncio.create_task(act_later("kill", [p for p in others if p not in pack]))
                elif role == "seer":
                    asyncio.create_task(act_later("see", [p for p in others if p not in seen]))
                elif role == "doctor":
                    asyncio.create_task(act_later("save", m["alive"]))
            else:
                await c.send("chat", text=random.choice(
                    ["I'm just a humble villager.", "Something smells off...", "I trust nobody."]))
                choices = [p for p in others if p not in pack] if role == "werewolf" else others
                asyncio.create_task(act_later("vote", choices))
        elif kind == "error" and "protect the same" in m.get("text", ""):
            pass  # bot will just skip; the phase timer handles it
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
