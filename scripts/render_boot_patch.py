#!/usr/bin/env python3
"""Boot-time patcher for the Render image-based deployment.

The Render service runs the prebuilt upstream image
(docker.io/lfnovo/open_notebook:v1-latest), so code fixes cannot be shipped by
pushing to this fork. Instead, the container's dockerCommand downloads and
runs this script at every boot, BEFORE supervisord starts the api/worker/
frontend programs. The patches are small, idempotent and fail-soft: if an
anchor is not found (e.g. upstream image changed), the patch is skipped with
a warning and the boot continues normally with stock behavior.

Why patch at all?
-----------------
surreal-commands 1.3.x workers only ever pick up commands with
status = 'new' (one-time startup scan + live listener for new records).
A command claimed by a worker is flipped to 'running' and only leaves that
state when the same process marks it completed/failed. If the container
restarts mid-job (Render free tier: OOM, suspend/wake, redeploy), the job is
orphaned in 'running' forever. Every future worker boot sees "No existing
commands found" and the UI shows "processing in progress" indefinitely.

Patches applied
---------------
1. worker-requeue (surreal_commands/core/worker.py):
   at the start of listen_for_commands(), requeue any 'running' command back
   to 'new'. Safe in this deployment: a single worker runs per container, so
   when a worker boots, every 'running' command is by definition orphaned.

2. fastfail-missing-file (commands/source_commands.py):
   in process_source_command(), fail immediately (ValueError is in the retry
   stop_on list) when content_state.file_path no longer exists on disk. The
   upload directory /app/data/uploads is ephemeral on Render, so a job
   requeued after a container restart would otherwise burn all 15 retries
   (~25 min of exponential backoff) before finally failing.
"""

from __future__ import annotations

import os
from pathlib import Path

MARKER = "[open-notebook-deploy]"
# BOOT_PATCH_ROOT allows testing the patcher against a sandbox directory that
# mimics the container filesystem (e.g. BOOT_PATCH_ROOT=/tmp/fake-root).
ROOT = Path(os.environ.get("BOOT_PATCH_ROOT", "/"))

WORKER_ANCHOR = (
    "        async with db_connection() as db:\n"
    "            # First, process any existing commands with status 'new'\n"
)

WORKER_INSERT = (
    "            # " + MARKER + " worker-requeue: requeue commands orphaned in\n"
    "            # 'running' state by a worker/container restart. surreal-commands\n"
    "            # 1.3.x only ever scans 'new' commands (startup scan + live\n"
    "            # listener), so a worker death mid-job leaves the job 'running'\n"
    "            # forever and sources stuck on \"processing\". With a single worker\n"
    "            # per container, anything in 'running' at worker boot is by\n"
    "            # definition orphaned.\n"
    "            try:\n"
    "                _stuck = await db.query(\n"
    "                    \"UPDATE command SET status = 'new' WHERE status = 'running'\"\n"
    "                )\n"
    "                console.log(\n"
    "                    \"[bold yellow]\" + \"" + MARKER + " worker-requeue\" +\n"
    "                    \" requeued: \" + str(_stuck)[:200] + \"[/bold yellow]\"\n"
    "                )\n"
    "            except Exception as _requeue_err:\n"
    "                console.log(\n"
    "                    \"[bold red]\" + \"" + MARKER + " worker-requeue failed: \" +\n"
    "                    repr(_requeue_err) + \"[/bold red]\"\n"
    "                )\n"
)

SOURCE_ANCHOR = '        logger.info(f"Embed: {input_data.embed}")\n'

SOURCE_INSERT = (
    "\n"
    "        # " + MARKER + " fastfail-missing-file: the container's upload disk\n"
    "        # (/app/data/uploads) is ephemeral on Render free tier. If the file\n"
    "        # vanished (container restart/suspend mid-processing), fail immediately\n"
    "        # with a clear message instead of burning all 15 retries (~25 min limbo).\n"
    "        import os as _os\n"
    "\n"
    "        _missing_file = (input_data.content_state or {}).get(\"file_path\")\n"
    "        if _missing_file and not _os.path.exists(_missing_file):\n"
    "            raise ValueError(\n"
    "                \"Uploaded file is no longer available on the server (\"\n"
    "                + str(_missing_file)\n"
    "                + \"). The container restarted and the upload was lost. \"\n"
    "                \"Please re-upload the file.\"\n"
    "            )\n"
)


def log(msg: str) -> None:
    print(f"{MARKER} {msg}", flush=True)


def patch_file(path: Path, anchor: str, insert: str, label: str) -> bool:
    """Insert `insert` right after the first occurrence of `anchor`."""
    tag = MARKER + " " + label
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"SKIP {label}: cannot read {path} ({exc})")
        return False

    if tag in text:
        log(f"OK {label}: already patched, skipping")
        return True

    idx = text.find(anchor)
    if idx < 0:
        log(f"SKIP {label}: anchor not found in {path} (upstream image changed?)")
        return False

    patched = text[: idx + len(anchor)] + insert + text[idx + len(anchor) :]
    try:
        path.write_text(patched, encoding="utf-8")
    except OSError as exc:
        log(f"FAIL {label}: cannot write {path} ({exc})")
        return False
    log(f"OK {label}: patched {path}")
    return True


def find_first(patterns: list[str]) -> Path | None:
    for pattern in patterns:
        try:
            matches = sorted(ROOT.glob(pattern.lstrip("/")))
        except OSError:
            continue
        if matches:
            return matches[0]
    return None


def main() -> int:
    log("boot patch starting")

    # ---- Patch 1: worker requeues orphaned 'running' commands at boot ----
    worker_py = find_first(
        [
            "app/.venv/lib/python3.*/site-packages/surreal_commands/core/worker.py",
            "usr/local/lib/python3.*/site-packages/surreal_commands/core/worker.py",
            "usr/lib/python3*/site-packages/surreal_commands/core/worker.py",
            "app/**/site-packages/surreal_commands/core/worker.py",
        ]
    )
    if worker_py is None:
        log("SKIP worker-requeue: surreal_commands worker.py not found in image")
    else:
        patch_file(worker_py, WORKER_ANCHOR, WORKER_INSERT, "worker-requeue")

    # ---- Patch 2: fast-fail process_source when the upload file is gone ----
    source_py = find_first(
        [
            "app/commands/source_commands.py",
            "app/.venv/lib/python3.*/site-packages/commands/source_commands.py",
            "usr/local/lib/python3.*/site-packages/commands/source_commands.py",
        ]
    )
    if source_py is None:
        log("SKIP fastfail-missing-file: commands/source_commands.py not found in image")
    else:
        patch_file(source_py, SOURCE_ANCHOR, SOURCE_INSERT, "fastfail-missing-file")

    log("boot patch finished")
    return 0


if __name__ == "__main__":
    # NOTE: no SystemExit here! This script is exec'd from the container's
    # bootstrap, where __name__ is inherited as "__main__"; raising SystemExit
    # would kill the bootstrap before it execs supervisord.
    main()
