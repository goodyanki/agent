#!/usr/bin/env python3
import os

def generate_tree_lines(root_path, prefix=""):
    """
    Recursively returns a list of ASCII tree lines for files/directories under root_path.
    """
    lines = []
    try:
        entries = sorted(os.listdir(root_path))
    except FileNotFoundError:
        return [f"[Error] Path not found: {root_path}"]

    for idx, entry in enumerate(entries):
        path = os.path.join(root_path, entry)
        connector = "└── " if idx == len(entries) - 1 else "├── "
        lines.append(prefix + connector + entry)
        if os.path.isdir(path):
            extension = "    " if idx == len(entries) - 1 else "│   "
            lines.extend(generate_tree_lines(path, prefix + extension))
    return lines

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate directory tree and save to memory/<project>.txt")
    parser.add_argument('-p', '--path', default="download", help='Root path to walk')
    args = parser.parse_args()

    # Extract project name from path
    project_name = os.path.basename(os.path.normpath(args.path)) or args.path

    # Ensure memory directory exists
    memory_dir = os.path.join(os.getcwd(), 'memory')
    os.makedirs(memory_dir, exist_ok=True)
    output_file = os.path.join(memory_dir, f"{project_name}.txt")

    # Generate tree lines
    lines = [args.path] + generate_tree_lines(args.path)

    # Write to file
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        print(f"Directory tree written to {output_file}")
    except Exception as e:
        print(f"Failed to write tree to file: {e}")
