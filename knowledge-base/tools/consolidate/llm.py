"""Вызов `claude -p` без инструментов, MCP, настроек и сохранения сессий. Промпт — через stdin
(variadic `--disallowedTools` съел бы позиционный аргумент)."""
import json
import os
import shutil
import subprocess
import time

DISALLOWED = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task"
CANDIDATES = [os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude"]

LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "limit reached", "limit will reset",
                 "out of extra usage", "hit your limit", "5-hour limit", "weekly limit",
                 "overloaded_error", "too many requests", "status 429", "error 429")


MAX_OUT_TOKENS = 64000


class LimitHit(Exception):
    """Лимит подписки/скорости: прогон останавливается, ledger позволит продолжить."""


class LLMError(Exception):
    pass


def claude_bin():
    b = os.environ.get("KB_CLAUDE_BIN")
    if b:
        return b
    return shutil.which("claude") or next((p for p in CANDIDATES if os.access(p, os.X_OK)), "claude")


def build_cmd(model):
    return [claude_bin(), "-p", "--no-session-persistence", "--model", model,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--output-format", "json", "--tools", "", "--disallowedTools", DISALLOWED]


def _looks_limit(text):
    t = (text or "").lower()
    return any(m in t for m in LIMIT_MARKERS)


def run(prompt, model, timeout, cwd):
    """→ dict(result, tokens_in, tokens_out, cost_usd, duration_s). LimitHit / LLMError при сбое.
    Потолок выхода — MAX_OUT_TOKENS (норма s1-v2 даёт длинные ответы), если не задан в окружении."""
    t = time.time()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"))}
    if not env.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(MAX_OUT_TOKENS)
    try:
        p = subprocess.run(build_cmd(model), input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd, env=env,
                           preexec_fn=lambda: os.nice(10))
    except subprocess.TimeoutExpired:
        raise LLMError(f"timeout {timeout}s")
    except OSError as e:
        raise LLMError(f"exec: {e}")
    dur = round(time.time() - t, 2)
    out = p.stdout or ""
    data = None
    try:
        data = json.loads(out)
    except ValueError:
        # иногда перед JSON бывают строки-предупреждения: берём последнюю JSON-строку
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
    if data is None:
        msg = (p.stderr or out or "").strip()[:300]
        if _looks_limit(msg):
            raise LimitHit(msg)
        raise LLMError(f"rc={p.returncode}: нет JSON: {msg}")
    result = data.get("result") or ""
    if data.get("is_error") or p.returncode != 0:
        msg = (str(result) or p.stderr or "")[:300]
        if _looks_limit(msg) or _looks_limit(p.stderr) or data.get("subtype") == "error_rate_limit":
            raise LimitHit(msg)
        raise LLMError(f"rc={p.returncode}: {msg}")
    u = data.get("usage") or {}
    tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                           "cache_read_input_tokens"))
    return {"result": result, "tokens_in": tin, "tokens_out": int(u.get("output_tokens") or 0),
            "stop_reason": data.get("stop_reason"),
            "cost_usd": data.get("total_cost_usd"), "duration_s": dur}
