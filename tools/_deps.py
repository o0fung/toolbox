"""Dependency helpers for runtime install prompts across platforms."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

import typer

try:
    from ._cli_output import info, warn
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_output import info, warn


@dataclass(frozen=True)
class InstallOption:
    """Declarative install command with a detection prerequisite."""

    label: str
    command: str
    requires_bin: str


def ensure_binary_or_prompt_install(
    *,
    binary: str,
    missing_message: str,
    options: list[InstallOption],
) -> Optional[str]:
    """
    Ensure a binary exists; if missing, prompt and run one suitable installer.

    Returns the resolved binary path when available, otherwise None.
    """
    resolved = shutil.which(binary)
    if resolved is not None:
        return resolved

    warn(missing_message)
    option = _pick_option(options)
    if option is None:
        return None
    if not _confirm_install(binary=binary, option=option):
        return None
    if not _run_install(option=option):
        return None

    resolved = shutil.which(binary)
    if resolved is None:
        warn(
            f"Install command finished, but `{binary}` is still not on PATH. "
            "Open a new shell or ensure the package manager bin path is configured."
        )
        return None
    return resolved


def ensure_python_module_or_prompt_install(
    *,
    import_name: str,
    missing_message: str,
    options: list[InstallOption],
) -> bool:
    """
    Ensure a Python module is importable, with optional interactive installation.
    """
    if _module_exists(import_name):
        return True

    warn(missing_message)
    option = _pick_option(options)
    if option is None:
        return False
    if not _confirm_install(binary=import_name, option=option):
        return False
    if not _run_install(option=option):
        return False
    return _module_exists(import_name)


def ghostscript_install_options() -> list[InstallOption]:
    return [
        InstallOption("Homebrew", "brew install ghostscript", "brew"),
        InstallOption("APT", "sudo apt-get update && sudo apt-get install -y ghostscript", "apt-get"),
        InstallOption("DNF", "sudo dnf install -y ghostscript", "dnf"),
        InstallOption("YUM", "sudo yum install -y ghostscript", "yum"),
        InstallOption("Pacman", "sudo pacman -S --noconfirm ghostscript", "pacman"),
        InstallOption("Zypper", "sudo zypper --non-interactive install ghostscript", "zypper"),
        InstallOption("Winget", "winget install --id ArtifexSoftware.GhostScript -e", "winget"),
        InstallOption("Chocolatey", "choco install ghostscript -y", "choco"),
    ]


def ffmpeg_install_options() -> list[InstallOption]:
    return [
        InstallOption("Homebrew", "brew install ffmpeg", "brew"),
        InstallOption("APT", "sudo apt-get update && sudo apt-get install -y ffmpeg", "apt-get"),
        InstallOption("DNF", "sudo dnf install -y ffmpeg", "dnf"),
        InstallOption("YUM", "sudo yum install -y ffmpeg", "yum"),
        InstallOption("Pacman", "sudo pacman -S --noconfirm ffmpeg", "pacman"),
        InstallOption("Zypper", "sudo zypper --non-interactive install ffmpeg", "zypper"),
        InstallOption("Winget", "winget install --id Gyan.FFmpeg -e", "winget"),
        InstallOption("Chocolatey", "choco install ffmpeg -y", "choco"),
    ]


def yt_dlp_install_options() -> list[InstallOption]:
    python = sys.executable
    return [
        InstallOption("pip (current Python)", f"{python} -m pip install -U yt-dlp", python),
        InstallOption("pipx", "pipx install yt-dlp", "pipx"),
        InstallOption("Homebrew", "brew install yt-dlp", "brew"),
        InstallOption("APT", "sudo apt-get update && sudo apt-get install -y yt-dlp", "apt-get"),
        InstallOption("DNF", "sudo dnf install -y yt-dlp", "dnf"),
        InstallOption("YUM", "sudo yum install -y yt-dlp", "yum"),
        InstallOption("Pacman", "sudo pacman -S --noconfirm yt-dlp", "pacman"),
        InstallOption("Zypper", "sudo zypper --non-interactive install yt-dlp", "zypper"),
        InstallOption("Winget", "winget install --id yt-dlp.yt-dlp -e", "winget"),
        InstallOption("Chocolatey", "choco install yt-dlp -y", "choco"),
    ]


def _pick_option(options: list[InstallOption]) -> Optional[InstallOption]:
    # Selection policy:
    # 1) Preserve the caller-specified order as priority (most trusted first).
    # 2) Keep OS consistency by only picking installers available on this machine.
    # 3) Return the first viable candidate so the user sees one clear command.
    for option in options:
        if _has_required_binary(option.requires_bin):
            return option
    return None


def _has_required_binary(binary: str) -> bool:
    if binary == sys.executable:
        return True
    return shutil.which(binary) is not None


def _confirm_install(*, binary: str, option: InstallOption) -> bool:
    try:
        return typer.confirm(
            f"`{binary}` is missing. Install now via {option.label}? ({option.command})",
            default=False,
        )
    except (typer.Abort, EOFError):
        return False


def _run_install(*, option: InstallOption) -> bool:
    info(f"Running: {option.command}")
    try:
        subprocess.run(option.command, check=True, shell=True)
        info(f"Install command completed: {option.label}")
        return True
    except subprocess.CalledProcessError as exc:
        warn(f"Install command failed ({option.label}): {exc}")
        return False
    except Exception as exc:
        warn(f"Failed to run install command ({option.label}): {exc}")
        return False


def _module_exists(import_name: str) -> bool:
    try:
        importlib.import_module(import_name)
        return True
    except Exception:
        return False
