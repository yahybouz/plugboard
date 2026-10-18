# Contributing to Plugboard

Thank you for helping. This document explains where to contribute, how to set up a development environment, and what a merge request needs before it can be merged.

## Where to contribute

- The **canonical repository is on GitLab**. Open issues and merge requests there.
- The GitHub repository is a read-only mirror. Pull requests opened on GitHub are not reviewed; please move them to GitLab.
- Security problems: do not open a public issue. Follow [SECURITY.md](SECURITY.md).
- By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Requirements: Python 3.12 (`python3.12` on `PATH`, or `uv`, which downloads it), `make`, git.

```bash
git clone <repository-url> plugboard
cd plugboard
make setup              # .venv with Python 3.12 and the dependencies
make init               # .env with a random ADMIN_KEY and API_KEY
make dev                # server with hot reload on http://localhost:8765
make precommit-install  # git hooks (formatting, lint, secret scan, commit message)
```

Useful targets:

| Command | What it does |
|---|---|
| `make test` | Full test suite with coverage. End-to-end and live-provider tests are skipped unless explicitly enabled. |
| `make lint` / `make format` | Ruff lint and format |
| `make check` | Lint, tests and a Bandit security scan |
| `make secret-scan` | Look for secrets before you push |
| `make test-e2e` | Browser tests of the dashboard (Playwright, isolated server) |

Tests never need real provider credentials: providers are mocked. Do not add tests that call a paid API by default.

## Making a change

1. Open an issue first for anything larger than a small fix, so the approach can be agreed before you invest time.
2. Create a branch from `main`: `feat/short-description`, `fix/short-description`, `docs/...`, `chore/...`.
3. Keep the change focused: one merge request, one purpose.
4. Add or update tests. A bug fix comes with a test that fails without the fix.
5. Update the documentation in `docs/` and `README.md` when behaviour, endpoints or configuration change. New environment variables go in [the configuration reference](docs/guide/configuration.md) and `.env.example`.
6. Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.
7. Run `make check` and `make secret-scan` before pushing.

### Commit messages

Commits follow [Conventional Commits](https://www.conventionalcommits.org): `type(scope): summary`, for example `fix(router): keep the provider prefix on retries`. Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `chore`, `ci`, `build`. The commit-message hook checks the format.

### Code style

- Python 3.12, type hints on public functions, formatted and linted with Ruff.
- Keep HTTP handling in `app.py` thin; put logic in the module that owns it (`router.py`, `billing.py`, ...).
- No secrets, tokens or personal data in code, tests, fixtures, logs or documentation. Use obviously fake values in examples.
- Errors returned to clients use FastAPI's `{"detail": "..."}` format; secrets are masked in every response.

### Architecture decisions

A change that alters the architecture, the security model, the storage layout or behaviour existing deployments rely on needs an Architecture Decision Record in `docs/adr/`. See [docs/adr/README.md](docs/adr/README.md).

## Review and merge

- A maintainer reviews every merge request. The CI pipeline (lint, format check, tests with a coverage threshold, security scan) must pass.
- Reviews focus on correctness, security, tests and documentation.
- Maintainers squash or rebase on merge to keep the history readable.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE) of the project.
