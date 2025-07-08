#!/usr/bin/env python3
import argparse
import os
from download import clone_repo
from tree import generate_tree_lines

def main():
    parser = argparse.ArgumentParser(
        description="Clone a repo into download/<project> and save its tree into memory/<project>.txt"
    )
    parser.add_argument(
        '-u', '--url', required=True,
        help='Git repository URL to clone'
    )
    parser.add_argument(
        '-d', '--dir', default="download",
        help='Base download directory'
    )
    args = parser.parse_args()

    # 从 URL 中提取仓库名（例如 single-pool）
    repo_name = args.url.rstrip('/').split('/')[-1]
    # 拼出目标克隆路径：download/single-pool
    clone_dir = os.path.join(args.dir, repo_name)

    # 1. 克隆仓库到 download/<repo_name>
    clone_repo(args.url, clone_dir)

    # 2. 准备 memory 目录
    memory_dir = os.path.join(os.getcwd(), 'memory')
    os.makedirs(memory_dir, exist_ok=True)

    # 3. 生成目录树并写入 memory/<repo_name>.txt
    output_file = os.path.join(memory_dir, f"{repo_name}.txt")
    lines = [clone_dir] + generate_tree_lines(clone_dir)
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines))
        print(f"Directory tree written to {output_file}")
    except Exception as e:
        print(f"Failed to write tree to file: {e}")

if __name__ == "__main__":
    main()
