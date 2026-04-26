# Symbol-Agent Orchestrator

> **Status:** Design proposal. No code yet.
> **Related:**
> [agent-console.md](./agent-console.md) (operator-facing TUI — different concern),
> [ADR 0002](../adrs/0002-agent-console-and-strategy-operation.md),
> [ai-agent-automation.md](../ai/ai-agent-automation.md),
> [system-architecture.md](./system-architecture.md)
>
> **Terminology note.** "Agent" in this document = an in-process,
> stateful runtime actor (`SymbolAgent` or `StrategyAgent`) that
> consumes bars and emits `AgentVerdict` records. The operator-facing
> LLM agents hosted in TUI panes are a separate concern — see
> [agent-console.md](./agent-console.md). The two layers share no
> process; they meet only through the Tool Server, which exposes this
> layer's state read-only to console panes, and through Tier 1/2
> proposals from the console that mutate the strategy configs this
> layer consumes.

## Purpose

A runtime, in-process actor topology that mirrors the existing
`RegimeClassifierPool` / `RegimeOrchestrator` shape **one tier up**:
stateful agents fanned out from a single `PortfolioOrchestrator`,
with a star topology and event-driven LLM escalation.

The layer uses **two leaf agent types** under one orchestrator:

- **`SymbolAgent` (default, one per traded symbol)** — owns the
  symbol's regime/feature/position state. Hosts per-symbol strategies
  as pluggable `IStrategyEvaluator` rules (NOT as separate agents).
  This is the hot path and matches the proven `RegimeClassifierPool`
  shape one-to-one.
- **`StrategyAgent` (only for intrinsically multi-symbol strategies)** —
  owns cross-symbol rule state for strategies whose definition spans
  symbols (e.g. breadth/correlation, FAB4 straddler, basket regime
  transitions). Reads symbol state via read-only snapshots from
  `SymbolAgent`s through the orchestrator. Never owns a regime
  classifier or position itself.

Both leaf types emit structured `AgentVerdict` records. The
`PortfolioOrchestrator` applies portfolio-level rules (correlation,
exposure, capital, vetoes) across all verdicts and emits a single
`TradeDecision`.

