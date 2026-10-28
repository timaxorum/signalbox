"""Repository-pinned pnpm commands and the shared dependency environment."""

import json
import os
import re
import subprocess
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
    return {**os.environ, "CI": "true", "npm_config_yes": "true",
            "npm_config_store_dir": str(Path(state) / "pnpm-store")}
