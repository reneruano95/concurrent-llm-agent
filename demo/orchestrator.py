"""
orchestrator.py — Orchestrator for the multi-agent demo.

Uses the LLM to decompose a topic into per-agent tasks, dispatches them
via JSON files, collects results, and assembles a visual HTML page.

Usage:
    python orchestrator.py --scenario translate --topic "Gemma is an open AI model"
    python orchestrator.py --scenario svg_art --topic "Technology and AI"
"""

import argparse
import json
import os
import subprocess
import sys
import time

from scenarios import get_scenario
from utils import (
    COMMS_DIR,
    BUILD_DIR,
    RESET,
    DIM,
    BOLD,
    CYAN,
    GREEN,
    YELLOW,
    WHITE,
    stream_llm,
)
from runlog import (
    new_run_dir,
    write_sentinel,
    write_run_meta,
    save_page,
)

POLL_INTERVAL = 0.5


# ─── Step 1: Plan ───────────────────────────────────────────


def _direct_tasks(agents: list[dict], topic: str) -> list[dict]:
    """Strategy 'direct': use each agent's own direct_instruction. No LLM call."""
    tasks = []
    for a in agents:
        instr_tpl = a.get("direct_instruction", "Work on: {topic}")
        try:
            instr = instr_tpl.format(topic=topic, **a)
        except (KeyError, IndexError):
            instr = instr_tpl.replace("{topic}", topic)
        tasks.append({"name": a["name"], "instruction": instr})
    return tasks


def _extract_json_array(raw: str) -> str:
    """Strip <think> blocks and markdown fences, return text from first '[' to last ']'."""
    # Drop <think>...</think> reasoning
    while "<think>" in raw and "</think>" in raw:
        s = raw.index("<think>")
        e = raw.index("</think>") + len("</think>")
        raw = raw[:s] + raw[e:]
    raw = raw.strip()
    # Strip markdown code fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    s = raw.find("[")
    e = raw.rfind("]")
    if s != -1 and e != -1 and e > s:
        return raw[s : e + 1]
    return raw


def _validate_subjects(parsed, n: int) -> str | None:
    """Return None if valid, else an error description for repair."""
    if not isinstance(parsed, list):
        return f"Expected a JSON array, got {type(parsed).__name__}."
    if len(parsed) != n:
        return f"Expected exactly {n} items, got {len(parsed)}."
    for i, x in enumerate(parsed):
        if not isinstance(x, str) or not x.strip():
            return f"Item {i} is not a non-empty string."
    if len({s.strip().lower() for s in parsed}) < n:
        return "Duplicate subjects detected — each must be unique."
    return None


def _decompose_tasks(
    api_url: str, scenario: dict, topic: str, run_dir: str | None = None
) -> list[dict]:
    """Strategy 'decompose': LLM produces N subjects; Python templates the instruction."""
    agents = scenario["agents"]
    n = len(agents)
    decomp = scenario["decompose"]
    template = scenario["instruction_template"]

    user_prompt = decomp["user"].replace("{topic}", topic)
    messages = [
        {"role": "system", "content": decomp["system"]},
        {"role": "user", "content": user_prompt},
    ]
    plan_tokens = max(512, n * 60)

    subjects: list[str] | None = None
    last_error: str | None = None

    for attempt in range(2):  # initial + one repair
        raw = stream_llm(
            api_url,
            messages,
            agent_name="planner",
            color="1;36",
            max_tokens=plan_tokens,
            json_schema=decomp["schema"],
            run_dir=run_dir,
        )
        print("\n")
        try:
            parsed = json.loads(_extract_json_array(raw))
        except json.JSONDecodeError as e:
            last_error = f"Output was not valid JSON: {e.msg}"
            parsed = None

        if parsed is not None:
            err = _validate_subjects(parsed, n)
            if err is None:
                subjects = parsed
                break
            last_error = err

        # Repair: tell the model what was wrong and ask again.
        print(f"{YELLOW}⚠️  Plan invalid ({last_error}) — repairing...{RESET}\n")
        messages = [
            {"role": "system", "content": decomp["system"]},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Your previous output was invalid: {last_error} "
                    f"Output ONLY a JSON array of exactly {n} unique subject strings. "
                    "Nothing else."
                ),
            },
        ]

    if subjects is None:
        print(
            f"{YELLOW}⚠️  Planner failed twice — falling back to direct_instruction.{RESET}\n"
        )
        return _direct_tasks(agents, topic)

    # Bind subjects to agents and template the final instruction.
    tasks = []
    for agent, subject in zip(agents, subjects):
        try:
            instr = template.format(topic=topic, subject=subject, **agent)
        except (KeyError, IndexError):
            instr = template.replace("{topic}", topic).replace("{subject}", subject)
        tasks.append(
            {
                "name": agent["name"],
                "instruction": instr,
                "label": subject,
            }
        )
    return tasks


