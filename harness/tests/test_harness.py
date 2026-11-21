import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
BRIDGE = HERE / "mcp_bridge.py"
sys.path.insert(0, str(HERE))
import harness_sync  # noqa: E402

FAKE_STDIO = r'''
import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "read_x"}, {"name": "send_x"}]}
    elif m["method"] == "tools/call":
        r = {"content": [{"type": "text", "text": "env=" + os.environ.get("FAKE_SECRET", "-")}]}
    else:
        r = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


def run_bridge(tmp_path, toml, server, messages, timeout=20):
    cfg = tmp_path / "config.toml"
    cfg.write_text(toml)
    env = {**os.environ, "HARNESS_CODEX_CONFIG": str(cfg), "HARNESS_MCP_LOG": str(tmp_path / "log.jsonl"), "HARNESS_MCP_CACHE_DIR": str(tmp_path / "cache"),
           "HARNESS_MCP_RETRY_DELAYS": "0.01,0.01"}
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    p = subprocess.run([sys.executable, str(BRIDGE), server], input=stdin, capture_output=True,
                       text=True, env=env, timeout=timeout)
    out = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    log = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()] \
        if (tmp_path / "log.jsonl").exists() else []
    return p, {m["id"]: m for m in out if "id" in m}, log


CALLS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_x", "arguments": {"q": "secret-arg"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "send_x", "arguments": {}}},
]


def test_stdio_env_from_codex_config_and_tool_filter(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE_STDIO)
    toml = f'''[mcp_servers.fake]
command = "{sys.executable}"
args = ["{fake}"]
enabled_tools = ["read_x"]
[mcp_servers.fake.env]
FAKE_SECRET = "s3cr3t"
'''
    p, out, log = run_bridge(tmp_path, toml, "fake", CALLS)
    assert p.returncode == 0, p.stderr
    assert [t["name"] for t in out[2]["result"]["tools"]] == ["read_x"]
    assert out[3]["result"]["content"][0]["text"] == "env=s3cr3t"
    assert "not enabled" in out[4]["error"]["message"]
    text = json.dumps(log)
    assert "secret-arg" not in text and "s3cr3t" not in text
    assert {"event": "blocked", "tool": "send_x"}.items() <= next(r for r in log if r["event"] == "blocked").items()
    assert any(r.get("tool") == "read_x" and r.get("status") == "ok" for r in log)


def test_stdio_child_crash_fails_pending(tmp_path):
    toml = f'''[mcp_servers.dead]
