"""
wolfnet.py -- the ONE-FILE communications library for Moonfall.

Primitive: UNIX domain STREAM sockets driven by asyncio (asynchronous,
non-blocking I/O on a single event loop).

Wire format: every message is a "frame" = 4-byte big-endian length + UTF-8 JSON
object that must contain a string field "type".  Frames larger than MAX_FRAME
are rejected (protects against memory-exhaustion attacks).

Security / robustness features:
  * Identity comes from the kernel (SO_PEERCRED): the server learns the real
    UID/PID of each connecting process, so player names = Linux usernames and
    cannot be spoofed.  (A --dev mode allows nicknames for single-user testing.)
  * The server stamps "sender" onto every message it routes; clients can't forge it.
  * Socket file is created with mode 0660 (owner + game group only).
  * Each peer must say hello within HELLO_TIMEOUT seconds or is dropped.
  * Token-bucket rate limiting per peer; abusive peers are disconnected.
  * Each peer has a bounded outgoing queue + its own writer task, so one slow
    or stuck client can never block the server or the other players.

Server API
    srv = Server(path, on_join, on_message, on_leave, dev=False)
    await srv.start() / await srv.close()
    srv.send(name, msg)                  # unicast
    srv.multicast(group, msg, exclude=())# to all members of a named group
    srv.broadcast(msg, exclude=())       # to every connected peer
    srv.join_group(name, group) / srv.leave_group(name, group)
    srv.members(group) -> set[str]
    srv.kick(name, reason)
  Callbacks (plain functions or coroutines):
    on_join(name), on_message(name, msg_dict), on_leave(name)

Client API
    c = Client(path)
    name = await c.connect(nick=None)    # returns the server-assigned name
    await c.send(type, **fields)
    msg = await c.recv()                 # dict, or None when server closes
    await c.close()
"""
import asyncio
import json
import os
import pwd
import re
import socket
import struct
import time

MAX_FRAME = 64 * 1024
HELLO_TIMEOUT = 10.0
SEND_QUEUE_MAX = 256
_HDR = struct.Struct("!I")
_NICK_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")


class ProtocolError(Exception):
    pass


