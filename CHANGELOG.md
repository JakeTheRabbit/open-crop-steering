# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Phase 0 repo bootstrap: MIT LICENSE, README banner, repository.yaml (HA add-on repo metadata), hacs.json, pyproject.toml (ruff/mypy/pytest), package.json (Next.js 15 + shadcn-ready), Alembic skeleton, GitHub Actions workflows (lint, test, docker, addon-test, docs, release), CONTRIBUTING/CODE_OF_CONDUCT/SECURITY.

### Architecture
- Recipe-as-truth: immutable revisions + runtime overlays + effective_target materialization (full design in [plan v3](https://github.com/JakeTheRabbit/open-crop-steering/blob/main/docs/architecture.md)).
- Command queue + idempotency + readback verification — every HA service call audited end-to-end.
- s6-split workers + Postgres advisory locks — no duplicate control loops on restart.
- Tamper-evident audit log (HMAC chain + key_id rotation + off-box daily seal export).
- 4-role RBAC (operator / cultivator / QAP / admin) via HA ingress identity (add-on) or JWT (standalone).
- Strict LLM JSON schema with snapshot_id echo + bounded action validator.
- Event taxonomy: info_event / controlled_adjustment / system_warning / guardrail_rejection / formal_deviation / critical_incident.
- Cultivation knowledge baked in: 12 anti-patterns, 15 equipment-coupling rules, 7 saturation indicators.
- 8-week graduated rollout per param-class with QAP-recorded gates.

[Unreleased]: https://github.com/JakeTheRabbit/open-crop-steering/compare/v0.0.0...HEAD
