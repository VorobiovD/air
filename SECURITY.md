# Security Policy

## Scope

air is a Claude Code plugin that orchestrates code reviews. It executes shell commands (`gh`, `git`) and passes PR data to AI agents. Security concerns include:

- **Prompt injection** via PR titles, bodies, commit messages, or review comments
- **Command injection** via malformed PR numbers, SHAs, or repo names
- **Credential exposure** if secrets appear in diffs passed to agents
- **Wiki tampering** if a malicious actor pushes crafted patterns to the wiki

## Reporting a Vulnerability

If you discover a security issue, please report it privately:

1. **GitHub:** Use [private vulnerability reporting](https://github.com/VorobiovD/air/security/advisories/new) (preferred)
2. **Subject:** `[air security] <brief description>`
3. **Include:** steps to reproduce, affected version, potential impact

Please do **not** open a public issue for security vulnerabilities.

## Response

- Acknowledgment within 48 hours
- Fix or mitigation within 7 days for critical issues
- Credit in the fix commit unless you prefer anonymity

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | Yes       |
