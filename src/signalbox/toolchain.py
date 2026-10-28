"""Repository-pinned pnpm commands and the shared dependency environment."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def pnpm_version(root: Path) -> str | None:
    root = Path(root)
    try:
        manager = json.loads((root / "package.json").read_text()).get("packageManager", "")
        if isinstance(manager, str) and manager.startswith("pnpm@"):
            version = manager[5:].split("+sha", 1)[0]
            if re.fullmatch(r"\d+\.\d+\.\d+", version):
                return version
    except (OSError, ValueError, AttributeError):
        pass
    for workflow in sorted((root / ".github/workflows").glob("*.yml")):
        try:
            content = workflow.read_text()
        except OSError:
            continue
        # A step starts at a YAML sequence item; keep its with block attached
        # even when name or with precedes uses.
        steps = re.split(r"(?m)^(\s*)-\s+", content)
        for step in steps[2::2]:
            if not re.search(r"(?m)^\s*uses:\s*['\"]?pnpm/action-setup@", step):
                continue
            match = re.search(
                r"(?m)^\s*with:\s*\n(?:(?:[ \t]+.*)?\n)*?"
                r"[ \t]+version:\s*['\"]?(\d+\.\d+\.\d+)['\"]?\s*(?:#.*)?$",
                step,
            )
            if match:
                return match[1]
    return None


def pnpm_shim_dir(root: Path, state: Path) -> Path | None:
    """Make bare pnpm use the repository's pinned version (#473)."""
    version = pnpm_version(root)
    if version is None:
        return None
    directory = Path(state) / "shims" / f"pnpm-{version}"
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "pnpm"
    content = f'#!/bin/sh\nexec npx -y "pnpm@{version}" "$@"\n'
    try:
        if shim.read_text() == content and shim.stat().st_mode & 0o777 == 0o755:
            return directory
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o755)
            os.replace(temporary_path, shim)
        finally:
            temporary_path.unlink(missing_ok=True)
    return directory


def with_pinned_pnpm(env: dict[str, str], root: Path, state: Path) -> dict[str, str]:
    """Give agent and suite processes the pinned pnpm and shared store (#473)."""
    result = dict(env)
    directory = pnpm_shim_dir(root, state)
    if directory is not None:
        result["PATH"] = os.pathsep.join((str(directory), env["PATH"])) if "PATH" in env else str(directory)
        result["npm_config_store_dir"] = str(Path(state) / "pnpm-store")
    return result


def pnpm_argv(root: Path) -> list[str]:
    version = pnpm_version(root)
    return ["npx", "-y", f"pnpm@{version}"] if version else ["pnpm"]


def pnpm_packages(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    if (root / "pnpm-workspace.yaml").is_file():
        return [root]
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "*pnpm-lock.yaml", "pnpm-lock.yaml"],
            cwd=root, capture_output=True, text=True, check=False,
        )
    except OSError:
        return []
    if result.returncode:
        return []
    return sorted({root / Path(name).parent for name in result.stdout.split("\0")
                   if name and Path(name).name == "pnpm-lock.yaml"})


def install_env(state: Path) -> dict[str, str]:
    """The environment a suite runs pnpm in: the operator's, plus the shared store."""
    return {**os.environ, "CI": "true", "npm_config_yes": "true",
            "npm_config_store_dir": str(Path(state) / "pnpm-store")}


# What an install may see of the operator's environment. An install runs outside
# the agent sandbox, so it gets no credentials: no GH_TOKEN, no SSH agent, no
# cloud keys. Only what pnpm and npx need to find themselves and the store.
INSTALL_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def install_only_env(state: Path) -> dict[str, str]:
    """The minimal environment a dependency install runs in (#465 gate review)."""
    kept = {key: os.environ[key] for key in INSTALL_ENV_KEYS if key in os.environ}
    return {**kept, "CI": "true", "npm_config_yes": "true",
            "npm_config_store_dir": str(Path(state) / "pnpm-store")}


# An install never runs lifecycle scripts: the target repo's own package.json
# scripts would otherwise run with signalbox's privileges. pnpm 10 already skips
# dependencies' build scripts unless a repo allowlists them.
INSTALL_ARGS = ["install", "--frozen-lockfile", "--prod=false", "--ignore-scripts"]

# Per install. prepare-workspace's handler budget in emergent.toml (1,800 s)
# covers five cold installs.
INSTALL_TIMEOUT = 300