This is **not** the Agent Console (operator TUI). This is the runtime
decision tier that LEAN strategies (or a future C# decision service)
can consume.

## Non-Goals

- Replace LEAN as the order executor.
- Put an LLM on the per-bar hot path.
- Allow agents to talk peer-to-peer (mesh forbidden — star only).
- Replace `RegimeClassifierService` / `RegimeClassifierPool`. The
  Symbol-Agent layer **wraps and consumes** them, never replaces them.
- Replace operator-facing console panes (see `agent-console.md`).

## Why this fits AlgoSpark

The shape is already proven on the ML side:

| ML tier (exists) | Agent tier (proposed) |
|---|---|
| `RegimeClassifierPool` (one service per symbol) | `ISymbolAgentPool` (one agent per symbol) |
| `RegimeOrchestrator` (fan-out, fault isolation) | `PortfolioOrchestrator` (fan-out, portfolio rules, snapshot routing) |
| `IsFaulted` / `HardReset` per service (B2/B5) | `IsFaulted` / `HardReset` per leaf (Symbol or Strategy) |
| `Pool.Remove(symbol)` disposes per-symbol state | Same contract; `IStrategyAgentRegistry.Remove(strategyId)` mirrors it |
| `BLEND_ALPHA`, `MARGIN_THR`, `B_UP` blending math | Same math reused for agent-confidence blending |
| (n/a — features computed per symbol inline) | `IStrategyEvaluator` plug-ins inside `SymbolAgent` for per-symbol strategies |
| (n/a — no multi-symbol ML model today) | `IStrategyAgent` second leaf type for intrinsically multi-symbol strategies only |

## Topology

```
                  ┌──────────────────────────────┐
                  │  MarketBus (existing)        │
                  └──────────────┬───────────────┘
                                 │ Bar (per symbol)
                                 ▼
              ┌──────────────────────────────────────────┐
              │  PortfolioOrchestrator (single instance) │
              │  - fans bars to SymbolAgents             │
              │  - publishes read-only symbol snapshots  │
              │    to StrategyAgents                     │
              └──┬──────────┬──────────┬──────────┬──────┘
                 │          │          │          │
        ┌────────▼──┐ ┌─────▼────┐ ┌───▼─────┐ ┌──▼─────────────┐
        │SymbolAgent│ │SymbolAgnt│ │SymbolAgn│ │ StrategyAgent  │
        │  "SPY"    │ │  "QQQ"   │ │  "NVDA" │ │ "BreadthCorr"  │
        │ owns:     │ │  ...     │ │  ...    │ │ owns:          │
        │  Regime   │ │          │ │         │ │  cross-symbol  │
        │  Features │ │          │ │         │ │  rule state    │
        │  Position │ │          │ │         │ │ reads:         │
        │  Strategy │ │          │ │         │ │  N symbol      │
        │  Evaluatrs│ │          │ │         │ │  snapshots     │
        │  Mailbox  │ │          │ │         │ │ (no regime svc)│
        └─────┬─────┘ └─────┬────┘ └────┬────┘ └────┬───────────┘
              │             │           │           │
              └─────────────┴─────┬─────┴───────────┘
                                  │ AgentVerdict stream
                                  ▼
                  ┌──────────────────────────────┐
                  │  PortfolioOrchestrator       │
                  │  (correlation, exposure,     │
                  │   capital, vetoes)           │
                  └──────────────┬───────────────┘
                                 │ TradeDecision
                                 ▼
                  ┌──────────────────────────────┐
                  │  LEAN strategy (existing)    │
                  └──────────────────────────────┘
```

**Star topology only.** Leaf agents (Symbol or Strategy) never
communicate peer-to-peer. All cross-leaf coordination — including
passing symbol snapshots to `StrategyAgent`s — flows through
`PortfolioOrchestrator`.

## Core Contracts (shape, not final)

```csharp
public interface ISymbolAgent : IAsyncDisposable
{
    string Symbol { get; }
    bool IsFaulted { get; }

    // Cheap, deterministic, every bar. Updates internal state
    // (regime classifier, features, position) and runs all
    // registered IStrategyEvaluator rules. No LLM call.
    ValueTask OnBarAsync(Bar bar, CancellationToken ct);

    // Rare, event-driven. May invoke LLM. Returns null when no verdict.
    ValueTask<AgentVerdict?> OnEventAsync(AgentEvent evt, CancellationToken ct);

    // Read-only snapshot consumed by StrategyAgents via the orchestrator.
    // Never exposes mutable internal state.
    SymbolSnapshot GetSnapshot();

    void HardReset();
}

public interface ISymbolAgentPool : IAsyncDisposable
{
    ISymbolAgent GetAgent(string symbol);
    bool Remove(string symbol);  // disposes per-symbol state
}

// Per-symbol strategy logic plugs in here, NOT as a separate agent.
// One SymbolAgent hosts N evaluators. Deterministic, no LLM.
public interface IStrategyEvaluator
{
    string StrategyId { get; }
    AgentVerdict? Evaluate(in SymbolSnapshot snap, in Bar bar);
}

// Multi-symbol strategies only. Reads snapshots, owns no regime svc,
// no position, no per-symbol state beyond what the rule needs.
public interface IStrategyAgent : IAsyncDisposable
{
    string StrategyId { get; }
    IReadOnlyList<string> SubscribedSymbols { get; }
    bool IsFaulted { get; }

    // Called by PortfolioOrchestrator when any subscribed symbol
    // produces a new bar. Receives the full snapshot bundle.
    ValueTask<AgentVerdict?> OnBarAsync(
        IReadOnlyDictionary<string, SymbolSnapshot> snapshots,
        CancellationToken ct);

    void HardReset();
}

public interface IStrategyAgentRegistry : IAsyncDisposable
{
    IReadOnlyList<IStrategyAgent> Active { get; }
    bool Remove(string strategyId);
}

public sealed record AgentVerdict(
    string  SourceId,         // symbol ("SPY") or strategy ("BreadthCorr")
    string? Symbol,           // target symbol if directional; null for vetoes
    DateTime BarTimestamp,
    AgentSide Side,           // Long / Short / Flat / Veto
    double  Confidence,       // [0, 1]
    string  Reason,           // structured, machine-readable
    string? LlmNarrative);    // optional human-readable

public sealed class PortfolioOrchestrator
{
    // Subscribes to AgentVerdict stream from all SymbolAgents and
    // StrategyAgents, applies portfolio-level rules, emits TradeDecision.
    // Owns the snapshot fan-out from SymbolAgents to StrategyAgents.
}
```

Place in a new `src/AlgoSpark.Agents/` project, sibling to
`AlgoSpark.RegimeClassifier`. Strictly downstream — the regime
classifier never depends on `AlgoSpark.Agents`.

## Library Layout & Plug-in Seams

`AlgoSpark.Agents` ships as **one csproj with internal folder
boundaries**, the same discipline `AlgoSpark.RegimeClassifier` uses
today (`Abstractions/`, `Inference/`, `Features/`, `Generated/`).
One library, but layered so plug-ins, hosts, and tests only see the
parts they need. See [ADR 0004](../adrs/0004-agents-library-layout.md)
for the decision and the split triggers.

```
src/AlgoSpark.Agents/
├── Abstractions/        ← contracts only, zero deps beyond BCL
│   ├── ISymbolAgent.cs
│   ├── IStrategyAgent.cs
│   ├── IStrategyEvaluator.cs
│   ├── IPortfolioRule.cs
│   ├── ISymbolFeatureSource.cs
│   ├── IAgentSink.cs
│   ├── IAgentEventHandler.cs        ← Phase C only
│   ├── AgentVerdict.cs
│   ├── SymbolSnapshot.cs
│   ├── AgentEvent.cs
│   └── TradeDecision.cs
│
├── Runtime/             ← orchestrator, pools, channels, fault model
│   ├── SymbolAgent.cs                (internal sealed)
│   ├── SymbolAgentPool.cs            (internal sealed)
│   ├── StrategyAgent.cs              (internal sealed)
│   ├── StrategyAgentRegistry.cs      (internal sealed)
│   ├── PortfolioOrchestrator.cs      (internal sealed)
│   ├── AgentMailbox.cs               ← System.Threading.Channels wrapper
│   └── FaultGuard.cs                 ← B2/B5-style faulted+HardReset
│
├── Composition/         ← DI + builder, no business logic
│   ├── AgentServiceCollectionExtensions.cs
│   ├── AgentSystemBuilder.cs
│   └── AgentSystemOptions.cs
│
└── Evaluators/          ← built-in portfolio rules only
    └── PortfolioRules/
        ├── MaxGrossExposureRule.cs
        ├── CorrelationCapRule.cs
        └── DrawdownCircuitBreakerRule.cs
```

### Five seams that do all the modularity work

```csharp
// What feeds a SymbolAgent each bar.
// RegimeClassifierService is wrapped by an adapter that implements this;
// the runtime never sees RegimeClassifierService directly.
public interface ISymbolFeatureSource
{
    SymbolSnapshot UpdateAndSnapshot(in Bar bar);
    void HardReset();
}

// Per-symbol strategy logic. Most common plug-in point.
public interface IStrategyEvaluator
{
    string StrategyId { get; }
    AgentVerdict? Evaluate(in SymbolSnapshot snap, in Bar bar);
}

// Multi-symbol strategy. Only when the rule is intrinsically cross-symbol.
public interface IStrategyAgent { /* see Core Contracts above */ }

// Portfolio-level rule. Correlation cap, exposure cap, drawdown breaker.
public interface IPortfolioRule
{
    TradeDecision? Apply(IReadOnlyList<AgentVerdict> verdicts,
                         PortfolioState state);
}

// Where TradeDecisions go. LeanExecutionEngine (ADR 0003) implements this.
public interface IAgentSink
{
    ValueTask EmitAsync(TradeDecision decision, CancellationToken ct);
}
```

Phase C adds a sixth seam — `IAgentEventHandler` — for event-driven
LLM hooks (regime flip, signal candidate, anomaly). Not part of the
Phase A surface.

### Modularity rules

1. **`Abstractions/` depends on nothing** except BCL primitives. No
   ONNX, no `RegimeClassifier`, no LEAN. This is what plug-in authors
   reference.
2. **`Runtime/` depends only on `Abstractions/`** plus
   `System.Threading.Channels`. All runtime types are
   `internal sealed` — consumers reach the runtime through
   `Composition/`, never by `new`-ing `PortfolioOrchestrator` directly.
3. **`Composition/` is the only public wire-up surface.** A single
   `AddAlgoSparkAgents(builder => …)` extension method on
   `IServiceCollection` is the entry point.
4. **Strategy-specific evaluators live in their own projects**
   (e.g. `AlgoSpark.Strategies.ElephantBar`) and reference only
   `AlgoSpark.Agents` (or, after the future split,
   `AlgoSpark.Agents.Abstractions`). They never reference
   `Runtime/`.
5. **`AlgoSpark.RegimeClassifier` does not reference
   `AlgoSpark.Agents`.** The dependency is one-way: an adapter in
   the host (or in a small `AlgoSpark.Agents.RegimeClassifier`
   bridge project, if it grows) wraps `RegimeClassifierService` as
   `ISymbolFeatureSource`.

### One-call wire-up (illustrative)

```csharp
services.AddAlgoSparkAgents(builder => builder
    .UseFeatureSource<RegimeFeatureSource>()         // wraps RegimeClassifierService
    .UseSink<LeanExecutionEngineSink>()              // wraps IExecutionEngine (ADR 0003)
    .AddSymbol("SPY")
    .AddSymbol("QQQ")
    .AddSymbol("NVDA")
    .AddEvaluator<ElephantBarEvaluator>(symbols: new[] { "SPY", "QQQ" })
    .AddEvaluator<IgnitionSetupEvaluator>(symbols: AllSymbols)
    .AddStrategyAgent<BreadthCorrelationAgent>(
        symbols: new[] { "SPY", "QQQ", "IWM" })
    .AddPortfolioRule<MaxGrossExposureRule>()
    .AddPortfolioRule<CorrelationCapRule>()
    .ConfigureChannels(opts => opts.Capacity = 16)
);
```

### When to split the csproj

Stay one csproj **until** one of these triggers fires:

| Trigger | Split into |
|---|---|
| External plug-in authors want to reference contracts without pulling in `System.Threading.Channels` and the runtime | `AlgoSpark.Agents.Abstractions` + `AlgoSpark.Agents` (runtime) |
| Built-in evaluators grow past ~5 with heavy deps | `AlgoSpark.Agents.Evaluators` |
| A second runtime is being prototyped (e.g. actor-based) against the same contracts | `AlgoSpark.Agents.Runtime.Akka` (or similar), keeping the existing one |

Until then, `internal sealed` on runtime types plus folder
boundaries give 90% of the modularity with 10% of the project sprawl.
The split is a refactor, not a redesign — `Abstractions/` already
isolates the public contract.

### What's deliberately *not* in this library

- **Execution.** The library emits `TradeDecision`. `IExecutionEngine`
  ([ADR 0003](../adrs/0003-execution-engine-seam.md), extracted to
  `AlgoSpark.Execution` per
  [ADR 0005](../adrs/0005-extract-execution-project.md)) consumes it.
  Two libraries, two concerns. `AlgoSpark.Agents` references
  `AlgoSpark.Execution.Abstractions`, never `AlgoSpark.AlgoEngine`.
- **Feature engineering.** `ISymbolFeatureSource` is a port; the
  implementation lives in `AlgoSpark.RegimeClassifier` (or wherever
  features come from).
- **LLM calls.** `IAgentEventHandler` is the Phase C plug-in point.
  Not part of the Phase A surface.
- **Persistence.** Verdict / decision streams flow through
  `IAgentSink` and an optional `IAgentJournal`. Storage choice is
  the host's problem.

### When to choose which

| Strategy shape | Lives as | Examples (see `docs/strategies/`) |
|---|---|---|
| Triggers on a single symbol's bars + regime | `IStrategyEvaluator` inside `SymbolAgent` | `elephant-bar-strategy`, `ignition-setup-strategy`, `sma20-pullback-strategy`, `sideways-mean-reversion-strategy`, `color-change-strategy`, `leg-counting-strategy` |
| Definition intrinsically spans 2+ symbols | `IStrategyAgent` (separate leaf) | `breadth-correlation-monitor`, `fab4-straddler-filter`, basket variants of `regime-transition-strategy` / `market-regime-classifier-v2` |

Default to `IStrategyEvaluator`. Only promote to `IStrategyAgent` when
the rule cannot be expressed without reading sibling-symbol state.

## Threading & Concurrency

- **One mailbox per agent** via `System.Threading.Channels`. Each
  `SymbolAgent` and each `StrategyAgent` consumes its own channel
  single-threaded. No locks inside an agent.
- **Snapshot ordering.** `SymbolAgent.OnBarAsync` must complete
  (and publish its updated `SymbolSnapshot`) BEFORE the orchestrator
  forwards the bar event to any `StrategyAgent` subscribed to that
  symbol. This guarantees `StrategyAgent`s never see torn state.
- **Per-leaf fault isolation.** If a `SymbolAgent` or `StrategyAgent`
  throws, mark `IsFaulted = true`, drop it from the active set, log;
  siblings keep running. Same contract as `RegimeClassifierService`
  (B2). A faulted `StrategyAgent` never disables its underlying
  symbols — the strategy is dropped, not the symbols.
- **ONNX Runtime gotcha (verified).** Default
  `IntraOpNumThreads` makes parallel cross-symbol fan-out give ~zero
  speedup vs sequential. Set `SessionOptions.IntraOpNumThreads = 1`
  in any `RegimeClassifierService` consumed by the agent layer if true
  per-symbol concurrency is required.
- **Backpressure.** Bounded channels (capacity ~ 8–16 bars). If a
  SymbolAgent falls behind, drop oldest bar and emit a stale-data
  metric. Never block the producer.

## LLM Discipline

The deterministic C# layer (RegimeClassifier, indicators, position
math) runs **every bar**. The LLM runs **only on events**:

