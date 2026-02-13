import argparse
import hashlib
import os
from pathlib import Path
from shutil import copy2

def get_hash(filepath):
    """Calculate the SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            data = f.read(65536)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()

def sync_dir(source_base, master_base, sub_dir, execute, verbose):
    """Sync a specific subdirectory (e.g. agents or skills)."""
    source_dir = Path(source_base) / sub_dir
    master_dir = Path(master_base) / sub_dir
    
    new_files = []
    modified_files = []

    if not source_dir.exists():
        return [], []

    for root, dirs, files in os.walk(source_dir):
        for file in files:
            source_file = Path(root) / file
            # Calculate relative path from source_dir to keep structure aligned
            rel_path = source_file.relative_to(source_base)
            master_file = Path(master_base) / rel_path

            if not master_file.exists():
                new_files.append((source_file, master_file))
            elif get_hash(source_file) != get_hash(master_file):
                modified_files.append((source_file, master_file))

    if verbose:
        for src, dst in new_files:
            print(f"🆕 New file: {src}")
        for src, dst in modified_files:
            print(f"📝 Modified file: {src}")

    if execute:
        for src, dst in new_files:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy2(src, dst)
        for src, dst in modified_files:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy2(src, dst)

    return new_files, modified_files

def merge_memories(source_dir, master_dir, execute, verbose):
    """Merge memories from source to master."""
    source_gemini = Path(source_dir) / '.gemini' / 'GEMINI.md'
    master_gemini = Path(master_dir) / '.gemini' / 'GEMINI.md'

    if not source_gemini.exists() or not master_gemini.exists():
        return []

    # Simple extraction logic (could be more robust with regex)
    def extract_memories(path):
        memories = []
        in_memory_section = False
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if '## Gemini Added Memories' in line:
                    in_memory_section = True
                    continue
                if in_memory_section and line.startswith('##'):
                    in_memory_section = False
                    break
                if in_memory_section and line.strip().startswith('-'):
                    memories.append(line.strip())
        return memories

    try:
        source_memories = extract_memories(source_gemini)
        master_memories = extract_memories(master_gemini)
    except Exception as e:
        print(f"⚠️ Error reading memories: {e}")
        return []

    new_memories = [m for m in source_memories if m not in master_memories]

    if verbose:
        for new_memory in new_memories:
            print(f"🧠 New memory: {new_memory}")

    if execute and new_memories:
        # Append to the end of the file, assuming it's safe. 
        # Ideally we'd insert into the section, but appending is safer than parsing for now.
        with open(master_gemini, 'a', encoding='utf-8') as f:
            f.write('\n\n## Gemini Added Memories (Distilled)\n')
            for new_memory in new_memories:
                f.write(f"{new_memory}\n")

    return new_memories

import subprocess

def ensure_branch(repo_path, target_branch):
    """Ensure the target repository is on the specified branch."""
    repo_path = Path(repo_path).resolve()
    
    try:
        # Find the git root
        root_result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=repo_path, capture_output=True, text=True, check=True
        )
        git_root = Path(root_result.stdout.strip())
        
        # Get current branch
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], 
            cwd=git_root, capture_output=True, text=True, check=True
        )
        current_branch = result.stdout.strip()

        if current_branch == target_branch:
            print(f"✅ Master is on correct branch: {target_branch}")
            return True

        print(f"🔄 Switching Master from '{current_branch}' to '{target_branch}'...")
        
        # Check for dirty state
        status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=git_root, capture_output=True, text=True, check=True
        )
        if status.stdout.strip():
            print(f"❌ Abort: Master repository at {git_root} has uncommitted changes. Commit or stash them first.")
            return False

        # Checkout
        subprocess.run(['git', 'checkout', target_branch], cwd=git_root, check=True)
        print(f"✅ Switched to {target_branch}")
        return True

    except subprocess.CalledProcessError:
        print(f"⚠️ Path {repo_path} is not inside a git repository. Skipping branch check.")
        return True
    except Exception as e:
        print(f"❌ Error managing git branch: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description='Synchronize knowledge from source to master.')
    parser.add_argument('--source', required=True, help='Path to the deployed/child project')
    parser.add_argument('--master', default=os.getcwd(), help='Path to the master/current project')
    parser.add_argument('--branch', default='skill_dev_branch', help='Target branch in Master repo (default: skill_dev_branch)')
    parser.add_argument('--execute', action='store_true', help='Execute the synchronization (default: dry run)')
    parser.add_argument('--verbose', action='store_true', help='Show detailed logs')
    args = parser.parse_args()

    print(f"🧪 Knowledge Distiller running...")
    print(f"Source: {args.source}")
    print(f"Master: {args.master}")
    print(f"Target Branch: {args.branch}")

    if args.execute:
        if not ensure_branch(args.master, args.branch):
            print("❌ Synchronization aborted due to branch/git issues.")
            return

    all_new = 0
    all_mod = 0

    # Sync Agents
    n, m = sync_dir(args.source, args.master, 'agents', args.execute, args.verbose)
    all_new += len(n)
    all_mod += len(m)

    # Sync Skills
    n, m = sync_dir(args.source, args.master, 'skills', args.execute, args.verbose)
    all_new += len(n)
    all_mod += len(m)

    # Sync Memories
    memories = merge_memories(args.source, args.master, args.execute, args.verbose)

    print(f"\nSummary:")
    print(f"  🆕 New files: {all_new}")
    print(f"  📝 Modified files: {all_mod}")
    print(f"  🧠 New memories: {len(memories)}")

    if not args.execute:
        print("(DRY RUN - No changes made. Use --execute to apply.)")

if __name__ == "__main__":
    main()
