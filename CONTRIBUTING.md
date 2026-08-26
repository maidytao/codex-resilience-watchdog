# Contributing

Thank you for helping improve Codex Resilience Watchdog. Safety boundaries are
part of the public API: a change that makes recovery more permissive must be
treated as a security-sensitive design change.

## Development setup

Requirements:

- Windows 10 or Windows 11
- Python 3.12+
- Windows PowerShell 5.1 or PowerShell 7

```powershell
git clone https://github.com/maidytao/codex-resilience-watchdog.git
cd codex-resilience-watchdog
python -m pip install -e .
python -m unittest discover -s tests -t . -v
```

The runtime intentionally uses only the Python standard library.

## Making a change

1. Create a focused branch from `main`.
2. Add or update a test that demonstrates the intended behavior.
3. Keep recovery fail-closed: unknown effects and uncertain outcomes require
   confirmation.
4. Run the complete test and compile commands.
5. Open a pull request using the repository template.

```powershell
python -m unittest discover -s tests -t . -v
python -m compileall -q src scripts skills\codex-resilience-watchdog\scripts
```

## Safety invariants

Changes must preserve all of these invariants unless an approved design
explicitly replaces them:

- only `read-only` and repeatable operations can replay automatically;
- Codex databases are opened read-only;
- result probes cannot execute arbitrary commands;
- recovery and restart counters are persisted before risky actions;
- every task has hard recovery, restart, and same-fault limits;
- no approval-bypass or sandbox-bypass option is allowed;
- install and uninstall paths stay inside watchdog-owned Codex directories.

## Pull requests

Keep pull requests small and explain:

- the problem and user-visible behavior;
- the safety impact;
- tests run and their results;
- Windows or Codex versions used for manual verification, if relevant.

Do not include prompts, credentials, private task history, real session IDs, or
local audit databases in issues, tests, or pull requests.