| Event | Trigger | LLM? |
|---|---|---|
| Regime flip | `RegimeBarResult.SmoothedLabel` changes | Yes (explain + confirm) |
| Signal candidate | Strategy rule fires | Yes (sanity check) |
| Anomaly | Rule-based detector fires (gap, spread blowout) | Yes (classify) |
| Per-bar update | Every bar | **No** |

**Caching is mandatory.** Key responses by
`(symbol, bar_ts, prompt_hash)` and persist to disk. Without this,
backtests are non-reproducible and per-event LLM cost compounds.

**Local-first via Gemma.** Use `Microsoft.ML.OnnxRuntime` (already
shipped at 1.20.1) or LlamaSharp for in-process inference.
`temperature = 0`, fixed seed, versioned prompts in a generated file
(same pattern as `EnsembleWeights.g.cs`).

## Scaling Notes

- Comfortable scale: **10–50 symbols**. Matches current 13 gold
  symbols (AAPL, CRWD, TQQQ, NVDA, HOOD, C, AMD, SPY, QQQ, GLD, META,
  XLE, IWM).
- Beyond ~50: tier hot vs cold symbols. Hot symbols get a dedicated
  agent; cold symbols share a generic agent. Same idea as a thread-pool
  for low-priority work.

## Phased Roll-Out

