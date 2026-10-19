#!/usr/bin/env python3
"""moonfall_server.py -- the moderator process for Moonfall (a Werewolves-like game).
All networking goes through wolfnet.py.  The server is authoritative: every
action is validated here, never trusted from the client."""
import argparse
import asyncio
import random
from collections import Counter

from wolfnet import Server

DEFAULT_SOCKET = "/tmp/moonfall/game.sock"


def ev(kind, text, **extra):
    return {"type": "event", "kind": kind, "text": text, **extra}


class Game:
    def __init__(self, a):
        self.srv = Server(a.socket, self.on_join, self.on_message, self.on_leave, dev=a.dev)
        self.min_players, self.night_secs, self.day_secs = a.min_players, a.night, a.day
        self.players = {}                  # name -> {"role":..., "alive":...}
        self.phase, self.round = "lobby", 0
        self.start_evt, self.phase_done = asyncio.Event(), asyncio.Event()
        self.last_saved = None
        self.reset_actions()

    # ---------------- helpers ----------------
    def reset_actions(self):
        self.wolf_votes, self.day_votes = {}, {}
        self.seer_target = self.doctor_target = None

    def alive(self, role=None):
        return [n for n, p in self.players.items()
                if p["alive"] and (role is None or p["role"] == role)]

    def is_alive(self, n):
        return n in self.players and self.players[n]["alive"]

    def role(self, n):
        return self.players[n]["role"]

    def kill(self, name, announcement):
        self.players[name]["alive"] = False
        self.srv.leave_group(name, "wolves")
        self.srv.leave_group(name, "village")
        self.srv.join_group(name, "ghosts")
        self.srv.broadcast(ev("death", announcement, victim=name, alive=self.alive()))
        self.srv.send(name, ev("system", "You are dead. You can still watch, and chat with other ghosts."))

    # ---------------- wolfnet callbacks ----------------
    def on_join(self, name):
        if self.phase != "lobby" or name in self.players:
            self.srv.join_group(name, "ghosts")
            self.srv.send(name, ev("system", "A game is in progress; you are a spectator (ghost chat only)."))
            return
        self.players[name] = {"role": None, "alive": True}
        self.srv.join_group(name, "village")
        self.srv.broadcast(ev("system", f"{name} entered the village. {len(self.players)} present, "
                                        f"need {self.min_players}. Anyone can type /start."))

    def on_leave(self, name):
        p = self.players.get(name)
        if not p:
            return
        if self.phase == "lobby":
            del self.players[name]
            self.srv.broadcast(ev("system", f"{name} left the lobby."))
        elif p["alive"] and self.phase != "over":
            self.kill(name, f"{name} fled the village (disconnected). They were a {p['role']}.")
            self.check_phase_done()

    def on_message(self, name, msg):
        t = msg["type"]
        text = str(msg.get("text", ""))[:500]
        if t == "chat":
            self.chat(name, text)
        elif t == "whisper":
            self.whisper(name, msg.get("to"), text)
        elif t == "act":
            self.act(name, msg.get("verb"), msg.get("target"))
        elif t == "start":
            self.try_start(name)
        elif t == "who":
            self.who(name)
        else:
            self.srv.send(name, ev("error", f"unknown message type {t!r}"))

    # ---------------- communication routing ----------------
    def chat(self, name, text):
        if not text:
            return
        if self.phase not in ("lobby",) and not self.is_alive(name):
            self.srv.multicast("ghosts", ev("ghost", text, sender=name))            # MULTICAST
        elif self.phase == "night":
            if self.role(name) == "werewolf":
                self.srv.multicast("wolves", ev("wolf", text, sender=name))         # MULTICAST
            else:
                self.srv.send(name, ev("error", "It's night. You're asleep -- only werewolves may speak."))
        else:
            self.srv.broadcast(ev("chat", text, sender=name))                       # BROADCAST

    def whisper(self, name, to, text):
        if self.phase != "day" or not self.is_alive(name):
            return self.srv.send(name, ev("error", "Whispers are only allowed by living players during the day."))
        if not self.is_alive(to) or to == name:
            return self.srv.send(name, ev("error", f"Can't whisper to {to!r}."))
        self.srv.send(to, ev("whisper", text, sender=name))                         # UNICAST
        self.srv.send(name, ev("whisper", f"(to {to}) {text}", sender=name))
        # everyone sees THAT a whisper happened -- a strategic tell
        self.srv.broadcast(ev("system", f"{name} whispered something to {to}."), exclude=(name, to))

    def who(self, name):
        alive = ", ".join(self.alive()) or "-"
        dead = ", ".join(n for n, p in self.players.items() if not p["alive"]) or "-"
        self.srv.send(name, ev("system", f"Phase: {self.phase} {self.round}. Alive: {alive}. Dead: {dead}."))

    # ---------------- actions ----------------
    def act(self, name, verb, target):
        err = lambda s: self.srv.send(name, ev("error", s))
        if not self.is_alive(name):
            return err("The dead can't act.")
        r = self.role(name)
        # Check role + phase BEFORE the target, so a misused command gets the
        # most useful error (classmate feedback: Khushi Sharma).
        rules = {"vote": (None, "day", "You can only vote during the day."),
                 "kill": ("werewolf", "night", "Only werewolves can /kill, and only at night."),
                 "see": ("seer", "night", "Only the seer can /see, and only at night."),
                 "save": ("doctor", "night", "Only the doctor can /save, and only at night.")}
        if verb not in rules:
            return err(f"Unknown action {verb!r}.")
        need_role, need_phase, why = rules[verb]
        if self.phase != need_phase or (need_role and r != need_role):
            return err(why)
        if not self.is_alive(target):
            return err(f"{target!r} is not a living player.")
        if verb == "vote":
            if self.phase != "day":
                return err("You can only vote during the day.")
            self.day_votes[name] = target
            tally = Counter(self.day_votes.values())
            self.srv.broadcast(ev("vote", f"{name} votes to banish {target}. "
                                          f"Tally: {dict(tally)}", voter=name, target=target))
        elif verb == "kill":
            if self.phase != "night" or r != "werewolf":
                return err("Only werewolves can /kill, and only at night.")
            if self.role(target) == "werewolf":
                return err("You can't attack a fellow wolf.")
            self.wolf_votes[name] = target
            self.srv.multicast("wolves", ev("wolf", f"{name} wants to devour {target}.", sender="pack"))
        elif verb == "see":
            if self.phase != "night" or r != "seer":
                return err("Only the seer can /see, and only at night.")
            if self.seer_target:
                return err("You already used your vision tonight.")
            if target == name:
                return err("You already know yourself.")
            self.seer_target = target
            verdict = "a WEREWOLF" if self.role(target) == "werewolf" else "not a werewolf"
            self.srv.send(name, ev("private", f"Your vision reveals: {target} is {verdict}."))  # UNICAST
        elif verb == "save":
            if self.phase != "night" or r != "doctor":
                return err("Only the doctor can /save, and only at night.")
            if target == self.last_saved:
                return err("You can't protect the same person two nights in a row.")
            self.doctor_target = target
            self.srv.send(name, ev("private", f"You will protect {target} tonight."))
        else:
            return err(f"Unknown action {verb!r}.")
        self.check_phase_done()

    def check_phase_done(self):
        if self.phase == "night":
            done = all(w in self.wolf_votes for w in self.alive("werewolf"))
            done &= not self.alive("seer") or self.seer_target is not None
            done &= not self.alive("doctor") or self.doctor_target is not None
        elif self.phase == "day":
            done = all(n in self.day_votes for n in self.alive())
        else:
            return
        if done:
            self.phase_done.set()

    # ---------------- game flow ----------------
    def try_start(self, name):
        if self.phase != "lobby":
            return self.srv.send(name, ev("error", "The game has already started."))
        if len(self.players) < self.min_players:
            return self.srv.send(name, ev("error", f"Need at least {self.min_players} players."))
        self.phase = "starting"
        self.start_evt.set()

    def assign_roles(self):
        names = list(self.players)
        random.shuffle(names)
        n_wolves = max(1, len(names) // 4)
        roles = ["werewolf"] * n_wolves + ["seer"] + (["doctor"] if len(names) >= 5 else [])
        roles += ["villager"] * (len(names) - len(roles))
        wolves = names[:n_wolves]
        for n, r in zip(names, roles):
            self.players[n]["role"] = r
            extra = {"pack": wolves} if r == "werewolf" else {}
            self.srv.send(n, ev("role", f"Your secret role is: {r.upper()}.", role=r, **extra))  # UNICAST
        for w in wolves:
            self.srv.join_group(w, "wolves")
        self.srv.multicast("wolves", ev("wolf", f"Your pack: {', '.join(wolves)}.", sender="pack"))
        counts = Counter(roles)
        self.srv.broadcast(ev("system", "Roles dealt: " + ", ".join(f"{v} {k}" for k, v in counts.items())))

    async def _countdown(self, secs):
        for remaining in (30, 10):
            if secs > remaining:
                await asyncio.sleep(secs - remaining)
                secs = remaining
                self.srv.broadcast(ev("system", f"⏳ {remaining} seconds left."))

    async def wait_phase(self, secs):
        self.phase_done.clear()
        self.check_phase_done()
        timer = asyncio.create_task(self._countdown(secs))   # runs concurrently with the phase
        try:
            await asyncio.wait_for(self.phase_done.wait(), secs)
        except asyncio.TimeoutError:
            self.srv.broadcast(ev("system", "⏰ Time is up."))
        finally:
            timer.cancel()

    async def night(self):
        self.phase = "night"
        self.reset_actions()
        self.srv.broadcast(ev("phase", f"🌙 Night {self.round} falls. Close your eyes...",
                              phase="night", alive=self.alive()))
        self.srv.multicast("wolves", ev("wolf", "Wolves: discuss here, then /kill <name>.", sender="pack"))
        for s in self.alive("seer"):
            self.srv.send(s, ev("private", "Seer: /see <name> to learn if they are a werewolf."))
        for d in self.alive("doctor"):
            self.srv.send(d, ev("private", "Doctor: /save <name> to protect someone tonight."))
        await self.wait_phase(self.night_secs)

        self.phase = "resolving"
        victim = None
        if self.wolf_votes:
            tally = Counter(t for w, t in self.wolf_votes.items() if self.is_alive(w) and self.is_alive(t))
            if tally:
                best = max(tally.values())
                victim = random.choice([t for t, c in tally.items() if c == best])
        self.last_saved = self.doctor_target
        if victim and victim == self.doctor_target:
            self.srv.broadcast(ev("system", "☀ Dawn. The wolves attacked, but the doctor saved their target!"))
        elif victim:
            self.kill(victim, f"☀ Dawn. {victim} was found dead. They were a {self.role(victim)}.")
        else:
            self.srv.broadcast(ev("system", "☀ Dawn. A quiet night -- nobody died."))

    async def day(self):
        self.phase = "day"
        self.day_votes = {}
        self.srv.broadcast(ev("phase", f"☀ Day {self.round}. Discuss, /w to whisper, then /vote <name>.",
                              phase="day", alive=self.alive()))
        await self.wait_phase(self.day_secs)
        self.phase = "resolving"
        tally = Counter(t for v, t in self.day_votes.items() if self.is_alive(v) and self.is_alive(t))
        if not tally:
            return self.srv.broadcast(ev("system", "No votes were cast. Nobody is banished."))
        best = max(tally.values())
        leaders = [t for t, c in tally.items() if c == best]
        if len(leaders) > 1:
            self.srv.broadcast(ev("system", f"Tie between {', '.join(leaders)}. Nobody is banished."))
        else:
            x = leaders[0]
            self.kill(x, f"The village banishes {x} ({best} votes). They were a {self.role(x)}.")

    def check_win(self):
        w = len(self.alive("werewolf"))
        others = len(self.alive()) - w
        if w == 0:
            winner = "🏡 The VILLAGE wins -- every werewolf is gone!"
        elif w >= others:
            winner = "🐺 The WEREWOLVES win -- they outnumber the village!"
        else:
            return False
        self.phase = "over"
        reveal = "; ".join(f"{n}={p['role']}" for n, p in self.players.items())
        self.srv.broadcast(ev("gameover", f"{winner}  Roles: {reveal}"))
        return True

    async def run(self):
        await self.srv.start()
        print(f"[moonfall] listening on {self.srv.path} (dev={self.srv.dev})")
        await self.start_evt.wait()
        self.assign_roles()
        while True:
            self.round += 1
            await self.night()
            if self.check_win():
                break
            await self.day()
            if self.check_win():
                break
        await asyncio.sleep(1)
        await self.srv.close()
        print("[moonfall] game over")


def main():
    ap = argparse.ArgumentParser(description="Moonfall game server")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--min-players", type=int, default=4)
    ap.add_argument("--night", type=int, default=60, help="night length (s)")
    ap.add_argument("--day", type=int, default=120, help="day length (s)")
    ap.add_argument("--dev", action="store_true", help="allow nicknames (single-user testing)")
    asyncio.run(Game(ap.parse_args()).run())


if __name__ == "__main__":
    main()
