#!/usr/bin/env python3
"""Stdio MCP bridge: runs a server defined in ~/.codex/config.toml for another agent.

Antigravity (Gemini in bb) only speaks stdio/SSE MCP, while several servers here are
streamable-HTTP. The bridge is launched as `mcp_bridge.py <server-name>` and:
  * reads the server definition from the Codex config at spawn time, so secrets
    (env, http_headers) are never copied into another agent's config;
  * stdio servers are spawned as a child process with a minimal environment (like
    Codex: a few base variables plus the server's own env); URL servers are proxied
    over streamable HTTP (JSON or SSE responses, Mcp-Session-Id, re-initialize after
    the server drops the session, retries on 429/503, no redirects);
  * `enabled_tools` from the Codex config is enforced on every tool list and on every
    tools/call, fail-closed: malformed or id-less calls never reach the server;
  * every request is logged (method, tool, duration, status) without payloads.
"""
from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request

CODEX_CONFIG = os.environ.get("HARNESS_CODEX_CONFIG", os.path.expanduser("~/.codex/config.toml"))
LOG_PATH = os.environ.get("HARNESS_MCP_LOG", os.path.expanduser("~/.agents/harness/logs/mcp_bridge.jsonl"))
LOG_MAX_BYTES = 20 * 1024 * 1024
HTTP_TIMEOUT = float(os.environ.get("HARNESS_MCP_HTTP_TIMEOUT", "300"))
RETRY_DELAYS = tuple(float(x) for x in os.environ.get("HARNESS_MCP_RETRY_DELAYS", "2,5").split(",") if x)
# Same base set Codex passes to stdio MCP servers; everything else must come from `env`/`env_vars`.
BASE_ENV = ("HOME", "LOGNAME", "PATH", "SHELL", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ")
CACHE_DIR = os.environ.get("HARNESS_MCP_CACHE_DIR", os.path.expanduser("~/.agents/harness/state/mcp-cache"))
LAZY = os.environ.get("HARNESS_MCP_LAZY", "1") != "0"
# Requests a client sends at startup; their replies are cached so stdio servers can start lazily.
CACHE_TTL_S = 24 * 3600
CACHED_METHODS = {"server/discover", "tools/list", "resources/list", "resources/templates/list", "prompts/list"}


class Telemetry:
    def __init__(self, server: str, transport: str, path: str = LOG_PATH):
        self.server, self.transport, self.path = server, transport, path
        self._lock = threading.Lock()

    def log(self, event: str, **fields) -> None:
        rec = {"ts": round(time.time(), 3), "server": self.server, "transport": self.transport,
               "pid": os.getpid(), "event": event, **fields}
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            lock_fd = os.open(self.path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
            with self._lock, os.fdopen(lock_fd, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)  # rotation is shared by all bridge processes
                try:
                    if os.path.getsize(self.path) > LOG_MAX_BYTES:
                        os.replace(self.path, self.path + ".1")
                except FileNotFoundError:
                    pass
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # telemetry must never break the bridge


# Codex server keys the bridge understands. Anything else could change what the server may do
# (a new filter, auth mode...), so the bridge refuses to start rather than silently ignore it.
KNOWN_KEYS = {"command", "args", "env", "env_vars", "cwd", "url", "http_headers", "env_http_headers",
              "bearer_token_env_var", "enabled", "required", "enabled_tools", "disabled_tools",
              "startup_timeout_sec", "startup_timeout_ms", "tool_timeout_sec"}


def load_server(name: str, config_path: str = CODEX_CONFIG) -> dict:
    with open(config_path, "rb") as f:
        servers = tomllib.load(f).get("mcp_servers", {})
    if name not in servers:
        raise SystemExit(f"mcp_bridge: server {name!r} not found in {config_path}")
    spec = servers[name]
    if spec.get("enabled") is False:
        raise SystemExit(f"mcp_bridge: server {name!r} is disabled in {config_path}")
    unknown = sorted(set(spec) - KNOWN_KEYS)
    if unknown:
        raise SystemExit(f"mcp_bridge: server {name!r} uses unsupported keys {unknown}; refusing to start")
    for key in ("enabled_tools", "disabled_tools"):  # a bare string would turn into a set of characters
        if key in spec and not (isinstance(spec[key], list) and all(isinstance(t, str) for t in spec[key])):
            raise SystemExit(f"mcp_bridge: server {name!r}: {key} must be a list of strings")
    timeout = spec.get("tool_timeout_sec")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
        raise SystemExit(f"mcp_bridge: server {name!r}: tool_timeout_sec must be a positive number")
    if not spec.get("command") and not spec.get("url"):
        raise SystemExit(f"mcp_bridge: server {name!r} has neither command nor url")
    return spec


def _key(req_id) -> str:
    return json.dumps(req_id, sort_keys=True)


class Bridge:
    """Client side (stdin/stdout) plus tool filtering and per-request telemetry."""

    def __init__(self, telemetry: Telemetry, enabled_tools: list[str] | None,
                 disabled_tools: list[str] | None = None, tool_timeout: float | None = None):
        self.tel = telemetry
        self.allowed = set(enabled_tools) if enabled_tools is not None else None
        self.denied = set(disabled_tools or ())
        self.tool_timeout = tool_timeout
        self._out_lock = threading.Lock()
        self._pending: dict[str, tuple] = {}  # request id -> (method, tool, started)
        self._timed_out: set[str] = set()  # ids already answered with a timeout: drop the late reply
        self._timers: dict[str, threading.Timer] = {}  # cancelled as soon as the call is answered
        self._pending_lock = threading.Lock()

    def tool_ok(self, name) -> bool:
        return isinstance(name, str) and (self.allowed is None or name in self.allowed) and name not in self.denied

    def write_client(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False)
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def error_reply(self, req_id, code: int, message: str) -> None:
        self.write_client({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def from_client(self, msg) -> bool:
        """Book-keep a client message. Returns False when it must not be forwarded."""
        if not isinstance(msg, dict):
            self.tel.log("bad_client_message")
            return False
        method, req_id = msg.get("method"), msg.get("id")
        has_id = req_id is not None
        if method is not None and not isinstance(method, str):
            self.tel.log("bad_client_message")
            if has_id:
                self.error_reply(req_id, -32600, "invalid request")
            return False
        tool = None
        if method == "tools/call":
            params = msg.get("params")
            tool = params.get("name") if isinstance(params, dict) else None
            if not isinstance(tool, str) or not has_id:
                self.tel.log("blocked", method=method, reason="malformed tools/call")
                if has_id:
                    self.error_reply(req_id, -32602, "invalid tools/call params")
                return False
            if not self.tool_ok(tool):
                self.tel.log("blocked", method=method, tool=tool)
                self.error_reply(req_id, -32602, f"tool {tool!r} is not enabled for this server")
                return False
        if method is not None and has_id:
            entry = (method, tool, time.monotonic())
            with self._pending_lock:
                self._pending[_key(req_id)] = entry
                self._timed_out.discard(_key(req_id))
            if tool is not None and self.tool_timeout:
                t = threading.Timer(self.tool_timeout, self._expire, (req_id, entry))
                t.daemon = True
                with self._pending_lock:
                    self._timers[_key(req_id)] = t
                t.start()
        return True

    def _done(self, key: str):
        """Pop a pending entry and stop its timer. Caller holds _pending_lock."""
        timer = self._timers.pop(key, None)
        if timer:
            timer.cancel()
        return self._pending.pop(key, None)

    def _expire(self, req_id, entry: tuple) -> None:
        with self._pending_lock:
            if self._pending.get(_key(req_id)) is not entry:
                return  # already answered (or the id was reused by a newer request)
            self._done(_key(req_id))
            if len(self._timed_out) > 1000:  # late replies that never came: keep the set bounded
                self._timed_out.clear()
            self._timed_out.add(_key(req_id))
        self.tel.log("response", method=entry[0], tool=entry[1], status="error", error="timeout")
        self.error_reply(req_id, -32000, f"tool call timed out after {self.tool_timeout:g}s")

    def from_server(self, msg) -> None:
        if not isinstance(msg, dict):
            self.tel.log("bad_server_message")
            return
        result = msg.get("result")
        filtered = self.allowed is not None or self.denied
        if filtered and isinstance(result, dict) and isinstance(result.get("tools"), list):
            # Filter every tool list, not only replies we matched to tools/list: fail-closed on id reuse.
            result["tools"] = [t for t in result["tools"] if isinstance(t, dict) and self.tool_ok(t.get("name"))]
        if "id" in msg and "method" not in msg:
            with self._pending_lock:
                entry = self._done(_key(msg["id"]))
                if entry is None and _key(msg["id"]) in self._timed_out:
                    self._timed_out.discard(_key(msg["id"]))
                    self.tel.log("late_reply_dropped")
                    return
            if entry:
                method, tool, started = entry
                err = msg.get("error") if isinstance(msg.get("error"), dict) else None
                self.tel.log("response", method=method, tool=tool,
                             duration_ms=round((time.monotonic() - started) * 1000),
                             status="error" if "error" in msg else "ok",
                             **({"error_code": err.get("code")} if err else {}))
        self.write_client(msg)

    def is_pending(self, req_id) -> bool:
        with self._pending_lock:
            return _key(req_id) in self._pending

    def fail(self, req_id, reason: str) -> None:
        with self._pending_lock:
            entry = self._done(_key(req_id))
            self._timed_out.discard(_key(req_id))
        if entry:
            self.tel.log("response", method=entry[0], tool=entry[1], status="error", error=reason)
            self.error_reply(req_id, -32000, reason)

    def fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            keys = list(self._pending)
        for key in keys:
            self.fail(json.loads(key), reason)


def iter_client_messages(tel: Telemetry):
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            tel.log("bad_client_json")
            continue
        yield from (msg if isinstance(msg, list) else [msg])


def child_env(spec: dict) -> dict:
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update({k: os.environ[k] for k in spec.get("env_vars", []) if k in os.environ})
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    return env


class ReplyCache:
    """Startup replies of one stdio server (initialize, tool/resource/prompt lists), so a later
    session can answer them without starting the server. Bound to a digest of the server spec:
    any change in the Codex config (command, env, filters...) invalidates it. Only successful
    list replies are kept (an error must not hide the tools for good), and a cache older than
    CACHE_TTL_S is not used for lazy starts, so a changed server gets re-read at least daily."""

    def __init__(self, path: str, spec: dict):
        self.path = path
        self.digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
        self._lock = threading.Lock()
        self.disabled = False
        self.data = self._load() or self._empty()
        try:
            self.age = time.time() - os.path.getmtime(path)
        except OSError:
            self.age = float("inf")

    def _empty(self) -> dict:
        return {"digest": self.digest, "initialize": {}, "replies": {}}

    def _load(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if (isinstance(data, dict) and data.get("digest") == self.digest
                and isinstance(data.get("initialize"), dict) and isinstance(data.get("replies"), dict)):
            return data
        return None

    @staticmethod
    def key(method, params) -> str | None:
        if method not in CACHED_METHODS:
            return None
        params = {k: v for k, v in (params if isinstance(params, dict) else {}).items() if k != "_meta"}
        return None if params else method  # paginated or parameterised lists are never cached

    def init_reply(self, params) -> dict | None:
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return self.data["initialize"].get(version) if isinstance(version, str) else None

    def reply(self, method, params) -> dict | None:
        k = self.key(method, params)
        return self.data["replies"].get(k) if k else None

    def warm(self) -> bool:
        tools = self.data["replies"].get("tools/list")
        return (not self.disabled and self.age < CACHE_TTL_S and bool(self.data["initialize"])
                and isinstance(tools, dict) and "result" in tools)

    def ready(self, init_params) -> bool:
        return self.warm() and self.init_reply(init_params) is not None

    def record(self, method, params, msg: dict) -> bool:
        """Store a server reply; returns True when it differs from what was cached."""
        if self.disabled:
            return False
        if method == "initialize":
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            if not isinstance(version, str) or not isinstance(msg.get("result"), dict):
                return False
            slot, k, body = "initialize", version, msg["result"]
        else:
            k = self.key(method, params)
            # Lists: results only. server/discover: its error is the normal answer, keep it too.
            body = {f: msg[f] for f in ("result", "error") if f in msg and (f == "result" or method == "server/discover")}
            if k is None or not body or (isinstance(body.get("result"), dict) and body["result"].get("nextCursor")):
                return False
            slot = "replies"
        with self._lock, self._file_lock():
            data = self._load() or self._empty()  # merge with what other bridge processes wrote
            if data[slot].get(k) == body:
                self.data = data
                try:
                    os.utime(self.path)  # confirmed by the live server: fresh for another day
                    self.age = 0.0
                except OSError:
                    pass
                return False
            data[slot][k] = body
            self.data = data
            try:
                tmp = f"{self.path}.{os.getpid()}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:
                pass
            return True

    def invalidate(self) -> None:
        """Forget the server: the next session starts it for real and re-reads everything."""
        self.disabled = True
        with self._lock, self._file_lock():
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def _file_lock(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        f = os.fdopen(os.open(self.path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600), "w")

        class _Held:
            def __enter__(self_inner):
                fcntl.flock(f, fcntl.LOCK_EX)

            def __exit__(self_inner, *exc):
                f.close()  # releases the lock
        return _Held()


def startup_timeout(spec: dict) -> float:
    if isinstance(spec.get("startup_timeout_sec"), (int, float)):
        return float(spec["startup_timeout_sec"])
    if isinstance(spec.get("startup_timeout_ms"), (int, float)):
        return spec["startup_timeout_ms"] / 1000
    return 60.0


def run_stdio(spec: dict, bridge: Bridge, tel: Telemetry, cache: ReplyCache | None = None,
              lazy: bool = False) -> int:
    """Pass-through to a child process. With a warm `cache` the child is started lazily: the
    handshake and list requests are answered from the cache, and the server is launched on the
    first request that needs it (usually tools/call), replaying the client's initialize first."""
    proc: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    # Lazy only with a warm cache; a cold one starts the server right away and fills the cache.
    lazy = lazy and cache is not None and cache.warm()
    dead = False  # the server is gone: every later request gets an error instead of silence
    write_lock = threading.Lock()
    started_failed = False
    client_init: dict | None = None
    client_initialized = False
    sent: dict[str, tuple] = {}  # id -> (method, params) of requests whose replies feed the cache
    internal: dict[str, threading.Event] = {}  # bridge-originated requests: replies are not forwarded
    internal_replies: dict[str, dict] = {}
    own_ids: set[str] = set()  # ids the bridge issued itself; late replies to them are dropped
    ids = itertools.count(1)

    def pump_server(p: subprocess.Popen):
        try:
            for raw in p.stdout:
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    for m in msg if isinstance(msg, list) else [msg]:
                        k = _key(m.get("id")) if isinstance(m, dict) and "method" not in m else None
                        if cache is not None and k in sent:
                            method, params = sent.pop(k)
                            if cache.record(method, params, json.loads(json.dumps(m))):
                                tel.log("cache_updated", method=method)
                        if k in own_ids:
                            own_ids.discard(k)
                            ev = internal.pop(k, None)  # one step: the waiter may time out concurrently
                            if ev is not None:
                                internal_replies[k] = m
                                ev.set()
                            continue  # never the client's reply, even when it comes too late
                        bridge.from_server(m)
                except OSError:
                    return  # client went away
                except Exception as e:  # one bad message must not stall every later reply
                    tel.log("bad_server_message", error=type(e).__name__)
        finally:
            nonlocal dead
            dead = True
            for ev in list(internal.values()):
                ev.set()
            bridge.fail_pending("MCP server process exited")

    def write(msg: dict) -> None:
        if cache is not None and msg.get("id") is not None and msg.get("method") is not None:
            if msg["method"] == "initialize" or ReplyCache.key(msg["method"], msg.get("params")):
                sent[_key(msg["id"])] = (msg["method"], msg.get("params"))
            else:
                sent.pop(_key(msg["id"]), None)  # a reused id must not file this reply under a list
        with write_lock:
            proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            proc.stdin.flush()

    def request(method: str, params) -> dict | None:
        rid = f"harness-{method}-{next(ids)}"
        own_ids.add(_key(rid))
        ev = internal[_key(rid)] = threading.Event()
        write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        ev.wait(startup_timeout(spec))
        internal.pop(_key(rid), None)
        return internal_replies.pop(_key(rid), None)

    def start(replay: bool) -> bool:
        nonlocal proc, reader, started_failed, dead
        if proc is not None:
            return True
        if started_failed:
            return False
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen([spec["command"], *spec.get("args", [])], env=child_env(spec),
                                    cwd=spec.get("cwd"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as e:
            tel.log("spawn_failed", error=type(e).__name__)
            started_failed = True
            if cache is not None:
                cache.invalidate()
            return False
        reader = threading.Thread(target=pump_server, args=(proc,), daemon=True)
        reader.start()
        if not replay:
            tel.log("started")
            return True
        # Lazy start: the client already finished its handshake against the cache.
        before = cache.reply("tools/list", None) if cache else None
        init_params = (client_init or {}).get("params") or {}
        cached_init = cache.init_reply(init_params) or {}  # before the replay re-records it
        try:
            init = request("initialize", init_params)
            error = "timeout" if init is None else (None if "result" in init else "initialize error")
        except (OSError, ValueError):  # the server died before it could read the handshake
            init, error = None, "pipe closed"
        if error:
            tel.log("lazy_start_failed", duration_ms=round((time.monotonic() - t0) * 1000), error=error)
            started_failed = dead = True
            cache.invalidate()
            try:
                os.killpg(proc.pid, signal.SIGTERM)  # a hung server would otherwise swallow every call
            except (ProcessLookupError, PermissionError):
                pass
            return False
        if isinstance(init.get("result"), dict) and init["result"].get("protocolVersion") != cached_init.get("protocolVersion"):
            # The client is bound to the cached handshake for this session; fix the next one.
            tel.log("handshake_changed")
            cache.invalidate()
        try:
            if client_initialized:
                write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except (OSError, ValueError):
            pass  # the main loop notices the dead pipe on its next write
        tel.log("started", lazy=True, duration_ms=round((time.monotonic() - t0) * 1000))

        def refresh():  # in the background, so the call that woke the server is not delayed
            try:
                fresh = request("tools/list", None)
            except (OSError, ValueError):
                return
            if isinstance(fresh, dict) and "result" in fresh and before is not None and {"result": fresh["result"]} != before:
                tel.log("tools_changed")
                bridge.write_client({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

        threading.Thread(target=refresh, daemon=True).start()
        return True

    def answer_from_cache(msg: dict) -> bool:
        nonlocal client_init, client_initialized
        method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params")
        if method == "initialize" and req_id is not None:
            if not cache.ready(params):
                return False
            client_init = msg
            tel.log("lazy_ready")
            bridge.from_server({"jsonrpc": "2.0", "id": req_id, "result": json.loads(json.dumps(cache.init_reply(params)))})
            return True
        # Before the handshake only discovery may be served (Gemini sends server/discover first).
        if client_init is None and method not in ("server/discover", "ping"):
            return False  # no cached handshake: behave like a plain pass-through
        if method == "notifications/initialized":
            client_initialized = True
            return True
        if method == "notifications/cancelled":
            return True  # nothing is in flight upstream yet
        if method == "ping" and req_id is not None:
            bridge.from_server({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return True
        cached = cache.reply(method, params) if req_id is not None else None
        if cached is None:
            return False
        bridge.from_server({"jsonrpc": "2.0", "id": req_id, **json.loads(json.dumps(cached))})
        return True

    if not lazy:
        if not start(replay=False):
            raise SystemExit(f"mcp_bridge: cannot start {spec['command']!r}")
    try:
        for msg in iter_client_messages(tel):
            if not bridge.from_client(msg):
                continue
            if proc is None:
                if lazy and answer_from_cache(msg):
                    continue
                if not start(replay=client_init is not None):
                    if msg.get("id") is not None:
                        bridge.fail(msg["id"], "MCP server failed to start")
                    continue
            if dead:
                if msg.get("id") is not None:
                    bridge.fail(msg["id"], "MCP server process exited")
                continue
            try:
                write(msg)
            except (BrokenPipeError, ValueError, OSError):
                tel.log("server_pipe_closed")
                dead = True
                if msg.get("id") is not None:
                    bridge.fail(msg["id"], "MCP server process exited")
    finally:
        code = None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            for sig in (None, signal.SIGTERM, signal.SIGKILL):
                if sig is not None:
                    try:
                        os.killpg(proc.pid, sig)  # also reaps npx/uvx grandchildren
                    except ProcessLookupError:
                        pass
                try:
                    code = proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)  # grandchildren (npx/uvx) may outlive the direct child
            except (ProcessLookupError, PermissionError):
                pass
            if reader is not None:
                reader.join(timeout=2)
        tel.log("stopped", exit_code=code, started=proc is not None)
    return code or 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward credentials to another location; surfaces as HTTPError 3xx


_OPENER = urllib.request.build_opener(_NoRedirect)


class SessionExpired(Exception):
    pass


class HttpUpstream:
    def __init__(self, spec: dict, bridge: Bridge, tel: Telemetry):
        self.url = spec["url"]
        self.headers = {k: str(v) for k, v in (spec.get("http_headers") or {}).items()}
        # Values are read from the bridge's own environment at launch, as Codex does; never logged.
        self.headers.update({k: os.environ[v] for k, v in (spec.get("env_http_headers") or {}).items()
                             if v in os.environ})
        token_var = spec.get("bearer_token_env_var")
        if isinstance(token_var, str) and token_var in os.environ:
            self.headers["Authorization"] = f"Bearer {os.environ[token_var]}"
        self.bridge, self.tel = bridge, tel
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.init_params: dict | None = None
        self._reinit_lock = threading.Lock()
        self._reinit_ids = itertools.count(1)

    def _open(self, msg: dict, with_session: bool = True):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if with_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        data = json.dumps(msg).encode()
        for attempt in range(len(RETRY_DELAYS) + 1):
            req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                return _OPENER.open(req, timeout=HTTP_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code == 404 and headers.get("Mcp-Session-Id"):
                    raise SessionExpired(headers["Mcp-Session-Id"]) from e
                if e.code not in (429, 503) or attempt == len(RETRY_DELAYS):
                    raise
                retry_after = e.headers.get("Retry-After", "") if e.headers else ""
                delay = min(float(retry_after), 30.0) if retry_after.isdigit() else RETRY_DELAYS[attempt]
                self.tel.log("retry", http_status=e.code, method=msg.get("method"), attempt=attempt + 1, delay_s=delay)
                time.sleep(delay)

    def _exchange(self, msg: dict, on_message, with_session: bool = True) -> None:
        """POST one message and feed every JSON-RPC message of the reply to on_message."""
        with self._open(msg, with_session) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            ctype = resp.headers.get("Content-Type", "")
            if resp.status == 202:
                return
            if ctype.startswith("text/event-stream"):
                data: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        self._emit("\n".join(data), on_message)
                        data = []
                if data:
                    self._emit("\n".join(data), on_message)
            else:
                body = resp.read()
                if body.strip():
                    self._emit(body, on_message)

    def _emit(self, raw, on_message) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            self.tel.log("bad_server_message", error="ValueError")
            return
        for m in payload if isinstance(payload, list) else [payload]:
            result = m.get("result") if isinstance(m, dict) else None
            if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
                self.protocol_version = result["protocolVersion"]
            on_message(m)

    def _reinitialize(self, expired_sid: str) -> None:
        with self._reinit_lock:
            if self.session_id != expired_sid:
                return  # another thread already re-initialized
            self.tel.log("session_expired")
            # Keep the expired id until a new session exists: if initialize fails now, the next
            # request gets 404 again and retries the re-initialization instead of going sessionless.
            init = {"jsonrpc": "2.0", "id": f"harness-reinit-{next(self._reinit_ids)}", "method": "initialize",
                    "params": self.init_params or {}}
            self._exchange(init, lambda m: None, with_session=False)
            if self.session_id == expired_sid:
                raise SessionExpired(expired_sid)  # server answered without issuing a session
            self._exchange({"jsonrpc": "2.0", "method": "notifications/initialized"}, lambda m: None)

    def post(self, msg: dict) -> None:
        is_request = msg.get("method") is not None and msg.get("id") is not None
        if msg.get("method") == "initialize":
            self.init_params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            try:
                self._exchange(msg, self.bridge.from_server)
            except SessionExpired as e:
                self._reinitialize(str(e))
                self._exchange(msg, self.bridge.from_server)
        except urllib.error.HTTPError as e:
            self.tel.log("http_error", http_status=e.code, method=msg.get("method"))
            if is_request:
                self.bridge.fail(msg["id"], f"upstream HTTP {e.code}")
        except Exception as e:  # URLError, IncompleteRead, bad JSON...: the client must always get an answer
            self.tel.log("transport_error", error=type(e).__name__, method=msg.get("method"))
            if is_request:
                self.bridge.fail(msg["id"], f"upstream unreachable: {type(e).__name__}")
        if is_request and self.bridge.is_pending(msg["id"]):
            self.bridge.fail(msg["id"], "upstream closed the stream without a response")


    def close(self) -> None:
        """End the server-side session (spec: HTTP DELETE); servers may answer 405, which is fine."""
        if not self.session_id:
            return
        headers = {**self.headers, "Mcp-Session-Id": self.session_id}
        try:
            _OPENER.open(urllib.request.Request(self.url, headers=headers, method="DELETE"), timeout=5).close()
        except Exception as e:
            self.tel.log("session_close_failed", error=type(e).__name__)


def run_http(spec: dict, bridge: Bridge, tel: Telemetry) -> int:
    upstream = HttpUpstream(spec, bridge, tel)
    tel.log("started")
    workers: list[threading.Thread] = []
    # Notifications go through one ordered worker, so a slow or retried one never blocks client input.
    notes: queue.Queue = queue.Queue()

    def pump_notes() -> None:
        while (msg := notes.get()) is not None:
            upstream.post(msg)

    notifier = threading.Thread(target=pump_notes, daemon=True)
    notifier.start()
    try:
        for msg in iter_client_messages(tel):
            if not bridge.from_client(msg):
                continue
            # The handshake (initialize, then notifications/initialized) must finish before anything else.
            if msg.get("method") in ("initialize", "notifications/initialized"):
                upstream.post(msg)
                continue
            if msg.get("id") is None:
                notes.put(msg)
                continue
            t = threading.Thread(target=upstream.post, args=(msg,), daemon=True)
            t.start()
            workers = [w for w in workers if w.is_alive()] + [t]
    finally:  # also on SIGTERM/SIGHUP: end the server session
        notes.put(None)
        deadline = time.monotonic() + 10  # one shared budget, not 10 s per stuck call
        for w in [notifier, *workers]:
            w.join(timeout=max(0.0, deadline - time.monotonic()))
        upstream.close()
        tel.log("stopped")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: mcp_bridge.py <codex-mcp-server-name>", file=sys.stderr)
        return 2
    name = argv[1]
    spec = load_server(name)
    transport = "http" if spec.get("url") else "stdio"
    tel = Telemetry(name, transport)
    bridge = Bridge(tel, spec.get("enabled_tools"), spec.get("disabled_tools"), spec.get("tool_timeout_sec"))
    # Turn SIGTERM/SIGHUP into a normal exit so the `finally` blocks stop the server process group.
    def on_signal(signum, _frame):
        for s in (signal.SIGTERM, signal.SIGHUP):  # a second signal must not abort the cleanup in `finally`
            signal.signal(s, signal.SIG_IGN)
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    if transport == "http":
        return run_http(spec, bridge, tel)
    cache = ReplyCache(os.path.join(CACHE_DIR, f"{name}.json"), spec)
    return run_stdio(spec, bridge, tel, cache, lazy=LAZY)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
