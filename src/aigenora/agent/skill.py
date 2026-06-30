"""aigenora skill subcommand: manage the SKILL.md installed for user agents.

Design notes:
- Single source of truth (SOT): the frontmatter version of the packaged `aigenora/skill/SKILL.md` matches the pip package version (validated by CI).
- Explicit install: users must specify --target (claude-code / codex / opencode) or --path.
- Auto-overwrite: when the package version is greater than the installed version, update overwrites with a backup; when the package version is <= the installed version, no special handling is applied.
- Backup: before each overwrite, the old file is backed up as SKILL.md.bak-<old_version>-<yyyymmddhhmmss>; only the most recent 3 copies are kept.
- Multi-target tracking: `~/.aigenora/skill_targets.json` records all target paths the user has installed, enabling one-click update.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

from aigenora import __version__ as PKG_VERSION


TARGET_PRESETS: dict[str, str] = {
    "claude-code": ".claude/skills/aigenora/SKILL.md",
    "codex": ".agents/skills/aigenora/SKILL.md",
    "opencode": ".opencode/skills/aigenora/SKILL.md",
}

PERSONAL_FILENAME = "PERSONAL.md"
# v018: companion appendix files shipped next to SKILL.md and installed together,
# so the main SKILL.md stays thin; the agent loads appendices on demand via the index.
APPENDIX_FILES = ("HOOKS.md", "PROTOCOL-DEV.md", "UI-DEV.md", "REFERENCE.md", "ADVANCED.md", "GAMES.md")

BACKUP_KEEP = 3
TRACKER_PATH = Path.home() / ".aigenora" / "skill_targets.json"


# ---------- Version parsing and comparison ----------

_VERSION_RE = re.compile(r"^version:\s*([^\s]+)\s*$", re.MULTILINE)


def _parse_version_tuple(v: str) -> tuple[int, ...]:
    """Laxly parse version strings like '1.2.3' / '0.1.0' into a comparable integer tuple.

    Non-numeric segments are treated as 0 instead of raising; this way CI/callers do not need an extra packaging dependency.
    """
    parts = []
    for chunk in v.strip().strip("\"'").split("."):
        m = re.match(r"^(\d+)", chunk)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


def _version_cmp(a: str, b: str) -> int:
    ta, tb = _parse_version_tuple(a), _parse_version_tuple(b)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def _extract_skill_version(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1).strip().strip("\"'") if m else None


def _read_packaged_skill() -> tuple[str, str]:
    """Read the content and version of the packaged SKILL.md."""
    res = resources.files("aigenora.skill").joinpath("SKILL.md")
    text = res.read_text(encoding="utf-8-sig")
    ver = _extract_skill_version(text)
    if not ver:
        raise RuntimeError("packaged SKILL.md missing 'version:' frontmatter")
    return text, ver


def _read_target_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    return _extract_skill_version(text)


# ---------- Target tracking ----------

def _load_tracker() -> dict:
    if not TRACKER_PATH.is_file():
        return {"targets": []}
    try:
        return json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"targets": []}


def _save_tracker(data: dict) -> None:
    TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACKER_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _remember_target(target_name: str, path: Path) -> None:
    data = _load_tracker()
    entry = {"target": target_name, "path": str(path.resolve())}
    seen = {(t["target"], t["path"]) for t in data.get("targets", [])}
    if (entry["target"], entry["path"]) not in seen:
        data.setdefault("targets", []).append(entry)
        _save_tracker(data)


def _forget_target(path: Path) -> None:
    data = _load_tracker()
    resolved = str(path.resolve())
    data["targets"] = [t for t in data.get("targets", []) if t["path"] != resolved]
    _save_tracker(data)


# ---------- Backup ----------

def _backup_path(target: Path, old_version: str | None) -> Path:
    ts = time.strftime("%Y%m%d%H%M%S")
    ver = old_version or "unknown"
    return target.with_name(f"{target.name}.bak-{ver}-{ts}")


def _backup_and_trim(target: Path, old_version: str | None) -> Path | None:
    if not target.is_file():
        return None
    backup = _backup_path(target, old_version)
    shutil.copy2(target, backup)
    # Keep only the most recent BACKUP_KEEP copies
    pattern = f"{target.name}.bak-*"
    backups = sorted(target.parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup


# ---------- Target resolution ----------

def _resolve_target(args) -> tuple[str, Path]:
    """Return (target_name, absolute_path).

    Priority: --path > --target preset. The base directory defaults to cwd and can be overridden via --base.
    """
    base = Path(args.base).resolve() if getattr(args, "base", None) else Path.cwd()
    explicit_path = getattr(args, "path", None)
    target_name = getattr(args, "target", None)

    if explicit_path:
        p = Path(explicit_path).expanduser()
        if not p.is_absolute():
            p = (base / p).resolve()
        return ("custom", p)

    if target_name:
        if target_name not in TARGET_PRESETS:
            raise SystemExit(
                f"[ERR] unknown --target: {target_name}; choices: {', '.join(TARGET_PRESETS)}"
            )
        return (target_name, (base / TARGET_PRESETS[target_name]).resolve())

    raise SystemExit("[ERR] must specify --target {claude-code|codex|opencode} or --path PATH")


# ---------- Subcommand implementations ----------

def cmd_version(args) -> int:
    pkg_text, pkg_ver = _read_packaged_skill()
    print(f"package: {PKG_VERSION}")
    print(f"skill (packaged): {pkg_ver}")
    return 0


def cmd_path(args) -> int:
    res = resources.files("aigenora.skill").joinpath("SKILL.md")
    print(str(res))
    return 0


def cmd_install(args) -> int:
    target_name, target_path = _resolve_target(args)
    pkg_text, pkg_ver = _read_packaged_skill()

    if target_path.exists() and not args.force:
        old_ver = _read_target_version(target_path)
        print(f"[SKIP] {target_path} already exists (version={old_ver}); use --force to overwrite")
        _remember_target(target_name, target_path)
        _ensure_personal_and_appendices(target_path, target_name)
        return 0

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target_path.exists():
        backup = _backup_and_trim(target_path, _read_target_version(target_path))
    target_path.write_text(pkg_text, encoding="utf-8")
    _remember_target(target_name, target_path)

    # Create PERSONAL.md template if missing, or append missing coach fields without overwriting.
    _ensure_personal_and_appendices(target_path, target_name)

    print(f"[OK] installed {pkg_ver} -> {target_path}")
    if backup:
        print(f"  backup: {backup.name}")
    return 0


def cmd_update(args) -> int:
    pkg_text, pkg_ver = _read_packaged_skill()

    # When neither --target nor --path is given: update all targets in the tracker
    if not getattr(args, "target", None) and not getattr(args, "path", None):
        data = _load_tracker()
        targets = data.get("targets", [])
        if not targets:
            print("[INFO] no tracked targets; run `aigenora skill install --target ...` first")
            return 0
        rc = 0
        for entry in targets:
            tp = Path(entry["path"])
            rc |= _update_one(tp, entry.get("target", "custom"), pkg_text, pkg_ver, args.force)
        return rc

    target_name, target_path = _resolve_target(args)
    return _update_one(target_path, target_name, pkg_text, pkg_ver, args.force)


def _update_one(target_path: Path, target_name: str, pkg_text: str, pkg_ver: str, force: bool) -> int:
    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(pkg_text, encoding="utf-8")
        _remember_target(target_name, target_path)
        _ensure_personal_and_appendices(target_path, target_name)
        print(f"[OK] installed {pkg_ver} -> {target_path}")
        return 0

    old_ver = _read_target_version(target_path)
    cmp = _version_cmp(pkg_ver, old_ver or "0")

    if cmp > 0 or force:
        backup = _backup_and_trim(target_path, old_ver)
        target_path.write_text(pkg_text, encoding="utf-8")
        _remember_target(target_name, target_path)
        _ensure_personal_and_appendices(target_path, target_name)
        action = "forced" if (cmp <= 0 and force) else "updated"
        print(f"[OK] {action} {old_ver} -> {pkg_ver} @ {target_path}")
        if backup:
            print(f"  backup: {backup.name}")
        return 0

    if cmp == 0:
        _ensure_personal_and_appendices(target_path, target_name)
        print(f"[OK] up-to-date ({pkg_ver}) @ {target_path}")
        return 0

    # cmp < 0: the packaged version is lower; per user decision, no special handling for now
    _ensure_personal_and_appendices(target_path, target_name)
    print(f"[SKIP] packaged ({pkg_ver}) <= installed ({old_ver}) @ {target_path}; use --force to overwrite")
    return 0


def cmd_check(args) -> int:
    pkg_text, pkg_ver = _read_packaged_skill()

    if not getattr(args, "target", None) and not getattr(args, "path", None):
        data = _load_tracker()
        targets = data.get("targets", [])
        if not targets:
            print(f"packaged: {pkg_ver}")
            print("[INFO] no tracked targets")
            return 0
        print(f"packaged: {pkg_ver}")
        rc = 0
        for entry in targets:
            tp = Path(entry["path"])
            old_ver = _read_target_version(tp)
            status = _status_label(pkg_ver, old_ver, tp)
            print(f"  [{status}] {entry.get('target','custom'):12s} {old_ver or '-':>10s}  {tp}")
            if status in ("OUTDATED", "MISSING"):
                rc = 1
            if status in ("OK", "AHEAD"):
                rc |= _print_appendix_check(tp, indent="    ")
        return rc

    target_name, target_path = _resolve_target(args)
    old_ver = _read_target_version(target_path)
    status = _status_label(pkg_ver, old_ver, target_path)
    print(f"packaged: {pkg_ver}")
    print(f"target ({target_name}): {old_ver or '-'} @ {target_path}")
    print(f"status: {status}")
    if status not in ("OK", "AHEAD"):
        return 1
    return _print_appendix_check(target_path, indent="")


def _status_label(pkg_ver: str, target_ver: str | None, target_path: Path) -> str:
    if not target_path.exists():
        return "MISSING"
    if target_ver is None:
        return "UNKNOWN"
    cmp = _version_cmp(pkg_ver, target_ver)
    if cmp > 0:
        return "OUTDATED"
    if cmp == 0:
        return "OK"
    return "AHEAD"


def _install_appendices(skill_dir: Path) -> list[str]:
    """Install/overwrite companion appendix files next to SKILL.md (v018).

    Appendices are product docs (not user-personalized), always overwritten to match the
    installed package. Silently skips names missing in the package (older versions).
    """
    installed = []
    for name in APPENDIX_FILES:
        try:
            text = resources.files("aigenora.skill").joinpath(name).read_text(encoding="utf-8-sig")
        except (FileNotFoundError, OSError):
            continue
        try:
            (skill_dir / name).write_text(text, encoding="utf-8")
            installed.append(name)
        except OSError:
            pass
    return installed


def _appendix_problems(skill_dir: Path) -> tuple[list[str], list[str]]:
    """Return (missing, outdated) companion appendix names for an installed skill dir."""
    missing = []
    outdated = []
    for name in APPENDIX_FILES:
        try:
            packaged = resources.files("aigenora.skill").joinpath(name).read_text(encoding="utf-8-sig")
        except (FileNotFoundError, OSError):
            continue
        target = skill_dir / name
        if not target.is_file():
            missing.append(name)
            continue
        try:
            current = target.read_text(encoding="utf-8-sig")
        except OSError:
            missing.append(name)
            continue
        if current != packaged:
            outdated.append(name)
    return missing, outdated


def _print_appendix_check(target_path: Path, indent: str = "") -> int:
    missing, outdated = _appendix_problems(target_path.parent)
    if not missing and not outdated:
        return 0
    parts = []
    if missing:
        parts.append("missing=" + ",".join(missing))
    if outdated:
        parts.append("outdated=" + ",".join(outdated))
    print(f"{indent}appendices: {'; '.join(parts)}")
    return 1


def _ensure_personal_and_appendices(target_path: Path, target_name: str) -> None:
    """Ensure PERSONAL.md + v018 appendix files are present next to SKILL.md."""
    _ensure_personal_for_target(target_path=target_path, target_name=target_name)
    _install_appendices(target_path.parent)


def _ensure_personal_for_target(target_path: Path, target_name: str) -> None:
    """Create PERSONAL.md if missing; append the coach block if an existing file lacks it."""
    personal_path = target_path.parent / PERSONAL_FILENAME
    if not personal_path.exists():
        _install_personal_template(personal_path, target_name)
        print(f"[OK] created {personal_path} (personalization template)")
        return
    if _ensure_coach_field(personal_path, target_name):
        print(f"[OK] updated {personal_path} (added embedded coach settings)")
    else:
        print(f"[INFO] {personal_path} already exists, skipped")


# ---------- Personalization template ----------

KNOWN_COACH_AGENTS = ("claude-code", "codex", "opencode")

# v014-M2: detect an existing coach:user_agent declaration (any value) so we never clobber a
# user's choice when appending the coach block.
_COACH_USER_AGENT_RE = re.compile(r"<!--\s*coach:user_agent\s*:")

_COACH_FIELD_BLOCK = """\

