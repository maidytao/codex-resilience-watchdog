# Watchdog protocol

Run commands through `scripts/watchdog.py`. The wrapper supplies `--home` from
`CODEX_HOME`, falling back to the current user's `.codex` directory.

## Task lifecycle

```powershell
python scripts/watchdog.py arm --task TASK_ID --session SESSION_ID --class ordinary --threshold 300
python scripts/watchdog.py heartbeat --task TASK_ID --evidence "tool output advanced"
python scripts/watchdog.py checkpoint --task TASK_ID --step STEP_ID --effect read-only --repeatable --input-digest DIGEST --probe file-exists --target PATH
python scripts/watchdog.py complete --task TASK_ID
```

Task and session identifiers are opaque local identifiers. Do not place prompts,
secrets, message bodies, or credentials in `--evidence` or `--input-digest`.

## Effect classes

| Value | Meaning | Automatic replay |
|---|---|---|
| `read-only` | Observation with no external mutation | Only with `--repeatable` |
| `write` | Creates or changes data | Never |
| `external-message` | Sends or publishes content | Never |
| `delete` | Removes data | Never |
| `paid` | May create a charge or trade | Never |
| `unknown` | Effect cannot be proven | Never |

Omit `--repeatable` unless the same read produces no additional effect. A
misclassified effect defeats the recovery boundary, so uncertain work is
`unknown`.

## Result probes

- `file-exists`: confirms only whether the declared target exists.
- `file-sha256`: compares the target with the declared expected digest.
- `backend-terminal`: consumes an already recorded terminal result.

Probes never execute an arbitrary command. If a side-effecting step lacks
positive evidence, recovery stops at `pending-confirmation`.

## States

- `armed` / `running`: monitoring is active.
- `suspect` / `reconciling`: progress stopped and real outcomes are being read.
- `recovery-ready` / `recovering`: a bounded safe recovery is in progress.
- `pending-confirmation`: automation stopped because safety or outcome is
  uncertain.
- `circuit-open`: automation stopped because a limit, repeated fault, integrity
  failure, or compatibility failure was reached.
- `completed` / `failed`: terminal; use a new task ID for new work.

Useful operator commands:

```powershell
python scripts/watchdog.py status
python scripts/watchdog.py incidents --limit 50
python scripts/watchdog.py recover --task TASK_ID
python scripts/watchdog.py reset-circuit --task TASK_ID
python scripts/watchdog.py enable
python scripts/watchdog.py disable
```

Run `reset-circuit` only after checking the actual result and cause. It does not
erase counters or prove that replay is safe.
