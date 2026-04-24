
## Pre-commit leak scanner

This repo ships with a pre-commit hook at `.githooks/pre-commit` that blocks commits containing:
- RFC1918 IPs (`10.x`, `172.16–31.x`, `192.168.x`)
- Internal hostnames (miniboss, giga, mecha, mega, mongo, rainbow)
- Leaked email patterns

### Activate the hook (one-time, per clone)
```bash
git config core.hooksPath .githooks
```

### Allowlist
Documentation (README, docs/) and RFC 5737 ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) are allowed.

### Bypass (use sparingly)
```bash
git commit --no-verify
```