command = "{sys.executable}"
args = ["-c", "import sys; sys.stdin.readline(); sys.exit(3)"]
'''
    p, out, log = run_bridge(tmp_path, toml, "dead", CALLS[:1])
    assert out[1]["error"]["message"] == "MCP server process exited"
    assert any(r["event"] == "stopped" and r["exit_code"] == 3 for r in log)


def test_unknown_or_disabled_server(tmp_path):
    toml = '[mcp_servers.off]\ncommand = "true"\nenabled = false\n'
    p, _, _ = run_bridge(tmp_path, toml, "off", [])
    assert p.returncode != 0 and "disabled" in p.stderr
    p, _, _ = run_bridge(tmp_path, toml, "missing", [])
    assert p.returncode != 0 and "not found" in p.stderr


class FakeHttp(BaseHTTPRequestHandler):
    seen: list = []
    flaky_left = 0
    sessions = 0
    valid_session = None
    init_fail_left = 0

    def log_message(self, *a):
        pass

    def do_DELETE(self):
        FakeHttp.seen.append(("DELETE", self.headers.get("Mcp-Session-Id"), None))
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHttp.seen.append((msg.get("method"), self.headers.get("Mcp-Session-Id"), self.headers.get("Authorization")))
        if msg.get("method") == "initialize" and FakeHttp.init_fail_left > 0:
            FakeHttp.init_fail_left -= 1
            self.send_response(503)
            self.end_headers()
            return
        sid = self.headers.get("Mcp-Session-Id")
        if msg.get("method") != "initialize" and sid != FakeHttp.valid_session:
            self.send_response(404)
            self.end_headers()
            return
        if "id" not in msg:
            if msg.get("method") == "notifications/slow":  # e.g. a notification stuck in 429/503 retries
                import time
                time.sleep(2)
            self.send_response(202)
            self.end_headers()
            return
        if msg["method"] == "tools/call" and msg["params"]["name"] == "flaky" and FakeHttp.flaky_left > 0:
            FakeHttp.flaky_left -= 1
            self.send_response(429)
            self.end_headers()
            return
        if msg["method"] == "tools/call" and msg["params"]["name"] == "drop":  # SSE closed without a response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            return
        if msg["method"] == "tools/call" and msg["params"]["name"] == "partial":  # connection dies mid-body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"jsonrpc": "2.0"')
            self.wfile.flush()
            self.close_connection = True
            return
        if msg["method"] == "tools/call" and msg["params"]["name"] == "redirect":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()
            return
        if msg["method"] == "tools/call" and msg["params"]["name"] == "boom":
            self.send_response(500)
            self.end_headers()
            return
        result = {"initialize": {"protocolVersion": "2025-03-26", "capabilities": {}},
                  "tools/list": {"tools": [{"name": "read_x"}, {"name": "send_x"}, {"name": "boom"}]}}.get(
            msg["method"], {"content": [{"type": "text", "text": "ok"}]})
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        self.send_response(200)
        if msg["method"] == "initialize":
            FakeHttp.sessions += 1
            FakeHttp.valid_session = f"sess-{FakeHttp.sessions}"
            self.send_header("Mcp-Session-Id", FakeHttp.valid_session)
        if msg["method"] == "tools/list":  # exercise the SSE path
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {body}\n\n".encode())
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())


@pytest.fixture
def http_server():
    FakeHttp.seen, FakeHttp.sessions, FakeHttp.valid_session, FakeHttp.init_fail_left = [], 0, None, 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHttp)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


def test_http_session_headers_sse_filter_and_upstream_error(tmp_path, http_server):
    toml = f'''[mcp_servers.web]
url = "{http_server}"
enabled_tools = ["read_x", "boom"]
[mcp_servers.web.http_headers]
Authorization = "Bearer tok"
'''
    msgs = CALLS + [{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "boom"}}]
    p, out, log = run_bridge(tmp_path, toml, "web", msgs)
    assert p.returncode == 0, p.stderr
    assert [t["name"] for t in out[2]["result"]["tools"]] == ["read_x", "boom"]
    assert out[3]["result"]["content"][0]["text"] == "ok"
    assert "not enabled" in out[4]["error"]["message"]
    assert out[5]["error"]["message"] == "upstream HTTP 500"
    assert FakeHttp.seen[0] == ("initialize", None, "Bearer tok")
    assert all(sid == "sess-1" for m, sid, _ in FakeHttp.seen[1:])
    assert sum(m == "tools/call" for m, *_ in FakeHttp.seen) == 2  # send_x never reached upstream
    assert "Bearer tok" not in json.dumps(log)


@pytest.mark.parametrize("failures,ok", [(2, True), (3, False)])
def test_http_retries_rate_limit(tmp_path, http_server, failures, ok):
    FakeHttp.flaky_left = failures
    toml = f'[mcp_servers.web]\nurl = "{http_server}"\n'
    msgs = CALLS[:2] + [{"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "flaky"}}]
    p, out, log = run_bridge(tmp_path, toml, "web", msgs)
    assert ("result" in out[9]) is ok
    if not ok:
        assert out[9]["error"]["message"] == "upstream HTTP 429"
    assert sum(r["event"] == "retry" for r in log) == min(failures, 2)


def test_http_unreachable(tmp_path):
    toml = '[mcp_servers.web]\nurl = "http://127.0.0.1:9/mcp"\n'
    p, out, log = run_bridge(tmp_path, toml, "web", CALLS[:1])
    assert out[1]["error"]["message"].startswith("upstream unreachable")
    assert any(r["event"] == "transport_error" for r in log)


def make_skill(root, name):
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")


def test_sync_skills_and_mcp(tmp_path):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    for n in ("alpha", "beta", "off"):
        make_skill(src, n)
    (src / "_backup").mkdir()
    (src / "nofile").mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'''[[skills.config]]
