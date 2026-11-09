"""Entry point: python run.py"""

import os
import uvicorn

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def login_url(host: str, port: int, token: str):
    """Dashboard sign-in link with the one-time token, for a loopback bind only."""
    if host not in _LOOPBACK or not token:
        return None
    netloc = f"[{host}]" if ":" in host else host
    return f"http://{netloc}:{port}/login?token={token}"


def _open_dashboard_when_ready(host: str, port: int) -> None:
    """`make dev`: once the server answers, open the dashboard signed in.

    The server writes a fresh single-use token to data/.login_token at
    startup; the link carries it, and the browser is then remembered.
    """
    import threading
    import time
    import urllib.request
    import webbrowser

    from plugboard.storage import data_dir

    if host not in _LOOPBACK:
        return
    netloc = f"[{host}]" if ":" in host else host

    def _wait_and_open() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://{netloc}:{port}/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            return
        token_file = data_dir() / ".login_token"
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return
        url = login_url(host, port, token)
        if url and not webbrowser.open(url):
            print(f"Open http://{netloc}:{port}/login?token=<content of {token_file}>")

    threading.Thread(target=_wait_and_open, daemon=True).start()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    # The app decides whether this is a local install from the real bind.
    os.environ["PLUGBOARD_BIND_HOST"] = host
    if os.environ.get("PLUGBOARD_OPEN_BROWSER") == "1":
        _open_dashboard_when_ready(host, int(os.environ.get("PORT", 8765)))
    uvicorn.run(
        "plugboard.app:app",
        host=host,
        port=int(os.environ.get("PORT", 8765)),
        reload=os.environ.get("RELOAD", "false").lower() == "true",
        log_level="info",
    )
