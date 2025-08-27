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


@app.command('work')
def work(
    path: str = typer.Argument(..., help='Root directory to display as a tree'),
    depth: int = typer.Option(1, '-d', '--depth', help='Depth of the folder to display (default 1)'),
    skip: bool = typer.Option(True, '-s', '--skip-hidden', help='Skip hidden files and directories (default True)'),
    module: str = typer.Option('_script', '-m', '--module', help='Python script name (without .py) to import from the same folder'),
    func: str = typer.Option('_test', '-f', '--func', help='Function name to import and execute from the script'),
):
    """
    Display a tree like `show`, and for each file call the provided script function.
    If the function returns a string, append the string to that file's label; if it returns None, append nothing.
    The script module is searched/created next to the provided path.
    """
    # Validate and normalize path
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        typer.secho(f"Path does not exist: {path}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Determine the folder that contains the Python script to import
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    
    # Load the module from the target folder
    callable_fn = _load_module(folder, module, func)

    # Build and print the tree with callback to append statuses
    tree_obj = Tree(f"Directory: {path}")
    _add_tree(tree_obj, path, depth, current_level=1, skip_hidden=skip, callback=callable_fn)
    print(tree_obj)


def _add_tree(tree, path, max_depth, current_level, skip_hidden=False, callback=None):
    """
    Recursively add files and folders to the tree up to max_depth.
    If a callback is provided, it will be called for each file with the file's full path.
    If the callback returns a string, that string is appended to the filename in the tree.
    """
    # Terminate the tree function when it reaches the max depth
    if max_depth != 0 and current_level > max_depth:
        return

    try:
        path = os.path.expanduser(path)     # Parse the system user folder to path

        for entry in os.listdir(path):

            # Decide whether to work with the hidden/special files
            if skip_hidden and (entry.startswith('.') or entry.startswith('_')):
                continue

            full_path = os.path.join(path, entry)       # Get the full path with entry filenames

            if os.path.isdir(full_path):
                # For directory folders, simply go inside and continue walk through the tree
                branch = tree.add(f" [yellow]{entry}[yellow]")
                _add_tree(branch, full_path, max_depth, current_level + 1, skip_hidden, callback)

            else:
                # For files, run the callback function if provided
                # Append the suffix to the filename if callback return any string
                suffix = ""
                if callback is not None:
                    try:
                        result = callback(full_path)
                        if isinstance(result, str) and result:
                            suffix = result

                    except Exception as e:
                        # Ignore callback errors for tree display
                        typer.secho(f"Callback error for '{full_path}': {e}", fg=typer.colors.RED)

                tree.add(f" {entry}\t[green]{suffix}[green]")

    except PermissionError:
        tree.add("[red]Permission Denied[/red]")


def _load_module(folder, module, func):
    module_file = os.path.join(folder, f"{module}.py")

    # Ensure the module file exists; copy template if available
    if not os.path.isfile(module_file):
        try:
            # Copy template tools/_script.py into place when creating a new module file
            template_path = os.path.join(os.path.dirname(__file__), '_script.py')
            if os.path.isfile(template_path):
                shutil.copyfile(template_path, module_file)
                typer.secho(
                    f"Created module from template: {module_file}",
                    fg=typer.colors.YELLOW,
                )
            
            else:
                # Fallback: create an empty file if template is missing
                # This only happen if the pip install has problem, because the _script.py is stored at site-packages
                with open(module_file, 'w', encoding='utf-8') as f:
                    f.write("")
                typer.secho(
                    f"Template not found. Created empty module file: {module_file}",
                    fg=typer.colors.YELLOW,
                )
        
        except Exception as e:
            typer.secho(f"Failed to create module file '{module_file}': {e}", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    # Import the module and function
    try:
        # Load the spec of module from specific file location
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
            try:
                # If no function in the module file, append an empty function stub to the module file
                with open(module_file, 'a', encoding='utf-8') as f:
                    f.write(f"def {func}(filepath: str):\n")
                    f.write("    \"\"\"Auto-created stub. Return None.\"\"\"\n")
                    f.write("    return\n")

                typer.secho(
                    f"Function '{func}' not found in module '{module}'. Created empty function stub.",
                    fg=typer.colors.YELLOW,
                )
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
        
        return callable_fn

    except Exception as exc:
        typer.secho(f"Error loading module/function: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


# Entry point for running the script directly
if __name__ == '__main__':
    app()