def plan_tasks(
    api_url: str, scenario: dict, topic: str, run_dir: str | None = None
) -> list[dict]:
    """Dispatch to the right planning strategy for this scenario."""
    print(f"\n{CYAN}{'━' * 60}{RESET}")
    print(f"{CYAN}  🧠 STEP 1: PLANNING{RESET}")
    print(f"{CYAN}{'━' * 60}{RESET}\n")

    strategy = scenario.get("planning_strategy", "direct")
    agents = scenario["agents"]

    if strategy == "direct":
        print(f"{DIM}Strategy: direct (no LLM planning needed){RESET}\n")
        tasks = _direct_tasks(agents, topic)
    elif strategy == "decompose":
        print(
            f"{DIM}Strategy: decompose (LLM → subjects, Python → instructions){RESET}\n"
        )
        tasks = _decompose_tasks(api_url, scenario, topic, run_dir=run_dir)
    else:
        print(
            f"{YELLOW}⚠️  Unknown strategy '{strategy}' — falling back to direct.{RESET}\n"
        )
        tasks = _direct_tasks(agents, topic)

    print(f"{GREEN}✅ Plan: {len(tasks)} tasks{RESET}\n")
    for t in tasks:
        name = t.get("name", "?")
        agent = next((a for a in agents if a["name"] == name), None)
        emoji = agent["emoji"] if agent else "❓"
        instr = t.get("instruction", "")[:60]
        print(f"  {emoji}  {BOLD}{name}{RESET} {DIM}{instr}...{RESET}")
    print()
    return tasks


# ─── Step 2: Dispatch ───────────────────────────────────────


def dispatch(tasks: list[dict], agents: list[dict], system_prompt: str = ""):
    """Write task files so specialist agents can pick them up."""
    print(f"{CYAN}{'━' * 60}{RESET}")
    print(f"{CYAN}  🚀 STEP 2: DISPATCHING{RESET}")
    print(f"{CYAN}{'━' * 60}{RESET}\n")

    task_id = f"task_{int(time.time())}"

    for task in tasks:
        name = task["name"]
        path = os.path.join(COMMS_DIR, f"task_{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "task_id": task_id,
                    "instruction": task["instruction"],
                    "system_prompt": system_prompt,
                },
                f,
            )

        agent = next((a for a in agents if a["name"] == name), None)
        emoji = agent["emoji"] if agent else "📦"
        print(f"  {emoji}  {name}")

    print(f"\n{GREEN}✅ {len(tasks)} tasks dispatched!{RESET}\n")


# ─── Step 3: Collect ────────────────────────────────────────


