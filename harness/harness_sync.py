#!/usr/bin/env python3
"""Give Gemini (Antigravity in bb) the same skills and MCP servers as Codex.

Sources of truth stay where they are:
  * skills      — ~/.agents/skills (minus skills disabled in Codex [skills].config);
  * MCP servers — [mcp_servers] in ~/.codex/config.toml (minus enabled = false).

Targets (Antigravity global customizations, verified 2026-11-13):
  * ~/.gemini/config/skills/<name> -> compact card pointing at the full source;
  * ~/.gemini/config/mcp_config.json -> one entry per server, each launching
    mcp_bridge.py <name>, so no secret is copied out of the Codex config.

Entries not created by this script (real skill dirs, foreign symlinks, MCP servers
that do not use the bridge) are left untouched. `--check` reports drift and exits 1.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

HOME = Path.home()
BRIDGE = Path(__file__).resolve().with_name("mcp_bridge.py")
MAIN = Path(__file__).resolve().with_name("main_skills.json")
COMPACT = Path(__file__).resolve().with_name("gemini_skill_descriptions.json")
CARD_MARK = "<!-- managed by harness_sync: compact card -->"
# Antigravity fills its skill list shortest-first up to ~44 KB of description bytes;
# cards stay well under the cutoff so they are always listed.
CARD_MAX_BYTES = 300
CATALOG_DESCRIPTION_BUDGET_BYTES = 32000  # leave room below Antigravity's ~44 KB cutoff


def desired_skills(skills_src: Path, codex_cfg: dict) -> dict[str, Path]:
    # Codex disables by SKILL.md path, which may point at another copy of the same skill: match by name too.
    off = [Path(e["path"]).parent for e in codex_cfg.get("skills", {}).get("config", [])
           if e.get("enabled") is False and e.get("path")]
    off_names, off_dirs = {d.name for d in off}, {d.resolve() for d in off}
    out = {}
    for d in sorted(skills_src.iterdir()):
        if d.name.startswith((".", "_")) or not (d / "SKILL.md").is_file():
            continue
        if d.name in off_names or d.resolve() in off_dirs:
            continue
        out[d.name] = d
    return out


def load_compact(path: Path | None) -> dict[str, str]:
    """Short Gemini-only descriptions: Antigravity lists skills shortest-description-first
    until a context budget runs out, so long (mostly Russian) descriptions drop out."""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"compact descriptions {path}: invalid JSON: {e}")
    if not isinstance(data, dict) or not all(
            isinstance(v, str) and v.strip() and "\n" not in v for v in data.values()):
        raise SystemExit(f"compact descriptions {path}: expected {{name: one-line non-empty string}}")
    long = sorted(k for k, v in data.items() if len(v.encode()) > CARD_MAX_BYTES)
    if long:
        raise SystemExit(f"compact descriptions {path}: over {CARD_MAX_BYTES} UTF-8 bytes: {', '.join(long)}")
    return data


MAC_ONLY_NOTE = (
    "Скилл работает только на вашем Маке: на текущем хосте нет нужной сети или учётных данных. "
    "Выполняй его через скилл mac-bridge: команды и скрипты из инструкции — `mac -l '<команда>'` "
    "(`-l` — логин-шелл с токенами), пути к скриптам маковские (`~/.claude/skills/{name}/...`); "
    "агент с MCP Мака — `mac-run \"задача\"`. Изменяющие действия — только после превью и явного "
    "подтверждения пользователя.\n\n")


def card_text(name: str, src: Path, description: str, mac_only: bool = False) -> str:
    # JSON strings are valid YAML double-quoted scalars, so any quote or backslash is safe.
    return (f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
            f"{CARD_MARK}\n\n" + (MAC_ONLY_NOTE.format(name=name) if mac_only else "")
            + f"Полная инструкция скилла — `{src / 'SKILL.md'}`. Прочитай её целиком и работай "
            f"по ней; относительные пути в ней считай от `{src}/`.\n")


def skill_description(src: Path) -> str:
    import yaml  # only needed for Claude stubs
    text = (src / "SKILL.md").read_text(encoding="utf-8")
    fm = None
    if text.startswith("---"):
        end = text.find("\n---", 3)  # the closing fence, not a '---' inside a value
        try:
            fm = yaml.safe_load(text[3:end] if end > 0 else "")
        except yaml.YAMLError:
            fm = None
    desc = fm.get("description") if isinstance(fm, dict) else None
    if desc is None:
        return ""
    return " ".join((desc if isinstance(desc, str) else json.dumps(desc, ensure_ascii=False)).split())


def catalog_descriptions(want: dict[str, Path], overrides: dict[str, str]) -> dict[str, str]:
    """Keep every enabled skill discoverable, including newly installed skills.

    Only discovery metadata is shortened. Each card requires reading the original
    complete instruction, whose content and supporting files stay untouched.
    """
    if not want:
        return {}
    limit = min(CARD_MAX_BYTES, CATALOG_DESCRIPTION_BUDGET_BYTES // len(want))
    if limit < 1:
        raise SystemExit("skill catalogue exceeds description budget; not touching anything")
    out = {}
    for name, source in want.items():
        description = " ".join((overrides.get(name) or skill_description(source) or
                                f"Use {name}; read the full instructions.").split())
        if len(description.encode("utf-8")) > limit:
            # Decode only a valid UTF-8 prefix and prefer a complete final word.
            prefix = description.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
            whole = prefix.rsplit(" ", 1)[0] if " " in prefix else prefix
            description = whole.strip() or prefix
        if not description:
            description = "x"  # a tiny budget must still produce valid metadata
        out[name] = description
    return out


def load_main(path: Path | None) -> tuple[list[str], set[str]]:
    """Main skills shared with Claude, and which of them only work from the Mac."""
    if path is None or not path.is_file():
        return [], set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        main, mac = data["main"], data.get("mac_only", [])
        if not (isinstance(main, list) and isinstance(mac, list)
                and all(isinstance(x, str) for x in main + mac)):
            raise ValueError("main and mac_only must be lists of names")
    except (ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"main skills {path}: {e}")
    return main, set(mac)


def _manifest_names(path: Path) -> set[str]:
    """Names from a hand-editable manifest: plain entry names only, never a path out of the dir."""
    try:
        managed = json.loads(path.read_text(encoding="utf-8")).get("managed")
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(managed, list):
        return set()
    return {n for n in managed if isinstance(n, str) and n and "/" not in n and "\\" not in n
            and not n.startswith(".")}


def _write_manifest(target: Path, names: set[str]) -> None:
    tmp = target / ".harness_sync.json.tmp"
    tmp.write_text(json.dumps({"managed": sorted(names)}, indent=1), encoding="utf-8")
    tmp.replace(target / ".harness_sync.json")


def sync_claude_skills(skills_src: Path, target: Path, codex_cfg: dict, main: list[str], mac_only: set[str],
                       apply: bool) -> list[str]:
    """Claude gets the main skills too: a symlink to the shared copy, or for Mac-only skills a
    stub that sends it through mac-bridge. Claude's own entries are never touched; only entries
    listed in the manifest (and still exactly ours) are changed or removed. The manifest is
    widened before anything is created, so a crash never leaves an entry nobody owns.
    An empty or missing main list does nothing at all (protection against a lost file)."""
    if not main or not target.is_dir():
        return []
    ours = _manifest_names(target / ".harness_sync.json")

    def is_ours(name: str) -> bool:
        p = target / name
        if p.is_symlink():
            return Path(os.readlink(p)) == skills_src / name
        try:  # an empty dir is a stub interrupted before its file landed
            return is_card(p) or (p.is_dir() and not any(p.iterdir()))
        except OSError:
            return False

    def remove(p: Path) -> None:
        if p.is_symlink():
            p.unlink()
        else:
            (p / "SKILL.md").unlink(missing_ok=True)
            p.rmdir()

    want = {n: src for n, src in desired_skills(skills_src, codex_cfg).items() if n in main}
    changes: list[str] = []
    plan: list[tuple[str, str, object]] = []  # (name, action, payload)
    for name in sorted(ours - set(want)):
        p = target / name
        if (p.exists() or p.is_symlink()) and is_ours(name):
            changes.append(f"claude -{name}")
            plan.append((name, "remove", None))
    managed = set()
    for name, src in want.items():
        p = target / name
        present = p.exists() or p.is_symlink()
        mine = name in ours and present and is_ours(name)
        if present and not mine:
            continue  # Claude's own entry, whatever it is
        managed.add(name)
        if name in mac_only:
            desc = skill_description(src)
            if not desc:  # no stub without a description; never leave the raw Mac-only instructions
                managed.discard(name)
                if mine:
                    changes.append(f"claude -{name} (no description for a stub)")
                    plan.append((name, "remove", None))
                continue
            text = card_text(name, src, desc, mac_only=True)
            if mine and not p.is_symlink() and (p / "SKILL.md").is_file() \
                    and (p / "SKILL.md").read_text(encoding="utf-8") == text:
                continue
            changes.append(f"claude +{name} (mac stub)")
            plan.append((name, "stub", text))
        else:
            if mine and p.is_symlink():
                continue
            changes.append(f"claude +{name}")
            plan.append((name, "link", src))
    if managed != ours and not any(c.startswith(("claude +", "claude -")) for c in changes):
        changes.append("claude manifest")
    if not apply:
        return changes
    _write_manifest(target, ours | managed)  # intent first: whatever gets created is already owned
    for name, action, payload in plan:
        p = target / name
        if action == "remove":
            remove(p)
        elif action == "link":
            if p.exists() or p.is_symlink():
                remove(p)
            p.symlink_to(payload)
        elif (p / "SKILL.md").is_file() and not p.is_symlink():  # update a stub: one atomic file swap
            tmp = target / f".{name}.SKILL.md.tmp"
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(p / "SKILL.md")
        else:  # new stub (or replacing a link / an empty leftover): build aside, appear in one rename
            tmp_dir = target / f".{name}.card-tmp"
            shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir()
            (tmp_dir / "SKILL.md").write_text(payload, encoding="utf-8")
            if p.exists() or p.is_symlink():
                remove(p)
            tmp_dir.rename(p)
    _write_manifest(target, managed)
    return changes


def is_card(p: Path) -> bool:
    """Exactly what card_text writes: one SKILL.md whose body starts with the marker."""
    if p.is_symlink() or not p.is_dir():
        return False
    try:
        if [c.name for c in p.iterdir()] != ["SKILL.md"]:
            return False
        text = (p / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    end = text.find("\n---\n", 4)
    return text.startswith("---\nname: ") and end > 0 and text[end + 5:].startswith(f"\n{CARD_MARK}\n")


def sync_skills(skills_src: Path, target: Path, codex_cfg: dict, apply: bool,
                compact: dict[str, str] | None = None, mac_only: set[str] | None = None) -> list[str]:
    changes: list[str] = []
    compact = compact or {}
    want = desired_skills(skills_src, codex_cfg)
    if apply:
        target.mkdir(parents=True, exist_ok=True)
    existing = {p.name: p for p in target.iterdir()} if target.is_dir() else {}
    gone: set[str] = set()  # removed (or, in check mode, would be removed)
    for name, p in existing.items():
        if p.is_symlink():
            ours = Path(os.readlink(p)) == skills_src / name  # exactly the links this script creates
            stale = name not in want or name in compact
        else:
            ours = is_card(p)
            stale = name not in want or name not in compact
        if ours and stale:
            gone.add(name)
            if name not in want:
                changes.append(f"skill -{name}")
            if apply:
                if p.is_symlink():
                    p.unlink()
                else:
                    (p / "SKILL.md").unlink(missing_ok=True)
                    p.rmdir()
    for name, src in want.items():
        p = target / name
        present = (p.exists() or p.is_symlink()) and name not in gone
        if name in compact:
            text = card_text(name, src, compact[name], name in (mac_only or ()))
            if present and not is_card(p):
                changes.append(f"skill ={name} (kept: not managed by harness_sync)")
                continue
            if present and (p / "SKILL.md").read_text(encoding="utf-8") == text:
                continue
            changes.append(f"skill +{name} (compact)")
            if apply:
                if present:  # update in place, atomically
                    tmp = target / f".{name}.SKILL.md.tmp"
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(p / "SKILL.md")
                else:  # build aside, then appear complete in one rename
                    tmp_dir = target / f".{name}.card-tmp"
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    tmp_dir.mkdir()
                    (tmp_dir / "SKILL.md").write_text(text, encoding="utf-8")
                    tmp_dir.rename(p)
            continue
        if present and p.is_symlink() and Path(os.readlink(p)) == src:
            continue
        if present:
            changes.append(f"skill ={name} (kept: not managed by harness_sync)")
            continue
        changes.append(f"skill +{name}")
        if apply:
            p.symlink_to(src)
    return changes


def desired_mcp(codex_cfg: dict, python: str) -> dict[str, dict]:
    return {name: {"command": python, "args": [str(BRIDGE), name]}
            for name, spec in codex_cfg.get("mcp_servers", {}).items()
            if spec.get("enabled") is not False}


def sync_mcp(mcp_path: Path, codex_cfg: dict, python: str, apply: bool) -> list[str]:
    current: dict = {}
    if mcp_path.is_file() and mcp_path.read_text(encoding="utf-8").strip():
        try:
            current = json.loads(mcp_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SystemExit(f"Gemini mcp_config {mcp_path}: invalid JSON, not touching anything: {e}")
        servers_in = current.get("mcpServers", {}) if isinstance(current, dict) else None
        if not isinstance(servers_in, dict) or not all(isinstance(v, dict) for v in servers_in.values()):
            raise SystemExit(f"Gemini mcp_config {mcp_path}: expected {{\"mcpServers\": {{name: object}}}}, "
                             "not touching anything")
    servers = dict(current.get("mcpServers", {}))
    want = desired_mcp(codex_cfg, python)
    changes = []
    for name, spec in list(servers.items()):
        managed = str(BRIDGE) in (spec.get("args") or [])
        if managed and name not in want:
            changes.append(f"mcp -{name}")
            del servers[name]
    for name, spec in want.items():
        old = servers.get(name)
        if old is not None and str(BRIDGE) not in (old.get("args") or []):
            changes.append(f"mcp ={name} (kept: not managed by harness_sync)")
            continue
        if old is None or any(old.get(k) != v for k, v in spec.items()):
            changes.append(f"mcp +{name}")
            servers[name] = {**(old or {}), **spec}  # keep user fields such as "disabled"
    if apply and any("(kept" not in c for c in changes):
        write_json(mcp_path, {**current, "mcpServers": servers})
    return changes


def write_json(path: Path, data: dict) -> None:
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    tmp = target.with_suffix(".json.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)  # never readable by others, even briefly
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(target)


def codex_prompt_hooks(path: Path) -> list[dict]:
    """UserPromptSubmit command hooks from Codex hooks.json, flattened for the Gemini proxy."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        groups = data.get("hooks", {}).get("UserPromptSubmit", []) or []
        out = []
        for group in groups:
            for h in group.get("hooks", []):
                if h.get("type", "command") == "command" and isinstance(h.get("command"), str) and h["command"].strip():
                    t = h.get("timeout")
                    out.append({"command": h["command"],
                                "timeout": t if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else 3})
        return out
    except (ValueError, AttributeError, TypeError) as e:
        raise SystemExit(f"Codex hooks {path}: unreadable, not syncing prompt hooks: {e}")


