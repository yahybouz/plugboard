"""The auth core moved out of ``plugboard/app.py`` (PLU-97), one module per subject.

What sits behind the FastAPI dependencies: the audit principal and the limit and
lockout counters, the admin key and its sessions, the cross-site refusal, the
admin and personal-key authentication, the MFA step-up and the local auto-login.
The dependencies themselves, ``_resolve_client`` and ``_require_admin``, stay in
``app.py``: same object, same name.

Each module exposes ``register(app, core) -> SimpleNamespace``: it declares no
route, no middleware and no exception handler, and returns the closures
``app.py`` re-exports. ``core`` is the ``plugboard.app`` module object that
registers them, so every import of ``app.py`` binds the auth core to itself.

- Any name defined or re-exported by ``app.py`` is read as ``core.<name>`` when
  the code runs, never bound at import, even a function of the same module
  (R13). The constants of this package and ``KEY_PREFIXES`` are read directly
  and never changed at run time.
- ``app.py`` re-exports every moved name, always (R14).
- Between modules of this package, a module-level constant is imported directly
  (``from plugboard.auth_core.scopes import _ADMIN_SCOPE``); a function is read
  as ``core.<name>``, its re-export in ``app.py`` (R10). No relative import.
- ``app.py`` imports this package last among its top imports; a module here only
  imports, at module level, modules ``app.py`` already imports at module level,
  so no ``plugboard`` module is loaded earlier or in addition (R11).
- A module that logs uses ``logging.getLogger("plugboard.app")``: same logger,
  same ``source_service``.
- A module never imports ``plugboard.app``: ``core`` is its only access to it.

Design: ``docs/superpowers/specs/2026-11-18-plu-97-auth-core-design.md``.
"""
