from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

from .model import ModelClient
from .prompts import MEMORY_STAGE_ONE_MODEL
from .prompts import MEMORY_STAGE_ONE_REASONING_EFFORT
from .prompts import MEMORY_STAGE_TWO_MODEL
from .prompts import MEMORY_STAGE_TWO_REASONING_EFFORT
from .prompts import MEMORY_PHASE2_WORKSPACE_DIFF_FILE
from .prompts import build_memory_stage_one_input_message
from .prompts import build_memory_consolidation_prompt
from .prompts import memory_stage_one_system_prompt
from .prompts import read_asset
from .types import VolleyConfig, VolleyResult, PromptRequest


@dataclass(frozen=True)
class MemoryStageOneOutput:
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None


@dataclass(frozen=True)
class MemoryStageOneRecord:
    thread_id: str
    source_updated_at: datetime
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None
    rollout_path: Path | str
    cwd: Path | str
    git_branch: str | None = None
    usage_count: int = 0
    last_usage: datetime | None = None
    selected_for_phase2: bool = False


@dataclass(frozen=True)
class MemoryWorkspaceChange:
    status: str
    path: str


@dataclass(frozen=True)
class MemoryRollout:
    thread_id: str
    rollout_path: Path
    cwd: Path
    source_updated_at: datetime
    git_branch: str | None
    source: str
    memory_mode: str
    items: list[dict[str, Any]]
    serialized_contents: str


@dataclass(frozen=True)
class MemoryStartupResult:
    records: list[MemoryStageOneRecord]
    skipped: list[Path]
    memory_root: Path
    status: str = "completed"
    phase2_result: Any | None = None
    rate_limit_allowed: bool | None = None


@dataclass(frozen=True)
class MemoryPhase2Result:
    status: str
    selected: list[MemoryStageOneRecord]
    memory_root: Path
    workspace_changed: bool = False
    final_message: str = ""


@dataclass(frozen=True)
class MemoryThreadRecord:
    thread_id: str
    rollout_path: Path | str
    cwd: Path | str
    updated_at: datetime
    created_at: datetime | None = None
    source: str = "cli"
    memory_mode: str = "enabled"
    git_branch: str | None = None


@dataclass(frozen=True)
class MemoryJobClaim:
    outcome: str
    ownership_token: str | None = None
    input_watermark: int | None = None


@dataclass(frozen=True)
class MemoryStageOneStartupClaim:
    thread_id: str
    rollout_path: Path
    source_updated_at: datetime
    ownership_token: str


class MemoryBackgroundTask:
    def __init__(self, *, kind: str, target: Any):
        self.kind = kind
        self.result: Any | None = None
        self.error: BaseException | None = None
        self.started_at = datetime.now(timezone.utc)
        self.completed_at: datetime | None = None
        self._target = target
        self._thread = threading.Thread(target=self._run, name=f"volley-{kind}", daemon=True)
        self._thread.start()

    @property
    def status(self) -> str:
        if self._thread.is_alive():
            return "running"
        if self.error is not None:
            return "failed"
        return "completed"

    def join(self, timeout: float | None = None) -> Any | None:
        self._thread.join(timeout)
        return self.result

    def done(self) -> bool:
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            self.result = self._target()
        except BaseException as exc:
            self.error = exc
        finally:
            self.completed_at = datetime.now(timezone.utc)


MEMORY_WORKSPACE_DIFF_MAX_BYTES = 4 * 1024 * 1024
MEMORY_MAX_UNUSED_DAYS = 30
MEMORY_MAX_ROLLOUT_AGE_DAYS = 10
MEMORY_MIN_ROLLOUT_IDLE_HOURS = 6
MEMORY_MAX_RAW_MEMORIES_FOR_CONSOLIDATION = 256
MEMORY_EXTENSION_RESOURCE_RETENTION_DAYS = 7
MEMORY_EXTENSION_RESOURCE_TIMESTAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"
MEMORY_AD_HOC_EXTENSION_INSTRUCTIONS_ASSET = "prompts/memories/write/extensions/ad_hoc/instructions.md"
MEMORY_STAGE1_JOB_KIND = "memory_stage1"
MEMORY_PHASE2_JOB_KIND = "memory_consolidate_global"
MEMORY_PHASE2_JOB_KEY = "global"
MEMORY_PHASE2_SUCCESS_COOLDOWN_SECONDS = 6 * 60 * 60
MEMORY_DEFAULT_RETRY_REMAINING = 3
MEMORY_MAX_ROLLOUTS_PER_STARTUP = 2
MEMORY_STAGE1_THREAD_SCAN_LIMIT = 5_000
MEMORY_STAGE1_JOB_LEASE_SECONDS = 3_600
MEMORY_STAGE1_JOB_RETRY_DELAY_SECONDS = 3_600
MEMORY_STAGE1_PRUNE_BATCH_SIZE = 200
MEMORY_MIN_RATE_LIMIT_REMAINING_PERCENT = 25
INTERACTIVE_MEMORY_SESSION_SOURCES = frozenset({"cli", "vscode", "atlas", "chatgpt"})


class MemoryStateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_schema()

    @classmethod
    def open_volley_home(cls, volley_home: Path | str) -> "MemoryStateStore":
        return cls(Path(volley_home) / "state" / "memory.sqlite3")

    def close(self) -> None:
        self.conn.close()

    def upsert_thread(self, record: MemoryThreadRecord) -> None:
        created_at = _timestamp(record.created_at or record.updated_at)
        updated_at = _timestamp(record.updated_at)
        with self.conn:
            self.conn.execute(
                """
INSERT INTO threads (
    id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
    sandbox_policy, approval_mode, git_branch, memory_mode
) VALUES (?, ?, ?, ?, ?, 'openai', ?, '', '', '', ?, ?)
ON CONFLICT(id) DO UPDATE SET
    rollout_path = excluded.rollout_path,
    updated_at = excluded.updated_at,
    source = excluded.source,
    cwd = excluded.cwd,
    git_branch = excluded.git_branch
                """,
                (
                    record.thread_id,
                    str(record.rollout_path),
                    created_at,
                    updated_at,
                    record.source,
                    str(record.cwd),
                    record.git_branch,
                    record.memory_mode,
                ),
            )

    def set_thread_memory_mode(self, thread_id: str, memory_mode: str) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE threads SET memory_mode = ? WHERE id = ?",
                (memory_mode, thread_id),
            )
        return cursor.rowcount > 0

    def clear_memory_data(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM stage1_outputs")
            self.conn.execute(
                "DELETE FROM jobs WHERE kind IN (?, ?)",
                (MEMORY_STAGE1_JOB_KIND, MEMORY_PHASE2_JOB_KIND),
            )

    def record_stage1_output_usage(self, thread_ids: list[str], *, now: datetime | None = None) -> int:
        if not thread_ids:
            return 0
        ts = _timestamp(now or datetime.now(timezone.utc))
        updated = 0
        with self.conn:
            for thread_id in thread_ids:
                cursor = self.conn.execute(
                    """
UPDATE stage1_outputs
SET usage_count = COALESCE(usage_count, 0) + 1,
    last_usage = ?
WHERE thread_id = ?
                    """,
                    (ts, thread_id),
                )
                updated += cursor.rowcount
        return updated

    def claim_stage1_jobs_for_startup(
        self,
        *,
        current_thread_id: str | None,
        scan_limit: int,
        max_claimed: int,
        max_age_days: int,
        min_rollout_idle_hours: int,
        allowed_sources: set[str] | frozenset[str],
        lease_seconds: int,
        max_running_jobs: int,
        now: datetime | None = None,
    ) -> list[MemoryStageOneStartupClaim]:
        if scan_limit <= 0 or max_claimed <= 0:
            return []

        reference = _to_utc(now or datetime.now(timezone.utc))
        max_age_cutoff = _timestamp(reference - timedelta(days=max(max_age_days, 0)))
        idle_cutoff = _timestamp(reference - timedelta(hours=max(min_rollout_idle_hours, 0)))
        source_clause = ""
        source_args: list[str] = []
        if allowed_sources:
            source_args = sorted(str(source) for source in allowed_sources)
            placeholders = ", ".join("?" for _ in source_args)
            source_clause = f" AND threads.source IN ({placeholders})"

        rows = self.conn.execute(
            f"""
SELECT
    threads.id,
    threads.rollout_path,
    threads.updated_at
FROM threads
LEFT JOIN stage1_outputs
    ON stage1_outputs.thread_id = threads.id
LEFT JOIN jobs
    ON jobs.kind = ?
   AND jobs.job_key = threads.id
WHERE threads.archived = 0
  AND threads.memory_mode = 'enabled'
  AND threads.id != ?
  AND threads.updated_at >= ?
  AND threads.updated_at <= ?
  AND ((COALESCE(stage1_outputs.source_updated_at, -1) + 1) <= threads.updated_at)
  AND ((COALESCE(jobs.last_success_watermark, -1) + 1) <= threads.updated_at)
  {source_clause}
ORDER BY threads.updated_at DESC, threads.id DESC
LIMIT ?
            """,
            (
                MEMORY_STAGE1_JOB_KIND,
                current_thread_id or "",
                max_age_cutoff,
                idle_cutoff,
                *source_args,
                scan_limit,
            ),
        ).fetchall()

        worker_id = current_thread_id or str(uuid4())
        claims: list[MemoryStageOneStartupClaim] = []
        for row in rows:
            if len(claims) >= max_claimed:
                break
            source_updated_at = int(row["updated_at"])
            claim = self.try_claim_stage1_job(
                thread_id=str(row["id"]),
                worker_id=worker_id,
                source_updated_at=source_updated_at,
                lease_seconds=lease_seconds,
                max_running_jobs=max_running_jobs,
                now=reference,
            )
            if claim.outcome != "claimed" or claim.ownership_token is None:
                continue
            claims.append(
                MemoryStageOneStartupClaim(
                    thread_id=str(row["id"]),
                    rollout_path=Path(str(row["rollout_path"])),
                    source_updated_at=datetime.fromtimestamp(source_updated_at, tz=timezone.utc),
                    ownership_token=claim.ownership_token,
                )
            )
        return claims

    def try_claim_stage1_job(
        self,
        *,
        thread_id: str,
        worker_id: str,
        source_updated_at: datetime | int,
        lease_seconds: int,
        max_running_jobs: int,
        now: datetime | None = None,
    ) -> MemoryJobClaim:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        source_ts = _timestamp_like(source_updated_at)
        lease_until = now_ts + max(lease_seconds, 0)
        ownership_token = str(uuid4())

        with self.conn:
            existing_output = self.conn.execute(
                "SELECT source_updated_at FROM stage1_outputs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if existing_output is not None and int(existing_output["source_updated_at"]) >= source_ts:
                return MemoryJobClaim("skipped_up_to_date")

            existing_job = self.conn.execute(
                "SELECT * FROM jobs WHERE kind = ? AND job_key = ?",
                (MEMORY_STAGE1_JOB_KIND, thread_id),
            ).fetchone()
            if existing_job is not None and _row_int(existing_job, "last_success_watermark", -1) >= source_ts:
                return MemoryJobClaim("skipped_up_to_date")

            running_count = self._running_job_count(MEMORY_STAGE1_JOB_KIND, now_ts, exclude_key=thread_id)
            if running_count >= max_running_jobs:
                return MemoryJobClaim("skipped_running")

            retry_remaining = MEMORY_DEFAULT_RETRY_REMAINING
            if existing_job is not None:
                existing_input = _row_int(existing_job, "input_watermark", -1)
                retry_remaining = (
                    MEMORY_DEFAULT_RETRY_REMAINING
                    if source_ts > existing_input
                    else _row_int(existing_job, "retry_remaining", MEMORY_DEFAULT_RETRY_REMAINING)
                )
                if _row_str(existing_job, "status") == "running" and _row_int(existing_job, "lease_until", 0) > now_ts:
                    return MemoryJobClaim("skipped_running")
                if source_ts <= existing_input and _row_int(existing_job, "retry_at", 0) > now_ts:
                    return MemoryJobClaim("skipped_retry_backoff")
                if source_ts <= existing_input and retry_remaining <= 0:
                    return MemoryJobClaim("skipped_retry_exhausted")

            self.conn.execute(
                """
INSERT INTO jobs (
    kind, job_key, status, worker_id, ownership_token, started_at, finished_at,
    lease_until, retry_at, retry_remaining, last_error, input_watermark,
    last_success_watermark
) VALUES (?, ?, 'running', ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, NULL)
ON CONFLICT(kind, job_key) DO UPDATE SET
    status = 'running',
    worker_id = excluded.worker_id,
    ownership_token = excluded.ownership_token,
    started_at = excluded.started_at,
    finished_at = NULL,
    lease_until = excluded.lease_until,
    retry_at = NULL,
    retry_remaining = ?,
    last_error = NULL,
    input_watermark = excluded.input_watermark
                """,
                (
                    MEMORY_STAGE1_JOB_KIND,
                    thread_id,
                    worker_id,
                    ownership_token,
                    now_ts,
                    lease_until,
                    retry_remaining,
                    source_ts,
                    retry_remaining,
                ),
            )
        return MemoryJobClaim("claimed", ownership_token=ownership_token)

    def mark_stage1_job_succeeded(
        self,
        *,
        thread_id: str,
        ownership_token: str,
        source_updated_at: datetime | int,
        raw_memory: str,
        rollout_summary: str,
        rollout_slug: str | None,
        now: datetime | None = None,
    ) -> bool:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        source_ts = _timestamp_like(source_updated_at)
        with self.conn:
            cursor = self.conn.execute(
                """
UPDATE jobs
SET status = 'done',
    finished_at = ?,
    lease_until = NULL,
    last_error = NULL,
    last_success_watermark = input_watermark
WHERE kind = ? AND job_key = ?
  AND status = 'running' AND ownership_token = ?
                """,
                (now_ts, MEMORY_STAGE1_JOB_KIND, thread_id, ownership_token),
            )
            if cursor.rowcount == 0:
                return False
            self.conn.execute(
                """
INSERT INTO stage1_outputs (
    thread_id, source_updated_at, raw_memory, rollout_summary, rollout_slug,
    generated_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(thread_id) DO UPDATE SET
    source_updated_at = excluded.source_updated_at,
    raw_memory = excluded.raw_memory,
    rollout_summary = excluded.rollout_summary,
    rollout_slug = excluded.rollout_slug,
    generated_at = excluded.generated_at
WHERE excluded.source_updated_at >= stage1_outputs.source_updated_at
                """,
                (thread_id, source_ts, raw_memory, rollout_summary, rollout_slug, now_ts),
            )
            self._enqueue_global_consolidation_tx(source_ts)
        return True

    def mark_stage1_job_succeeded_no_output(
        self,
        *,
        thread_id: str,
        ownership_token: str,
        now: datetime | None = None,
    ) -> bool:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        with self.conn:
            cursor = self.conn.execute(
                """
UPDATE jobs
SET status = 'done',
    finished_at = ?,
    lease_until = NULL,
    last_error = NULL,
    last_success_watermark = input_watermark
WHERE kind = ? AND job_key = ?
  AND status = 'running' AND ownership_token = ?
                """,
                (now_ts, MEMORY_STAGE1_JOB_KIND, thread_id, ownership_token),
            )
            if cursor.rowcount == 0:
                return False
            job = self.conn.execute(
                "SELECT input_watermark FROM jobs WHERE kind = ? AND job_key = ?",
                (MEMORY_STAGE1_JOB_KIND, thread_id),
            ).fetchone()
            deleted = self.conn.execute(
                "DELETE FROM stage1_outputs WHERE thread_id = ?",
                (thread_id,),
            ).rowcount
            if deleted > 0 and job is not None:
                self._enqueue_global_consolidation_tx(_row_int(job, "input_watermark", 0))
        return True

    def mark_stage1_job_failed(
        self,
        *,
        thread_id: str,
        ownership_token: str,
        failure_reason: str,
        retry_delay_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        retry_at = now_ts + max(retry_delay_seconds, 0)
        with self.conn:
            cursor = self.conn.execute(
                """
UPDATE jobs
SET status = 'error',
    finished_at = ?,
    lease_until = NULL,
    retry_at = ?,
    retry_remaining = retry_remaining - 1,
    last_error = ?
WHERE kind = ? AND job_key = ?
  AND status = 'running' AND ownership_token = ?
                """,
                (now_ts, retry_at, failure_reason, MEMORY_STAGE1_JOB_KIND, thread_id, ownership_token),
            )
        return cursor.rowcount > 0

    def list_stage1_outputs_for_global(self, n: int) -> list[MemoryStageOneRecord]:
        if n <= 0:
            return []
        rows = self.conn.execute(
            """
SELECT
    so.thread_id,
    COALESCE(t.rollout_path, '') AS rollout_path,
    so.source_updated_at,
    so.raw_memory,
    so.rollout_summary,
    so.rollout_slug,
    COALESCE(t.cwd, '') AS cwd,
    t.git_branch AS git_branch,
    COALESCE(so.usage_count, 0) AS usage_count,
    so.last_usage,
    so.selected_for_phase2
FROM stage1_outputs AS so
LEFT JOIN threads AS t ON t.id = so.thread_id
WHERE t.memory_mode = 'enabled'
  AND (length(trim(so.raw_memory)) > 0 OR length(trim(so.rollout_summary)) > 0)
ORDER BY so.source_updated_at DESC, so.thread_id DESC
LIMIT ?
            """,
            (n,),
        ).fetchall()
        return [_memory_record_from_row(row) for row in rows]

    def prune_stage1_outputs_for_retention(
        self,
        *,
        max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
        limit: int = 100,
        now: datetime | None = None,
    ) -> int:
        if limit <= 0:
            return 0
        cutoff = _timestamp(now or datetime.now(timezone.utc)) - max(max_unused_days, 0) * 24 * 60 * 60
        with self.conn:
            rows = self.conn.execute(
                """
SELECT thread_id
FROM stage1_outputs
WHERE selected_for_phase2 = 0
  AND COALESCE(last_usage, source_updated_at) < ?
ORDER BY
  COALESCE(last_usage, source_updated_at) ASC,
  source_updated_at ASC,
  thread_id ASC
LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
            thread_ids = [row["thread_id"] for row in rows]
            for thread_id in thread_ids:
                self.conn.execute("DELETE FROM stage1_outputs WHERE thread_id = ?", (thread_id,))
        return len(thread_ids)

    def get_phase2_input_selection(
        self,
        *,
        n: int,
        max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
        now: datetime | None = None,
    ) -> list[MemoryStageOneRecord]:
        if n <= 0:
            return []
        cutoff = _timestamp(now or datetime.now(timezone.utc)) - max(max_unused_days, 0) * 24 * 60 * 60
        rows = self.conn.execute(
            """
SELECT
    selected.thread_id,
    selected.rollout_path,
    selected.source_updated_at,
    selected.raw_memory,
    selected.rollout_summary,
    selected.rollout_slug,
    selected.cwd,
    selected.git_branch,
    selected.usage_count,
    selected.last_usage,
    selected.selected_for_phase2
FROM (
    SELECT
        so.thread_id,
        COALESCE(t.rollout_path, '') AS rollout_path,
        so.source_updated_at,
        so.raw_memory,
        so.rollout_summary,
        so.rollout_slug,
        COALESCE(t.cwd, '') AS cwd,
        t.git_branch AS git_branch,
        COALESCE(so.usage_count, 0) AS usage_count,
        so.last_usage,
        so.selected_for_phase2
    FROM stage1_outputs AS so
    LEFT JOIN threads AS t ON t.id = so.thread_id
    WHERE t.memory_mode = 'enabled'
      AND (length(trim(so.raw_memory)) > 0 OR length(trim(so.rollout_summary)) > 0)
      AND (
            (so.last_usage IS NOT NULL AND so.last_usage >= ?)
            OR (so.last_usage IS NULL AND so.source_updated_at >= ?)
      )
    ORDER BY
        COALESCE(so.usage_count, 0) DESC,
        COALESCE(so.last_usage, so.source_updated_at) DESC,
        so.source_updated_at DESC,
        so.thread_id DESC
    LIMIT ?
) AS selected
ORDER BY selected.thread_id ASC
            """,
            (cutoff, cutoff, n),
        ).fetchall()
        return [_memory_record_from_row(row) for row in rows]

    def enqueue_global_consolidation(self, input_watermark: datetime | int) -> None:
        with self.conn:
            self._enqueue_global_consolidation_tx(_timestamp_like(input_watermark))

    def try_claim_global_phase2_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> MemoryJobClaim:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        lease_until = now_ts + max(lease_seconds, 0)
        cooldown_cutoff = now_ts - MEMORY_PHASE2_SUCCESS_COOLDOWN_SECONDS
        ownership_token = str(uuid4())
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE kind = ? AND job_key = ?",
                (MEMORY_PHASE2_JOB_KIND, MEMORY_PHASE2_JOB_KEY),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    """
INSERT INTO jobs (
    kind, job_key, status, worker_id, ownership_token, started_at, finished_at,
    lease_until, retry_at, retry_remaining, last_error, input_watermark,
    last_success_watermark
) VALUES (?, ?, 'running', ?, ?, ?, NULL, ?, NULL, ?, NULL, 0, 0)
                    """,
                    (
                        MEMORY_PHASE2_JOB_KIND,
                        MEMORY_PHASE2_JOB_KEY,
                        worker_id,
                        ownership_token,
                        now_ts,
                        lease_until,
                        MEMORY_DEFAULT_RETRY_REMAINING,
                    ),
                )
                return MemoryJobClaim("claimed", ownership_token=ownership_token, input_watermark=0)

            if _row_int(row, "retry_at", 0) > now_ts:
                return MemoryJobClaim("skipped_retry_unavailable")
            if _row_str(row, "status") == "running" and _row_int(row, "lease_until", 0) > now_ts:
                return MemoryJobClaim("skipped_running")
            if row["last_error"] is None and _row_int(row, "finished_at", 0) > cooldown_cutoff:
                return MemoryJobClaim("skipped_cooldown")

            input_watermark = _row_int(row, "input_watermark", 0)
            self.conn.execute(
                """
UPDATE jobs
SET status = 'running',
    worker_id = ?,
    ownership_token = ?,
    started_at = ?,
    finished_at = NULL,
    lease_until = ?,
    retry_at = NULL,
    last_error = NULL
WHERE kind = ? AND job_key = ?
                """,
                (
                    worker_id,
                    ownership_token,
                    now_ts,
                    lease_until,
                    MEMORY_PHASE2_JOB_KIND,
                    MEMORY_PHASE2_JOB_KEY,
                ),
            )
        return MemoryJobClaim("claimed", ownership_token=ownership_token, input_watermark=input_watermark)

    def heartbeat_global_phase2_job(
        self,
        *,
        ownership_token: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        lease_until = now_ts + max(lease_seconds, 0)
        with self.conn:
            cursor = self.conn.execute(
                """
UPDATE jobs
SET lease_until = ?
WHERE kind = ? AND job_key = ?
  AND status = 'running' AND ownership_token = ?
                """,
                (lease_until, MEMORY_PHASE2_JOB_KIND, MEMORY_PHASE2_JOB_KEY, ownership_token),
            )
        return cursor.rowcount > 0

    def mark_global_phase2_job_succeeded(
        self,
        *,
        ownership_token: str,
        completed_watermark: datetime | int,
        selected_outputs: list[MemoryStageOneRecord],
        now: datetime | None = None,
    ) -> bool:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        completed_ts = _timestamp_like(completed_watermark)
        with self.conn:
            cursor = self.conn.execute(
                """
UPDATE jobs
SET status = 'done',
    finished_at = ?,
    lease_until = NULL,
    last_error = NULL,
    last_success_watermark = max(COALESCE(last_success_watermark, 0), ?)
WHERE kind = ? AND job_key = ?
  AND status = 'running' AND ownership_token = ?
                """,
                (now_ts, completed_ts, MEMORY_PHASE2_JOB_KIND, MEMORY_PHASE2_JOB_KEY, ownership_token),
            )
            if cursor.rowcount == 0:
                return False
            self.conn.execute(
                """
UPDATE stage1_outputs
SET selected_for_phase2 = 0,
    selected_for_phase2_source_updated_at = NULL
WHERE selected_for_phase2 != 0 OR selected_for_phase2_source_updated_at IS NOT NULL
                """
            )
            for output in selected_outputs:
                source_ts = _timestamp(output.source_updated_at)
                self.conn.execute(
                    """
UPDATE stage1_outputs
SET selected_for_phase2 = 1,
    selected_for_phase2_source_updated_at = ?
WHERE thread_id = ? AND source_updated_at = ?
                    """,
                    (source_ts, output.thread_id, source_ts),
                )
        return True

    def mark_global_phase2_job_failed(
        self,
        *,
        ownership_token: str,
        failure_reason: str,
        retry_delay_seconds: int,
        now: datetime | None = None,
        allow_unowned: bool = False,
    ) -> bool:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        retry_at = now_ts + max(retry_delay_seconds, 0)
        ownership_clause = "(ownership_token = ? OR ownership_token IS NULL)" if allow_unowned else "ownership_token = ?"
        with self.conn:
            cursor = self.conn.execute(
                f"""
UPDATE jobs
SET status = 'error',
    finished_at = ?,
    lease_until = NULL,
    retry_at = ?,
    retry_remaining = max(retry_remaining - 1, 0),
    last_error = ?
WHERE kind = ? AND job_key = ?
  AND status = 'running'
  AND {ownership_clause}
                """,
                (
                    now_ts,
                    retry_at,
                    failure_reason,
                    MEMORY_PHASE2_JOB_KIND,
                    MEMORY_PHASE2_JOB_KEY,
                    ownership_token,
                ),
            )
        return cursor.rowcount > 0

    def mark_thread_memory_mode_polluted(self, thread_id: str, *, now: datetime | None = None) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE threads SET memory_mode = 'polluted' WHERE id = ? AND memory_mode != 'polluted'",
                (thread_id,),
            )
            if cursor.rowcount == 0:
                return False
            row = self.conn.execute(
                "SELECT selected_for_phase2 FROM stage1_outputs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is not None and _row_int(row, "selected_for_phase2", 0) != 0:
                self._enqueue_global_consolidation_tx(_timestamp(now or datetime.now(timezone.utc)))
        return True

    def get_job(self, kind: str, job_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE kind = ? AND job_key = ?",
            (kind, job_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_stage1_output(self, thread_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM stage1_outputs WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _enqueue_global_consolidation_tx(self, input_watermark: int) -> None:
        row = self.conn.execute(
            "SELECT status, retry_remaining, input_watermark FROM jobs WHERE kind = ? AND job_key = ?",
            (MEMORY_PHASE2_JOB_KIND, MEMORY_PHASE2_JOB_KEY),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
INSERT INTO jobs (
    kind, job_key, status, worker_id, ownership_token, started_at, finished_at,
    lease_until, retry_at, retry_remaining, last_error, input_watermark,
    last_success_watermark
) VALUES (?, ?, 'pending', NULL, NULL, NULL, NULL, NULL, NULL, ?, NULL, ?, 0)
                """,
                (
                    MEMORY_PHASE2_JOB_KIND,
                    MEMORY_PHASE2_JOB_KEY,
                    MEMORY_DEFAULT_RETRY_REMAINING,
                    input_watermark,
                ),
            )
            return
        current = _row_int(row, "input_watermark", 0)
        next_watermark = input_watermark if input_watermark > current else current + 1
        next_status = "running" if _row_str(row, "status") == "running" else "pending"
        retry_remaining = max(_row_int(row, "retry_remaining", 0), MEMORY_DEFAULT_RETRY_REMAINING)
        self.conn.execute(
            """
UPDATE jobs
SET status = ?,
    retry_at = CASE WHEN status = 'running' THEN retry_at ELSE NULL END,
    retry_remaining = ?,
    input_watermark = ?
WHERE kind = ? AND job_key = ?
            """,
            (
                next_status,
                retry_remaining,
                next_watermark,
                MEMORY_PHASE2_JOB_KIND,
                MEMORY_PHASE2_JOB_KEY,
            ),
        )

    def _running_job_count(self, kind: str, now_ts: int, *, exclude_key: str | None = None) -> int:
        if exclude_key is None:
            row = self.conn.execute(
                """
SELECT COUNT(*) AS count
FROM jobs
WHERE kind = ? AND status = 'running'
  AND lease_until IS NOT NULL AND lease_until > ?
                """,
                (kind, now_ts),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
SELECT COUNT(*) AS count
FROM jobs
WHERE kind = ? AND status = 'running'
  AND lease_until IS NOT NULL AND lease_until > ?
  AND job_key != ?
                """,
                (kind, now_ts, exclude_key),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _ensure_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled'
)
                """
            )
            self.conn.execute(
                """
CREATE TABLE IF NOT EXISTS stage1_outputs (
    thread_id TEXT PRIMARY KEY,
    source_updated_at INTEGER NOT NULL,
    raw_memory TEXT NOT NULL,
    rollout_summary TEXT NOT NULL,
    generated_at INTEGER NOT NULL,
    rollout_slug TEXT,
    usage_count INTEGER,
    last_usage INTEGER,
    selected_for_phase2 INTEGER NOT NULL DEFAULT 0,
    selected_for_phase2_source_updated_at INTEGER,
    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE
)
                """
            )
            self.conn.execute(
                """
CREATE TABLE IF NOT EXISTS jobs (
    kind TEXT NOT NULL,
    job_key TEXT NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT,
    ownership_token TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    lease_until INTEGER,
    retry_at INTEGER,
    retry_remaining INTEGER NOT NULL,
    last_error TEXT,
    input_watermark INTEGER,
    last_success_watermark INTEGER,
    PRIMARY KEY (kind, job_key)
)
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stage1_outputs_source_updated_at ON stage1_outputs(source_updated_at DESC, thread_id DESC)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_kind_status_retry_lease ON jobs(kind, status, retry_at, lease_until)"
            )


def memory_stage_one_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "rollout_summary": {"type": "string"},
            "rollout_slug": {"type": ["string", "null"]},
            "raw_memory": {"type": "string"},
        },
        "required": ["rollout_summary", "rollout_slug", "raw_memory"],
        "additionalProperties": False,
    }


def build_memory_stage_one_request(
    *,
    rollout_path: Path | str,
    rollout_cwd: Path | str,
    rollout_contents: str,
    model_context_window: int | None = None,
    effective_context_window_percent: int = 95,
    model: str = MEMORY_STAGE_ONE_MODEL,
    prompt_cache_key: str | None = None,
) -> PromptRequest:
    input_text = build_memory_stage_one_input_message(
        rollout_path=rollout_path,
        rollout_cwd=rollout_cwd,
        rollout_contents=rollout_contents,
        model_context_window=model_context_window,
        effective_context_window_percent=effective_context_window_percent,
    )
    return PromptRequest(
        model=model,
        instructions=memory_stage_one_system_prompt(),
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": input_text}],
            }
        ],
        tools=[],
        parallel_tool_calls=False,
        prompt_cache_key=prompt_cache_key,
        reasoning={"effort": MEMORY_STAGE_ONE_REASONING_EFFORT, "summary": "auto"},
        include=["reasoning.encrypted_content"],
        output_schema=memory_stage_one_output_schema(),
        output_schema_strict=True,
    )


def extract_memory_stage_one(
    *,
    model_client: ModelClient,
    rollout_path: Path | str,
    rollout_cwd: Path | str,
    rollout_contents: str,
    model_context_window: int | None = None,
    effective_context_window_percent: int = 95,
    prompt_cache_key: str | None = None,
) -> MemoryStageOneOutput:
    request = build_memory_stage_one_request(
        rollout_path=rollout_path,
        rollout_cwd=rollout_cwd,
        rollout_contents=rollout_contents,
        model_context_window=model_context_window,
        effective_context_window_percent=effective_context_window_percent,
        prompt_cache_key=prompt_cache_key,
    )
    response = model_client.create(request)
    return parse_memory_stage_one_output(_response_text(response.output))


def load_memory_rollout(rollout_path: Path | str) -> MemoryRollout:
    path = Path(rollout_path)
    lines = load_rollout_jsonl(path)
    items = [item for item in (_response_item_from_rollout_line(line) for line in lines) if item is not None]
    metadata = _rollout_metadata(lines, path)
    serialized = serialize_filtered_rollout_response_items(items)
    return MemoryRollout(
        thread_id=metadata["thread_id"],
        rollout_path=path,
        cwd=Path(metadata["cwd"]),
        source_updated_at=metadata["source_updated_at"],
        git_branch=metadata["git_branch"],
        source=metadata["source"],
        memory_mode=metadata["memory_mode"],
        items=items,
        serialized_contents=serialized,
    )


def load_rollout_jsonl(rollout_path: Path | str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    path = Path(rollout_path)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            lines.append(value)
    return lines


def serialize_filtered_rollout_response_items(items: list[dict[str, Any]]) -> str:
    filtered = []
    for item in items:
        sanitized = sanitize_response_item_for_memories(item)
        if sanitized is not None:
            filtered.append(sanitized)
    return _redact_secrets(json.dumps(filtered, ensure_ascii=False, separators=(",", ":")))


def sanitize_response_item_for_memories(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    if item_type == "message":
        role = item.get("role")
        if role == "developer":
            return None
        if role != "user":
            return dict(item)
        content = [
            part
            for part in item.get("content", [])
            if not _is_memory_excluded_contextual_user_fragment(part)
        ]
        if not content:
            return None
        sanitized = dict(item)
        sanitized["content"] = content
        return sanitized
    if item_type in {
        "local_shell_call",
        "function_call",
        "tool_search_call",
        "function_call_output",
        "tool_search_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
    }:
        return dict(item)
    return None


def run_memory_stage_one_for_rollout(
    *,
    model_client: ModelClient,
    rollout_path: Path | str,
    model_context_window: int | None = None,
    effective_context_window_percent: int = 95,
) -> MemoryStageOneRecord | None:
    rollout = load_memory_rollout(rollout_path)
    return run_memory_stage_one_for_loaded_rollout(
        model_client=model_client,
        rollout=rollout,
        model_context_window=model_context_window,
        effective_context_window_percent=effective_context_window_percent,
    )


def run_memory_stage_one_for_loaded_rollout(
    *,
    model_client: ModelClient,
    rollout: MemoryRollout,
    model_context_window: int | None = None,
    effective_context_window_percent: int = 95,
) -> MemoryStageOneRecord | None:
    if not rollout.serialized_contents or rollout.serialized_contents == "[]":
        return None
    output = extract_memory_stage_one(
        model_client=model_client,
        rollout_path=rollout.rollout_path,
        rollout_cwd=rollout.cwd,
        rollout_contents=rollout.serialized_contents,
        model_context_window=model_context_window,
        effective_context_window_percent=effective_context_window_percent,
        prompt_cache_key=rollout.thread_id,
    )
    if not output.raw_memory or not output.rollout_summary:
        return None
    return MemoryStageOneRecord(
        thread_id=rollout.thread_id,
        source_updated_at=rollout.source_updated_at,
        raw_memory=output.raw_memory,
        rollout_summary=output.rollout_summary,
        rollout_slug=output.rollout_slug,
        rollout_path=rollout.rollout_path,
        cwd=rollout.cwd,
        git_branch=rollout.git_branch,
    )


def run_memory_startup_once(
    *,
    volley_home: Path | str,
    model_client: ModelClient,
    state_store: MemoryStateStore | None = None,
    max_rollouts: int = MEMORY_MAX_ROLLOUTS_PER_STARTUP,
    max_raw_memories_for_consolidation: int = MEMORY_MAX_RAW_MEMORIES_FOR_CONSOLIDATION,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    max_rollout_age_days: int = MEMORY_MAX_ROLLOUT_AGE_DAYS,
    min_rollout_idle_hours: int = MEMORY_MIN_ROLLOUT_IDLE_HOURS,
    current_thread_id: str | None = None,
    allowed_sources: set[str] | frozenset[str] = INTERACTIVE_MEMORY_SESSION_SOURCES,
    model_context_window: int | None = None,
    sync_phase2_inputs: bool = True,
) -> MemoryStartupResult:
    home = Path(volley_home)
    memory_root = home / "memories"
    seed_extension_instructions(memory_root)
    records: list[MemoryStageOneRecord] = []
    skipped: list[Path] = []
    if state_store is not None:
        backfill_memory_threads_from_rollouts(
            home,
            state_store,
            limit=MEMORY_STAGE1_THREAD_SCAN_LIMIT,
        )
        claims = state_store.claim_stage1_jobs_for_startup(
            current_thread_id=current_thread_id,
            scan_limit=MEMORY_STAGE1_THREAD_SCAN_LIMIT,
            max_claimed=max_rollouts,
            max_age_days=max_rollout_age_days,
            min_rollout_idle_hours=min_rollout_idle_hours,
            allowed_sources=allowed_sources,
            lease_seconds=MEMORY_STAGE1_JOB_LEASE_SECONDS,
            max_running_jobs=max(max_rollouts, 1),
        )
        for claim in claims:
            record = _run_claimed_stage1_startup_claim(
                state_store=state_store,
                model_client=model_client,
                claim=claim,
                model_context_window=model_context_window,
            )
            if record is None:
                skipped.append(claim.rollout_path)
            else:
                records.append(record)
    else:
        for path in memory_rollout_candidates(home, limit=max_rollouts):
            try:
                record = run_memory_stage_one_for_rollout(
                    model_client=model_client,
                    rollout_path=path,
                    model_context_window=model_context_window,
                )
            except (OSError, ValueError, RuntimeError):
                skipped.append(path)
                continue
            if record is None:
                skipped.append(path)
            else:
                records.append(record)

    if sync_phase2_inputs:
        phase2_records = (
            state_store.get_phase2_input_selection(
                n=max_raw_memories_for_consolidation,
                max_unused_days=max_unused_days,
            )
            if state_store is not None
            else records
        )
        sync_phase2_workspace_inputs(
            memory_root,
            phase2_records,
            max_raw_memories_for_consolidation,
            max_unused_days=max_unused_days,
        )
    return MemoryStartupResult(records=records, skipped=skipped, memory_root=memory_root)


def run_memory_startup_pipeline_once(
    *,
    volley_home: Path | str,
    model_client: ModelClient,
    state_store: MemoryStateStore | None = None,
    base_config: VolleyConfig | None = None,
    max_rollouts: int = MEMORY_MAX_ROLLOUTS_PER_STARTUP,
    max_raw_memories_for_consolidation: int = MEMORY_MAX_RAW_MEMORIES_FOR_CONSOLIDATION,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    max_rollout_age_days: int = MEMORY_MAX_ROLLOUT_AGE_DAYS,
    min_rollout_idle_hours: int = MEMORY_MIN_ROLLOUT_IDLE_HOURS,
    current_thread_id: str | None = None,
    model_context_window: int | None = None,
    run_phase2: bool = True,
    rate_limit_snapshot: Any | None = None,
    min_rate_limit_remaining_percent: int = MEMORY_MIN_RATE_LIMIT_REMAINING_PERCENT,
) -> MemoryStartupResult:
    home = Path(volley_home)
    memory_root = home / "memories"
    seed_extension_instructions(memory_root)
    if state_store is not None:
        state_store.prune_stage1_outputs_for_retention(
            max_unused_days=max_unused_days,
            limit=MEMORY_STAGE1_PRUNE_BATCH_SIZE,
        )
    rate_limit_allowed = memory_rate_limit_allows_startup(
        rate_limit_snapshot,
        min_remaining_percent=min_rate_limit_remaining_percent,
    )
    if not rate_limit_allowed:
        return MemoryStartupResult(
            records=[],
            skipped=[],
            memory_root=memory_root,
            status="skipped_rate_limit",
            rate_limit_allowed=False,
        )
    startup = run_memory_startup_once(
        volley_home=home,
        model_client=model_client,
        state_store=state_store,
        max_rollouts=max_rollouts,
        max_raw_memories_for_consolidation=max_raw_memories_for_consolidation,
        max_unused_days=max_unused_days,
        max_rollout_age_days=max_rollout_age_days,
        min_rollout_idle_hours=min_rollout_idle_hours,
        current_thread_id=current_thread_id,
        model_context_window=model_context_window,
        sync_phase2_inputs=not run_phase2,
    )
    phase2_result = None
    if run_phase2 and state_store is not None:
        phase2_result = run_memory_phase2_once(
            volley_home=home,
            state_store=state_store,
            base_config=base_config,
            model_client=model_client,
            max_raw_memories_for_consolidation=max_raw_memories_for_consolidation,
            max_unused_days=max_unused_days,
        )
    return MemoryStartupResult(
        records=startup.records,
        skipped=startup.skipped,
        memory_root=startup.memory_root,
        status=startup.status,
        phase2_result=phase2_result,
        rate_limit_allowed=True,
    )


def start_memory_startup_task(
    *,
    volley_home: Path | str,
    model_client: ModelClient,
    state_store_path: Path | str | None = None,
    base_config: VolleyConfig | None = None,
    max_rollouts: int = MEMORY_MAX_ROLLOUTS_PER_STARTUP,
    max_raw_memories_for_consolidation: int = MEMORY_MAX_RAW_MEMORIES_FOR_CONSOLIDATION,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    max_rollout_age_days: int = MEMORY_MAX_ROLLOUT_AGE_DAYS,
    min_rollout_idle_hours: int = MEMORY_MIN_ROLLOUT_IDLE_HOURS,
    current_thread_id: str | None = None,
    model_context_window: int | None = None,
    run_phase2: bool = True,
    rate_limit_snapshot: Any | None = None,
    min_rate_limit_remaining_percent: int = MEMORY_MIN_RATE_LIMIT_REMAINING_PERCENT,
) -> MemoryBackgroundTask:
    def target() -> MemoryStartupResult:
        local_store = MemoryStateStore(state_store_path) if state_store_path is not None else None
        try:
            return run_memory_startup_pipeline_once(
                volley_home=volley_home,
                model_client=model_client,
                state_store=local_store,
                base_config=base_config,
                max_rollouts=max_rollouts,
                max_raw_memories_for_consolidation=max_raw_memories_for_consolidation,
                max_unused_days=max_unused_days,
                max_rollout_age_days=max_rollout_age_days,
                min_rollout_idle_hours=min_rollout_idle_hours,
                current_thread_id=current_thread_id,
                model_context_window=model_context_window,
                run_phase2=run_phase2,
                rate_limit_snapshot=rate_limit_snapshot,
                min_rate_limit_remaining_percent=min_rate_limit_remaining_percent,
            )
        finally:
            if local_store is not None:
                local_store.close()

    return MemoryBackgroundTask(kind="memory-startup", target=target)


def memory_rate_limit_allows_startup(
    snapshot: Any | None,
    *,
    min_remaining_percent: int = MEMORY_MIN_RATE_LIMIT_REMAINING_PERCENT,
) -> bool:
    if snapshot is None:
        return True
    if isinstance(snapshot, bool):
        return snapshot
    if callable(snapshot):
        return memory_rate_limit_allows_startup(
            snapshot(),
            min_remaining_percent=min_remaining_percent,
        )
    if not isinstance(snapshot, dict):
        return True
    if snapshot.get("rate_limit_reached_type") is not None:
        return False
    max_used_percent = 100 - max(0, min(100, int(min_remaining_percent)))
    return _rate_limit_window_allows(snapshot.get("primary"), max_used_percent) and _rate_limit_window_allows(
        snapshot.get("secondary"),
        max_used_percent,
    )


def _rate_limit_window_allows(window: Any, max_used_percent: int) -> bool:
    if window is None:
        return True
    if not isinstance(window, dict):
        return True
    used = window.get("used_percent")
    if used is None:
        return True
    try:
        return float(used) <= float(max_used_percent)
    except (TypeError, ValueError):
        return True


def memory_rollout_candidates(volley_home: Path | str, limit: int = 5_000) -> list[Path]:
    sessions = Path(volley_home) / "sessions"
    if not sessions.exists():
        return []
    candidates = [path for path in sessions.rglob("*.jsonl") if path.is_file()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[: max(limit, 0)]


def backfill_memory_threads_from_rollouts(
    volley_home: Path | str,
    state_store: MemoryStateStore,
    *,
    limit: int = MEMORY_STAGE1_THREAD_SCAN_LIMIT,
) -> int:
    updated = 0
    for path in memory_rollout_candidates(volley_home, limit=limit):
        try:
            rollout = load_memory_rollout(path)
        except (OSError, ValueError, RuntimeError):
            continue
        state_store.upsert_thread(
            MemoryThreadRecord(
                thread_id=rollout.thread_id,
                rollout_path=rollout.rollout_path,
                cwd=rollout.cwd,
                updated_at=rollout.source_updated_at,
                source=rollout.source,
                memory_mode=rollout.memory_mode,
                git_branch=rollout.git_branch,
            )
        )
        updated += 1
    return updated


def memory_rollout_is_stage1_startup_eligible(
    rollout: MemoryRollout,
    *,
    current_thread_id: str | None = None,
    max_rollout_age_days: int = MEMORY_MAX_ROLLOUT_AGE_DAYS,
    min_rollout_idle_hours: int = MEMORY_MIN_ROLLOUT_IDLE_HOURS,
    allowed_sources: set[str] | frozenset[str] = INTERACTIVE_MEMORY_SESSION_SOURCES,
    now: datetime | None = None,
) -> bool:
    if current_thread_id and rollout.thread_id == current_thread_id:
        return False
    if rollout.memory_mode != "enabled":
        return False
    if allowed_sources and rollout.source not in allowed_sources:
        return False
    reference = _to_utc(now or datetime.now(timezone.utc))
    updated_at = _to_utc(rollout.source_updated_at)
    if updated_at < reference - timedelta(days=max(max_rollout_age_days, 0)):
        return False
    if updated_at > reference - timedelta(hours=max(min_rollout_idle_hours, 0)):
        return False
    return True


def _run_claimed_stage1_startup_job(
    *,
    state_store: MemoryStateStore,
    model_client: ModelClient,
    rollout_path: Path,
    model_context_window: int | None,
    current_thread_id: str | None,
    max_rollout_age_days: int,
    min_rollout_idle_hours: int,
    allowed_sources: set[str] | frozenset[str],
    max_running_jobs: int,
) -> MemoryStageOneRecord | None:
    try:
        rollout = load_memory_rollout(rollout_path)
    except (OSError, ValueError, RuntimeError):
        return None

    if not memory_rollout_is_stage1_startup_eligible(
        rollout,
        current_thread_id=current_thread_id,
        max_rollout_age_days=max_rollout_age_days,
        min_rollout_idle_hours=min_rollout_idle_hours,
        allowed_sources=allowed_sources,
    ):
        return None

    state_store.upsert_thread(
        MemoryThreadRecord(
            thread_id=rollout.thread_id,
            rollout_path=rollout.rollout_path,
            cwd=rollout.cwd,
            updated_at=rollout.source_updated_at,
            source=rollout.source,
            memory_mode=rollout.memory_mode,
            git_branch=rollout.git_branch,
        )
    )
    claim = state_store.try_claim_stage1_job(
        thread_id=rollout.thread_id,
        worker_id="python-memory-startup",
        source_updated_at=rollout.source_updated_at,
        lease_seconds=MEMORY_STAGE1_JOB_LEASE_SECONDS,
        max_running_jobs=max_running_jobs,
    )
    if claim.outcome != "claimed" or claim.ownership_token is None:
        return None

    try:
        record = run_memory_stage_one_for_loaded_rollout(
            model_client=model_client,
            rollout=rollout,
            model_context_window=model_context_window,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        state_store.mark_stage1_job_failed(
            thread_id=rollout.thread_id,
            ownership_token=claim.ownership_token,
            failure_reason=f"{type(exc).__name__}: {exc}",
            retry_delay_seconds=MEMORY_STAGE1_JOB_RETRY_DELAY_SECONDS,
        )
        return None

    if record is None:
        state_store.mark_stage1_job_succeeded_no_output(
            thread_id=rollout.thread_id,
            ownership_token=claim.ownership_token,
        )
        return None

    state_store.mark_stage1_job_succeeded(
        thread_id=record.thread_id,
        ownership_token=claim.ownership_token,
        source_updated_at=record.source_updated_at,
        raw_memory=record.raw_memory,
        rollout_summary=record.rollout_summary,
        rollout_slug=record.rollout_slug,
    )
    return record


def _run_claimed_stage1_startup_claim(
    *,
    state_store: MemoryStateStore,
    model_client: ModelClient,
    claim: MemoryStageOneStartupClaim,
    model_context_window: int | None,
) -> MemoryStageOneRecord | None:
    try:
        rollout = load_memory_rollout(claim.rollout_path)
        if rollout.thread_id != claim.thread_id:
            raise ValueError(
                f"claimed thread {claim.thread_id} but rollout metadata resolved to {rollout.thread_id}"
            )
        record = run_memory_stage_one_for_loaded_rollout(
            model_client=model_client,
            rollout=rollout,
            model_context_window=model_context_window,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        state_store.mark_stage1_job_failed(
            thread_id=claim.thread_id,
            ownership_token=claim.ownership_token,
            failure_reason=f"{type(exc).__name__}: {exc}",
            retry_delay_seconds=MEMORY_STAGE1_JOB_RETRY_DELAY_SECONDS,
        )
        return None

    if record is None:
        state_store.mark_stage1_job_succeeded_no_output(
            thread_id=claim.thread_id,
            ownership_token=claim.ownership_token,
        )
        return None

    state_store.mark_stage1_job_succeeded(
        thread_id=record.thread_id,
        ownership_token=claim.ownership_token,
        source_updated_at=claim.source_updated_at,
        raw_memory=record.raw_memory,
        rollout_summary=record.rollout_summary,
        rollout_slug=record.rollout_slug,
    )
    return MemoryStageOneRecord(
        thread_id=record.thread_id,
        source_updated_at=claim.source_updated_at,
        raw_memory=record.raw_memory,
        rollout_summary=record.rollout_summary,
        rollout_slug=record.rollout_slug,
        rollout_path=record.rollout_path,
        cwd=record.cwd,
        git_branch=record.git_branch,
    )


def _phase2_completed_watermark(input_watermark: int, selected: list[MemoryStageOneRecord]) -> int:
    watermark = input_watermark
    for record in selected:
        watermark = max(watermark, _timestamp(record.source_updated_at))
    return watermark


def parse_memory_stage_one_output(text: str) -> MemoryStageOneOutput:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid memory stage-one JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid memory stage-one JSON: expected object")

    required = {"rollout_summary", "rollout_slug", "raw_memory"}
    extra = set(data) - required
    missing = required - set(data)
    if extra:
        raise ValueError(f"invalid memory stage-one JSON: unexpected keys {sorted(extra)}")
    if missing:
        raise ValueError(f"invalid memory stage-one JSON: missing keys {sorted(missing)}")

    raw_memory = data["raw_memory"]
    rollout_summary = data["rollout_summary"]
    rollout_slug = data["rollout_slug"]
    if not isinstance(raw_memory, str) or not isinstance(rollout_summary, str):
        raise ValueError("invalid memory stage-one JSON: raw_memory and rollout_summary must be strings")
    if rollout_slug is not None and not isinstance(rollout_slug, str):
        raise ValueError("invalid memory stage-one JSON: rollout_slug must be string or null")

    return MemoryStageOneOutput(
        raw_memory=_redact_secrets(raw_memory),
        rollout_summary=_redact_secrets(rollout_summary),
        rollout_slug=_redact_secrets(rollout_slug) if rollout_slug is not None else None,
    )


def ensure_memory_layout(root: Path | str) -> None:
    rollout_summaries_dir(root).mkdir(parents=True, exist_ok=True)


def raw_memories_file(root: Path | str) -> Path:
    return Path(root) / "raw_memories.md"


def rollout_summaries_dir(root: Path | str) -> Path:
    return Path(root) / "rollout_summaries"


def memory_extensions_root(root: Path | str) -> Path:
    return Path(root) / "extensions"


def seed_extension_instructions(memory_root: Path | str) -> None:
    seed_ad_hoc_extension_instructions(memory_root)


def seed_ad_hoc_extension_instructions(memory_root: Path | str) -> None:
    extension_root = memory_extensions_root(memory_root) / "ad_hoc"
    extension_root.mkdir(parents=True, exist_ok=True)
    instructions_path = extension_root / "instructions.md"
    content = read_asset(MEMORY_AD_HOC_EXTENSION_INSTRUCTIONS_ASSET)
    try:
        fd = os.open(instructions_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)


def sync_phase2_workspace_inputs(
    root: Path | str,
    memories: list[MemoryStageOneRecord],
    max_raw_memories_for_consolidation: int,
    *,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    now: datetime | None = None,
) -> None:
    sync_rollout_summaries_from_memories(
        root,
        memories,
        max_raw_memories_for_consolidation,
        max_unused_days=max_unused_days,
        now=now,
    )
    rebuild_raw_memories_file_from_memories(
        root,
        memories,
        max_raw_memories_for_consolidation,
        max_unused_days=max_unused_days,
        now=now,
    )
    prune_old_extension_resources(root, now=now)


def rebuild_raw_memories_file_from_memories(
    root: Path | str,
    memories: list[MemoryStageOneRecord],
    max_raw_memories_for_consolidation: int,
    *,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    now: datetime | None = None,
) -> None:
    ensure_memory_layout(root)
    retained = _retained_memories(
        memories,
        max_raw_memories_for_consolidation,
        max_unused_days=max_unused_days,
        now=now,
    )
    body = "# Raw Memories\n\n"
    if not retained:
        raw_memories_file(root).write_text(body + "No raw memories yet.\n", encoding="utf-8")
        return

    body += "Merged stage-1 raw memories (stable ascending thread-id order):\n\n"
    for memory in retained:
        rollout_summary_file = f"{rollout_summary_file_stem(memory)}.md"
        body += f"## Thread `{memory.thread_id}`\n"
        body += f"updated_at: {_format_rfc3339(memory.source_updated_at)}\n"
        body += f"cwd: {memory.cwd}\n"
        body += f"rollout_path: {memory.rollout_path}\n"
        body += f"rollout_summary_file: {rollout_summary_file}\n\n"
        body += memory.raw_memory.strip()
        body += "\n\n"

    raw_memories_file(root).write_text(body, encoding="utf-8")


def sync_rollout_summaries_from_memories(
    root: Path | str,
    memories: list[MemoryStageOneRecord],
    max_raw_memories_for_consolidation: int,
    *,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    now: datetime | None = None,
) -> None:
    ensure_memory_layout(root)
    retained = _retained_memories(
        memories,
        max_raw_memories_for_consolidation,
        max_unused_days=max_unused_days,
        now=now,
    )
    keep = {rollout_summary_file_stem(memory) for memory in retained}

    for path in rollout_summaries_dir(root).iterdir():
        if path.is_file() and path.suffix == ".md" and path.stem not in keep:
            path.unlink()

    for memory in retained:
        _write_rollout_summary_for_thread(root, memory)


def rollout_summary_file_stem(memory: MemoryStageOneRecord) -> str:
    return rollout_summary_file_stem_from_parts(
        memory.thread_id,
        memory.source_updated_at,
        memory.rollout_slug,
    )


def rollout_summary_file_stem_from_parts(
    thread_id: str,
    source_updated_at: datetime,
    rollout_slug: str | None,
) -> str:
    timestamp_fragment, short_hash_seed = _rollout_stem_prefix_parts(thread_id, source_updated_at)
    short_hash = _base62_4(short_hash_seed % 14_776_336)
    file_prefix = f"{timestamp_fragment}-{short_hash}"

    if rollout_slug is None:
        return file_prefix
    slug = _sanitize_rollout_slug(rollout_slug)
    if not slug:
        return file_prefix
    return f"{file_prefix}-{slug}"


def write_memory_workspace_diff(
    root: Path | str,
    changes: list[MemoryWorkspaceChange],
    unified_diff: str,
) -> Path:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    path = root_path / MEMORY_PHASE2_WORKSPACE_DIFF_FILE
    path.write_text(render_memory_workspace_diff_file(changes, unified_diff), encoding="utf-8")
    return path


def prepare_memory_workspace(root: Path | str) -> None:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    remove_memory_workspace_diff(root_path)
    if _usable_git_baseline(root_path):
        return
    reset_memory_workspace_baseline(root_path)


def memory_workspace_diff(root: Path | str) -> tuple[list[MemoryWorkspaceChange], str]:
    root_path = Path(root)
    remove_memory_workspace_diff(root_path)
    if not _usable_git_baseline(root_path):
        prepare_memory_workspace(root_path)
    status_output = _git(root_path, "status", "--porcelain=v1", "--untracked-files=all")
    changes = _parse_git_status(status_output)
    unified_diff = _git(root_path, "diff", "--no-ext-diff", "HEAD", "--")
    unified_diff += _render_untracked_diff(root_path, changes)
    return changes, unified_diff


def write_current_memory_workspace_diff(root: Path | str) -> Path:
    changes, unified_diff = memory_workspace_diff(root)
    return write_memory_workspace_diff(root, changes, unified_diff)


def reset_memory_workspace_baseline(root: Path | str) -> None:
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    remove_memory_workspace_diff(root_path)
    git_path = root_path / ".git"
    if git_path.is_dir() and not git_path.is_symlink():
        shutil.rmtree(git_path)
    elif git_path.exists() or git_path.is_symlink():
        git_path.unlink()
    _git(root_path, "init")
    _git(root_path, "config", "user.name", "Volley")
    _git(root_path, "config", "user.email", "noreply@openai.com")
    _git(root_path, "add", "-A")
    _git(
        root_path,
        "commit",
        "--allow-empty",
        "-m",
        "Initialize Volley git baseline\n\nCo-authored-by: Volley <noreply@openai.com>",
    )


def remove_memory_workspace_diff(root: Path | str) -> None:
    path = Path(root) / MEMORY_PHASE2_WORKSPACE_DIFF_FILE
    path.unlink(missing_ok=True)


def prune_old_extension_resources(memory_root: Path | str, *, now: datetime | None = None) -> None:
    cutoff = _to_utc(now or datetime.now(timezone.utc)) - timedelta(
        days=MEMORY_EXTENSION_RESOURCE_RETENTION_DAYS
    )
    extensions_root = memory_extensions_root(memory_root)
    try:
        extension_paths = list(extensions_root.iterdir())
    except FileNotFoundError:
        return

    for extension_path in extension_paths:
        if not extension_path.is_dir() or not (extension_path / "instructions.md").exists():
            continue
        resources_path = extension_path / "resources"
        if not resources_path.is_dir():
            continue
        for resource_path in resources_path.iterdir():
            if not resource_path.is_file() or resource_path.suffix != ".md":
                continue
            timestamp = _memory_extension_resource_timestamp(resource_path.name)
            if timestamp is None or timestamp > cutoff:
                continue
            resource_path.unlink(missing_ok=True)


def render_memory_workspace_diff_file(
    changes: list[MemoryWorkspaceChange],
    unified_diff: str,
    max_bytes: int = MEMORY_WORKSPACE_DIFF_MAX_BYTES,
) -> str:
    rendered = (
        "# Memory Workspace Diff\n\n"
        "Generated by Volley before Phase 2 memory consolidation. Read this file first and do not edit it.\n\n"
        "## Status\n"
    )
    if not changes and not unified_diff:
        return rendered + "- none\n"

    for change in changes:
        rendered += f"- {change.status} {change.path}\n"
    rendered += "\n## Diff\n\n```diff\n"
    rendered += _bounded_diff(unified_diff, max_bytes)
    rendered += "```\n"
    return rendered


def build_memory_consolidation_config(
    *,
    memory_root: Path | str,
    base_config: VolleyConfig | None = None,
) -> VolleyConfig:
    config = base_config or VolleyConfig()
    memory_root_path = Path(memory_root)
    return VolleyConfig(
        model=MEMORY_STAGE_TWO_MODEL,
        session_source="internal_memory_consolidation",
        cwd=memory_root_path,
        sandbox="workspace-write",
        approval_policy="never",
        network_access="restricted",
        writable_roots=(memory_root_path,),
        volley_home=config.volley_home,
        json_events=config.json_events,
        output_last_message=None,
        skip_git_repo_check=True,
        ephemeral=True,
        max_iterations=config.max_iterations,
        prompt_asset=config.prompt_asset,
        compact_prompt=config.compact_prompt,
        model_context_window=config.model_context_window,
        model_auto_compact_token_limit=None,
        include_unified_exec_tool=config.include_unified_exec_tool,
        include_shell_command_tool=config.include_shell_command_tool,
        include_update_plan_tool=config.include_update_plan_tool,
        include_request_user_input_tool=False,
        include_view_image_tool=config.include_view_image_tool,
        include_multi_agent_tools=False,
        include_web_search_tool=False,
        web_search_external_web_access=False,
        web_search_filters=None,
        web_search_user_location=None,
        web_search_context_size=None,
        web_search_content_types=None,
        include_environment_context=config.include_environment_context,
        include_permissions_instructions=config.include_permissions_instructions,
        collaboration_mode=config.collaboration_mode,
        approval_provider=config.approval_provider,
        request_user_input_available_modes=config.request_user_input_available_modes,
        request_user_input_answers=config.request_user_input_answers,
        request_user_input_provider=None,
        model_supports_image_input=config.model_supports_image_input,
        model_supports_image_detail_original=config.model_supports_image_detail_original,
        memory_tool_enabled=False,
        memory_generate_memories=False,
        memory_disable_on_external_context=config.memory_disable_on_external_context,
        use_memories=False,
        memory_state_store=None,
        memory_startup_background=False,
        memory_run_phase2_on_startup=False,
        memory_max_raw_memories_for_consolidation=config.memory_max_raw_memories_for_consolidation,
        memory_max_unused_days=config.memory_max_unused_days,
        memory_max_rollout_age_days=config.memory_max_rollout_age_days,
        memory_max_rollouts_per_startup=config.memory_max_rollouts_per_startup,
        memory_min_rollout_idle_hours=config.memory_min_rollout_idle_hours,
        memory_rate_limit_provider=None,
        memory_min_rate_limit_remaining_percent=config.memory_min_rate_limit_remaining_percent,
        model_reasoning_effort=MEMORY_STAGE_TWO_REASONING_EFFORT,
        model_reasoning_summary=config.model_reasoning_summary,
        model_verbosity=config.model_verbosity,
        service_tier=config.resolved_service_tier(),
        client_metadata=config.client_metadata,
        output_schema=None,
        output_schema_strict=config.output_schema_strict,
        input_images=(),
        provider_is_azure_responses_endpoint=config.provider_is_azure_responses_endpoint,
        current_date=config.current_date,
        timezone=config.timezone,
        use_responses_api=config.use_responses_api,
    )


def run_memory_consolidation_session(
    *,
    memory_root: Path | str,
    base_config: VolleyConfig | None = None,
    model_client: ModelClient | None = None,
) -> VolleyResult:
    from .core import VolleySession
    from .model import default_model_client

    config = build_memory_consolidation_config(memory_root=memory_root, base_config=base_config)
    session = VolleySession(config, model_client=model_client or default_model_client(config))
    return session.run(build_memory_consolidation_prompt(memory_root))


def run_memory_phase2_once(
    *,
    volley_home: Path | str,
    state_store: MemoryStateStore,
    base_config: VolleyConfig | None = None,
    model_client: ModelClient | None = None,
    max_raw_memories_for_consolidation: int = MEMORY_MAX_RAW_MEMORIES_FOR_CONSOLIDATION,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    lease_seconds: int = 60 * 60,
) -> MemoryPhase2Result:
    memory_root = Path(volley_home) / "memories"
    claim = state_store.try_claim_global_phase2_job(
        worker_id="python-memory-phase2",
        lease_seconds=lease_seconds,
    )
    if claim.outcome != "claimed" or claim.ownership_token is None:
        return MemoryPhase2Result(status=claim.outcome, selected=[], memory_root=memory_root)

    try:
        prepare_memory_workspace(memory_root)
        selected = state_store.get_phase2_input_selection(
            n=max_raw_memories_for_consolidation,
            max_unused_days=max_unused_days,
        )
        completed_watermark = _phase2_completed_watermark(claim.input_watermark or 0, selected)
        sync_phase2_workspace_inputs(
            memory_root,
            selected,
            max_raw_memories_for_consolidation,
            max_unused_days=max_unused_days,
        )
        changes, diff = memory_workspace_diff(memory_root)
        if not changes and not diff:
            state_store.mark_global_phase2_job_succeeded(
                ownership_token=claim.ownership_token,
                completed_watermark=completed_watermark,
                selected_outputs=selected,
            )
            return MemoryPhase2Result(
                status="succeeded_no_workspace_changes",
                selected=selected,
                memory_root=memory_root,
                workspace_changed=False,
            )

        write_memory_workspace_diff(memory_root, changes, diff)
        state_store.heartbeat_global_phase2_job(
            ownership_token=claim.ownership_token,
            lease_seconds=lease_seconds,
        )
        result = run_memory_consolidation_session(
            memory_root=memory_root,
            base_config=base_config,
            model_client=model_client,
        )
        reset_memory_workspace_baseline(memory_root)
        state_store.mark_global_phase2_job_succeeded(
            ownership_token=claim.ownership_token,
            completed_watermark=completed_watermark,
            selected_outputs=selected,
        )
        return MemoryPhase2Result(
            status="succeeded",
            selected=selected,
            memory_root=memory_root,
            workspace_changed=True,
            final_message=result.final_message,
        )
    except Exception as exc:
        state_store.mark_global_phase2_job_failed(
            ownership_token=claim.ownership_token,
            failure_reason=f"{type(exc).__name__}: {exc}",
            retry_delay_seconds=60,
            allow_unowned=True,
        )
        raise


def select_phase2_memory_inputs(
    memories: list[MemoryStageOneRecord],
    max_raw_memories_for_consolidation: int,
    *,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    now: datetime | None = None,
) -> list[MemoryStageOneRecord]:
    if max_raw_memories_for_consolidation <= 0:
        return []

    cutoff = _to_utc(now or datetime.now(timezone.utc)) - timedelta(days=max(max_unused_days, 0))
    eligible = [
        memory
        for memory in memories
        if (memory.raw_memory.strip() or memory.rollout_summary.strip())
        and _phase2_memory_selection_time(memory) >= cutoff
    ]
    selected = sorted(
        eligible,
        key=lambda memory: (
            max(memory.usage_count, 0),
            _phase2_memory_selection_time(memory),
            _to_utc(memory.source_updated_at),
            memory.thread_id,
        ),
        reverse=True,
    )[:max_raw_memories_for_consolidation]
    return sorted(selected, key=lambda memory: memory.thread_id)


def prune_stage1_records_for_retention(
    memories: list[MemoryStageOneRecord],
    *,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    limit: int = 100,
    now: datetime | None = None,
) -> tuple[list[MemoryStageOneRecord], list[MemoryStageOneRecord]]:
    if limit <= 0:
        return (list(memories), [])

    cutoff = _to_utc(now or datetime.now(timezone.utc)) - timedelta(days=max(max_unused_days, 0))
    pruned = sorted(
        (
            memory
            for memory in memories
            if not memory.selected_for_phase2 and _phase2_memory_selection_time(memory) < cutoff
        ),
        key=lambda memory: (
            _phase2_memory_selection_time(memory),
            _to_utc(memory.source_updated_at),
            memory.thread_id,
        ),
    )[:limit]
    pruned_thread_ids = {memory.thread_id for memory in pruned}
    kept = [memory for memory in memories if memory.thread_id not in pruned_thread_ids]
    return (kept, pruned)


def _response_text(output: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


def _response_item_from_rollout_line(line: dict[str, Any]) -> dict[str, Any] | None:
    if line.get("type") == "response_item" and isinstance(line.get("payload"), dict):
        return line["payload"]
    if line.get("type") == "item.completed" and isinstance(line.get("item"), dict):
        return line["item"]
    return None


def _rollout_metadata(lines: list[dict[str, Any]], path: Path) -> dict[str, Any]:
    thread_id = path.stem
    cwd = path.parent
    git_branch = None
    source = "cli"
    memory_mode = "enabled"
    source_updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    for line in lines:
        if isinstance(line.get("thread_id"), str):
            thread_id = line["thread_id"]
        ts = line.get("ts")
        if isinstance(ts, (int, float)):
            source_updated_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        if line.get("type") == "session_meta" and isinstance(line.get("payload"), dict):
            meta_line = line["payload"]
            meta = meta_line.get("meta") if isinstance(meta_line.get("meta"), dict) else meta_line
            if isinstance(meta, dict):
                thread_id = str(meta.get("id") or thread_id)
                cwd = Path(meta.get("cwd") or cwd)
                if "source" in meta:
                    source = _normalize_session_source(meta.get("source"))
                if isinstance(meta.get("memory_mode"), str):
                    memory_mode = str(meta["memory_mode"])
                timestamp = meta.get("timestamp")
                if isinstance(timestamp, str):
                    parsed = _parse_datetime(timestamp)
                    if parsed is not None:
                        source_updated_at = parsed
            git = meta_line.get("git") if isinstance(meta_line, dict) else None
            if isinstance(git, dict):
                branch = git.get("branch")
                if isinstance(branch, str):
                    git_branch = branch
    return {
        "thread_id": thread_id,
        "cwd": cwd,
        "source_updated_at": source_updated_at,
        "git_branch": git_branch,
        "source": source,
        "memory_mode": memory_mode,
    }


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_session_source(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized or "unknown"
    if isinstance(value, dict):
        custom = value.get("custom")
        if isinstance(custom, str) and custom.strip():
            return custom.strip().lower()
        internal = value.get("internal")
        if isinstance(internal, str) and internal.strip():
            return f"internal_{internal.strip().lower()}"
        subagent = value.get("subagent")
        if isinstance(subagent, str) and subagent.strip():
            return f"subagent_{subagent.strip().lower()}"
    return "unknown"


def _is_memory_excluded_contextual_user_fragment(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    text = part.get("text")
    if not isinstance(text, str):
        return False
    return (
        _matches_marked_fragment(text, "# AGENTS.md instructions for ", "</INSTRUCTIONS>")
        or _matches_marked_fragment(text, "<skill>", "</skill>")
    )


def _matches_marked_fragment(text: str, start_marker: str, end_marker: str) -> bool:
    stripped_start = text.lstrip()
    stripped_end = text.rstrip()
    return stripped_start[: len(start_marker)].lower() == start_marker.lower() and stripped_end[
        -len(end_marker) :
    ].lower() == end_marker.lower()


def _bounded_diff(diff: str, max_bytes: int) -> str:
    if len(diff.encode("utf-8")) <= max_bytes:
        return diff if diff.endswith("\n") else diff + "\n"
    encoded = diff.encode("utf-8")[:max_bytes]
    bounded = encoded.decode("utf-8", errors="ignore")
    if not bounded.endswith("\n"):
        bounded += "\n"
    bounded += f"\n[workspace diff truncated at {max_bytes} bytes]\n"
    return bounded


def _usable_git_baseline(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    try:
        _git(root, "rev-parse", "--verify", "HEAD")
        return True
    except RuntimeError:
        return False


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {output}")
    return completed.stdout


def _parse_git_status(status_output: str) -> list[MemoryWorkspaceChange]:
    changes: list[MemoryWorkspaceChange] = []
    for line in status_output.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            _, path = path.split(" -> ", 1)
        if status == "??" or "A" in status:
            label = "A"
        elif "D" in status:
            label = "D"
        else:
            label = "M"
        if path and path != MEMORY_PHASE2_WORKSPACE_DIFF_FILE and not path.startswith(".git/"):
            changes.append(MemoryWorkspaceChange(label, path))
    changes.sort(key=lambda change: change.path)
    return changes


def _render_untracked_diff(root: Path, changes: list[MemoryWorkspaceChange]) -> str:
    rendered = ""
    for change in changes:
        if change.status != "A":
            continue
        path = root / change.path
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rendered += f"diff --git a/{change.path} b/{change.path}\n"
        rendered += "new file mode 100644\n"
        rendered += "index 0000000..0000000\n"
        rendered += "--- /dev/null\n"
        rendered += f"+++ b/{change.path}\n"
        for line in content.splitlines():
            rendered += f"+{line}\n"
        if content.endswith("\n"):
            continue
        rendered += "\\ No newline at end of file\n"
    return rendered


def _retained_memories(
    memories: list[MemoryStageOneRecord],
    max_raw_memories_for_consolidation: int,
    *,
    max_unused_days: int = MEMORY_MAX_UNUSED_DAYS,
    now: datetime | None = None,
) -> list[MemoryStageOneRecord]:
    return select_phase2_memory_inputs(
        memories,
        max_raw_memories_for_consolidation,
        max_unused_days=max_unused_days,
        now=now,
    )


def _phase2_memory_selection_time(memory: MemoryStageOneRecord) -> datetime:
    if memory.last_usage is not None:
        return _to_utc(memory.last_usage)
    return _to_utc(memory.source_updated_at)


def _memory_extension_resource_timestamp(file_name: str) -> datetime | None:
    timestamp = file_name[:19]
    try:
        parsed = datetime.strptime(timestamp, MEMORY_EXTENSION_RESOURCE_TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _write_rollout_summary_for_thread(root: Path | str, memory: MemoryStageOneRecord) -> None:
    path = rollout_summaries_dir(root) / f"{rollout_summary_file_stem(memory)}.md"
    body = f"thread_id: {memory.thread_id}\n"
    body += f"updated_at: {_format_rfc3339(memory.source_updated_at)}\n"
    body += f"rollout_path: {memory.rollout_path}\n"
    body += f"cwd: {memory.cwd}\n"
    if memory.git_branch:
        body += f"git_branch: {memory.git_branch}\n"
    body += "\n"
    body += memory.rollout_summary
    body += "\n"
    path.write_text(body, encoding="utf-8")


def _rollout_stem_prefix_parts(thread_id: str, source_updated_at: datetime) -> tuple[str, int]:
    try:
        thread_uuid = UUID(thread_id)
    except ValueError:
        return (_format_stem_timestamp(source_updated_at), _fallback_hash_seed(thread_id))

    if thread_uuid.version == 7:
        timestamp_ms = thread_uuid.int >> 80
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    else:
        timestamp = source_updated_at
    return (_format_stem_timestamp(timestamp), thread_uuid.int & 0xFFFF_FFFF)


def _fallback_hash_seed(thread_id: str) -> int:
    seed = 0
    for byte in thread_id.encode("utf-8"):
        seed = ((seed * 31) + byte) & 0xFFFF_FFFF
    return seed


def _base62_4(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    chars = ["0", "0", "0", "0"]
    for idx in range(3, -1, -1):
        chars[idx] = alphabet[value % len(alphabet)]
        value //= len(alphabet)
    return "".join(chars)


def _sanitize_rollout_slug(raw_slug: str) -> str:
    slug_chars: list[str] = []
    for char in raw_slug:
        if len(slug_chars) >= 60:
            break
        if char.isascii() and char.isalnum():
            slug_chars.append(char.lower())
        else:
            slug_chars.append("_")
    return "".join(slug_chars).rstrip("_")


def _format_stem_timestamp(value: datetime) -> str:
    return _to_utc(value).strftime("%Y-%m-%dT%H-%M-%S")


def _format_rfc3339(value: datetime) -> str:
    return _to_utc(value).isoformat()


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> int:
    return int(_to_utc(value).timestamp())


def _timestamp_like(value: datetime | int) -> int:
    if isinstance(value, datetime):
        return _timestamp(value)
    return int(value)


def _datetime_from_timestamp(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _row_int(row: sqlite3.Row, key: str, default: int) -> int:
    value = row[key]
    if value is None:
        return default
    return int(value)


def _row_str(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    return "" if value is None else str(value)


def _memory_record_from_row(row: sqlite3.Row) -> MemoryStageOneRecord:
    last_usage = _datetime_from_timestamp(row["last_usage"])
    return MemoryStageOneRecord(
        thread_id=str(row["thread_id"]),
        source_updated_at=datetime.fromtimestamp(int(row["source_updated_at"]), tz=timezone.utc),
        raw_memory=str(row["raw_memory"]),
        rollout_summary=str(row["rollout_summary"]),
        rollout_slug=row["rollout_slug"],
        rollout_path=str(row["rollout_path"]),
        cwd=str(row["cwd"]),
        git_branch=row["git_branch"],
        usage_count=_row_int(row, "usage_count", 0),
        last_usage=last_usage,
        selected_for_phase2=_row_int(row, "selected_for_phase2", 0) != 0,
    )


_SECRET_REPLACEMENTS = (
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_SECRET]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_SECRET]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}\b"), "Bearer [REDACTED_SECRET]"),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)([\"']?)[^\s\"']{8,}"),
        r"\1\2\3[REDACTED_SECRET]",
    ),
)


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern, replacement in _SECRET_REPLACEMENTS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