| Phase | Scope | LLM? |
|---|---|---|
| **A — Scaffolding** | `ISymbolAgent`, `ISymbolAgentPool`, `IStrategyEvaluator`, `PortfolioOrchestrator`, channel plumbing, fault model. SymbolAgent wraps `RegimeClassifierService`, runs registered evaluators, and emits structured `AgentVerdict`. Tests prove topology + fault isolation + backtest determinism. **No `StrategyAgent` yet.** | No |
| **B — Rule-based portfolio** | `PortfolioOrchestrator` applies correlation/exposure/capital rules across verdicts. Likely the highest-ROI phase on its own. | No |
| **B′ — Multi-symbol StrategyAgents** | Add `IStrategyAgent` + `IStrategyAgentRegistry`, snapshot fan-out, ordering guarantee. Port the first basket strategy (likely `breadth-correlation-monitor`). Only ship after Phase A is stable, since it adds a second leaf type to every orchestrator code path. | No |
| **C — Event-driven LLM** | Add LLM only on regime flip / signal / anomaly events. Response cache from day one. Start with one symbol (SPY), expand symbol-by-symbol. | Yes (cloud or local) |
| **D — Local Gemma via ORT** | Replace cloud LLM with quantized Gemma running through `Microsoft.ML.OnnxRuntime` for zero per-call cost and deterministic backtests. | Yes (local) |

