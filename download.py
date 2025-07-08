#!/usr/bin/env python3
import os
from git import Repo

def clone_repo(repo_url, target_dir="download"):
    """
    Clone the given repository URL into the target directory.
    If the directory exists, it will be reused.
    """
    os.makedirs(target_dir, exist_ok=True)
    try:
        print(f"Cloning {repo_url} into {target_dir} ...")
        Repo.clone_from(repo_url, target_dir)
        print("Clone completed successfully.")
    except Exception as e:
        print(f"Clone failed: {e}")
        raise

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clone a git repo into ./download")
    parser.add_argument('-u', '--url', required=True, help='Git repository URL to clone')
    args = parser.parse_args()
    clone_repo(args.url)
