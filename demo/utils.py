"""
utils.py — Shared utilities for the multi-agent demo.

Contains the LLM streaming logic, metrics writer, and shared constants
used by both the orchestrator and specialist agents.
"""

import json
import os
import sys
import time

from openai import OpenAI

from runlog import log_call as _runlog_log_call

# ─── Shared Paths ───────────────────────────────────────────

COMMS_DIR = os.path.join(os.path.dirname(__file__), ".agent_comms")
BUILD_DIR = os.path.join(os.path.dirname(__file__), "website_build")

# ─── ANSI Colors ────────────────────────────────────────────

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
WHITE = "\033[1;37m"


# ─── Metrics ────────────────────────────────────────────────


def write_metrics(
    name: str, status: str, tokens: int, elapsed: float, tps: float = None
):
    """Write metrics to .agent_comms/metrics_{name}.json atomically."""
    if tps is None:
        tps = tokens / elapsed if elapsed > 0 else 0.0
    metrics = {
        "name": name,
        "status": status,
        "tokens": tokens,
        "elapsed_s": round(elapsed, 2),
        "tps": round(tps, 1),
    }
    path = os.path.join(COMMS_DIR, f"metrics_{name}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(metrics, f)
    # On Windows, os.replace can fail with PermissionError if the target file
    # is momentarily open by a reader (e.g. the dashboard). Retry briefly.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    # Last resort: drop the tmp file silently — next tick will write again.
    try:
        os.remove(tmp)
    except OSError:
        pass


# ─── LLM Streaming ─────────────────────────────────────────


def stream_llm(
    api_url: str,
    messages: list[dict],
    agent_name: str,
    color: str = "1;37",
    max_tokens: int = 8000,
    json_schema: dict | None = None,
    enable_thinking: bool = True,
    run_dir: str | None = None,
) -> str:
    """Stream an LLM response, update metrics, and print tokens in color.

    Args:
        api_url:    Full chat completions URL (e.g. http://…/v1/chat/completions).
        messages:   OpenAI-style messages list.
        agent_name: Name used for metrics files.
        color:      ANSI color code for terminal output.
        max_tokens: Maximum tokens to generate.
        json_schema: Optional JSON Schema constraining the model's output.
                     Tries response_format=json_schema (strict), falls back to
                     json_object, then to unconstrained free text.
        enable_thinking: If False, ask reasoning models (Qwen3, DeepSeek-R1, …)
                     to skip their <think> chain. Done via chat_template_kwargs
                     when supported, plus a `/no_think` suffix as a fallback
                     hint for Qwen3-family models.

    Returns:
        The full response text (content only, excluding reasoning tokens).
        If the response is truncated (finish_reason='length') with no content,
        automatically retries once with thinking disabled.
    """
    base_url = api_url.rsplit("/chat/completions", 1)[0]
    client = OpenAI(base_url=base_url, api_key="sk-no-key")

    # If thinking is disabled, append a hint to the last user message for
    # models that don't honor chat_template_kwargs (e.g. Qwen3 GGUFs).
    effective_messages = messages
    if not enable_thinking:
        effective_messages = [dict(m) for m in messages]
        for m in reversed(effective_messages):
            if m.get("role") == "user":
                if "/no_think" not in m.get("content", ""):
                    m["content"] = m["content"].rstrip() + " /no_think"
                break

    # Build a list of response_format candidates to try, strongest first.
    rf_candidates: list[dict | None] = []
    if json_schema is not None:
        rf_candidates.append(
            {
                "type": "json_schema",
                "json_schema": {"name": "plan", "schema": json_schema, "strict": True},
            }
        )
        rf_candidates.append({"type": "json_object"})
    rf_candidates.append(None)  # always-available unconstrained fallback

    def _create_stream(rf):
        kwargs = {
            "model": "default",
            "messages": effective_messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if rf is not None:
            kwargs["response_format"] = rf
        # Pass thinking toggle via extra_body for servers that support it
        # (vLLM, llama.cpp w/ Qwen3, LM Studio with reasoning models).
        if not enable_thinking:
            kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False},
            }
        return client.chat.completions.create(**kwargs)

    full = ""
    chunk_count = 0
    server_tokens = None  # Will be set from usage if available
    finish_reason: str | None = None
    error_msg: str | None = None
    start_t = time.time()

    try:
        write_metrics(agent_name, "running", 0, 0.0, 0.0)

        last_poll_t = start_t
        poll_interval = 0.3
        chunks_since_poll = 0

        response = None
        last_err = None
        for rf in rf_candidates:
            try:
                response = _create_stream(rf)
                break
            except Exception as e:
                last_err = e
                continue
        if response is None:
            raise last_err if last_err else RuntimeError("LLM request failed")

        for chunk in response:
            # Final chunk with usage stats (no choices)
            if hasattr(chunk, "usage") and chunk.usage:
                server_tokens = chunk.usage.completion_tokens
                continue

            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = choice.delta

            # Handle reasoning tokens (thinking models)
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                sys.stdout.write(f"\033[2;37m{rc}\033[0m")
                sys.stdout.flush()

            c = delta.content or ""
            if c:
                full += c
                sys.stdout.write(f"\033[{color}m{c}\033[0m")
                sys.stdout.flush()

            if rc or c:
                chunks_since_poll += 1
                chunk_count += 1

                now = time.time()
                if (now - last_poll_t) >= poll_interval:
                    tps = chunks_since_poll / (now - last_poll_t)
                    tokens = server_tokens if server_tokens is not None else chunk_count
                    write_metrics(agent_name, "running", tokens, now - start_t, tps)
                    chunks_since_poll = 0
                    last_poll_t = now

    except Exception as e:
        error_msg = str(e)
        sys.stdout.write(f"\n\033[31m[ERROR] {e}\033[0m\n")

    total_elapsed = time.time() - start_t
    # Use server-reported token count if available, otherwise fall back to chunk count
    final_tokens = server_tokens if server_tokens is not None else chunk_count
    final_tps = final_tokens / total_elapsed if total_elapsed > 0 else 0.0
    write_metrics(agent_name, "done", final_tokens, total_elapsed, final_tps)

    # Persist this call to the run log (if a run_dir was provided).
    _runlog_log_call(
        run_dir,
        agent_name,
        request={"messages": effective_messages, "max_tokens": max_tokens},
        response=full,
        tokens=final_tokens,
        elapsed_s=total_elapsed,
        finish_reason=finish_reason,
        enable_thinking=enable_thinking,
        response_format=({"type": "json_schema"} if json_schema else None),
        error=error_msg,
    )

    # Auto-recover from "model thought itself out of budget": truncated by
    # length with no content emitted. Retry once with thinking disabled.
    if enable_thinking and finish_reason == "length" and len(full.strip()) < 10:
        sys.stdout.write(
            f"\n\033[33m[{agent_name}] Output truncated by reasoning. "
            f"Retrying with thinking disabled...\033[0m\n"
        )
        return stream_llm(
            api_url=api_url,
            messages=messages,
            agent_name=agent_name,
            color=color,
            max_tokens=max_tokens,
            json_schema=json_schema,
            enable_thinking=False,
            run_dir=run_dir,
        )

    return full
