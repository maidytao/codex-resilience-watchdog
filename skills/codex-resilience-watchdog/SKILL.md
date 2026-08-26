---
name: codex-resilience-watchdog
description: Use when a long-running Codex task may be stalled, stuck in Thinking, interrupted during a tool call, or needs bounded automatic recovery without replaying side effects.
---

# Codex Resilience Watchdog

Use the installed watchdog to preserve useful progress and recover only when the
last operation is demonstrably safe to repeat.

## Required workflow

1. Arm one watchdog task for the current Codex session before a long operation.
2. Record a heartbeat only after **real evidence** changes: new tool output, a
   completed substep, a changed result probe, or an advancing durable counter.
3. Before each recoverable step, create a checkpoint and classify `--effect`.
   Use `read-only` only when the operation is also repeatable.
4. Mark the task complete as soon as the intended outcome is verified.
5. If the state is `pending-confirmation` or `circuit-open`, stop automation and
   report the evidence needed from the user.

Read [references/protocol.md](references/protocol.md) for exact commands, effect
classes, result probes, and state meanings. Invoke them through
`scripts/watchdog.py` so the command does not depend on the working directory.

## Safety boundary

- Automatic replay is permitted only for a repeatable `read-only` checkpoint.
- `write`, `external-message`, `delete`, `paid`, and `unknown` checkpoints never
  replay automatically; they enter `pending-confirmation` when outcome is not
  proven.
- When the previous result is uncertain, inspect the declared real-world probe
  before deciding whether to continue.
- A timer tick, unchanged Thinking text, or the daemon being alive is not real
  progress.
- Never fabricate heartbeats to keep a task alive.
- The hard ceiling is **two automatic recoveries** and **one Codex restart** per
  task. A repeated fault fingerprint opens the durable circuit sooner.
- Reset a circuit only after the cause and actual outcome have been checked.

## Quick reference

| Situation | Action |
|---|---|
| Long task begins | `arm` |
| Verifiable progress occurs | `heartbeat` |
| Step can be reconciled | `checkpoint --effect ...` |
| Intended result is verified | `complete` |
| Recovery is ambiguous | Stop at `pending-confirmation` |
| Same fault repeats | Leave `circuit-open` until inspected |

