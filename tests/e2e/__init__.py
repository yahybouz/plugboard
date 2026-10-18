"""Browser-level end-to-end tests of the admin dashboard (Playwright + Chromium).

Distinct from ``tests/live_model_tests`` (which drives the Playground to test the
MODELS): this suite tests the dashboard FEATURES (tabs, CRUD flows, rendering)
against a fully isolated server (temp ``PLUGBOARD_DATA``) so it never touches
the real ``data/`` working tree and never calls a real LLM provider.

Run with: ``make test-e2e`` (or ``pytest tests/e2e``).
"""
