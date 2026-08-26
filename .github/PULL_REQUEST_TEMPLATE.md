## Summary

<!-- What problem does this change solve? -->

## Safety impact

<!-- Explain any effect on replay, reconciliation, recovery, restart, paths, or secrets. -->

- [ ] Automatic replay remains limited to read-only repeatable operations.
- [ ] Uncertain and side-effecting operations still require confirmation.
- [ ] No approval-bypass or sandbox-bypass behavior was added.

## Verification

<!-- List exact commands and results. -->

- [ ] `python -m unittest discover -s tests -t . -v`
- [ ] `python -m compileall -q src scripts skills/codex-resilience-watchdog/scripts`
- [ ] Documentation was updated when user-visible behavior changed.
- [ ] No prompts, credentials, session data, or local audit data are included.