Each phase is independently shippable and reversible. Phase A is a
prerequisite for B/B′/C/D; B′ is independent of B but should land
after A. Nothing else depends on B/B′/C/D shipping.

## Order-Path Safety

Inherits ADR 0002's structural guarantee: **the Symbol-Agent layer has
no broker tools.** It produces `TradeDecision` records consumed by
the execution engine. Per [ADR 0003](../adrs/0003-execution-engine-seam.md),
the `IExecutionEngine` boundary is the only order path; today the only
implementation is `LeanExecutionEngine`, but the guarantee no longer
depends on LEAN's process boundary specifically. Tier 3 absence is
enforced at the seam, not by the agent layer's internal policy.

## Relationship to the Agent Console

This layer and the [Agent Console](./agent-console.md) are **two
stacked layers that share no process and no broker tools**:

- This layer (Symbol-Agent Orchestrator) runs deterministically per
  bar inside the trading host. It owns regime/feature/position state
  and emits `TradeDecision` records to LEAN. Phase C may invoke an LLM
  on rare events (regime flip, signal candidate, anomaly) inside this
  process, with mandatory caching keyed by `(symbol, bar_ts,
  prompt_hash)`.
- The Agent Console runs in a separate process (Python `textual` TUI
  + per-pane LLM subprocess). Its agents help the operator review,
  propose, and approve changes. They never produce verdicts and never
  call LEAN.