path = "{src}/off/SKILL.md"
enabled = false
[mcp_servers.a]
command = "x"
[mcp_servers.b]
url = "http://h"
[mcp_servers.c]
command = "y"
enabled = false
''')
    (gem / "skills").mkdir(parents=True)
    make_skill(gem / "skills", "beta")  # user's own real dir must survive
    (gem / "mcp_config.json").write_text(json.dumps({"mcpServers": {"mine": {"command": "z"}}}))
    base = ["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem), "--python", "py",
            "--compact", str(tmp_path / "none.json"), "--no-auto-compact"]

    assert harness_sync.main(base + ["--check"]) == 1
    assert not (gem / "skills" / "alpha").exists()  # check mode changes nothing
    assert harness_sync.main(base) == 0
    assert os.readlink(gem / "skills" / "alpha") == str(src / "alpha")
    assert not (gem / "skills" / "beta").is_symlink()
    assert not (gem / "skills" / "off").exists() and not (gem / "skills" / "_backup").exists()
    servers = json.loads((gem / "mcp_config.json").read_text())["mcpServers"]
    assert set(servers) == {"mine", "a", "b"}
    assert servers["a"] == {"command": "py", "args": [str(harness_sync.BRIDGE), "a"]}
    assert harness_sync.main(base + ["--check"]) == 0  # idempotent

    # source skill removed and server dropped -> managed entries go, foreign ones stay
    (src / "alpha" / "SKILL.md").unlink()
    cfg.write_text(cfg.read_text().replace('[mcp_servers.b]\nurl = "http://h"\n', ""))
    assert harness_sync.main(base) == 0
    assert not (gem / "skills" / "alpha").is_symlink()
    assert set(json.loads((gem / "mcp_config.json").read_text())["mcpServers"]) == {"mine", "a"}


def test_compact_skill_cards(tmp_path):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    for n in ("alpha", "beta", "gamma"):
        make_skill(src, n)
    (gem / "skills").mkdir(parents=True)
    make_skill(gem / "skills", "gamma")  # user's own dir: never replaced by a card
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    compact = tmp_path / "compact.json"
    compact.write_text(json.dumps({"alpha": 'Короткое: «x» "q" \\ y', "gamma": "g", "ghost": "z"}, ensure_ascii=False))
    base = ["--no-auto-compact", "--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem),
            "--python", "py", "--compact", str(compact)]

    assert harness_sync.main(base[:-1] + [str(tmp_path / "none.json")]) == 0  # no file: plain symlinks
    assert (gem / "skills" / "alpha").is_symlink()
    assert harness_sync.main(base + ["--check"]) == 1
    assert harness_sync.main(base) == 0
    card = gem / "skills" / "alpha"
    assert card.is_dir() and not card.is_symlink() and [p.name for p in card.iterdir()] == ["SKILL.md"]
    text = (card / "SKILL.md").read_text()
    import yaml
    fm = yaml.safe_load(text.split("---")[1])
    assert fm == {"name": "alpha", "description": 'Короткое: «x» "q" \\ y'}
    assert str(src / "alpha" / "SKILL.md") in text
    assert os.readlink(gem / "skills" / "beta") == str(src / "beta")
    assert (gem / "skills" / "gamma" / "SKILL.md").read_text() == "---\nname: gamma\n---\n"
    assert not (gem / "skills" / "ghost").exists()
    assert harness_sync.main(base + ["--check"]) == 0  # idempotent

    compact.write_text(json.dumps({"alpha": "новое"}, ensure_ascii=False))
    assert harness_sync.main(base + ["--check"]) == 1  # changed description is drift
    assert harness_sync.main(base) == 0
    assert "новое" in (card / "SKILL.md").read_text()

    compact.write_text("{}")  # card dropped -> back to the symlink
    assert harness_sync.main(base) == 0
    assert os.readlink(gem / "skills" / "alpha") == str(src / "alpha")

    compact.write_text(json.dumps({"alpha": "a"}))
    assert harness_sync.main(base) == 0 and card.is_dir() and not card.is_symlink()
    (src / "alpha" / "SKILL.md").unlink()  # source skill gone -> card goes too
    assert harness_sync.main(base) == 0
    assert not card.exists()

    (card).mkdir()
    (card / "SKILL.md").write_text("<!-- managed by harness_sync: compact card -->")
    (card / "notes.txt").write_text("user file")  # card with a foreign file is not ours to delete
    assert harness_sync.main(base) == 0
    assert (card / "notes.txt").exists()


@pytest.mark.parametrize("bad", ['["a"]', '{"a": 1}', '{"a": ""}', '{"a": "x\\ny"}', "not json",
                                 '{"a": "' + "я" * 151 + '"}'])
def test_compact_file_must_be_valid(tmp_path, bad):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    make_skill(src, "a")
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    compact = tmp_path / "compact.json"
    compact.write_text(bad)
    with pytest.raises(SystemExit, match="compact"):
        harness_sync.main(["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem),
                           "--python", "py", "--compact", str(compact)])
    assert not (gem / "skills").exists()  # nothing applied on a bad file



def test_sync_prompt_hooks_from_codex(tmp_path):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    make_skill(src, "a")
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    codex_hooks = tmp_path / "hooks.json"
    codex_hooks.write_text(json.dumps({"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "start"}]}],
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "kb", "timeout": 3, "statusMessage": "x"},
                       {"type": "prompt", "prompt": "not a command"}]},
            {"matcher": "x", "hooks": [{"type": "command", "command": "second"}]},
        ]}}))
    base = ["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem), "--python", "py",
            "--compact", str(tmp_path / "none.json"), "--codex-hooks", str(codex_hooks)]
    target = gem / "prompt_hooks.json"

    assert harness_sync.main(base + ["--check"]) == 1 and not target.exists()
    assert harness_sync.main(base) == 0
    assert json.loads(target.read_text()) == {"_managed_by": "harness_sync", "UserPromptSubmit": [
        {"command": "kb", "timeout": 3}, {"command": "second", "timeout": 3}]}
    assert harness_sync.main(base + ["--check"]) == 0

    codex_hooks.write_text(json.dumps({"hooks": {}}))  # hook removed in Codex -> removed for Gemini
    assert harness_sync.main(base) == 0
    assert json.loads(target.read_text())["UserPromptSubmit"] == []

    target.write_text(json.dumps({"UserPromptSubmit": [{"command": "mine"}]}))  # user's own file: kept
    codex_hooks.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "kb"}]}]}}))
    assert harness_sync.main(base) == 0
    assert json.loads(target.read_text()) == {"UserPromptSubmit": [{"command": "mine"}]}

    target.unlink()
    codex_hooks.write_text("{broken")  # unreadable Codex hooks: fail closed, write nothing
    with pytest.raises(SystemExit, match="hooks"):
        harness_sync.main(base)
    assert not target.exists()


def test_cards_never_claim_user_dirs(tmp_path):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    for n in ("alpha", "beta"):
        make_skill(src, n)
    skills = gem / "skills"
    (skills / "alpha").mkdir(parents=True)  # user's empty dir under a carded name
    (skills / "gone").mkdir()  # user's empty dir under an unknown name
    (skills / "quote").mkdir()  # user's skill that merely quotes the marker
    (skills / "quote" / "SKILL.md").write_text(f"---\nname: quote\n---\nsee {harness_sync.CARD_MARK}\n")
    (skills / "latin1").mkdir()
    (skills / "latin1" / "SKILL.md").write_bytes(b"\xff\xfe broken \xe9")
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    compact = tmp_path / "compact.json"
    compact.write_text(json.dumps({"alpha": "a", "beta": "b"}))
    base = ["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem), "--python", "py",
            "--compact", str(compact), "--codex-hooks", str(tmp_path / "none.json")]
    assert harness_sync.main(base) == 0
    assert list((skills / "alpha").iterdir()) == []  # not filled
    assert (skills / "gone").is_dir()  # not removed
    assert (skills / "quote" / "SKILL.md").exists() and (skills / "latin1" / "SKILL.md").exists()
    assert harness_sync.is_card(skills / "beta")
    assert not list(skills.glob(".*"))  # no temp leftovers


def test_bad_mcp_config_applies_nothing(tmp_path):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    make_skill(src, "alpha")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.a]\ncommand = "x"\n')
    gem.mkdir()
    (gem / "mcp_config.json").write_text("{broken")
    with pytest.raises(SystemExit, match="mcp_config"):
        harness_sync.main(["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem),
                           "--python", "py", "--compact", str(tmp_path / "none.json"),
                           "--codex-hooks", str(tmp_path / "none.json")])
    assert not (gem / "skills").exists() and not (gem / "prompt_hooks.json").exists()


@pytest.mark.parametrize("bad", ['{"mcpServers": []}', '{"mcpServers": {"a": "x"}}'])
def test_malformed_mcp_config_shapes_exit_cleanly(tmp_path, bad):
    src, gem = tmp_path / "skills", tmp_path / "gemini"
    make_skill(src, "alpha")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.a]\ncommand = "x"\n')
    gem.mkdir()
    (gem / "mcp_config.json").write_text(bad)
    with pytest.raises(SystemExit, match="mcp_config"):
        harness_sync.main(["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem),
                           "--python", "py", "--compact", str(tmp_path / "none.json"),
                           "--codex-hooks", str(tmp_path / "none.json")])


def test_claude_gets_main_skills_and_mac_stubs(tmp_path):
    src, gem, claude = tmp_path / "skills", tmp_path / "gemini", tmp_path / "claude"
    for n in ("alpha", "beta", "tracker", "other"):
        (src / n).mkdir(parents=True)
        (src / n / "SKILL.md").write_text(f'---\nname: {n}\ndescription: "Про {n}: «x» и \\"q\\""\n---\nbody\n')
    claude.mkdir()
    (claude / "beta").mkdir()  # Claude's own copy: kept
    (claude / "beta" / "SKILL.md").write_text("---\nname: beta\n---\nmine\n")
    (claude / "keepme").symlink_to(src / "other")  # someone else's link: never removed
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    main = tmp_path / "main.json"
    main.write_text(json.dumps({"main": ["alpha", "beta", "tracker", "ghost"], "mac_only": ["tracker"]}))
    compact = tmp_path / "compact.json"
    compact.write_text(json.dumps({"tracker": "Трекер"}))
    base = ["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem), "--python", "py",
            "--compact", str(compact), "--codex-hooks", str(tmp_path / "none.json"),
            "--main", str(main), "--claude-skills", str(claude)]
    assert harness_sync.main(base + ["--check"]) == 1 and not (claude / "alpha").exists()
    assert harness_sync.main(base) == 0
    assert os.readlink(claude / "alpha") == str(src / "alpha")
    assert (claude / "beta" / "SKILL.md").read_text().endswith("mine\n")
    import yaml
    stub = (claude / "tracker" / "SKILL.md").read_text()
    assert yaml.safe_load(stub.split("---")[1]) == {"name": "tracker", "description": 'Про tracker: «x» и "q"'}
    assert "mac-bridge" in stub and str(src / "tracker" / "SKILL.md") in stub
    assert "mac-bridge" in (gem / "skills" / "tracker" / "SKILL.md").read_text()  # Gemini card says so too
    assert not (claude / "ghost").exists()
    assert harness_sync.main(base + ["--check"]) == 0

    main.write_text(json.dumps({"main": ["beta"], "mac_only": []}))  # dropped from main -> our entries go
    assert harness_sync.main(base) == 0
    assert not (claude / "alpha").exists() and not (claude / "tracker").exists()
    assert (claude / "keepme").is_symlink() and (claude / "beta").is_dir()


def _claude_setup(tmp_path, main, mac):
    src, gem, claude = tmp_path / "skills", tmp_path / "gemini", tmp_path / "claude"
    for n in ("a", "b", "c"):
        if not (src / n).exists():
            (src / n).mkdir(parents=True)
            (src / n / "SKILL.md").write_text(f"---\nname: {n}\ndescription: про {n} --- и дальше\n---\nbody\n")
    claude.mkdir(exist_ok=True)
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    (tmp_path / "main.json").write_text(json.dumps({"main": main, "mac_only": mac}))
    return claude, ["--codex-config", str(cfg), "--skills-src", str(src), "--gemini-config", str(gem),
                    "--python", "py", "--compact", str(tmp_path / "none.json"),
                    "--codex-hooks", str(tmp_path / "none.json"), "--main", str(tmp_path / "main.json"),
                    "--claude-skills", str(claude)]


def test_claude_entries_switch_type_with_mac_only(tmp_path):
    claude, base = _claude_setup(tmp_path, ["a", "b"], ["b"])
    assert harness_sync.main(base) == 0
    assert (claude / "a").is_symlink() and harness_sync.is_card(claude / "b")
    assert "про b --- и дальше" in (claude / "b" / "SKILL.md").read_text()  # not cut at '---'
    claude, base = _claude_setup(tmp_path, ["a", "b"], ["a"])
    assert harness_sync.main(base + ["--check"]) == 1
    assert harness_sync.main(base) == 0
    assert harness_sync.is_card(claude / "a") and (claude / "b").is_symlink()
    assert harness_sync.main(base + ["--check"]) == 0


def test_claude_crash_leftovers_stay_managed(tmp_path):
    claude, base = _claude_setup(tmp_path, ["a", "c"], ["c"])
    # as if a previous run died after creating entries but before its final manifest write
    (claude / "a").symlink_to(tmp_path / "skills" / "a")
    (claude / "c").mkdir()  # an empty dir left by an interrupted stub
    (claude / ".harness_sync.json").write_text(json.dumps({"managed": ["a", "c"]}))
    assert harness_sync.main(base) == 0
    assert harness_sync.is_card(claude / "c")
    claude, base = _claude_setup(tmp_path, ["b"], [])
    assert harness_sync.main(base) == 0
    assert not (claude / "a").exists() and not (claude / "c").exists()


@pytest.mark.parametrize("managed", [["../outside/z"], [str(Path("/tmp/x"))], [1, {"x": 1}], "ab", [".hidden"]])
def test_claude_manifest_cannot_reach_outside(tmp_path, managed):
    claude, base = _claude_setup(tmp_path, ["a"], [])
    outside = tmp_path / "outside" / "z"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(harness_sync.card_text("z", tmp_path / "skills" / "a", "d"))
    (claude / ".harness_sync.json").write_text(json.dumps({"managed": managed}))
    assert harness_sync.main(base) == 0
    assert outside.exists()


def test_claude_manifest_drift_is_reported_by_check(tmp_path):
    claude, base = _claude_setup(tmp_path, ["a"], [])
    assert harness_sync.main(base) == 0
    (claude / ".harness_sync.json").write_text(json.dumps({"managed": ["a", "gone"]}))
    assert harness_sync.main(base + ["--check"]) == 1  # a run would shrink the manifest
    assert harness_sync.main(base) == 0
    assert json.loads((claude / ".harness_sync.json").read_text())["managed"] == ["a"]
    assert harness_sync.main(base + ["--check"]) == 0


def test_claude_stub_needs_a_description(tmp_path):
    claude, base = _claude_setup(tmp_path, ["a"], ["a"])
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("---\nname: a\ndescription: [1, 2]\n---\n")
    assert harness_sync.main(base) == 0
    text = (claude / "a" / "SKILL.md").read_text()
    assert 'description: "[1, 2]"' in text


def test_claude_mac_only_without_description_drops_the_full_link(tmp_path):
    claude, base = _claude_setup(tmp_path, ["a"], [])
    assert harness_sync.main(base) == 0 and (claude / "a").is_symlink()
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("---\nname: a\n---\nbody\n")
    claude, base = _claude_setup(tmp_path, ["a"], ["a"])
    assert harness_sync.main(base) == 0
    assert not (claude / "a").exists()  # no Mac warning possible: better no entry than the raw instructions
