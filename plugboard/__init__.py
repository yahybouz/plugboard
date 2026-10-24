__version__ = "4.3.0"

# Before any submodule reads its settings: load .env (unless skipped), then
# accept pre-rename variable names. Loading first lets a new name written in
# .env win over a legacy name, and gives every store the same data directory.
import os as _os  # noqa: E402

if "1" not in (
    _os.environ.get("PLUGBOARD_SKIP_DOTENV"),
    _os.environ.get("LLM_" + "ENABLER_SKIP_DOTENV"),
):
    try:
        from dotenv import load_dotenv as _load_dotenv

        _load_dotenv()
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        pass

from plugboard import envcompat as _envcompat  # noqa: E402

_envcompat.apply()
