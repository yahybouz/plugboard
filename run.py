"""Entry point: python run.py"""

import os
import uvicorn

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    # The app decides whether this is a local install from the real bind.
    os.environ["PLUGBOARD_BIND_HOST"] = host
    uvicorn.run(
        "plugboard.app:app",
        host=host,
        port=int(os.environ.get("PORT", 8765)),
        reload=os.environ.get("RELOAD", "false").lower() == "true",
        log_level="info",
    )
