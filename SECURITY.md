# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x   | ✅ (in development) |
| < 0.1   | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Use GitHub's private vulnerability reporting:

1. Go to https://github.com/JakeTheRabbit/open-crop-steering/security/advisories/new
2. Describe the vulnerability + steps to reproduce
3. Include affected versions if known

I'll acknowledge within 7 days and aim for a fix or mitigation within 30 days for high-severity issues.

## Scope

In scope:
- Authentication / RBAC bypass
- Audit log tampering or chain-validation bypass
- Recipe revision corruption / unauthorized edit
- Command queue idempotency / readback bypass that could result in unintended HA actuation
- Secrets exposure (HMAC key, API tokens, etc.)
- Add-on container escape

Out of scope:
- Vulnerabilities in upstream dependencies (please report directly to that project; we'll bump our pin)
- Denial of service via repeated requests (covered by rate limiting; unless you find a way to bypass it)
- Social engineering of facility operators (procedural, not technical)

## Hard reminder

This add-on can actuate physical hardware controlling a licensed cultivation facility. A compromised instance can damage crops, falsify audit records, and trigger compliance deviations. Take security reports seriously; we will too.
