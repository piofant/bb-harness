import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
BRIDGE = HERE / "mcp_bridge.py"

# Records every start and every message it sees; behaviour switches via files next to it.
FAKE = r'''
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(d, "starts.log"), "a").write("start\n")
if os.path.exists(os.path.join(d, "die_on_start")):
    sys.exit(4)
v2 = os.path.exists(os.path.join(d, "tools_v2"))
seen = open(os.path.join(d, "seen.log"), "a")
for line in sys.stdin:
    m = json.loads(line)
    seen.write(json.dumps({"method": m.get("method"), "id": m.get("id"), "params": m.get("params")}) + "\n")
    seen.flush()
    if "id" not in m:
        continue
    if m["method"] == "initialize" and not os.path.exists(os.path.join(d, "proto_v2")):
        out = {"result": {"protocolVersion": m["params"].get("protocolVersion"), "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake"}}}
    elif m["method"] == "tools/list" and os.path.exists(os.path.join(d, "list_error")):
        out = {"error": {"code": -32603, "message": "auth expired"}}
    elif m["method"] == "initialize" and os.path.exists(os.path.join(d, "proto_v2")):
        out = {"result": {"protocolVersion": "2099-01-01", "capabilities": {}, "serverInfo": {"name": "fake"}}}
    elif m["method"] == "tools/list":
        tools = [{"name": "read_x"}, {"name": "send_x"}] + ([{"name": "read_y"}] if v2 else [])
        out = {"result": {"tools": tools}}
    elif m["method"] == "tools/call":
        out = {"result": {"content": [{"type": "text", "text": "called " + m["params"]["name"]}]}}
    else:
        out = {"error": {"code": -32601, "message": "Method not found"}}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], **out}), flush=True)
'''

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "gemini"}}}
STARTUP = [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
           {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {}},
           {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}]
CALL = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_x", "arguments": {}}}


@pytest.fixture
def srv(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[mcp_servers.fake]\ncommand = "{sys.executable}"\nargs = ["{fake}"]\n'
                   f'enabled_tools = ["read_x", "read_y"]\nstartup_timeout_sec = 3\n'
                   f'[mcp_servers.fake.env]\nFAKE_TOKEN = "tok-s3cret"\n')
    return tmp_path


def run(srv, messages, check=True, **env):
    e = {**os.environ, "HARNESS_CODEX_CONFIG": str(srv / "config.toml"), "HARNESS_MCP_LOG": str(srv / "log.jsonl"),
         "HARNESS_MCP_CACHE_DIR": str(srv / "cache"), **env}
    p = subprocess.run([sys.executable, str(BRIDGE), "fake"], input="".join(json.dumps(m) + "\n" for m in messages),
                       capture_output=True, text=True, env=e, timeout=30)
    assert p.returncode == 0 or not check, p.stderr
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def starts(srv):
    return len((srv / "starts.log").read_text().splitlines()) if (srv / "starts.log").exists() else 0


def seen(srv):
    return [json.loads(line) for line in (srv / "seen.log").read_text().splitlines()]


def log(srv):
    return [json.loads(line) for line in (srv / "log.jsonl").read_text().splitlines()]


def by_id(out):
    return {m["id"]: m for m in out if "id" in m}


def test_warm_cache_answers_startup_without_the_server(srv):
    first = run(srv, STARTUP)  # cold cache: the server starts at once and fills the cache
    assert starts(srv) == 1
    cache = srv / "cache" / "fake.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert "tok-s3cret" not in cache.read_text()

    second = run(srv, STARTUP + [{"jsonrpc": "2.0", "id": 9, "method": "ping"}])
    assert starts(srv) == 1  # never started
    assert by_id(second)[1] == by_id(first)[1] and by_id(second)[2] == by_id(first)[2]
    assert [t["name"] for t in by_id(second)[3]["result"]["tools"]] == ["read_x"]  # filter applies to the cache
    assert by_id(second)[9] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert any(r["event"] == "lazy_ready" for r in log(srv))
    assert any(r["event"] == "stopped" and r["started"] is False for r in log(srv))


