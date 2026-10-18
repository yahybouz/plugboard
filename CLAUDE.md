# Notes for coding agents

Plugboard is a self-hosted LLM gateway for every provider, behind one standard chat API (Python 3.12, FastAPI).
Human documentation lives in `README.md` and `docs/guide/`; this file only lists
what an agent needs to work safely in the repository.

## Run and test

- Setup: `make setup` (creates `.venv` with Python 3.12), then `make init` (creates `.env`
  with random keys). Start with `make dev` or `python run.py`.
- Tests: `make test`. `tests/conftest.py` points `PLUGBOARD_DATA` at a temporary directory;
  never run the suite against a live install's `data/`.
- Lint: `make lint` (ruff). Secrets: `make secret-scan` (gitleaks).

## Rules

- Runtime data (`data/`: providers, keys, pricing grids, registry) is never committed.
- Write JSON stores through `plugboard.storage` (`atomic_write_json`), never in place.
- Dashboard auto-login is for a local install only (`PLUGBOARD_LOCAL_AUTO_LOGIN`,
  see `docs/guide/troubleshooting.md`); never trust the `Host` header alone.
- Environment variables use the `PLUGBOARD_` prefix; the older `LLM_ENABLER_` names are
  still accepted (`plugboard/envcompat.py`).
- Conventional Commits; one change per merge request; GitLab is the canonical forge.
- No personal data, real keys or client names in code, docs, tests or commit messages.
