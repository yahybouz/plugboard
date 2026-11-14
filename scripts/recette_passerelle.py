#!/usr/bin/env python3
"""Recette client de la passerelle : le parcours qu'un client suit, en un passage.

Étapes, dans l'ordre (arrêt au premier échec, code de sortie 1) :
  1. health      GET /health                 -> 200, version présente
  2. ready       GET /health/ready           -> 200
  3. models      GET /v1/models (clé client) -> 200, le modèle cible est listé
  4. chat        POST /v1/chat/completions   -> 200, réponse non vide

Le MCP n'est vérifié que si PLUGBOARD_MCP_URL est donné (sinon l'étape est
signalée « non testée », sans échec).

Configuration (environnement uniquement, rien en argument) :
  PLUGBOARD_URL              cible, ex. http://127.0.0.1:18765 (défaut : http://127.0.0.1:8765)
  PLUGBOARD_API_KEY          clé client pb_ (obligatoire, jamais affichée)
  PLUGBOARD_RECETTE_MODEL    identifiant du modèle à appeler (obligatoire)
  PLUGBOARD_RECETTE_TIMEOUT  délai par requête en secondes (défaut : 60)
  PLUGBOARD_MCP_URL          optionnel : endpoint MCP HTTP streamable
  PLUGBOARD_MCP_TOKEN        optionnel : jeton bearer du MCP

Codes de sortie : 0 tout passe ; 1 un contrôle échoue ; 2 configuration absente.
Aucun secret n'est écrit sur la sortie. Le script ne modifie rien côté serveur,
mais l'étape chat consomme une requête réelle du modèle (max_tokens=16).
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"


class RecetteError(Exception):
    """Échec d'un contrôle. Le message ne contient jamais la clé ni le corps d'erreur."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Une redirection est un échec : la cible doit être l'URL finale."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Recette:
    def __init__(self, base: str, key: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def redact(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

    def request(self, method: str, path: str, payload=None, auth: bool = True):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RecetteError(
                    f"redirection HTTP {exc.code} : indiquer l'URL finale dans PLUGBOARD_URL"
                ) from None
            return exc.code, exc.read()
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as exc:
            # Only the class name: the message may echo the request, hence the key.
            raise RecetteError(f"injoignable : {type(exc).__name__}") from None

    @staticmethod
    def json_body(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


def _report(name: str, ok: bool, detail: str = "") -> None:
    state = "OK" if ok else "ÉCHEC"
    print(f"[{name}] {state}{(' — ' + detail) if detail else ''}", flush=True)


def _run_steps(env: dict) -> int:
    key = env.get("PLUGBOARD_API_KEY", "").strip()
    model = env.get("PLUGBOARD_RECETTE_MODEL", "").strip()
    base = env.get("PLUGBOARD_URL", "").strip() or DEFAULT_URL
    try:
        timeout = float(env.get("PLUGBOARD_RECETTE_TIMEOUT", "60"))
    except ValueError:
        print("PLUGBOARD_RECETTE_TIMEOUT doit être un nombre", file=sys.stderr)
        return 2
    if any(ord(ch) < 33 for ch in key):
        print(
            "PLUGBOARD_API_KEY contient un espace ou un caractère de contrôle : valeur non affichée",
            file=sys.stderr,
        )
        return 2
    if not key:
        print(
            "PLUGBOARD_API_KEY absent : renseigner la clé client dans l'environnement",
            file=sys.stderr,
        )
        return 2
    if not model:
        print(
            "PLUGBOARD_RECETTE_MODEL absent : nommer le modèle à appeler",
            file=sys.stderr,
        )
        return 2

    rec = Recette(base, key, timeout)
    print(f"Recette passerelle sur {base} (modèle {model})", flush=True)

    # 1. health
    try:
        status, raw = rec.request("GET", "/health", auth=False)
    except RecetteError as exc:
        _report("health", False, str(exc))
        return 1
    body = rec.json_body(raw)
    ok = status == 200 and isinstance(body, dict) and bool(body.get("version"))
    _report(
        "health",
        ok,
        f"HTTP {status}" + (f", version {body.get('version')}" if ok else ""),
    )
    if not ok:
        return 1

    # 2. ready
    status, _ = rec.request("GET", "/health/ready", auth=False)
    _report("ready", status == 200, f"HTTP {status}")
    if status != 200:
        return 1

    # 3. models
    status, raw = rec.request("GET", "/v1/models")
    body = rec.json_body(raw)
    ids = [m.get("id") for m in body.get("data", [])] if isinstance(body, dict) else []
    ok = status == 200 and model in ids
    _report(
        "models",
        ok,
        f"HTTP {status}, {len(ids)} modèle(s)" + ("" if ok else f", {model} absent"),
    )
    if not ok:
        return 1

    # 4. chat (premier appel réel : petite réponse, plafonnée)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Réponds par un mot : bonjour."}],
        "max_tokens": 16,
    }
    status, raw = rec.request("POST", "/v1/chat/completions", payload)
    body = rec.json_body(raw)
    content = ""
    if isinstance(body, dict):
        try:
            content = (body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            content = ""
    ok = status == 200 and bool(content)
    _report(
        "chat",
        ok,
        f"HTTP {status}"
        + (f", {len(content)} caractères" if ok else ", réponse vide ou invalide"),
    )
    if not ok:
        return 1

    # 5. MCP (optionnel)
    mcp_url = env.get("PLUGBOARD_MCP_URL", "").strip()
    if not mcp_url:
        print("[mcp] non testé : PLUGBOARD_MCP_URL absent", flush=True)
    else:
        mcp_token = env.get("PLUGBOARD_MCP_TOKEN", "").strip()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "recette-passerelle", "version": "1"},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if mcp_token:
            headers["Authorization"] = f"Bearer {mcp_token}"
        req = urllib.request.Request(
            mcp_url,
            data=json.dumps(init).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, ""
        except OSError as exc:
            _report("mcp", False, rec.redact(f"injoignable : {exc.__class__.__name__}"))
            return 1
        ok = status == 200 and "serverInfo" in raw
        _report("mcp", ok, f"HTTP {status}")
        if not ok:
            return 1

    print("TOUT PASSE", flush=True)
    return 0


def run(env: dict) -> int:
    try:
        return _run_steps(env)
    except RecetteError as exc:
        print(f"[échec] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(run(dict(os.environ)))
    except (
        Exception
    ) as exc:  # une erreur inattendue reste un échec, sans trace ni secret
        print(f"ÉCHEC inattendu : {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