def test_first_real_call_starts_the_server_with_the_clients_handshake(srv):
    run(srv, STARTUP)
    (srv / "seen.log").unlink()
    out = run(srv, STARTUP + [CALL])
    assert starts(srv) == 2
    assert by_id(out)[4]["result"]["content"][0]["text"] == "called read_x"
    got = seen(srv)
    assert got[0]["method"] == "initialize" and got[0]["params"] == INIT["params"]
    assert got[1]["method"] == "notifications/initialized"
    assert {"method": "tools/call", "id": 4, "params": CALL["params"]} in got
    assert not any(str(m.get("id", "")).startswith("harness-") for m in out)  # internal replies stay inside
    assert sorted(by_id(out)) == [1, 2, 3, 4]
    assert any(r["event"] == "started" and r.get("lazy") for r in log(srv))


def test_changed_tools_notify_the_client_and_refresh_the_cache(srv):
    run(srv, STARTUP)
    (srv / "tools_v2").touch()
    out = run(srv, STARTUP + [CALL, {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"} in out
    assert [t["name"] for t in by_id(out)[3]["result"]["tools"]] == ["read_x"]  # this session: the old cache
    out = run(srv, STARTUP)
    assert [t["name"] for t in by_id(out)[3]["result"]["tools"]] == ["read_x", "read_y"]  # next one: fresh


def test_lazy_start_failure_answers_with_an_error(srv):
    run(srv, STARTUP)
    (srv / "die_on_start").touch()
    out = run(srv, STARTUP + [CALL, dict(CALL, id=6)], check=False)  # exit code = the server's
    assert by_id(out)[4]["error"]["message"] in ("MCP server failed to start", "MCP server process exited")
    assert "error" in by_id(out)[6]
    assert any(r["event"] == "lazy_start_failed" for r in log(srv))


@pytest.mark.parametrize("change", ["protocol", "config", "disabled"])
def test_cache_is_bypassed(srv, change):
    run(srv, STARTUP)
    msgs, env = STARTUP, {}
    if change == "protocol":
        msgs = [dict(INIT, params={**INIT["params"], "protocolVersion": "2024-11-05"})] + STARTUP[1:]
    elif change == "config":  # any spec change (here: a new filter) invalidates the cache
        cfg = srv / "config.toml"
        cfg.write_text(cfg.read_text().replace('"read_x", "read_y"', '"read_x"'))
    else:
        env = {"HARNESS_MCP_LAZY": "0"}
    run(srv, msgs, **env)
    assert starts(srv) == 2


def test_unknown_request_starts_the_server(srv):
    run(srv, STARTUP)
    out = run(srv, STARTUP + [{"jsonrpc": "2.0", "id": 7, "method": "resources/read", "params": {"uri": "x"}}])
    assert starts(srv) == 2
    assert by_id(out)[7]["error"]["code"] == -32601


def test_requests_after_server_death_get_errors_not_silence(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[mcp_servers.dies]\ncommand = "{sys.executable}"\n'
                   f'args = ["-c", "import sys; sys.stdin.readline(); sys.exit(3)"]\n')
    e = {**os.environ, "HARNESS_CODEX_CONFIG": str(cfg), "HARNESS_MCP_LOG": str(tmp_path / "log.jsonl"),
         "HARNESS_MCP_CACHE_DIR": str(tmp_path / "cache")}
    p = subprocess.Popen([sys.executable, str(BRIDGE), "dies"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env=e)
    p.stdin.write(json.dumps(INIT) + "\n")
    p.stdin.flush()
    first = json.loads(p.stdout.readline())
    assert first["error"]["message"] == "MCP server process exited"
    for i in (2, 3):  # the server is gone; each later request must still be answered
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/list"}) + "\n")
        p.stdin.flush()
        assert json.loads(p.stdout.readline()) == {
            "jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": "MCP server process exited"}}
    p.stdin.close()
    assert p.wait(timeout=20) == 3


def test_gemini_order_discover_before_initialize_stays_lazy(srv):
    gemini = [{"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}}, INIT,
              {"jsonrpc": "2.0", "method": "notifications/initialized"},
              {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}]
    first = run(srv, gemini)
    second = run(srv, gemini)
    assert starts(srv) == 1
    assert by_id(second) == by_id(first)


def test_tools_list_errors_are_never_cached(srv):
    (srv / "list_error").touch()
    run(srv, STARTUP)
    run(srv, STARTUP)
    assert starts(srv) == 2  # an error is not a warm cache: still starting the real server

    (srv / "list_error").unlink()
    run(srv, STARTUP)  # good list cached
    (srv / "list_error").touch()
    run(srv, STARTUP + [CALL])  # lazy start; the background refresh gets an error
    (srv / "list_error").unlink()
    out = run(srv, STARTUP)
    assert [t["name"] for t in by_id(out)[3]["result"]["tools"]] == ["read_x"]  # good list kept


def test_server_dying_during_lazy_start_never_crashes_the_bridge(srv):
    run(srv, STARTUP)
    (srv / "die_on_start").touch()
    for _ in range(8):
        (srv / "cache").mkdir(exist_ok=True)
        cache = srv / "cache" / "fake.json"
        if not cache.exists():  # a failed start drops the cache; re-warm it for the next round
            (srv / "die_on_start").unlink()
            run(srv, STARTUP)
            (srv / "die_on_start").touch()
        e = {**os.environ, "HARNESS_CODEX_CONFIG": str(srv / "config.toml"),
             "HARNESS_MCP_LOG": str(srv / "log.jsonl"), "HARNESS_MCP_CACHE_DIR": str(srv / "cache")}
        msgs = STARTUP + [CALL, dict(CALL, id=6)]
        p = subprocess.run([sys.executable, str(BRIDGE), "fake"], input="".join(json.dumps(m) + "\n" for m in msgs),
                           capture_output=True, text=True, env=e, timeout=30)
        assert "Traceback" not in p.stderr, p.stderr
        out = by_id([json.loads(line) for line in p.stdout.splitlines() if line.strip()])
        assert "error" in out[4] and "error" in out[6]
    assert sum(r["event"] == "lazy_start_failed" for r in log(srv)) == 8


def test_failed_start_or_stale_cache_goes_back_to_eager(srv):
    run(srv, STARTUP)
    cache = srv / "cache" / "fake.json"
    (srv / "die_on_start").touch()
    run(srv, STARTUP + [CALL], check=False)
    assert not cache.exists()  # a broken server must not keep advertising its old tools
    (srv / "die_on_start").unlink()
    run(srv, STARTUP)
    assert cache.exists()
    old = cache.stat().st_mtime - 2 * 24 * 3600
    os.utime(cache, (old, old))
    n = starts(srv)
    run(srv, STARTUP)
    assert starts(srv) == n + 1  # older than a day: refresh by starting the server


def test_client_ids_that_look_internal_are_still_answered(srv):
    out = run(srv, [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    dict(CALL, id="harness-tools/list-1")])
    assert by_id(out)["harness-tools/list-1"]["result"]["content"][0]["text"] == "called read_x"


def test_reused_id_never_poisons_the_cache(srv):
    run(srv, [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
              {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}, dict(CALL, id=5)])
    data = json.loads((srv / "cache" / "fake.json").read_text())
    assert "content" not in json.dumps(data["replies"])


def test_handshake_change_on_lazy_start_invalidates_cache(srv):
    run(srv, STARTUP)
    (srv / "proto_v2").touch()
    run(srv, STARTUP + [CALL])
    assert any(r["event"] == "handshake_changed" for r in log(srv))
    assert not (srv / "cache" / "fake.json").exists()


def test_concurrent_cache_writers_merge(tmp_path):
    sys.path.insert(0, str(HERE))
    import mcp_bridge
    spec = {"command": "x"}
    path = str(tmp_path / "c.json")
    a, b = mcp_bridge.ReplyCache(path, spec), mcp_bridge.ReplyCache(path, spec)
    a.record("tools/list", {}, {"result": {"tools": []}})
    b.record("prompts/list", {}, {"result": {"prompts": []}})
    data = json.loads(open(path).read())
    assert set(data["replies"]) == {"tools/list", "prompts/list"}


def test_unchanged_server_rewarms_an_old_cache(srv):
    run(srv, STARTUP)
    cache = srv / "cache" / "fake.json"
    old = cache.stat().st_mtime - 2 * 24 * 3600
    os.utime(cache, (old, old))
    run(srv, STARTUP)  # too old: real start, same answers
    n = starts(srv)
    run(srv, STARTUP)
    assert starts(srv) == n  # confirmed fresh again: lazy
