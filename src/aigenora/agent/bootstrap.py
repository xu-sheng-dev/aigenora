"""aigenora bootstrap: one-shot environment probe for user agents.

Design principles (must be followed):
- Diagnosis only, never auto-fix. Repair suggestions are returned as strings; the
  caller (human/agent) decides whether to act on them.
- Output is both machine-parseable (--json) and human-readable.
- No network dependency, no community-server dependency.
- Fields are stable; new fields must be backward compatible; do not rename published fields.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform as pyplatform
import shutil
import sys
import sysconfig
from pathlib import Path

from aigenora import __version__ as PKG_VERSION


REQUIRED_DEPS = ("cryptography", "httpx", "iroh")


def _platform_id() -> str:
    sysname = sys.platform
    if sysname.startswith("win"):
        return "windows"
    if sysname == "darwin":
        return "macos"
    return "linux"


def _check_skill() -> tuple[str | None, str | None, str | None]:
    """Returns (skill_md_path, skill_version, error)."""
    try:
        from importlib import resources
        from aigenora.agent.skill import _extract_skill_version

        res = resources.files("aigenora.skill").joinpath("SKILL.md")
        text = res.read_text(encoding="utf-8-sig")
        ver = _extract_skill_version(text)
        return (str(res), ver, None if ver else "missing 'version:' frontmatter")
    except Exception as e:
        return (None, None, str(e))


def _check_deps() -> list[str]:
    return [m for m in REQUIRED_DEPS if importlib.util.find_spec(m) is None]


def _data_dir_default() -> str:
    raw = os.environ.get("P2P_DATA_DIR") or os.environ.get("AGENT_DIR")
    if raw:
        return str(Path(raw).expanduser())
    return str(Path.cwd() / ".aigenora")


def collect() -> dict:
    """Collect environment probe data."""
    scripts_dir = sysconfig.get_paths().get("scripts", "")
    cmd_path = shutil.which("aigenora")
    in_path = cmd_path is not None

    skill_path, skill_ver, skill_err = _check_skill()
    missing_deps = _check_deps()

    issues: list[dict] = []

    if missing_deps:
        issues.append({
            "code": "DEPS_MISSING",
            "message": f"missing python packages: {', '.join(missing_deps)}",
            "fix": "ask user to run: pip install aigenora-client",
        })

    if skill_err and not skill_path:
        issues.append({
            "code": "SKILL_NOT_PACKAGED",
            "message": f"packaged SKILL.md unavailable: {skill_err}",
            "fix": "reinstall aigenora-client; the package data may be corrupt",
        })

    if not in_path:
        issues.append({
            "code": "CMD_NOT_IN_PATH",
            "message": "the `aigenora` console script is not in PATH",
            "fix": f"use `{sys.executable} -m aigenora ...` (recommended); "
                   f"or add to PATH: {scripts_dir}",
        })

    data = {
        "ok": all(i["code"] not in ("DEPS_MISSING", "SKILL_NOT_PACKAGED") for i in issues),
        "version": PKG_VERSION,
        "python": sys.executable,
        "python_version": pyplatform.python_version(),
        "platform": _platform_id(),
        "recommended_entrypoint": f"{sys.executable} -m aigenora",
        "console_script_in_path": in_path,
        "console_script_path": cmd_path,
        "console_script_dir": scripts_dir,
        "skill_md_path": skill_path,
        "skill_version": skill_ver,
        "data_dir_default": _data_dir_default(),
        "issues": issues,
    }
    return data


def _print_human(data: dict) -> None:
    print(f"ok: {data['ok']}")
    print(f"version: {data['version']}")
    print(f"python: {data['python']} ({data['python_version']})")
    print(f"platform: {data['platform']}")
    print(f"recommended entrypoint: {data['recommended_entrypoint']}")
    if data["console_script_in_path"]:
        print(f"aigenora cmd: {data['console_script_path']}")
    else:
        print(f"aigenora cmd: NOT IN PATH")
        print(f"  scripts dir: {data['console_script_dir']}")
    print(f"skill md: {data['skill_md_path']} (version={data['skill_version']})")
    print(f"data dir default: {data['data_dir_default']}")
    if data["issues"]:
        print("issues:")
        for i in data["issues"]:
            print(f"  [{i['code']}] {i['message']}")
            print(f"    fix: {i['fix']}")
    else:
        print("issues: (none)")


def run(args) -> int:
    data = collect()
    if getattr(args, "json_output", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_human(data)
    return 0 if data["ok"] else 1


def build_subparser(parent_sub) -> argparse.ArgumentParser:
    bs = parent_sub.add_parser(
        "bootstrap",
        help="One-shot environment probe: returns python/package/skill/PATH status (agent-friendly)",
    )
    bs.add_argument("--json", action="store_true", dest="json_output",
                    help="Output machine-parseable JSON")
    return bs
