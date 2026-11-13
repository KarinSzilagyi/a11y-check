# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Karin Szilágyi
"""Queue-mode worker: the same image as the web service, started with `python worker.py`
(an event-driven Container Apps job runs it once per waiting check).

Takes a check from the storage queue, runs the audit, writes progress and the result to the blob
container, then exits. Needs the same settings as app.py in queue mode (STORAGE_ACCOUNT, QUEUE_NAME,
BLOB_CONTAINER, plus the audit settings MAX_PAGES, MAX_STATES, EXPLORE_SECONDS). Optional:
    WORKER_MAX_JOBS     checks one execution runs one after another while more are waiting (default 3)
    WORKER_SECONDS      time one execution may use; keep it below the job's replica timeout (default 3000)
    WORKER_JOB_SECONDS  assumed worst case for one check (default 1200)
"""
import os

os.environ["RUN_MODE"] = "queue"

import app  # noqa: E402

if __name__ == "__main__":
    app.queue_worker()