def sync_prompt_hooks(target: Path, hooks: list[dict], apply: bool) -> list[str]:
    """<gemini config>/prompt_hooks.json, read by the antigravity-acp proxy before each prompt."""
    want = {"_managed_by": "harness_sync", "UserPromptSubmit": hooks}
    current = None
    if target.is_file():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            current = None
        if not (isinstance(current, dict) and current.get("_managed_by") == "harness_sync"):
            return ["prompt_hooks = (kept: not managed by harness_sync)"]
    if current == want:
        return []
    if apply:
        write_json(target, want)
    return [f"prompt_hooks +{len(hooks)}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="report drift without changing anything (exit 1 on drift)")
    ap.add_argument("--codex-config", type=Path, default=HOME / ".codex/config.toml")
    ap.add_argument("--skills-src", type=Path, default=HOME / ".agents/skills")
    ap.add_argument("--gemini-config", type=Path, default=HOME / ".gemini/config")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--codex-hooks", type=Path, default=HOME / ".codex/hooks.json")
    ap.add_argument("--main", type=Path, default=MAIN, help="main skills shared with Claude")
    ap.add_argument("--claude-skills", type=Path, default=HOME / ".claude/skills")
    ap.add_argument("--compact", type=Path, default=COMPACT,
                    help="JSON {skill: short description} served to Gemini as compact cards")
    ap.add_argument("--no-auto-compact", action="store_true",
                    help="legacy mode: only use cards explicitly listed in --compact")
    args = ap.parse_args(argv)

    with open(args.codex_config, "rb") as f:
        codex_cfg = tomllib.load(f)
    compact = load_compact(args.compact)
    if not args.no_auto_compact:
        compact = catalog_descriptions(desired_skills(args.skills_src, codex_cfg), compact)
    prompt_hooks = codex_prompt_hooks(args.codex_hooks)
    main_skills, mac_only = load_main(args.main)
    apply = not args.check

    def run(apply_now: bool) -> list[str]:
        return (sync_skills(args.skills_src, args.gemini_config / "skills", codex_cfg, apply_now, compact, mac_only)
                + sync_claude_skills(args.skills_src, args.claude_skills, codex_cfg, main_skills, mac_only, apply_now)
                + sync_mcp(args.gemini_config / "mcp_config.json", codex_cfg, args.python, apply_now)
                + sync_prompt_hooks(args.gemini_config / "prompt_hooks.json", prompt_hooks, apply_now))

    changes = run(False)  # a dry pass first: any unreadable input stops us before the first write
    if apply:
        changes = run(True)
    drift = [c for c in changes if "(kept" not in c]
    for c in changes:
        print(c)
    n_skills = len(desired_skills(args.skills_src, codex_cfg))
    n_mcp = len(desired_mcp(codex_cfg, args.python))
    verb = "drift" if args.check else "applied"
    print(f"harness_sync: {verb} {len(drift)} change(s); target {n_skills} skills, {n_mcp} MCP servers")
    return 1 if args.check and drift else 0


if __name__ == "__main__":
    sys.exit(main())
