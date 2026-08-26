# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `0.1.x` | Yes |
| Earlier or unreleased snapshots | No |

## Reporting a vulnerability

Please do not open a public issue for a vulnerability or a recovery-policy
bypass. Use the repository's **Security** tab and submit a private vulnerability
report through GitHub Security Advisories.

Include, when safe to do so:

- affected version and Windows/Codex versions;
- the safety invariant that was bypassed;
- minimal reproduction steps using synthetic data;
- expected and actual behavior;
- whether credentials, messages, files, deletes, or paid actions may be at risk.

Do not attach real prompts, credentials, session databases, task history, or
audit data. You should receive an acknowledgement within 7 days and a status
update within 14 days.

## Security boundaries

The project is designed to fail closed. Unknown operations, uncertain results,
unverified Codex processes, incompatible CLI versions, and integrity failures
must stop automation or open the circuit. A report showing otherwise is
security-relevant even if no external side effect has yet occurred.

