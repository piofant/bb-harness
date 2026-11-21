"""Regression tests for the independent review of 2026-11-13."""
import json
import os
import subprocess
import sys

from test_harness import BRIDGE, CALLS, FAKE_STDIO, FakeHttp, harness_sync, http_server, make_skill, run_bridge  # noqa: F401

ECHO_ALL = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"seen": m.get("method"), "tool": (m.get("params") or {}).get("name")}}), flush=True)
    if m.get("id") is not None:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"],
                          "result": {"tools": [{"name": "read_x"}, {"name": "send_x"}]}}), flush=True)
"""


def fake_toml(tmp_path, body=FAKE_STDIO, extra=""):
    fake = tmp_path / "fake.py"
    fake.write_text(body)
    return (f'[mcp_servers.fake]\ncommand = "{sys.executable}"\nargs = ["{fake}"]\n'
            f'enabled_tools = ["read_x"]\n{extra}')


def bridge_env(tmp_path, toml):
    cfg = tmp_path / "config.toml"
    cfg.write_text(toml)
    return {**os.environ, "HARNESS_CODEX_CONFIG": str(cfg), "HARNESS_MCP_LOG": str(tmp_path / "log.jsonl"), "HARNESS_MCP_CACHE_DIR": str(tmp_path / "cache"),
            "HARNESS_MCP_RETRY_DELAYS": "0.01,0.01"}


def test_tools_call_without_id_never_reaches_server(tmp_path):
    msgs = [{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "send_x"}},
            {"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "send_x"}},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"}]
    p, out, log = run_bridge(tmp_path, fake_toml(tmp_path, ECHO_ALL), "fake", msgs)
    seen = [m["params"] for m in map(json.loads, p.stdout.splitlines()) if m.get("method") == "notifications/message"]
    assert seen == [{"seen": "ping", "tool": None}]
    assert sum(r["event"] == "blocked" for r in log) == 2


def test_duplicate_ids_still_filter_tool_lists(tmp_path):
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 1, "method": "ping"}]
    p, _, _ = run_bridge(tmp_path, fake_toml(tmp_path, ECHO_ALL), "fake", msgs)
    responses = [m for m in map(json.loads, p.stdout.splitlines()) if "id" in m]
    assert len(responses) == 2
    assert all(t["name"] == "read_x" for r in responses for t in r["result"]["tools"])


def test_malformed_client_messages_do_not_crash(tmp_path):
    raw = ('[1]\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":[1]}\n'
           '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":5}}\n{bad json\n'
           '{"jsonrpc":"2.0","id":4,"method":"tools/list"}\n')
    p = subprocess.run([sys.executable, str(BRIDGE), "fake"], input=raw, capture_output=True, text=True,
                       env=bridge_env(tmp_path, fake_toml(tmp_path)), timeout=20)
    out = {m["id"]: m for m in map(json.loads, p.stdout.splitlines()) if "id" in m}
    assert p.returncode == 0, p.stderr
    assert "error" in out[2] and "error" in out[3]
    assert [t["name"] for t in out[4]["result"]["tools"]] == ["read_x"]


def test_garbage_from_server_does_not_kill_reader(tmp_path):
    body = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    print("5", flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": "not-a-dict"}), flush=True)
"""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    _, out, _ = run_bridge(tmp_path, fake_toml(tmp_path, body), "fake", msgs)
    assert set(out) == {1, 2}


def test_stdio_child_gets_minimal_env(tmp_path):
    body = r"""
import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {
        "leak": os.environ.get("BRIDGE_PARENT_SECRET"), "path": bool(os.environ.get("PATH")),
        "own": os.environ.get("OWN")}}), flush=True)
"""
    toml = fake_toml(tmp_path, body, '[mcp_servers.fake.env]\nOWN = "1"\n')
    os.environ["BRIDGE_PARENT_SECRET"] = "x"
    try:
        _, out, _ = run_bridge(tmp_path, toml, "fake", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    finally:
        del os.environ["BRIDGE_PARENT_SECRET"]
    assert out[1]["result"] == {"leak": None, "path": True, "own": "1"}


def test_http_session_expiry_reinitializes(tmp_path, http_server):
    env = bridge_env(tmp_path, f'[mcp_servers.web]\nurl = "{http_server}"\n')
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "web"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)
    for m in CALLS[:2]:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["id"] == 1
    FakeHttp.valid_session = None  # server restarted: the old session is gone
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_x"}}) + "\n")
    proc.stdin.close()
    out = [json.loads(line) for line in proc.stdout]
    proc.wait(timeout=10)
    assert [m["id"] for m in out] == [3]
    assert out[0]["result"]["content"][0]["text"] == "ok"
    assert FakeHttp.sessions == 2


