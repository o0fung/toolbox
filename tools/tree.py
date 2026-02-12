from __future__ import annotations

import importlib
import importlib.util
import hashlib
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import typer
from rich import print
from rich.tree import Tree

try:
    from ._cli_output import error, fatal, warn
    from ._cli_common import new_typer_app
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_output import error, fatal, warn
    from tools._cli_common import new_typer_app


app = new_typer_app()

CallbackFn = Callable[[str], object]
DEFAULT_WORK_MODULE = "_script"
DEFAULT_WORK_FUNC = "_test"


@app.command("show")
def show(
    path: str = typer.Argument(..., help="Root path (directory or file) to display as a tree"),
    depth: int = typer.Option(0, "-d", "--depth", help="Depth to display (0 means unlimited)"),
    skip: bool = typer.Option(False, "-s", "--skip-hidden", help="Skip hidden files and directories"),
):
    """Display a directory/file tree."""
    target = _resolve_existing_path(path)
    _validate_depth(depth)

    tree_obj = _build_tree(
        target=target,
        max_depth=depth,
        skip_hidden=skip,
        callback=None,
    )
    print(tree_obj)


@app.command("work")
def work(
    path: str = typer.Argument(..., help="Target path (directory or file)"),
    depth: int = typer.Option(1, "-d", "--depth", help="Depth to display/process (default: 1)"),
    skip: bool = typer.Option(True, "-s", "--skip-hidden", help="Skip hidden files and directories (default: True)"),
    module: str = typer.Option(DEFAULT_WORK_MODULE, "-m", "--module", help="Python module name (without .py) next to target path"),
    func: str = typer.Option(DEFAULT_WORK_FUNC, "-f", "--func", help="Function name to call for each discovered file"),
):
    """Display a tree and invoke a callback function for each file."""
    target = _resolve_existing_path(path)
    _validate_depth(depth)

    module_folder = target if target.is_dir() else target.parent
    callback = _load_module(module_folder, module, func)

    tree_obj = _build_tree(
        target=target,
        max_depth=depth,
        skip_hidden=skip,
        callback=callback,
    )
    print(tree_obj)


def _resolve_existing_path(raw_path: str) -> Path:
    expanded = Path(raw_path).expanduser()
    if not expanded.exists():
        raise typer.BadParameter(f"Path does not exist: {expanded}")
    return expanded


def _validate_depth(depth: int) -> None:
    if depth < 0:
        raise typer.BadParameter("Depth must be >= 0 (0 means unlimited).")


def _build_tree(
    target: Path,
    max_depth: int,
    skip_hidden: bool,
    callback: CallbackFn | None,
) -> Tree:
    tree_obj = Tree(f"Directory: {target}")
    if target.is_file():
        _add_file_node(tree_obj, target, callback=callback)
        return tree_obj

    _add_tree(
        tree=tree_obj,
        directory=target,
        max_depth=max_depth,
        current_level=1,
        skip_hidden=skip_hidden,
        callback=callback,
    )
    return tree_obj


def _add_tree(
    tree: Tree,
    directory: Path,
    max_depth: int,
    current_level: int,
    skip_hidden: bool = False,
    callback: CallbackFn | None = None,
) -> None:
    """Recursively add files and folders to the rendered tree."""
    if max_depth and current_level > max_depth:
        return

    try:
        entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        tree.add("[red]Permission Denied[/red]")
        return

    for entry in entries:
        if _should_skip(entry.name, skip_hidden=skip_hidden):
            continue

        if entry.is_dir():
            branch = tree.add(f" [yellow]{entry.name}[/yellow]")
            _add_tree(
                tree=branch,
                directory=entry,
                max_depth=max_depth,
                current_level=current_level + 1,
                skip_hidden=skip_hidden,
                callback=callback,
            )
            continue

        _add_file_node(tree, entry, callback=callback)


def _should_skip(name: str, skip_hidden: bool) -> bool:
    return skip_hidden and (name.startswith(".") or name.startswith("_"))


def _add_file_node(tree: Tree, file_path: Path, callback: CallbackFn | None) -> None:
    suffix = _run_callback(callback, file_path)
    if suffix:
        tree.add(f" {file_path.name}\t[green]{suffix}[/green]")
        return
    tree.add(f" {file_path.name}")


def _run_callback(callback: CallbackFn | None, file_path: Path) -> str:
    if callback is None:
        return ""

    try:
        result = callback(str(file_path))
    except Exception as exc:  # pragma: no cover - user script runtime errors
        warn(f"Callback error for '{file_path}': {exc}")
        return ""

    return result if isinstance(result, str) else ""


def _load_module(folder: Path, module: str, func: str) -> CallbackFn:
    module_file = folder / f"{module}.py"
    _ensure_module_file(module_file)

    try:
        mod = _import_module(module_name=module, module_file=module_file)
    except Exception as exc:
        fatal(f"Error loading module '{module_file}': {exc}")

    if not hasattr(mod, func):
        try:
            _append_function_stub(module_file, func)
            warn(f"Function '{func}' not found in module '{module}'. Created empty function stub.")
            importlib.invalidate_caches()
            mod = _import_module(module_name=module, module_file=module_file)
        except Exception as exc:
            error(
                f"Failed to create function stub '{func}' in module '{module}': {exc}",
            )
            raise typer.Exit(code=1)

    callable_fn = getattr(mod, func)
    if not callable(callable_fn):
        fatal(f"Attribute '{func}' in module '{module}' is not callable.")

    return cast(CallbackFn, callable_fn)


def _ensure_module_file(module_file: Path) -> None:
    if module_file.is_file():
        return

    try:
        template_path = Path(__file__).with_name("_script.py")
        if template_path.is_file():
            shutil.copyfile(template_path, module_file)
            warn(f"Created module from template: {module_file}")
            return

        # Fallback if template is unavailable.
        module_file.write_text("", encoding="utf-8")
        warn(f"Template not found. Created empty module file: {module_file}")
    except Exception as exc:
        fatal(f"Failed to create module file '{module_file}': {exc}")


def _append_function_stub(module_file: Path, func_name: str) -> None:
    with module_file.open("a", encoding="utf-8") as handle:
        handle.write(f"\n\ndef {func_name}(filepath: str):\n")
        handle.write('    """Auto-created stub. Return None."""\n')
        handle.write("    return\n")


def _import_module(module_name: str, module_file: Path) -> ModuleType:
    digest = hashlib.sha1(str(module_file.resolve()).encode("utf-8")).hexdigest()[:12]
    module_key = f"_toolbox_tree_{module_name}_{digest}"
    spec = importlib.util.spec_from_file_location(module_key, str(module_file))
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to prepare import spec for module.")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    app()
