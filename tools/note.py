"""Markdown note management command implementation for `note`."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import typer
from rich import print

try:
    from ._cli_common import new_typer_app
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app


app = new_typer_app(
    context_settings={"allow_interspersed_args": True},
    invoke_without_command=True,
)

_DEFAULT_NOTE_CONFIG_PATH = os.path.expanduser("~/.config/lf-toolbox/note.defaults.json")
_CONFIG_KEYS = {"notes_dir", "editor", "add_title_heading"}
_TIMESTAMP_PATTERN = re.compile(r"^(\d{8}-\d{6})_")
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_INVALID_FOLDER_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _default_note_config_payload() -> Dict[str, object]:
    return {
        "notes_dir": "~/Documents/note",
        "editor": None,
        "add_title_heading": True,
    }


def _ensure_note_config_file(path: str) -> bool:
    config_path = Path(path).expanduser()
    if config_path.is_file():
        return False

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(_default_note_config_payload(), indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise typer.BadParameter(f"Cannot write config file '{config_path}': {exc}") from exc
    return True


def _normalize_config_value(key: str, value: object) -> object:
    if key == "notes_dir":
        if isinstance(value, str) and value.strip():
            return value
        raise typer.BadParameter("Config key 'notes_dir' must be a non-empty string")

    if key == "editor":
        if value is None or (isinstance(value, str) and value.strip()):
            return value
        raise typer.BadParameter("Config key 'editor' must be a non-empty string or null")

    if key == "add_title_heading":
        if isinstance(value, bool):
            return value
        raise typer.BadParameter("Config key 'add_title_heading' must be a boolean")

    raise typer.BadParameter(f"Unsupported config key: {key}")


def _load_note_config(path: str) -> Dict[str, object]:
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        return _default_note_config_payload()

    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON in config file '{config_path}': {exc.msg}") from exc
    except OSError as exc:
        raise typer.BadParameter(f"Cannot read config file '{config_path}': {exc}") from exc

    if not isinstance(loaded, dict):
        raise typer.BadParameter("Config file root must be a JSON object")

    unknown_keys = sorted(set(loaded) - _CONFIG_KEYS)
    if unknown_keys:
        raise typer.BadParameter(f"Unsupported config keys: {', '.join(unknown_keys)}")

    merged = _default_note_config_payload()
    for key, value in loaded.items():
        merged[key] = _normalize_config_value(key, value)
    return merged


def _resolve_editor(configured_editor: Optional[str]) -> List[str]:
    editor = configured_editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        command = shlex.split(editor, posix=os.name != "nt")
        if command:
            return command
        raise typer.BadParameter("Configured editor command is empty")

    if sys.platform == "darwin":
        return ["nano"]
    if os.name == "nt":
        return ["notepad"]
    if shutil.which("nano"):
        return ["nano"]
    if shutil.which("vi"):
        return ["vi"]
    raise typer.BadParameter("No editor found. Set 'editor' in the note config or set VISUAL/EDITOR.")


def _open_in_editor(path: Path, configured_editor: Optional[str]) -> None:
    command = [*_resolve_editor(configured_editor), str(path)]
    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        raise typer.BadParameter(f"Cannot open editor '{command[0]}': {exc}") from exc
    if result.returncode != 0:
        raise typer.BadParameter(f"Editor exited with status {result.returncode}: {command[0]}")


def _browse_directory(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise typer.BadParameter(f"Cannot open notes folder '{path}': {exc}") from exc


def _notes_root(config: Dict[str, object]) -> Path:
    raw_path = str(config["notes_dir"])
    return Path(os.path.expandvars(raw_path)).expanduser()


def _validate_folder_part(part: str) -> None:
    if part in {"", ".", ".."}:
        raise typer.BadParameter("Subfolder must not contain empty, '.' or '..' path components")
    if _INVALID_FOLDER_CHARS.search(part) or part.endswith((" ", ".")):
        raise typer.BadParameter(f"Subfolder contains characters unsupported across platforms: {part}")


def _resolve_subfolder(root: Path, subfolder: Optional[str], create: bool) -> Path:
    if not subfolder:
        target = root
    else:
        relative = Path(subfolder)
        if relative.is_absolute():
            raise typer.BadParameter("Subfolder must be relative to the notes directory")
        for part in relative.parts:
            _validate_folder_part(part)
        target = root.joinpath(relative)

    # Validate both lexical traversal and existing symlinks before creating or
    # searching. The resolved target must remain inside the configured notes root.
    root_resolved = root.resolve(strict=False)
    target_resolved = target.resolve(strict=False)
    if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
        raise typer.BadParameter("Subfolder must remain inside the notes directory")

    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise typer.BadParameter(f"Cannot create notes folder '{target}': {exc}") from exc
    elif not target.is_dir():
        if target == root and not target.exists():
            return target
        raise typer.BadParameter(f"Notes subfolder not found: {target}")
    return target


def _normalize_title(title: str) -> str:
    slug = _INVALID_FILENAME_CHARS.sub("-", title.strip())
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip(" .-_").lower()
    slug = slug[:120].rstrip(" .-_")
    if not slug:
        raise typer.BadParameter("Note title must contain at least one filename-safe character")
    return slug


def _create_note(
    folder: Path,
    title: str,
    add_title_heading: bool,
    now: Optional[datetime] = None,
) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base_name = f"{timestamp}_{_normalize_title(title)}"
    content = f"# {title.strip()}\n\n" if add_title_heading else ""

    # Creation is exclusive so simultaneous commands never overwrite each other.
    # Try the canonical timestamped filename first, then append a stable counter
    # while preserving the required timestamp prefix.
    suffix = 1
    while True:
        candidate_name = f"{base_name}.md" if suffix == 1 else f"{base_name}-{suffix}.md"
        candidate = folder / candidate_name
        try:
            with candidate.open("x", encoding="utf-8") as handle:
                handle.write(content)
            return candidate
        except FileExistsError:
            suffix += 1
        except OSError as exc:
            raise typer.BadParameter(f"Cannot create note '{candidate}': {exc}") from exc


def _find_notes(search_root: Path) -> List[Path]:
    try:
        return [path for path in search_root.rglob("*.md") if path.is_file()]
    except OSError as exc:
        raise typer.BadParameter(f"Cannot read notes folder '{search_root}': {exc}") from exc


def _note_sort_time(path: Path) -> float:
    match = _TIMESTAMP_PATTERN.match(path.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _sorted_notes(search_root: Path) -> List[Path]:
    return sorted(
        _find_notes(search_root),
        key=lambda path: (_note_sort_time(path), path.as_posix().casefold()),
        reverse=True,
    )


def _relative_display(path: Path, notes_root: Path) -> str:
    return path.relative_to(notes_root).as_posix()


def _list_notes(notes_root: Path, search_root: Path, match: Optional[str]) -> None:
    notes = _sorted_notes(search_root)
    if match:
        query = match.casefold()
        notes = [
            path
            for path in notes
            if query in _relative_display(path, notes_root).casefold()
        ]

    if not notes:
        typer.echo("No matching notes found." if match else "No notes found.")
        return

    for path in notes:
        typer.echo(_relative_display(path, notes_root))


def _unique_match(selector: str, candidates: Sequence[Path], notes_root: Path, search_root: Path) -> Path:
    query = selector.strip().replace("\\", "/").casefold()
    if not query:
        raise typer.BadParameter("Open selector must not be empty")

    # Matching proceeds from least ambiguous to most convenient:
    # 1) exact root-relative or selected-folder-relative path,
    # 2) exact filename/stem anywhere in scope,
    # 3) substring across the displayed relative path.
    # Every phase requires one unique result so the command never guesses.
    def path_forms(path: Path) -> List[str]:
        root_relative = _relative_display(path, notes_root).casefold()
        scope_relative = path.relative_to(search_root).as_posix().casefold()
        forms = [root_relative, scope_relative]
        forms.extend(
            value[:-3] for value in (root_relative, scope_relative) if value.endswith(".md")
        )
        return forms

    exact_paths = [path for path in candidates if query in path_forms(path)]
    if len(exact_paths) == 1:
        return exact_paths[0]
    if len(exact_paths) > 1:
        _raise_ambiguous_match(selector, exact_paths, notes_root)

    exact_names = [
        path
        for path in candidates
        if query in {path.name.casefold(), path.stem.casefold()}
    ]
    if len(exact_names) == 1:
        return exact_names[0]
    if len(exact_names) > 1:
        _raise_ambiguous_match(selector, exact_names, notes_root)

    partial = [
        path
        for path in candidates
        if query in _relative_display(path, notes_root).casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        _raise_ambiguous_match(selector, partial, notes_root)
    raise typer.BadParameter(f"No note matches: {selector}")


def _raise_ambiguous_match(selector: str, matches: Sequence[Path], notes_root: Path) -> None:
    choices = "\n".join(f"  {_relative_display(path, notes_root)}" for path in matches)
    raise typer.BadParameter(f"Multiple notes match '{selector}':\n{choices}")


def _config_editor_for_setup(path: str) -> Optional[str]:
    try:
        config = _load_note_config(path)
    except typer.BadParameter:
        return None
    editor = config["editor"]
    return editor if isinstance(editor, str) else None


@app.callback()
def note(
    title: Optional[List[str]] = typer.Argument(
        None,
        metavar="[TITLE]...",
        help="Title for a new note. Multiple words may be quoted or passed separately.",
    ),
    config: bool = typer.Option(
        False,
        "-c",
        "--config",
        help="Create/open the note configuration file and exit.",
    ),
    list_all: bool = typer.Option(False, "-l", "--list", help="List notes recursively, newest first."),
    match: Optional[str] = typer.Option(None, "-m", "--match", help="Filter --list by path or filename."),
    open_selector: Optional[str] = typer.Option(
        None,
        "-o",
        "--open",
        help="Open an existing note by relative path, filename, or unique partial match.",
    ),
    subfolder: Optional[str] = typer.Option(
        None,
        "-f",
        "--subfolder",
        help="Create, list, search, or browse within this relative notes subfolder.",
    ),
    browse: bool = typer.Option(False, "--browse", help="Open the notes directory in the system file browser."),
) -> None:
    """Create, list, find, and organize timestamped Markdown notes."""
    title_text = " ".join(title or []).strip()

    # Exactly one mode may run per invocation. Creation is the default mode when
    # title words are supplied; all other modes are explicit and side-effect
    # boundaries are checked before config, folders, or files are touched.
    selected_modes = [
        name
        for name, enabled in (
            ("create", bool(title_text)),
            ("config", config),
            ("list", list_all),
            ("open", open_selector is not None),
            ("browse", browse),
        )
        if enabled
    ]
    if not selected_modes:
        raise typer.BadParameter("Provide a note TITLE, or use --list, --open, --browse, or --config.")
    if len(selected_modes) > 1:
        raise typer.BadParameter(f"Choose only one action: {', '.join(selected_modes)}")
    if match is not None and not list_all:
        raise typer.BadParameter("--match may only be used with --list")
    if config and subfolder is not None:
        raise typer.BadParameter("--subfolder cannot be used with --config")

    if config:
        created = _ensure_note_config_file(_DEFAULT_NOTE_CONFIG_PATH)
        if created:
            print(f"[green]Created note config[/green]: {_DEFAULT_NOTE_CONFIG_PATH}")
        _open_in_editor(
            Path(_DEFAULT_NOTE_CONFIG_PATH).expanduser(),
            _config_editor_for_setup(_DEFAULT_NOTE_CONFIG_PATH),
        )
        return

    settings = _load_note_config(_DEFAULT_NOTE_CONFIG_PATH)
    notes_root = _notes_root(settings)
    create_folder = selected_modes[0] in {"create", "browse"}
    search_root = _resolve_subfolder(notes_root, subfolder, create=create_folder)

    if list_all:
        _list_notes(notes_root, search_root, match)
        return

    if open_selector is not None:
        target = _unique_match(
            open_selector,
            _sorted_notes(search_root),
            notes_root,
            search_root,
        )
        _open_in_editor(target, settings["editor"] if isinstance(settings["editor"], str) else None)
        return

    if browse:
        _browse_directory(search_root)
        return

    note_path = _create_note(
        search_root,
        title_text,
        add_title_heading=bool(settings["add_title_heading"]),
    )
    print(f"[green]Created note[/green]: {_relative_display(note_path, notes_root)}")
    _open_in_editor(note_path, settings["editor"] if isinstance(settings["editor"], str) else None)


if __name__ == "__main__":
    app()