def test_http_sse_without_response_and_redirect_fail_fast(tmp_path, http_server):
    msgs = CALLS[:2] + [{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "drop"}},
                        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "redirect"}}]
    _, out, _ = run_bridge(tmp_path, f'[mcp_servers.web]\nurl = "{http_server}"\n', "web", msgs)
    assert "without a response" in out[5]["error"]["message"]
    assert out[6]["error"]["message"] == "upstream HTTP 307"


def test_sync_respects_disabled_by_name_foreign_links_and_user_fields(tmp_path):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    for n in ("foo", "loop"):
        make_skill(src, n)
    alt = tmp_path / "alt"
    alt.mkdir()
    (alt / "foo").symlink_to(src / "foo")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[[skills.config]]\npath = "/elsewhere/loop/SKILL.md"\nenabled = false\n'
                   '[mcp_servers.a]\ncommand = "x"\n')
    (gem / "skills").mkdir(parents=True)
    (gem / "skills" / "myalias").symlink_to(src / "foo")
    (gem / "skills" / "foo").symlink_to(alt / "foo")
    base = ["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem), "--python", "py",
            "--compact", str(tmp_path / "none.json")]
    assert harness_sync.main(base) == 0
    assert not (gem / "skills" / "loop").exists()
    assert os.readlink(gem / "skills" / "myalias") == str(src / "foo")
    assert os.readlink(gem / "skills" / "foo") == str(alt / "foo")

    mcp = gem / "mcp_config.json"
    data = json.loads(mcp.read_text())
    data["mcpServers"]["a"]["disabled"] = True
    mcp.write_text(json.dumps(data))
    mcp.chmod(0o600)
    assert harness_sync.main(base) == 0
    assert json.loads(mcp.read_text())["mcpServers"]["a"]["disabled"] is True
    assert mcp.stat().st_mode & 0o777 == 0o600
    assert harness_sync.main(base + ["--check"]) == 0


# --- second review (2026-11-13) ---

def test_http_connection_dropped_mid_body_fails_fast(tmp_path, http_server):
    msgs = CALLS[:2] + [{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "partial"}}]
    _, out, _ = run_bridge(tmp_path, f'[mcp_servers.web]\nurl = "{http_server}"\n', "web", msgs)
    assert out[5]["error"]["message"].startswith("upstream unreachable")


def test_http_failed_reinitialize_is_retried_later(tmp_path, http_server):
    env = bridge_env(tmp_path, f'[mcp_servers.web]\nurl = "{http_server}"\n')
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "web"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)

    def call(i):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "read_x"}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    for m in CALLS[:2]:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["id"] == 1
    FakeHttp.valid_session = None
    FakeHttp.init_fail_left = 100  # server still starting: initialize fails through all retries
    assert "error" in call(3)
    FakeHttp.init_fail_left = 0  # server is up again
    assert call(4)["result"]["content"][0]["text"] == "ok"
    proc.stdin.close()
    proc.wait(timeout=10)


def test_stdio_non_utf8_output_does_not_kill_reader(tmp_path):
    body = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    sys.stdout.buffer.write(b"\xff\xfe\n"); sys.stdout.flush()
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {}}), flush=True)
"""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    _, out, _ = run_bridge(tmp_path, fake_toml(tmp_path, body), "fake", msgs)
    assert set(out) == {1, 2}


def test_non_string_tool_names_are_dropped(tmp_path):
    body = r"""
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": ["x"]}, {"name": {}}, {"name": "read_x"}]}}), flush=True)
"""
    _, out, _ = run_bridge(tmp_path, fake_toml(tmp_path, body), "fake", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    assert out[1]["result"]["tools"] == [{"name": "read_x"}]


def test_grandchildren_are_reaped(tmp_path):
    marker = tmp_path / "grandchild.pid"
    body = f"""