- They meet at the **Tool Server**: console panes read this layer's
  state via Tier 0 tools, and propose strategy/evaluator config
  changes via Tier 1 → Tier 2 apply. This layer reloads the changed
  config on its next cycle.

The "no LLM on the per-bar hot path" rule is satisfied because every
LLM call site — Phase C event escalation here, console panes there —
is off the per-bar deterministic loop. The console doc's stronger
phrasing ("no LLM on the trade decision path") refers specifically to
the console layer; this layer's Phase C is the in-scope exception, run
with different budgets and audit streams.

## Python LLM Substrate (in-repo)

Both LLM-touching surfaces in this design — the **Agent Console**
(separate process) and **Phase C event escalation** (in-process, but
strictly off the per-bar hot path) — need the same underlying
capability: **fan out N concurrent local Gemma calls from a single
orchestrator against one `llama-server`**, with caching, structured
output, and fault isolation.

That capability lives in this repo at **`python/agents-substrate/`**
(migrated from the standalone `concurrent-llm-agent` demo, which is
now archived). Co-locating it here is deliberate:

- One source of truth, one version, atomic cross-language changes.
- Proto-defined contracts (`proto/agents.proto`) regenerate for both
  C# and Python from the same root.
- ADRs, docs, and code stay in one tree.

Co-location does **not** weaken the architectural guarantees, because
those guarantees are structural (process boundaries, import
boundaries, seam ownership), not organizational. The substrate is
**not** a runtime tier and must never try to be one — `SymbolAgent` /
`PortfolioOrchestrator` stay in C# in-process per the rest of this
doc.

### Repo layout (relevant slice)

```
/                                  ← AlgoSpark monorepo root
├── src/                           ← C# (AlgoSpark.Agents, RegimeClassifier, …)
├── proto/
│   └── agents.proto               ← AgentVerdict / TradeDecision / Phase C RPC
├── python/
│   ├── agents-substrate/          ← orchestrator + specialists (Gemma fan-out)
│   │   ├── pyproject.toml
│   │   └── demo/
│   └── console/                   ← operator TUI built on the substrate
├── docs/
└── adrs/
```

### Roles, by phase

| Phase | Role for `python/agents-substrate/` |
|---|---|
| Now (no AlgoSpark runtime dependency) | **Agent Console base.** Long-lived event loop, panes = specialists, dashboard reused, Tool Server client added later. Lowest-risk slot — bugs cost operator minutes, not money. |
| AlgoSpark Phase A ships | Console panes wire to **Tool Server (Tier 0 read-only)** to observe live `AgentVerdict`s. |
| AlgoSpark Phase B ships | Console gains an **offline backtest-critique mode** that consumes recorded `AgentVerdict` / `TradeDecision` streams. |
| AlgoSpark Phase C lands | Same orchestrator + specialists are **promoted into the Phase C worker pool** behind a gRPC seam (`proto/agents.proto`) called by the C# host. |
| AlgoSpark Phase D | Phase C may collapse to in-process Gemma via ORT in C#. The substrate stays as the Console base regardless. |

### Hardening required before either slot qualifies

The demo as-shipped satisfies *shape*, not *substance*. Before it can
back the Console (and certainly before Phase C), it must add:

1. **Disk-backed response cache** keyed by `(symbol, bar_ts,
   prompt_hash)`. Mandatory per the LLM Discipline section above;
   without it, backtests are non-reproducible and Phase C cost
   compounds.