## Embedded Coach (v014-M2)

<!-- The webui coach panel lets you talk tactics with your OWN agent CLI during a live game. -->
<!-- Declare which agent CLI you use; the coach drives it via a command template. -->
<!-- coach:user_agent: {target} -->
<!-- Choices: claude-code | codex | opencode. If you remove this line, the coach falls back to -->
<!-- the most recent `aigenora skill install --target`, then to claude-code. -->
<!-- Optional command-template overrides. Placeholders (replaced by the coach as single argv -->
<!-- elements — never via a shell): {session_id}, {coach_skill_file}. When the template has no -->
<!-- {prompt} placeholder the prompt is fed via STDIN (claude-code default; avoids the Windows .cmd -->
<!-- shim corrupting multi-line argv). -->
<!-- IMPORTANT: your agent CLI loads its GLOBAL instruction file (~/.claude/CLAUDE.md for -->
<!-- claude-code, ~/.codex for codex, opencode global config, ...) whose persona can overpower -->
<!-- the coach role and make it ignore the injected game situation. The claude-code default below -->
<!-- isolates it via --system-prompt-file (COACH_SKILL.md becomes the session system prompt, fully -->
<!-- replacing the global file). For codex/opencode, override new_cmd/resume_cmd with your agent's -->
<!-- "custom system-prompt / disable global config" flag — see SKILL.md "Embedded Coach". -->
<!-- coach:new_cmd: claude --session-id {session_id} --system-prompt-file {coach_skill_file} -p -->
<!-- coach:resume_cmd: claude --resume {session_id} --system-prompt-file {coach_skill_file} -p -->
<!-- coach:timeout: 180 -->
<!-- coach:max_context_events: 12 -->
"""


def _ensure_coach_field(personal_path: Path, target_name: str) -> bool:
    """Ensure PERSONAL.md declares coach:user_agent for the given target (v014-M2).

    - file absent: no-op (caller is expected to create it first).
    - file present without a coach:user_agent line: append the coach block at the end, with
      user_agent set to target_name (unknown target -> claude-code).
    - file present with coach:user_agent already: leave untouched (preserve user choice).

    Returns True iff a block was appended.
    """
    if not personal_path.exists():
        return False
    try:
        text = personal_path.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    if _COACH_USER_AGENT_RE.search(text):
        return False
    agent = target_name if target_name in KNOWN_COACH_AGENTS else "claude-code"
    block = _COACH_FIELD_BLOCK.replace("{target}", agent)
    try:
        personal_path.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
    except OSError:
        return False
    return True


def _install_personal_template(personal_path: Path, target_name: str = "") -> None:
    """Copy the packaged PERSONAL.md template to the target directory, then ensure the
    coach:user_agent field is present (v014-M2). Never overwrites existing user content; only
    appends the coach block if it is missing.
    """
    if not personal_path.exists():
        try:
            res = resources.files("aigenora.skill").joinpath(PERSONAL_FILENAME)
            template = res.read_text(encoding="utf-8-sig")
        except (FileNotFoundError, TypeError):
            # Fallback: minimal template if packaged file is missing
            template = (
                "# Aigenora Personalization\n"
                "\n"
                "> This file is maintained by the user or user Agent.\n"
                "> `aigenora skill install/update` will NEVER overwrite this file.\n"
                "\n"
                "## User Preferences\n"
                "\n"
                "<!-- Record personalized preferences here. -->\n"
                "\n"
                "## Behavioral Habits\n"
                "\n"
                "<!-- Record user interaction habits. -->\n"
                "\n"
                "## Custom Notes\n"
                "\n"
                "<!-- Free-form notes. -->\n"
            )
        personal_path.parent.mkdir(parents=True, exist_ok=True)
        personal_path.write_text(template, encoding="utf-8")
    if target_name:
        _ensure_coach_field(personal_path, target_name)


# ---------- CLI routing ----------

def build_subparser(parent_sub) -> argparse.ArgumentParser:
    """Called by cli.py: register the skill subcommand on the main parser."""
    sk = parent_sub.add_parser("skill", help="Manage the SKILL.md installed for user agents")
    sk_sub = sk.add_subparsers(dest="skill_cmd", required=True)

    def _add_target_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--target", choices=list(TARGET_PRESETS), help="Preset install target")
        p.add_argument("--path", help="Custom SKILL.md path (overrides --target)")
        p.add_argument("--base", help="Base directory for preset targets (default: cwd)")

    inst = sk_sub.add_parser("install", help="Install packaged SKILL.md to a target (explicit)")
    _add_target_args(inst)
    inst.add_argument("--force", action="store_true", help="Overwrite existing SKILL.md")

    upd = sk_sub.add_parser("update", help="Update SKILL.md if package version is newer (default: all tracked targets)")
    _add_target_args(upd)
    upd.add_argument("--force", action="store_true", help="Force overwrite regardless of version")

    chk = sk_sub.add_parser("check", help="Check if installed version matches package (default: all tracked targets)")
    _add_target_args(chk)

    sk_sub.add_parser("version", help="Show packaged SKILL.md and client version")
    sk_sub.add_parser("path", help="Print the real path of packaged SKILL.md")
    return sk


def run(args) -> int:
    cmd = getattr(args, "skill_cmd", None)
    if cmd == "install":
        return cmd_install(args)
    if cmd == "update":
        return cmd_update(args)
    if cmd == "check":
        return cmd_check(args)
    if cmd == "version":
        return cmd_version(args)
    if cmd == "path":
        return cmd_path(args)
    print(f"[ERR] unknown skill subcommand: {cmd}", file=sys.stderr)
    return 2
