"""
specialist.py — Specialist agent for the multi-agent demo.

Polls for a task file, calls the LLM, streams the response, writes the result.

Usage:
    python specialist.py --name french --emoji "🇫🇷" --color "1;34" \
        --api-url http://127.0.0.1:8080/v1/chat/completions
"""

import argparse
import json
import os
import time

from utils import COMMS_DIR, RESET, DIM, stream_llm
from runlog import read_sentinel

POLL_INTERVAL = 0.5


def wait_for_task(name: str) -> dict:
    """Poll for a task file."""
    task_path = os.path.join(COMMS_DIR, f"task_{name}.json")
    while True:
        if os.path.exists(task_path):
            time.sleep(0.1)
            try:
                with open(task_path, "r", encoding="utf-8") as f:
                    task = json.load(f)
                os.remove(task_path)
                return task
            except (json.JSONDecodeError, IOError):
                pass
        time.sleep(POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--emoji", default="🤖")
    parser.add_argument("--color", default="1;37")
    parser.add_argument(
        "--api-url", default="http://127.0.0.1:8080/v1/chat/completions"
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Run folder to record this specialist's call into. "
        "If omitted, falls back to AGENT_RUN_DIR env var "
        "or the sentinel written by the orchestrator.",
    )
    args = parser.parse_args()

    color = args.color
    name = args.name
    display = name.upper().replace("_", " ")

    # Header
    print(f"\033[{color}m{'━' * 45}{RESET}")
    print(f"\033[{color}m  {args.emoji}  {display}{RESET}")
    print(f"\033[{color}m{'━' * 45}{RESET}")
    print(f"{DIM}  Waiting for task...{RESET}\n")

    # Wait → Execute → Report
    task = wait_for_task(name)
    instruction = task.get("instruction", "")
    system_prompt = task.get("system_prompt", "")

    print(f"\033[{color}m📋 {instruction[:60]}...{RESET}\n")
    print(f"\033[{color}m{'─' * 45}{RESET}\n")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": instruction})

    # Specialists execute a single, well-defined instruction. Any reasoning
    # the model chooses to do is recorded in the run log for observability.
    # Resolve run_dir: explicit flag > env var > sentinel file written by orchestrator.
    run_dir = args.run_dir or read_sentinel(COMMS_DIR)

    result = stream_llm(
        args.api_url,
        messages,
        agent_name=name,
        color=color,
        max_tokens=8000,
        run_dir=run_dir,
    )

    # Write result
    result_path = os.path.join(COMMS_DIR, f"result_{name}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(
            {"task_id": task.get("task_id", ""), "result": result},
            f,
            ensure_ascii=False,
        )

    print(f"\n\n\033[{color}m{'─' * 45}{RESET}")
    print(f"\033[{color}m  ✅ {args.emoji}  {display} — Done!{RESET}")
    print(f"\033[{color}m{'─' * 45}{RESET}")

    input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