def collect(tasks: list[dict], agents: list[dict]) -> dict[str, str]:
    """Wait for all agents to write their result files."""
    print(f"{YELLOW}⏳ Waiting for agents...{RESET}\n")

    results = {}
    pending = {t["name"] for t in tasks}

    while pending:
        for name in list(pending):
            path = os.path.join(COMMS_DIR, f"result_{name}.json")
            if os.path.exists(path):
                time.sleep(0.1)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    os.remove(path)
                    results[name] = data.get("result", "")
                    pending.remove(name)
                    done = len(tasks) - len(pending)
                    print(f"  {GREEN}✅{RESET}  Agent {done}/{len(tasks)} done")
                except (json.JSONDecodeError, IOError):
                    pass
        if pending:
            time.sleep(POLL_INTERVAL)

    print(f"\n{GREEN}🎉 All agents finished!{RESET}\n")
    return results


# ─── Step 4: Assemble ───────────────────────────────────────


def assemble(
    scenario: dict,
    topic: str,
    results: dict,
    tasks: list = None,
    run_dir: str | None = None,
):
    """Build the final HTML page from all agent results."""
    print(f"{CYAN}{'━' * 60}{RESET}")
    print(f"{CYAN}  🔧 STEP 3: ASSEMBLING{RESET}")
    print(f"{CYAN}{'━' * 60}{RESET}\n")

    from scenarios import build_page

    page_html = build_page(topic, scenario, results, tasks=tasks)

    os.makedirs(BUILD_DIR, exist_ok=True)
    path = os.path.join(BUILD_DIR, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page_html)

    save_page(run_dir, page_html)

    print(f"  {GREEN}✅ Assembled: index.html{RESET}")

    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
        print(f"  {GREEN}🌍 Opened in browser!{RESET}")
    except Exception:
        pass

    return path


# ─── Main ────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="translate")
    parser.add_argument(
        "--topic", default="Gemma is Google's most capable open AI model"
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=None,
        help="Number of tasks/LLMs (default: scenario default)",
    )
    parser.add_argument(
        "--api-url", default="http://127.0.0.1:8080/v1/chat/completions"
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Optional pre-existing run folder (for shared sessions). "
        "If omitted, a new timestamped folder is created.",
    )
    args = parser.parse_args()

    scenario = get_scenario(args.scenario, n_agents=args.tasks)
    agents = scenario["agents"]

    print(f"\n{CYAN}{'━' * 60}{RESET}")
    print(f"{CYAN}  🏗️  MULTI-AGENT ORCHESTRATOR{RESET}")
    print(f"{CYAN}{'━' * 60}{RESET}")
    print(f"\n{WHITE}  Scenario:{RESET} {args.scenario}")
    print(f"{WHITE}  Topic:{RESET} {args.topic}")
    print(f"{DIM}  {len(agents)} agents{RESET}\n")

    # Clean communication directories
    for d in [COMMS_DIR, BUILD_DIR]:
        if os.path.exists(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
        else:
            os.makedirs(d)

    # Set up the run folder for persistence (run logs + final HTML).
    run_dir = args.run_dir or new_run_dir(args.scenario, args.topic)
    write_sentinel(COMMS_DIR, run_dir)
    print(f"{DIM}  Run folder: {run_dir}{RESET}\n")

    run_started = time.time()
    tasks = plan_tasks(args.api_url, scenario, args.topic, run_dir=run_dir)
    dispatch(tasks, agents, system_prompt=scenario.get("system_prompt", ""))
    results = collect(tasks, agents)
    assemble(scenario, args.topic, results, tasks=tasks, run_dir=run_dir)

    write_run_meta(
        run_dir,
        {
            "scenario": args.scenario,
            "topic": args.topic,
            "n_agents": len(agents),
            "agents": [a["name"] for a in agents],
            "planning_strategy": scenario.get("planning_strategy", "direct"),
            "api_url": args.api_url,
            "total_elapsed_s": round(time.time() - run_started, 3),
            "tasks": tasks,
        },
    )

    print(f"\n{CYAN}{'━' * 60}{RESET}")
    print(f"{CYAN}  ✅ COMPLETE{RESET}")
    print(f"{CYAN}{'━' * 60}{RESET}\n")

    input("Press Enter to close...")


if __name__ == "__main__":
    main()
