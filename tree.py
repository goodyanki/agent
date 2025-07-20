import os
import re
import argparse
from typing import Dict, List, Optional, Any

# --- 正则表达式部分 ---
JS_TS_FUNCTION_REGEX = re.compile(
    r'^(?:'
    r'|(?:\s*export\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*\('
    r'|(?:\s*export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*(?:async)?\s*(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
    r'|(?:\s*export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_]+)\s*=\s*function\s*\('
    r'|(?:\s*public\s+|private\s+|protected\s+)?(?:static\s+)?(?:async\s+)?([a-zA-Z0-9_]+)\s*\([^)]*\)\s*(?:[:{]|\s*=>)'
    r'|([a-zA-Z0-9_]+)\s*:\s*(?:async\s*)?function\s*\('
    r'|([a-zA-Z0-9_]+)\s*:\s*(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'
    r')', re.MULTILINE
)

# 【优化点】修改 Rust 的正则表达式
# 1. 去掉了行首的 `^`，允许 `fn` 出现在行内任意位置（处理 impl 块内的缩进）
# 2. 在 `finditer` 中使用 re.MULTILINE，使其能正确处理多行代码
RUST_FUNCTION_REGEX = re.compile(
    r'\s*(?:pub(?:\([^)]+\))?\s+)?(?:const\s+)?(?:async\s+)?fn\s+([a-zA-Z0-9_]+)'
)

def extract_functions_from_file(file_path: str) -> Optional[List[str]]:
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return []

    functions = set()
    regex = None
    flags = re.MULTILINE

    if file_path.endswith(('.js', '.mjs', '.ts')):
        regex = JS_TS_FUNCTION_REGEX
    elif file_path.endswith('.rs'):
        regex = RUST_FUNCTION_REGEX
    else:
        return None

    matches = regex.finditer(content)
    for match in matches:
        func_name = next((g for g in match.groups() if g), None)
        if func_name and not func_name.isspace():
            functions.add(func_name.strip())
    return sorted(list(functions))

# ... 后续的 build_full_directory_tree, print_tree, main 函数保持不变 ...
def build_full_directory_tree(root_dir: str) -> Dict[str, Any]:
    tree = {}
    ignored_dirs = {'.git', 'node_modules', 'target', '__pycache__', '.vscode'}

    for root, dirs, files in os.walk(root_dir, topdown=True):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        parts = os.path.relpath(root, root_dir).split(os.sep)
        
        if parts == ['.']:
            current_level = tree
        else:
            current_level = tree
            for part in parts:
                current_level = current_level.setdefault(part, {})

        for file in files:
            current_level[file] = None
            
    return tree

def print_tree(tree_node: Dict[str, Any], base_path: str, prefix: str = ""):
    items = sorted(tree_node.items())
    for i, (name, content) in enumerate(items):
        is_last = i == (len(items) - 1)
        connector = "└── " if is_last else "├── "
        print(f"{prefix}{connector}{name}")

        new_prefix = prefix + ("    " if is_last else "│   ")
        current_path = os.path.join(base_path, name)

        if isinstance(content, dict):
            print_tree(content, current_path, new_prefix)
        else:
            if name.endswith(('.rs', '.js', '.mjs', '.ts')):
                functions = extract_functions_from_file(current_path)
                if functions:
                    for func in functions:
                        print(f"{new_prefix}  - {func}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Analyze and display the full directory structure, showing functions in various files."
    )
    parser.add_argument(
        '-d', '--directory',
        type=str,
        default='.',
        help="The path to the directory you want to analyze. Defaults to the current directory."
    )
    args = parser.parse_args()
    target_directory = os.path.abspath(args.directory)

    if not os.path.isdir(target_directory):
        print(f"Error: Directory '{target_directory}' not found.")
    else:
        full_tree = build_full_directory_tree(target_directory)
        
        root_name = os.path.basename(target_directory)
        
        print(f"\nAnalysis for: {target_directory}\n")
        print(f"{root_name}/")
        print_tree(full_tree, target_directory)