2. **Pydantic schemas** for verdict-shaped outputs. Reject malformed
   LLM output; never render free text into anything trade-adjacent.
3. **`temperature = 0`, fixed seed, versioned/hashed prompt files**
   (mirror the `EnsembleWeights.g.cs` pattern).
4. **Replace 0.5s filesystem polling** (`COMMS_DIR` + `POLL_INTERVAL`)
   with an in-process queue or socket IPC. Filesystem polling is
   fine for a demo, hostile to determinism, and would silently
   violate the snapshot-ordering invariant if ever placed near the
   hot path.
5. **Per-specialist fault model** mirroring `IsFaulted` / `HardReset`.
   A crashed specialist must be a structured fault, not a missing
   file.
6. **No broker tools, ever.** Inherits ADR 0002's structural
   guarantee. The Console process and the Phase C worker pool both
   stay Tier-3-absent. Enforced by Python import linting (no
   broker SDKs in `python/**` requirements).

### Sequencing rationale

The work to harden the substrate for the Console is ~80% the same
work Phase C will need (cache, schemas, prompt versioning, fault
model). Starting with the Console means that work gets done in a
low-risk setting, before any C# code depends on it, and is reused
unchanged when Phase C lands. Building the Console first is therefore
not a detour — it is the prerequisite.

### Monorepo discipline that preserves the guarantees

Co-location only works if the boundaries are enforced mechanically,
not socially:

- **Path-filtered CI.** C# jobs ignore `python/**`; Python jobs ignore
  `src/**`; changes under `proto/**` trigger both. AlgoSpark CI never
  needs `llama-server` or a GPU to go green.
- **`CODEOWNERS` by path.** `python/**` does not require trading-
  system review (and vice versa). The Console iterates fast; the
  runtime tier iterates carefully.
- **Import boundaries.** No Python module imports a broker SDK. No
  C# project under `src/AlgoSpark.Agents/` references generated
  Python. Enforced in CI (lint on Python deps; csproj graph check on
  C# side).
- **Proto is the only joint surface.** `proto/agents.proto` is the
  single contract both languages own jointly; everything else is
  one-side-only.
- **Process boundary at runtime is non-negotiable.** Console is its
  own process. Phase C worker pool is its own process (or, in
  Phase D, replaced entirely by in-process ORT). The C# trading host
  never imports Python code in-process.

### What this co-location does *not* change

- `SymbolAgent`, `StrategyAgent`, and `PortfolioOrchestrator` remain
  C# and in-process. Nothing in this section relaxes that.
- The Order-Path Safety guarantee is unaffected: neither the Console
  process nor the Phase C worker pool gets broker tools.
- `AlgoSpark.Agents` does not take a build-time dependency on the
  Python substrate. The relationship is one-way at runtime, via gRPC
  (Phase C) or the Tool Server (Console), and zero-way at build time
  except for the shared proto.

## Open Questions (defer to follow-up ADRs)

1. Does the Symbol-Agent layer live in a separate process from LEAN,
   or in-process inside a future `AlgoSpark.AlgoEngine` host? Phase A
   should remain in-process to minimize moving parts.
2. Schema for `AgentVerdict` / `TradeDecision` / `SymbolSnapshot`
   (protobuf vs records).
3. Wire format if the layer is ever extracted into its own service
   (likely gRPC, mirroring the future Tool Server choice).
4. Whether the operator-facing Agent Console (separate concern, see
   `agent-console.md`) gets a new pane to observe live agent verdicts,
   and whether that pane is read-only or can issue Tier 1 proposals
   against agent parameters.
5. Conflict resolution when two `IStrategyEvaluator`s on the same
   `SymbolAgent` produce opposing verdicts (e.g. one Long, one Short).
   Initial proposal: SymbolAgent emits both verdicts and the
   `PortfolioOrchestrator` resolves via priority + confidence;
   alternative is local resolution inside the SymbolAgent.
6. Whether `StrategyAgent` verdicts can target a symbol that has no
   active `SymbolAgent` (e.g. a basket signal naming a cold symbol).
   Default: orchestrator drops the verdict and emits a metric.
7. Wire format and process boundary for the Phase C worker pool when
   it is backed by `python/agents-substrate/` (likely gRPC over a
   Unix socket / named pipe defined in `proto/agents.proto`; cache
   lives on the Python side, keyed identically to the in-process
   cache so either backend is substitutable).
