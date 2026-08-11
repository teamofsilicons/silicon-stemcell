"""What this Silicon is running, and what the world has published since.

Reads the installed version of every runtime dependency — Python requirements,
global npm packages, console scripts — and asks each registry what the latest
is. Every lookup is best effort and bounded: a slow registry must not hold up
the report, and an unreachable one is reported as unknown rather than as
out of date.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from interface.agent.config import release_dir
from helpers.timefmt import utc_iso as _utc_iso

REGISTRY_TIMEOUT = 8
NPM_LIST_TIMEOUT = 12
NPM_RUNTIME_PACKAGES = (
    {"name": "@anthropic-ai/claude-code", "command": "claude"},
    {"name": "@openai/codex", "command": "codex"},
)
LOCAL_NPM_CLIS = (
    {
        "name": "@teamofsilicons/silicon-interface-cli",
        "label": "silicon-interface",
        "commands": (".silicon-interface/bin/si", "si", "silicon-interface"),
        "install_command": "silicon-interface",
    },
)
SCRIPT_CLIS = (
    {
        "name": "silicon",
        "command": "silicon",
        "source": "silicon CLI",
        "package": "silicon-cli",
        "update_args": ("script", "update"),
    },
    {
        "name": "silicon-browser",
        "command": "silicon-browser",
        "source": "Silicon Browser CLI",
        "package": "silicon-browser",
        "update_kind": "python_cli",
    },
)


def _request_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": "silicon-glass-agent/1.0"})
    with urlopen(req, timeout=REGISTRY_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_pypi_version(name: str) -> tuple[str, str]:
    try:
        body = _request_json(f"https://pypi.org/pypi/{quote(name)}/json")
        return str((body.get("info") or {}).get("version") or ""), ""
    except Exception as exc:
        return "", str(exc)


def _latest_npm_version(name: str) -> tuple[str, str]:
    try:
        body = _request_json(f"https://registry.npmjs.org/{quote(name, safe='')}/latest")
        return str(body.get("version") or ""), ""
    except Exception as exc:
        return "", str(exc)


def _lookup_many(names: list[str], lookup) -> dict[str, tuple[str, str]]:
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name and name not in seen:
            unique.append(name)
            seen.add(name)
    if not unique:
        return {}

    results: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        futures = {pool.submit(lookup, name): name for name in unique}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = "", str(exc)
    return results


def _latest_github_main(repo: str) -> tuple[str, str]:
    try:
        body = _request_json(f"https://api.github.com/repos/{repo}/commits/main")
        sha = str(body.get("sha") or "")
        return (f"main@{sha[:12]}" if sha else ""), ""
    except Exception as exc:
        return "", str(exc)


def _requirement_name(line: str) -> str:
    line = (line or "").split("#", 1)[0].split(";", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http://", "https://")):
        return ""
    match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[.*?\])?", line)
    return match.group(1) if match else ""


def _python_requirements(root: Path) -> list[tuple[str, str]]:
    req = root / "requirements.txt"
    if not req.exists():
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in req.read_text(encoding="utf-8").splitlines():
        name = _requirement_name(raw)
        key = name.lower().replace("_", "-")
        if name and key not in seen:
            seen.add(key)
            out.append((name, raw.strip()))
    return out


def _installed_python_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return ""


def _npm_global_versions() -> tuple[dict[str, str], str]:
    npm = shutil.which("npm")
    if not npm:
        return {}, "npm not found"
    try:
        proc = subprocess.run(
            [npm, "list", "-g", "--depth=0", "--json"],
            capture_output=True,
            text=True,
            timeout=NPM_LIST_TIMEOUT,
        )
        body = json.loads(proc.stdout or "{}")
        deps = body.get("dependencies") or {}
        return {
            name: str((info or {}).get("version") or "")
            for name, info in deps.items()
            if isinstance(info, dict)
        }, ""
    except Exception as exc:
        return {}, str(exc)


def _version_from_command(command: str) -> str:
    exe = command if os.path.sep in command else shutil.which(command)
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text:
        return ""
    match = re.search(r"\d+(?:\.\d+)+(?:[-+][A-Za-z0-9_.-]+)?", text[0])
    return match.group(0) if match else text[0][:80]


def _python_runner_from_executable(exe: str) -> list[str]:
    try:
        first_line = Path(exe).read_bytes()[:256].splitlines()[0].decode("utf-8", errors="ignore")
    except Exception:
        return []
    if first_line.startswith("#!") and "python" in first_line.lower():
        try:
            parts = shlex.split(first_line[2:].strip())
        except ValueError:
            parts = first_line[2:].strip().split()
        if parts:
            runner = parts[:]
            if Path(runner[0]).name == "env" and len(runner) == 1:
                runner.append("python3")
            return runner
    return []


def _python_console_package_version(root: Path, command: str, package: str) -> str:
    exe = _resolve_command(root, command)
    if not exe:
        return ""

    code = (
        "from importlib.metadata import PackageNotFoundError, version\n"
        f"try: print(version({package!r}))\n"
        "except PackageNotFoundError: pass\n"
    )
    runner = _python_runner_from_executable(exe)
    if runner:
        try:
            proc = subprocess.run(
                [*runner, "-c", code],
                capture_output=True,
                text=True,
                timeout=15,
            )
            text = (proc.stdout or "").strip().splitlines()
            if proc.returncode == 0 and text:
                return text[0]
        except Exception:
            pass

    return _installed_python_version(package)


def _resolve_command(root: Path, command: str) -> str:
    path = root / command
    if os.path.sep in command and path.exists():
        return str(path)
    found = shutil.which(command)
    return found or ""


def _file_identity(path: str) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return ""
        digest = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        return f"sha256:{digest}"
    except Exception:
        return ""


def _command_identity(root: Path, command: str) -> str:
    exe = _resolve_command(root, command)
    if not exe:
        return ""
    return _version_from_command(exe) or _file_identity(exe)


def _dependency_status(installed: str, latest: str) -> str:
    if not installed:
        return "missing"
    if latest and latest != installed:
        return "outdated"
    if latest:
        return "current"
    return "unknown"


def dependency_report(root: Path) -> dict:
    packages: list[dict] = []
    errors: list[str] = []
    requirements = _python_requirements(release_dir(root))
    script_packages = [str(item.get("package") or "") for item in SCRIPT_CLIS]
    pypi_latest = _lookup_many([name for name, _ in requirements] + script_packages, _latest_pypi_version)
    npm_latest = _lookup_many(
        [item["name"] for item in NPM_RUNTIME_PACKAGES] + [item["name"] for item in LOCAL_NPM_CLIS],
        _latest_npm_version,
    )

    for name, required in requirements:
        installed = _installed_python_version(name)
        latest, err = pypi_latest.get(name, ("", ""))
        if err:
            errors.append(f"pypi:{name}: {err}")
        packages.append(
            {
                "manager": "pip",
                "name": name,
                "required": required,
                "installed_version": installed,
                "latest_version": latest,
                "status": _dependency_status(installed, latest),
                "source": "requirements.txt",
            }
        )

    npm_versions, npm_err = _npm_global_versions()
    if npm_err:
        errors.append(f"npm: {npm_err}")
    for item in NPM_RUNTIME_PACKAGES:
        name = item["name"]
        installed = npm_versions.get(name) or _version_from_command(item["command"])
        latest, err = npm_latest.get(name, ("", ""))
        if err:
            errors.append(f"npm:{name}: {err}")
        packages.append(
            {
                "manager": "npm",
                "name": name,
                "required": "global runtime",
                "installed_version": installed,
                "latest_version": latest,
                "status": _dependency_status(installed, latest),
                "source": "npm global",
                "command": item["command"],
            }
        )

    for item in LOCAL_NPM_CLIS:
        name = item["name"]
        exe = ""
        installed = ""
        for command in item["commands"]:
            exe = _resolve_command(root, command)
            if exe:
                installed = _version_from_command(exe) or _file_identity(exe)
                break
        latest, err = npm_latest.get(name, ("", ""))
        if err:
            errors.append(f"npm:{name}: {err}")
        packages.append(
            {
                "manager": "npm",
                "name": item["label"],
                "package": name,
                "required": "local runtime CLI",
                "installed_version": installed,
                "latest_version": latest,
                "status": _dependency_status(installed, latest),
                "source": ".silicon-interface",
                "command": exe or item["commands"][0],
            }
        )

    for item in SCRIPT_CLIS:
        name = item["name"]
        installed = ""
        package = str(item.get("package") or "")
        target = str(item.get("target_version") or "")
        if package:
            installed = _python_console_package_version(root, item["command"], package)
        installed = installed or _command_identity(root, item["command"])
        if target:
            latest, err = target, ""
        elif package:
            latest, err = pypi_latest.get(package, ("", ""))
        else:
            latest, err = _latest_github_main(item["latest_repo"])
        if err:
            label = f"pypi:{package}" if package else f"github:{item['latest_repo']}"
            errors.append(f"{label}: {err}")
        if not installed:
            status = "missing"
        elif installed.startswith("sha256:"):
            status = "unknown"
        else:
            status = _dependency_status(installed, latest)
        packages.append(
            {
                "manager": "script",
                "name": name,
                "package": package,
                "required": item["source"],
                "installed_version": installed,
                "latest_version": latest,
                "status": status,
                "source": item["source"],
                "command": item["command"],
            }
        )

    summary = {"total": len(packages), "current": 0, "outdated": 0, "missing": 0, "unknown": 0}
    for pkg in packages:
        summary[pkg["status"]] = summary.get(pkg["status"], 0) + 1

    return {
        "checked_at": _utc_iso(),
        "packages": packages,
        "summary": summary,
        "errors": errors[:20],
    }


def dependency_summary_text(report: dict, *, updated: bool = False) -> str:
    summary = report.get("summary") or {}
    total = int(summary.get("total") or 0)
    outdated = int(summary.get("outdated") or 0)
    missing = int(summary.get("missing") or 0)
    failed = int(summary.get("failed_installs") or 0)
    prefix = "dependency update" if updated else "dependency report"
    detail = f"{prefix}: {total} checked, {outdated} outdated, {missing} missing"
    if failed:
        detail += f", {failed} install step(s) failed"
    return detail