import subprocess, sys
p = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open({str(marker)!r}, "w").write(str(p.pid))
sys.stdin.read()
"""
    run_bridge(tmp_path, fake_toml(tmp_path, body), "fake", [])
    pid = int(marker.read_text())
    import time
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    raise AssertionError("grandchild survived the bridge")


# --- review item 9: notifications must not block the client's input ---

def test_http_slow_notification_does_not_block_requests(tmp_path, http_server):
    import time
    env = bridge_env(tmp_path, f'[mcp_servers.web]\nurl = "{http_server}"\n')
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "web"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)
    for m in CALLS[:2]:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["id"] == 1
    t0 = time.monotonic()
    for m in ({"jsonrpc": "2.0", "method": "notifications/slow"},
              {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}},
              {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_x"}}):
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["id"] == 3
    assert time.monotonic() - t0 < 1.5
    proc.stdin.close()
    proc.wait(timeout=10)
    notes = [s[0] for s in FakeHttp.seen if s[0] and s[0].startswith("notifications/")]
    assert notes == ["notifications/initialized", "notifications/slow", "notifications/cancelled"]  # order kept, drained on exit


# --- third review (2026-11-13) ---

def test_disabled_tools_are_blocked_and_hidden(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text(ECHO_ALL)
    toml = f'[mcp_servers.fake]\ncommand = "{sys.executable}"\nargs = ["{fake}"]\ndisabled_tools = ["send_x"]\n'
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "send_x"}}]
    p, out, _ = run_bridge(tmp_path, toml, "fake", msgs)
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["read_x"]
    assert "not enabled" in out[2]["error"]["message"]
    assert all(s["params"]["tool"] != "send_x" for s in map(json.loads, p.stdout.splitlines())
               if s.get("method") == "notifications/message")


def test_unknown_server_keys_fail_closed(tmp_path):
    p, out, _ = run_bridge(tmp_path, fake_toml(tmp_path, extra='tool_filter = "x"\n'), "fake",
                           [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert p.returncode != 0 and "tool_filter" in p.stderr and not out


def test_http_bearer_and_env_headers(tmp_path, http_server):
    toml = (f'[mcp_servers.web]\nurl = "{http_server}"\nbearer_token_env_var = "WEB_TOKEN"\n'
            '[mcp_servers.web.env_http_headers]\nX-Extra = "WEB_EXTRA"\n')
    os.environ.update(WEB_TOKEN="tok2", WEB_EXTRA="e")
    try:
        _, out, _ = run_bridge(tmp_path, toml, "web", CALLS[:3])
    finally:
        del os.environ["WEB_TOKEN"], os.environ["WEB_EXTRA"]
    assert FakeHttp.seen[0][2] == "Bearer tok2"
    assert [t["name"] for t in out[2]["result"]["tools"]] == ["read_x", "send_x", "boom"]


def test_stdio_tool_timeout_answers_and_drops_late_reply(tmp_path):
    body = r"""
import json, sys, time
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "tools/call":
        time.sleep(1.5)
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"late": m.get("method") == "tools/call"}}), flush=True)
"""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_x"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    p, _, _ = run_bridge(tmp_path, fake_toml(tmp_path, body, "tool_timeout_sec = 0.3\n"), "fake", msgs)
    replies = [m for m in map(json.loads, p.stdout.splitlines()) if m.get("id") == 1]
    assert len(replies) == 1 and "timed out" in replies[0]["error"]["message"]


def test_sigterm_reaps_stdio_server(tmp_path):
    import signal
    import time
    marker = tmp_path / "server.pid"
    body = f"""
import os, signal, sys, time
signal.signal(signal.SIGHUP, signal.SIG_IGN)
open({str(marker)!r}, "w").write(str(os.getpid()))
sys.stdin.read()
time.sleep(60)  # lingers after EOF
"""
    env = bridge_env(tmp_path, fake_toml(tmp_path, body))
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "fake"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.05)
    pid = int(marker.read_text())
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    raise AssertionError("stdio server survived SIGTERM of the bridge")


# --- focused re-review (2026-11-13) ---

def test_bad_filter_types_fail_closed(tmp_path):
    for extra in ('disabled_tools = "send_x"\n', 'tool_timeout_sec = "soon"\n'):
        fake = tmp_path / "fake.py"
        fake.write_text(FAKE_STDIO)
        toml = f'[mcp_servers.fake]\ncommand = "{sys.executable}"\nargs = ["{fake}"]\n{extra}'
        p, out, _ = run_bridge(tmp_path, toml, "fake", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        assert p.returncode != 0 and not out, extra


def test_http_sigterm_closes_session(tmp_path, http_server):
    import signal
    env = bridge_env(tmp_path, f'[mcp_servers.web]\nurl = "{http_server}"\n')
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "web"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)
    for m in CALLS[:2]:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["id"] == 1
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)
    assert ("DELETE", "sess-1") in [(s[0], s[1]) for s in FakeHttp.seen]


def test_second_signal_does_not_abort_cleanup(tmp_path):
    import signal
    import time
    marker = tmp_path / "server.pid"
    body = f"""
import os, signal, sys, time
signal.signal(signal.SIGHUP, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)  # stubborn: only SIGKILL stops it
open({str(marker)!r}, "w").write(str(os.getpid()))
sys.stdin.read()
time.sleep(60)
"""
    env = bridge_env(tmp_path, fake_toml(tmp_path, body))
    proc = subprocess.Popen([sys.executable, str(BRIDGE), "fake"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.05)
    pid = int(marker.read_text())
    proc.send_signal(signal.SIGTERM)
    time.sleep(1)  # bridge is now inside proc.wait(timeout=5)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)
    raise AssertionError("server survived a double SIGTERM of the bridge")
