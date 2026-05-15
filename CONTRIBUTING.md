# Contributing to Open Crop Steering

Thanks for your interest. This is a working real-deployment project for a licensed cultivation facility, so contribution standards lean conservative — predictability and auditability matter more than cleverness.

## Before you contribute

1. Read the architecture docs in [`docs/architecture.md`](docs/architecture.md) (once Phase 13 lands) and the design plan it links.
2. Open an issue first for non-trivial changes — happy to discuss design before you write code.
3. The cultivation knowledge file (`docs/concepts/cultivation_knowledge.md`) is the source of truth for the AI's behavior. Changes to AP/EC/SAT IDs should be paired with tests.

## Dev loop

```bash
# Backend
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy backend/app
pytest

# Frontend
cd frontend
npm install
npm run lint
npm run type-check
npm run test
```

## Commits

- Author email **MUST** be a GitHub `noreply` address. Real personal emails get rejected by the pre-commit hook (Phase 0+).
- Use [Conventional Commits](https://www.conventionalcommits.org/) prefixes: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `perf:`.
- Sign commits if you can (`git commit -S`); not required.

## Pull requests

- One concern per PR. Big PRs get split.
- All CI checks must pass (lint, test, docker build, addon-test, docs build).
- Add tests for new behavior. Coverage threshold isn't strict but going down without reason gets a comment.
- Update `CHANGELOG.md` under `[Unreleased]`.
- Update `docs/` if user-facing behavior changed.
- Update the validation pack in the **private** facility repo if your change affects audit / RBAC / guardrails / executor / recipes (we'll flag on review).

## Adding a new equipment-saturation predicate, anti-pattern, or coupling rule

These have stable IDs (`SAT-*`, `AP-*`, `EC-*`). When adding:

1. Allocate the next free ID in `docs/concepts/cultivation_knowledge.md`.
2. Add the predicate / rule to the relevant `core/` module.
3. Add a fixture-based test in `backend/tests/`.
4. Add a docs entry under `docs/operations/anti_patterns.md` or `docs/concepts/equipment_coupling.md`.
5. Note in CHANGELOG.

## License

MIT. By contributing you agree your contribution is MIT-licensed.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Be a decent person.