def encode(msg: dict) -> bytes:
    data = json.dumps(msg, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise ProtocolError("message too large")
    return _HDR.pack(len(data)) + data


async def read_frame(reader: asyncio.StreamReader) -> dict:
    (n,) = _HDR.unpack(await reader.readexactly(_HDR.size))
    if n == 0 or n > MAX_FRAME:
        raise ProtocolError(f"bad frame length {n}")
    raw = await reader.readexactly(n)
    try:
        msg = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError("invalid JSON") from e
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("message must be an object with a 'type'")
    return msg


def peer_credentials(sock: socket.socket):
    """Ask the kernel who is on the other end: returns (pid, uid, gid)."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


async def _maybe_await(fn, *args):
    if fn is None:
        return
    res = fn(*args)
    if asyncio.iscoroutine(res):
        await res


class _Peer:
    def __init__(self, name, uid, pid, writer, rate, burst):
        self.name, self.uid, self.pid, self.writer = name, uid, pid, writer
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self.rate, self.tokens, self.last = rate, float(burst), time.monotonic()
        self.burst, self.strikes, self.closed = burst, 0, False
        self.task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self):
        try:
            while True:
                data = await self.queue.get()
                if data is None:
                    break
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.close()

    def send(self, msg: dict):
        if self.closed:
            return
        try:
            self.queue.put_nowait(encode(msg))
        except asyncio.QueueFull:          # client isn't reading: cut it loose
            self.close()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def close(self):
        if not self.closed:
            self.closed = True
            self.writer.close()


class Server:
    def __init__(self, path, on_join=None, on_message=None, on_leave=None, *,
                 dev=False, socket_mode=0o660, rate=10.0, burst=20):
        self.path, self.dev, self.socket_mode = path, dev, socket_mode
        self.on_join, self.on_message, self.on_leave = on_join, on_message, on_leave
        self.rate, self.burst = rate, burst
        self.peers: dict[str, _Peer] = {}
        self.groups: dict[str, set] = {}
        self._srv = None

    # ---------- lifecycle ----------
    async def start(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if os.path.exists(self.path):       # stale socket from a crash?
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(self.path)
                probe.close()
                raise RuntimeError(f"a server is already running on {self.path}")
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(self.path)
        old = os.umask(0o117)
        try:
            self._srv = await asyncio.start_unix_server(self._handle, path=self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, self.socket_mode)

    async def close(self):
        for p in list(self.peers.values()):
            p.send({"type": "bye"})
            p.queue.put_nowait(None) if not p.queue.full() else p.close()
        await asyncio.sleep(0.2)
        if self._srv:
            self._srv.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # ---------- routing primitives ----------
    def send(self, name, msg):
        p = self.peers.get(name)
        if p:
            p.send(msg)

    def multicast(self, group, msg, exclude=()):
        for n in list(self.groups.get(group, ())):
            if n not in exclude:
                self.send(n, msg)

    def broadcast(self, msg, exclude=()):
        for n in list(self.peers):
            if n not in exclude:
                self.send(n, msg)

    def join_group(self, name, group):
        if name in self.peers:
            self.groups.setdefault(group, set()).add(name)

    def leave_group(self, name, group):
        self.groups.get(group, set()).discard(name)

    def members(self, group):
        return set(self.groups.get(group, ()))

    def kick(self, name, reason="kicked"):
        p = self.peers.get(name)
        if p:
            p.send({"type": "error", "text": reason})
            p.close()

    # ---------- per-connection task ----------
    async def _handle(self, reader, writer):
        pid, uid, _gid = peer_credentials(writer.get_extra_info("socket"))
        try:
            hello = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
            if hello["type"] != "hello":
                raise ProtocolError("expected hello")
        except Exception:
            writer.close()
            return
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = f"uid{uid}"
        nick = hello.get("nick")
        name = nick if (self.dev and isinstance(nick, str) and _NICK_RE.fullmatch(nick)) else user
        if name in self.peers:
            writer.write(encode({"type": "error", "text": f"'{name}' is already connected"}))
            await writer.drain()
            writer.close()
            return

        peer = _Peer(name, uid, pid, writer, self.rate, self.burst)
        self.peers[name] = peer
        peer.send({"type": "welcome", "name": name})
        try:
            await _maybe_await(self.on_join, name)
            while not peer.closed:
                msg = await read_frame(reader)
                if not peer.allow():
                    peer.strikes += 1
                    if peer.strikes > 30:
                        self.kick(name, "rate limit exceeded")
                        break
                    peer.send({"type": "error", "text": "slow down (rate limited)"})
                    continue
                msg["sender"] = name        # authoritative; overrides anything client sent
                try:
                    await _maybe_await(self.on_message, name, msg)
                except Exception as e:      # a game bug must not kill the connection loop
                    print(f"[wolfnet] handler error for {name}: {e!r}")
        except (asyncio.IncompleteReadError, ConnectionError, ProtocolError):
            pass
        finally:
            peer.close()
            peer.task.cancel()
            if self.peers.get(name) is peer:
                del self.peers[name]
                for g in self.groups.values():
                    g.discard(name)
                await _maybe_await(self.on_leave, name)


class Client:
    def __init__(self, path):
        self.path = path
        self.reader = self.writer = None
        self.name = None

    async def connect(self, nick=None):
        self.reader, self.writer = await asyncio.open_unix_connection(self.path)
        await self.send("hello", nick=nick)
        msg = await read_frame(self.reader)
        if msg["type"] != "welcome":
            raise ConnectionError(msg.get("text", "connection refused"))
        self.name = msg["name"]
        return self.name

    async def send(self, type, **fields):
        self.writer.write(encode({"type": type, **fields}))
        await self.writer.drain()

    async def recv(self):
        try:
            return await read_frame(self.reader)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    async def close(self):
        if self.writer:
            self.writer.close()
