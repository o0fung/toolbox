import typer
import os

from rich.tree import Tree      # for display tree directory
from rich import print


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})

# Main CLI entry point, allows invocation without subcommand (entry from go.py)
@app.callback()
def tree(
    path: str = typer.Argument(..., help='Root directory to display as a tree'),
    depth: int = typer.Option(0, '-d', '--depth', help='Depth of the folder to display'),
    skip: bool = typer.Option(False, '-s', '--skip-hidden', help='Skip hidden files and directories'
    )
):
    """
    Main entry point for the CLI. Builds and prints a directory tree for the given path.
    Args:
        path (str): The root directory to display as a tree.
        depth (int): The maximum depth to display. 0 means unlimited.
        skip (bool): If True, skip hidden files and directories.
    """
    tree_obj = Tree(f"Directory: {path}")       # Create the root of the tree
    _add_tree(tree_obj, path, depth, current_level=1, skip_hidden=skip)
    print(tree_obj)                             # Print the tree using rich


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
        for entry in os.listdir(path):
            if skip_hidden and entry.startswith('.'):
                continue  # Skip hidden files and directories

            full_path = os.path.join(path, entry)   # Get full path
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
    typer.run(tree)
