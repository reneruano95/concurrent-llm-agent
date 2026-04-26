"""
runlog.py — Per-run persistence for the multi-agent demo.

Every LLM call (planner + each specialist + retries) is recorded as one JSON
file inside a timestamped run folder. The folder also stores a copy of the
final rendered page and a top-level run.json with metadata.

Layout:
    demo/.runs/
      2026-04-25T19-32-14_code_FizzBuzz/
        run.json
        planner__1.json
        python__1.json
        rust__1.json
        rust__2.json    ← retry attempt
        index.html

This module is intentionally tiny and dependency-free. Each writer is
process-local (no locking) because every agent owns a unique filename.
"""

import json
import os
import re
import time
from datetime import datetime

RUNS_DIR = os.path.join(os.path.dirname(__file__), ".runs")
RUN_DIR_ENV = "AGENT_RUN_DIR"  # fallback channel for child processes


# ─── Path helpers ───────────────────────────────────────────


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert an arbitrary string into a safe filesystem fragment."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return s[:max_len] or "untitled"


def new_run_dir(scenario: str, topic: str) -> str:
    """Create and return a fresh timestamped run folder."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    name = f"{ts}_{_slugify(scenario)}_{_slugify(topic)}"
    path = os.path.join(RUNS_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


# ─── Per-call record writer ─────────────────────────────────

# Track attempt counters per (run_dir, agent) within this process.
_attempt_counts: dict[tuple[str, str], int] = {}


def log_call(
    run_dir: str | None,
    agent: str,
    *,
    request: dict,
    response: str,
    tokens: int,
    elapsed_s: float,
    finish_reason: str | None,
    response_format: dict | None = None,
    error: str | None = None,
    reasoning_tokens: int | None = None,
    reasoning_preview: str | None = None,
    model: str | None = None,
) -> str | None:
    """Persist one LLM call. Returns the file path or None if disabled.

    Multiple calls with the same `agent` (e.g. retries) get suffixes __1, __2.
    """
    if not run_dir:
        return None
    try:
        os.makedirs(run_dir, exist_ok=True)
        key = (run_dir, agent)
        n = _attempt_counts.get(key, 0) + 1
        _attempt_counts[key] = n
        path = os.path.join(run_dir, f"{_slugify(agent)}__{n}.json")
        record = {
            "agent": agent,
            "attempt": n,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "request": request,
            "response_format": response_format,
            "finish_reason": finish_reason,
            "tokens": tokens,
            "reasoning_tokens": reasoning_tokens,
            "elapsed_s": round(elapsed_s, 3),
            "tps": round(tokens / elapsed_s, 1) if elapsed_s > 0 else 0.0,
            "response": response,
            "reasoning_preview": reasoning_preview,
            "error": error,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception:
        # Persistence must never break a run.
        return None


# ─── Top-level run metadata ─────────────────────────────────


def write_run_meta(run_dir: str | None, meta: dict) -> None:
    if not run_dir:
        return
    try:
        os.makedirs(run_dir, exist_ok=True)
        meta = dict(meta)
        meta.setdefault("written_at", datetime.now().isoformat(timespec="seconds"))
        with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_page(run_dir: str | None, html: str) -> None:
    if not run_dir:
        return
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass


# ─── Sentinel file in COMMS_DIR so specialists can find the run ──


def write_sentinel(comms_dir: str, run_dir: str) -> None:
    try:
        os.makedirs(comms_dir, exist_ok=True)
        with open(
            os.path.join(comms_dir, "current_run.txt"), "w", encoding="utf-8"
        ) as f:
            f.write(run_dir)
    except Exception:
        pass


def read_sentinel(comms_dir: str) -> str | None:
    """Used by specialists started in a separate process by run.ps1."""
    env = os.environ.get(RUN_DIR_ENV)
    if env:
        return env
    path = os.path.join(comms_dir, "current_run.txt")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None
