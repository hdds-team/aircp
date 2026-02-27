#!/usr/bin/env python3
"""
AIRCP Git Hooks - Mechanical git integration for workflow transitions.

Non-blocking hooks executed at each workflow phase transition.
If git fails, we log + warn but the transition continues.

Hooks:
  -> code        : record_start_commit (git rev-parse HEAD)
  code -> review : checkpoint_commit (git add + commit)
  -> review      : create_snapshot (devit snapshot)
  -> livrable    : tag_release (git tag wf-N)
  -> done        : log_summary (diff start..HEAD)

Design doc: docs/WIP-Git-Hooks-Workflow.md
Brainstorm: IDEA #16 (unanimous GO)

Author: @alpha
"""

import subprocess
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Default repo path (can be overridden per-workflow via metadata.repo_path)
DEFAULT_REPO_PATH = Path("/projects/aircp")


def _run_git(args: list, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command with timeout. Raises on failure."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )


def _get_repo_path(metadata: Optional[Dict] = None) -> Path:
    """Get repo path from workflow metadata or default."""
    if metadata and metadata.get("repo_path"):
        return Path(metadata["repo_path"])
    return DEFAULT_REPO_PATH


def record_start_commit(wf_id: int, metadata: Optional[Dict] = None) -> Optional[str]:
    """Record the current HEAD commit when entering code phase.

    Returns the commit SHA or None on failure.
    """
    repo = _get_repo_path(metadata)
    try:
        # Check for dirty tree (warn but don't block)
        status = _run_git(["status", "--porcelain"], cwd=repo)
        if status.stdout.strip():
            dirty_files = len(status.stdout.strip().splitlines())
            logger.warning(
                f"[GIT-HOOK] wf#{wf_id}: Dirty tree at code start "
                f"({dirty_files} modified files). Consider committing first."
            )

        result = _run_git(["rev-parse", "HEAD"], cwd=repo)
        if result.returncode == 0:
            sha = result.stdout.strip()
            logger.info(f"[GIT-HOOK] wf#{wf_id}: Recorded start commit {sha[:8]}")
            return sha
        else:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: git rev-parse failed: {result.stderr}")
            return None
    except subprocess.TimeoutExpired:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: git rev-parse timed out")
        return None
    except Exception as e:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: record_start_commit error: {e}")
        return None


def checkpoint_commit(wf_id: int, wf_name: str, metadata: Optional[Dict] = None) -> Optional[str]:
    """Auto-commit all changes when transitioning code -> review.

    Uses git add -A scoped to repo + .gitignore as guardrail (Option B).
    Returns the new commit SHA or None if nothing to commit / failure.
    """
    repo = _get_repo_path(metadata)
    try:
        # 1. Check dirty state
        status = _run_git(["status", "--porcelain"], cwd=repo)
        if status.returncode != 0:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: git status failed: {status.stderr}")
            return None

        if not status.stdout.strip():
            logger.info(f"[GIT-HOOK] wf#{wf_id}: Clean tree, no checkpoint needed")
            return None

        # 2. Stage everything (scoped to repo, .gitignore as guardrail)
        add_result = _run_git(["add", "-A", "."], cwd=repo)
        if add_result.returncode != 0:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: git add failed: {add_result.stderr}")
            return None

        # 3. Commit with workflow reference
        msg = f"[wf#{wf_id}] checkpoint: code phase complete - {wf_name}"
        commit_result = _run_git(["commit", "-m", msg], cwd=repo)
        if commit_result.returncode != 0:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: git commit failed: {commit_result.stderr}")
            return None

        # 4. Get the new commit SHA
        sha_result = _run_git(["rev-parse", "HEAD"], cwd=repo)
        if sha_result.returncode == 0:
            sha = sha_result.stdout.strip()
            logger.info(f"[GIT-HOOK] wf#{wf_id}: Checkpoint commit {sha[:8]}")

            # Post-commit log: what was staged (debug trace)
            diff_stat = _run_git(["diff", "--stat", "HEAD~1..HEAD"], cwd=repo)
            if diff_stat.returncode == 0 and diff_stat.stdout.strip():
                logger.info(f"[GIT-HOOK] wf#{wf_id}: Checkpoint diff:\n{diff_stat.stdout.strip()}")

            return sha
        return None

    except subprocess.TimeoutExpired:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: checkpoint_commit timed out")
        return None
    except Exception as e:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: checkpoint_commit error: {e}")
        return None


def create_snapshot(wf_id: int, metadata: Optional[Dict] = None) -> bool:
    """Create a devit filesystem snapshot before review starts.

    Returns True on success, False on failure.
    """
    repo = _get_repo_path(metadata)
    snapshot_name = f"wf-{wf_id}-review"
    try:
        # devit snapshot via MCP tool (subprocess to devit binary)
        # Fallback: just log if devit is not available
        result = subprocess.run(
            ["devit", "snapshot", "create", snapshot_name],
            capture_output=True,
            text=True,
            cwd=repo,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"[GIT-HOOK] wf#{wf_id}: Snapshot '{snapshot_name}' created")
            return True
        else:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: Snapshot failed: {result.stderr}")
            return False
    except FileNotFoundError:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: devit binary not found, skipping snapshot")
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: snapshot timed out")
        return False
    except Exception as e:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: create_snapshot error: {e}")
        return False


def tag_release(wf_id: int, wf_name: str, metadata: Optional[Dict] = None) -> bool:
    """Tag the current commit when entering livrable phase.

    Uses annotated tag (git tag -a) for richer metadata.
    Checks for tag collision (beta's suggestion).
    Returns True on success.
    """
    repo = _get_repo_path(metadata)
    tag_name = f"wf-{wf_id}"
    try:
        # Check if tag already exists (collision guard per beta's review)
        check = _run_git(["tag", "-l", tag_name], cwd=repo)
        if check.returncode == 0 and check.stdout.strip():
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: Tag '{tag_name}' already exists, skipping")
            return False

        # Create annotated tag
        tag_msg = f"Workflow #{wf_id}: {wf_name}"
        result = _run_git(["tag", "-a", tag_name, "-m", tag_msg], cwd=repo)
        if result.returncode == 0:
            logger.info(f"[GIT-HOOK] wf#{wf_id}: Tagged '{tag_name}'")
            return True
        else:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: git tag failed: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: tag_release timed out")
        return False
    except Exception as e:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: tag_release error: {e}")
        return False


def log_summary(wf_id: int, metadata: Optional[Dict] = None) -> Optional[str]:
    """Generate a diff summary between start_commit and current HEAD.

    Returns the summary string or None.
    """
    repo = _get_repo_path(metadata)
    start_commit = None
    if metadata:
        start_commit = metadata.get("start_commit")

    if not start_commit:
        logger.info(f"[GIT-HOOK] wf#{wf_id}: No start_commit in metadata, skipping summary")
        return None

    try:
        # Get shortstat
        result = _run_git(["diff", "--shortstat", f"{start_commit}..HEAD"], cwd=repo)
        if result.returncode != 0:
            logger.warning(f"[GIT-HOOK] wf#{wf_id}: git diff failed: {result.stderr}")
            return None

        shortstat = result.stdout.strip()
        if not shortstat:
            return f"WF#{wf_id}: No changes since start commit {start_commit[:8]}"

        # Get file list
        files_result = _run_git(["diff", "--name-only", f"{start_commit}..HEAD"], cwd=repo)
        file_list = files_result.stdout.strip() if files_result.returncode == 0 else ""

        summary = f"WF#{wf_id} summary: {shortstat}"
        if file_list:
            file_count = len(file_list.splitlines())
            summary += f" ({file_count} files)"

        logger.info(f"[GIT-HOOK] wf#{wf_id}: {summary}")
        return summary

    except subprocess.TimeoutExpired:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: log_summary timed out")
        return None
    except Exception as e:
        logger.warning(f"[GIT-HOOK] wf#{wf_id}: log_summary error: {e}")
        return None


# ==========================================================================
# Dispatcher — called by daemon on phase transitions
# ==========================================================================

def dispatch_git_hooks(
    prev_phase: Optional[str],
    curr_phase: str,
    wf_id: int,
    wf_name: str,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Dispatch git hooks based on phase transition.

    Non-blocking: all errors are caught and logged.
    Returns a dict of hook results for metadata storage.

    Args:
        prev_phase: Previous phase (None if workflow just created)
        curr_phase: New phase after transition
        wf_id: Workflow ID
        wf_name: Workflow name
        metadata: Existing workflow metadata dict (mutable, updated in-place)

    Returns:
        Dict with hook results (to merge into metadata)
    """
    if metadata is None:
        metadata = {}

    results = {}
    errors = metadata.get("git_hook_errors", [])

    try:
        # Hook: entering code phase -> record start commit
        if curr_phase == "code":
            sha = record_start_commit(wf_id, metadata)
            if sha:
                results["start_commit"] = sha
            else:
                errors.append(f"record_start_commit failed at {curr_phase}")

        # Hook: code -> review -> checkpoint commit (THE critical hook)
        if prev_phase == "code" and curr_phase == "review":
            sha = checkpoint_commit(wf_id, wf_name, metadata)
            if sha:
                results["checkpoint_commit"] = sha
            elif sha is None:
                # None means clean tree or failure - check if it was a failure
                # (clean tree is not an error)
                pass

        # Hook: entering review -> create snapshot
        if curr_phase == "review":
            success = create_snapshot(wf_id, metadata)
            if success:
                results["snapshot_id"] = f"wf-{wf_id}-review"
            else:
                errors.append(f"create_snapshot failed at {curr_phase}")

        # Hook: entering livrable -> tag release
        if curr_phase == "livrable":
            success = tag_release(wf_id, wf_name, metadata)
            if success:
                results["tag"] = f"wf-{wf_id}"
            else:
                errors.append(f"tag_release failed at {curr_phase}")

        # Hook: workflow done/completed -> log summary
        if curr_phase in ("done", "completed") or (prev_phase == "livrable"):
            summary = log_summary(wf_id, metadata)
            if summary:
                results["summary"] = summary

    except Exception as e:
        logger.error(f"[GIT-HOOK] wf#{wf_id}: Unhandled error in dispatch: {e}")
        errors.append(f"dispatch error: {str(e)}")

    if errors:
        results["git_hook_errors"] = errors

    return results
