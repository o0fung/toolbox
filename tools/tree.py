import typer
import os
import sys
import importlib
import shutil

from rich.tree import Tree      # for display tree directory
from rich import print


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})

# Main CLI entry point, allows invocation without subcommand (entry from go.py)
@app.command('show')
def show(
    path: str = typer.Argument(..., help='Root directory to display as a tree'),
    depth: int = typer.Option(0, '-d', '--depth', help='Depth of the folder to display'),
    skip: bool = typer.Option(False, '-s', '--skip-hidden', help='Skip hidden files and directories'),
):
    """
    Main entry point for the CLI. Builds and prints a directory tree for the given path.
    Args:
        path (str): The root directory to display as a tree.
        depth (int): The maximum depth to display. 0 means unlimited.
        skip (bool): If True, skip hidden files and directories.
    """
    # If no subcommand, require PATH
    if not path:
        raise typer.BadParameter("PATH is required when no subcommand is provided.")

    tree_obj = Tree(f"Directory: {path}")       # Create the root of the tree
    _add_tree(tree_obj, path, depth, current_level=1, skip_hidden=skip)
    print(tree_obj)                             # Print the tree using rich


@app.command('rename')
def rename(
    path: str = typer.Argument(..., help='Path to a file or directory; a Python script in the same folder will be imported'),
    module: str = typer.Option('script', '-m', '--module', help='Python script name (without .py) to import from the same folder'),
    func: str = typer.Option('test', '-f', '--func', help='Function name to import and execute from the script'),
    recursive: bool = typer.Option(False, '-r', '--recursive', help='If path is a directory, process files in subfolders recursively'),
):
    """
    Import a function from a Python script located in the same folder as the provided path,
    then execute it for each file discovered under the provided path.

    Defaults:
    - module: 'script' (expects a file named script.py next to path)
    - func:   'test' (expects a callable named test in that file)
    - recursive: process subfolders when path is a directory
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        typer.secho(f"Path does not exist: {path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Determine the folder that contains the Python script to import
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    module_file = os.path.join(folder, f"{module}.py")

    # Make sure the module file is available
    if not os.path.isfile(module_file):
        # Copy template tools/script.py into place when creating a new module file
        try:
            template_path = os.path.join(os.path.dirname(__file__), 'script.py')
            if os.path.isfile(template_path):
                shutil.copyfile(template_path, module_file)
                typer.secho(
                    f"Created module from template: {module_file}",
                    fg=typer.colors.YELLOW,
                )
            else:
                # Fallback: create an empty file if template is missing
                with open(module_file, 'w', encoding='utf-8') as f:
                    f.write("")
                typer.secho(
                    f"Template not found. Created empty module file: {module_file}",
                    fg=typer.colors.YELLOW,
                )
        except Exception as e:
            typer.secho(f"Failed to create module file '{module_file}': {e}", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    try:
        # Load module from specific file location
        spec = importlib.util.spec_from_file_location(module, module_file)
        if spec is None or spec.loader is None:
            typer.secho("Failed to prepare import spec for module.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        # Register temporarily so relative imports inside the module might work
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module] = mod
        spec.loader.exec_module(mod)

        # Make sure the function is available in the module file
        if not hasattr(mod, func):
            # Append an empty function stub to the module file
            try:
                with open(module_file, 'a', encoding='utf-8') as f:
                    f.write(f"def {func}(filepath: str):\n")
                    f.write("    \"\"\"Auto-created stub. Implement your rename logic here.\"\"\"\n")
                    f.write("    pass\n")
                typer.secho(
                    f"Function '{func}' not found in module '{module}'. Created empty function stub.",
                    fg=typer.colors.YELLOW,
                )

                # Reload the module to include the new function
                importlib.invalidate_caches()
                spec.loader.exec_module(mod)

            except Exception as e:
                typer.secho(f"Failed to create function stub '{func}' in module '{module}': {e}", fg=typer.colors.RED)
                raise typer.Exit(code=1)

        # Make sure the function is callable
        callable_fn = getattr(mod, func)
        if not callable(callable_fn):
            typer.secho(f"Attribute '{func}' in module '{module}' is not callable.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        # Build an iterator of files to process
        def _iter_files(target_path: str, do_recursive: bool):
            
            # Input path is a file, simply run the process on this file
            if os.path.isfile(target_path):
                yield target_path

            # Input path is a folder, see if we do recursive scan
            elif os.path.isdir(target_path):

                if do_recursive:
                    # Recursively run the process on all files inside the folder
                    for root, _dirs, files in os.walk(target_path):
                        for name in files:
                            yield os.path.join(root, name)

                else:
                    # Only run the process on files of the current folder
                    for name in os.listdir(target_path):
                        full = os.path.join(target_path, name)
                        if os.path.isfile(full):
                            yield full

        # Run target process on files of the target folder
        processed = 0
        for file_path in _iter_files(path, recursive):
            try:
                # Print and return function parameter for each success file
                result = callable_fn(file_path)
                if result is not None:
                    print(result)

                processed += 1      # Counting success file worked

            except Exception as file_exc:
                typer.secho(f"Error processing {file_path}: {file_exc}", fg=typer.colors.RED)

        typer.secho(f"Processed {processed} file(s).", fg=typer.colors.GREEN)

    except Exception as exc:
        typer.secho(f"Error executing imported function: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


def _add_tree(tree, path, max_depth, current_level, skip_hidden=False):
    """
    Recursively add files and folders to the tree up to max_depth.
    Args:
        tree (Tree): The rich Tree object to add branches to.
        path (str): The directory path to scan.
        max_depth (int): The maximum depth to display. 0 means unlimited.
        current_level (int): The current depth level in recursion.
        skip_hidden (bool): If True, skip hidden files and directories.
    """
    # If max_depth is set and we've reached it, stop recursion
    if max_depth != 0 and current_level > max_depth:
        return
    
    try:
        # List all entries in the directory
        for entry in os.listdir(os.path.expanduser(path)):
            if skip_hidden and (entry.startswith('.') or entry.startswith('_')):
                continue  # Skip hidden files and directories

            full_path = os.path.join(os.path.expanduser(path), entry)   # Get full path
            if os.path.isdir(full_path):            
                # If entry is a directory, Add directory as a branch
                branch = tree.add(f" {entry}")
                # Recurse into subdirectory
                _add_tree(branch, full_path, max_depth, current_level + 1, skip_hidden)
            
            else:
                tree.add(f" {entry}")               # Add file as a leaf
    
    except PermissionError:
        tree.add("[red]Permission Denied[/red]")    # Handle permission errors


# Entry point for running the script directly
if __name__ == '__main__':
    app()
