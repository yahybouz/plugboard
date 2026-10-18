"""
Direct LLM provider presets — config ready to use for Anthropic, OpenAI,
Gemini, GitHub Models, and GitHub Copilot.

Each preset describes the OpenAI-compatible endpoint + auth scheme.
When the user provides their credential, a Provider record is created
via config.create_provider() and is immediately routable via
plugboard:8765 without going through OpenClaw.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PRESETS: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "key": "anthropic",
        "name": "Anthropic Direct",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-5-5",
        "auth_header_name": "x-api-key",
        "auth_header_prefix": "",
        "provider_type": "cloud",
        "status": "available",
        "signup_url": "https://console.anthropic.com/settings/keys",
        "known_models": [
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",
            "claude-3-7-sonnet",
            "claude-3-7-sonnet-latest",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ],
        "extra_headers": {"anthropic-version": "2023-06-01"},
        "setup_steps": [
            {
                "title": "Create an Anthropic account",
                "link": "https://console.anthropic.com/",
                "description": "Sign up on the console if you do not have an account yet.",
            },
            {
                "title": "Open the API keys page",
                "link": "https://console.anthropic.com/settings/keys",
                "description": "In the console, go to Settings → API Keys.",
            },
            {
                "title": "Create a key",
                "description": "Click 'Create Key', name it (for example plugboard) and copy the value (sk-ant-...). It is shown only once.",
            },
            {
                "title": "Paste the key below",
                "description": "Paste the key in the 'API key' field and click Create.",
            },
        ],
    },
    "openai": {
        "key": "openai",
        "name": "OpenAI Direct",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "provider_type": "cloud",
        "status": "available",
        "signup_url": "https://platform.openai.com/api-keys",
        # Catalogue enrichi 2026-05-04 (BENCH-005 Option B) : famille GPT-5,
        # GPT-4.1, o3-pro ajoutees pour debloquer le bench cloud sans passer
        # par github-models-via-gh-cli (qui rate-limit a 46x HTTP 429 / 21min).
        "known_models": [
            # GPT-5.5 family (2026)
            "gpt-5.5",
            "gpt-5.5-chat",
            "gpt-5.5-mini",
            "gpt-5.5-nano",
            "gpt-5.5-pro",
            # GPT-5 family (2025+)
            "gpt-5",
            "gpt-5-chat",
            "gpt-5-mini",
            "gpt-5-nano",
            # GPT-4.1 family
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            # GPT-4o family
            "gpt-4o",
            "gpt-4o-mini",
            # o-series reasoning (BENCH-006 max_completion_tokens auto-gere)
            "o1",
            "o1-mini",
            "o1-preview",
            "o3",
            "o3-mini",
            "o3-pro",
            "o4-mini",
        ],
        "setup_steps": [
            {
                "title": "Add credit",
                "link": "https://platform.openai.com/settings/organization/billing/overview",
                "description": "The API needs a prepaid balance (about $5). A ChatGPT Plus subscription does not include API access: use the 'ChatGPT (via Codex CLI)' preset for that.",
            },
            {
                "title": "Create an API key",
                "link": "https://platform.openai.com/api-keys",
                "description": "Click 'Create new secret key', name it, choose 'All' permissions and copy it (sk-proj-...).",
            },
            {
                "title": "Paste the key below",
                "description": "Paste the key and click Create.",
            },
        ],
    },
    "gemini": {
        "key": "gemini",
        "name": "Google Gemini Direct",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "provider_type": "cloud",
        "status": "available",
        "signup_url": "https://aistudio.google.com/app/apikey",
        "known_models": [
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-preview",
            "gemini-3.1-flash-lite-preview",
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
        "setup_steps": [
            {
                "title": "Open Google AI Studio",
                "link": "https://aistudio.google.com/app/apikey",
                "description": "Sign in with your Google account.",
            },
            {
                "title": "Create an API key",
                "description": "Click 'Create API key' and pick a Google Cloud project (or create one). The key starts with AIza...",
            },
            {
                "title": "Paste the key below",
                "description": "Paste the key and click Create. (Without a key: use 'Gemini (via Gemini CLI)' to sign in with OAuth.)",
            },
        ],
    },
    "github-models": {
        "key": "github-models",
        "name": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "default_model": "openai/gpt-4o",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "provider_type": "cloud",
        "status": "available",
        "signup_url": "https://github.com/settings/tokens",
        "token_hint": "Use a GitHub Personal Access Token (classic) or fine-grained PAT with 'models:read' scope",
        "known_models": [
            # OpenAI family (GPT-4.1, GPT-5, o-series)
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
            "openai/gpt-4.1-nano",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "openai/gpt-5",
            "openai/gpt-5-chat",
            "openai/gpt-5-mini",
            "openai/gpt-5-nano",
            "openai/gpt-5.5",
            "openai/gpt-5.5-chat",
            "openai/gpt-5.5-mini",
            "openai/gpt-5.5-nano",
            "openai/gpt-5.5-pro",
            "openai/o1",
            "openai/o1-mini",
            "openai/o1-preview",
            "openai/o3",
            "openai/o3-mini",
            "openai/o4-mini",
            # Meta Llama
            "meta/llama-3.2-11b-vision-instruct",
            "meta/llama-3.2-90b-vision-instruct",
            "meta/llama-3.3-70b-instruct",
            "meta/llama-4-maverick-17b-128e-instruct-fp8",
            "meta/llama-4-scout-17b-16e-instruct",
            "meta/meta-llama-3.1-405b-instruct",
            "meta/meta-llama-3.1-8b-instruct",
            # DeepSeek
            "deepseek/deepseek-r1",
            "deepseek/deepseek-r1-0528",
            "deepseek/deepseek-v3-0324",
            # Mistral
            "mistral-ai/codestral-2501",
            "mistral-ai/ministral-3b",
            "mistral-ai/mistral-medium-2505",
            "mistral-ai/mistral-small-2503",
            # xAI Grok
            "xai/grok-3",
            "xai/grok-3-mini",
            # Microsoft Phi
            "microsoft/mai-ds-r1",
            "microsoft/phi-4",
            "microsoft/phi-4-mini-instruct",
            "microsoft/phi-4-mini-reasoning",
            "microsoft/phi-4-multimodal-instruct",
            "microsoft/phi-4-reasoning",
            # Cohere
            "cohere/cohere-command-a",
            "cohere/cohere-command-r-08-2024",
            "cohere/cohere-command-r-plus-08-2024",
            # AI21
            "ai21-labs/ai21-jamba-1.5-large",
        ],
        "setup_steps": [
            {
                "title": "Create a personal access token",
                "link": "https://github.com/settings/tokens/new",
                "description": "Generate a classic token. GitHub Models needs no specific scope.",
            },
            {
                "title": "Copy the token",
                "description": "Copy the value (ghp_...) now: it is shown only once.",
            },
            {
                "title": "Paste the token below",
                "description": "Paste the token and click Create. (Without a token: use 'GitHub Models (via gh CLI)'.)",
            },
        ],
    },
    "copilot": {
        "key": "copilot",
        "name": "GitHub Copilot (API key — legacy)",
        "provider_type": "cloud",
        "status": "roadmap",
        "reason": "Use 'github-copilot-oauth' instead (reads token from gh CLI).",
        "signup_url": "https://github.com/features/copilot",
    },
    # ── OAuth presets — delegate auth to installed CLIs ──────────────────
    "chatgpt-oauth": {
        "key": "chatgpt-oauth",
        "name": "ChatGPT (via Codex CLI)",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "default_model": "gpt-5.5",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "auth_source": "cli:codex",
        "provider_type": "cloud",
        "status": "available",
        "requires_cli": "codex",
        "signup_url": "https://github.com/openai/codex",
        "token_hint": "Install 'codex' CLI and run 'codex login' to authenticate with your ChatGPT account.",
        "known_models": [
            "gpt-5.5",
            "gpt-5.5-mini",
            "gpt-5.5-nano",
            "gpt-5.4",
            "gpt-5.4-mini",
            "o3",
            "o3-mini",
        ],
        "setup_steps": [
            {
                "title": "ChatGPT Plus or Pro subscription required",
                "link": "https://chatgpt.com/#pricing",
                "description": "This uses your ChatGPT subscription, not the paid API. Plus or Pro is required.",
            },
            {
                "title": "Install the Codex CLI",
                "command": "brew install codex",
                "link": "https://github.com/openai/codex",
                "description": "Or with npm: npm install -g @openai/codex-cli",
            },
            {
                "title": "Sign in with your ChatGPT account",
                "command": "codex login",
                "description": "A browser window opens: sign in to ChatGPT and authorise.",
            },
            {
                "title": "Check the connection",
                "command": "codex auth status",
                "description": "You should see 'Logged in as <email>'. The token is stored in ~/.codex/auth.json.",
            },
            {
                "title": "Click 'Check CLI' below",
                "description": "Plugboard checks that the Codex CLI is signed in and reads the account email.",
            },
        ],
    },
    "gemini-oauth": {
        "key": "gemini-oauth",
        "name": "Gemini (via Gemini CLI)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "auth_source": "cli:gemini",
        "provider_type": "cloud",
        "status": "available",
        "requires_cli": "gemini",
        "signup_url": "https://github.com/google-gemini/gemini-cli",
        "token_hint": "Install 'gemini' CLI and run 'gemini' once to authenticate with your Google account.",
        "known_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "setup_steps": [
            {
                "title": "Install the Gemini CLI",
                "command": "npm install -g @google/gemini-cli",
                "link": "https://github.com/google-gemini/gemini-cli",
                "description": "Needs Node.js 18 or later. On macOS it is also available with Homebrew.",
            },
            {
                "title": "Run gemini to sign in",
                "command": "gemini",
                "description": "On the first run a browser window opens for Google sign-in. Allow access to Gemini.",
            },
            {
                "title": "Check the credentials file",
                "command": "cat ~/.gemini/oauth_creds.json",
                "description": "It should contain access_token, refresh_token and expiry_date. The CLI creates this file.",
            },
            {
                "title": "Click 'Check CLI' below",
                "description": "Plugboard reads the token and refreshes it when it expires (after one hour by default).",
            },
        ],
    },
    "github-copilot-oauth": {
        "key": "github-copilot-oauth",
        "name": "GitHub Copilot (via gh CLI)",
        "base_url": "https://api.githubcopilot.com",
        "default_model": "gpt-4o",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "auth_source": "cli:gh+copilot",
        "provider_type": "cloud",
        "status": "available",
        "requires_cli": "gh",
        "signup_url": "https://cli.github.com/",
        "token_hint": "Install 'gh' CLI, run 'gh auth login', and ensure a Copilot subscription.",
        "known_models": ["gpt-4o", "claude-3.5-sonnet", "gpt-4o-mini"],
        "extra_headers": {
            "Editor-Version": "vscode/1.99.3",
            "Copilot-Integration-Id": "vscode-chat",
        },
        "setup_steps": [
            {
                "title": "Copilot subscription required",
                "link": "https://github.com/features/copilot",
                "description": "Needs a Copilot Individual, Business or Enterprise subscription.",
            },
            {
                "title": "Install the gh CLI",
                "command": "brew install gh",
                "link": "https://cli.github.com/",
                "description": "With Homebrew on macOS, or the binary from the official page.",
            },
            {
                "title": "Sign in to GitHub",
                "command": "gh auth login",
                "description": "Follow the prompts: GitHub.com → HTTPS → sign in with a browser.",
            },
            {
                "title": "Check",
                "command": "gh auth status",
                "description": "The 'copilot' scope should be listed.",
            },
            {
                "title": "Click 'Check CLI' below",
                "description": "Plugboard exchanges the gh token for a short-lived Copilot token (30 min) and refreshes it automatically.",
            },
        ],
    },
    "github-models-oauth": {
        "key": "github-models-oauth",
        "name": "GitHub Models (via gh CLI)",
        "base_url": "https://models.github.ai/inference",
        "default_model": "openai/gpt-4o",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "auth_source": "cli:gh",
        "provider_type": "cloud",
        "status": "available",
        "requires_cli": "gh",
        "signup_url": "https://cli.github.com/",
        "token_hint": "Install 'gh' CLI and run 'gh auth login'. Uses your GitHub PAT.",
        "known_models": [
            # OpenAI via GitHub Models
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
            "openai/gpt-4.1-nano",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "openai/gpt-5",
            "openai/gpt-5-chat",
            "openai/gpt-5-mini",
            "openai/gpt-5-nano",
            "openai/gpt-5.5",
            "openai/gpt-5.5-chat",
            "openai/gpt-5.5-mini",
            "openai/gpt-5.5-nano",
            "openai/gpt-5.5-pro",
            "openai/o1",
            "openai/o1-mini",
            "openai/o3",
            "openai/o3-mini",
            "openai/o4-mini",
            # Meta Llama
            "meta/llama-3.3-70b-instruct",
            "meta/llama-4-maverick-17b-128e-instruct-fp8",
            "meta/llama-4-scout-17b-16e-instruct",
            "meta/meta-llama-3.1-405b-instruct",
            "meta/meta-llama-3.1-8b-instruct",
            "meta/llama-3.2-11b-vision-instruct",
            "meta/llama-3.2-90b-vision-instruct",
            # DeepSeek
            "deepseek/deepseek-r1",
            "deepseek/deepseek-r1-0528",
            "deepseek/deepseek-v3-0324",
            # Mistral
            "mistral-ai/codestral-2501",
            "mistral-ai/mistral-medium-2505",
            "mistral-ai/mistral-small-2503",
            "mistral-ai/ministral-3b",
            # xAI Grok
            "xai/grok-3",
            "xai/grok-3-mini",
            # Microsoft Phi
            "microsoft/phi-4",
            "microsoft/phi-4-mini-instruct",
            "microsoft/phi-4-reasoning",
            "microsoft/phi-4-multimodal-instruct",
            "microsoft/mai-ds-r1",
            # Cohere, AI21
            "cohere/cohere-command-a",
            "cohere/cohere-command-r-plus-08-2024",
            "ai21-labs/ai21-jamba-1.5-large",
        ],
        "setup_steps": [
            {
                "title": "Install the gh CLI",
                "command": "brew install gh",
                "link": "https://cli.github.com/",
                "description": "The official GitHub CLI.",
            },
            {
                "title": "Sign in to GitHub",
                "command": "gh auth login",
                "description": "HTTPS and browser sign-in. No specific scope is needed.",
            },
            {
                "title": "Check",
                "command": "gh auth status",
                "description": "It should show 'Logged in to github.com'.",
            },
            {
                "title": "Click 'Check CLI' below",
                "description": "Plugboard uses your gh token to call GitHub Models (free, with a quota).",
            },
        ],
    },
    "claude-pro-oauth": {
        "key": "claude-pro-oauth",
        "name": "Claude Pro (roadmap)",
        "provider_type": "cloud",
        "status": "roadmap",
        "reason": "Reading Claude Code CLI token from macOS Keychain is on roadmap. Use Anthropic API key for now.",
        "signup_url": "https://claude.ai",
    },
    # ── SUBPROCESS presets (fork of OpenClaw) — spawn the installed CLI binary ─
    "agy-cli-subprocess": {
        "key": "agy-cli-subprocess",
        "name": "Antigravity (via agy CLI)",
        "base_url": "subprocess://agy",  # pseudo URL — router short-circuits
        "default_model": "gemini-3.8-flash-medium",
        "auth_source": "subprocess:agy",
        "connect_via": "subprocess_check",
        "provider_type": "cloud",
        "status": "available",
        # Fallback only: the live list comes from `agy models` (account-scoped).
        "known_models": [
            "gemini-4-argon-medium",
            "gemini-4-argon-high",
            "gemini-4-argon-low",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-low",
            "claude-opus-5-5-medium",
            "claude-sonnet-5-5-medium",
            "gpt-oss-120b-medium",
        ],
        "setup_steps": [
            {
                "title": "Install the Antigravity CLI",
                "link": "https://antigravity.google/",
                "description": "Install agy, then run `agy install` to put it on your PATH.",
            },
            {
                "title": "Sign in with Google",
                "command": "agy",
                "description": "The first run opens the Google sign-in.",
            },
            {
                "title": "Check the models",
                "command": "agy models",
                "description": "Plugboard reads this list to know which models your account can use.",
            },
            {
                "title": "Check below",
                "description": "Click 'Verify CLI' to create the provider.",
            },
        ],
    },
    "gemini-cli-subprocess": {
        "key": "gemini-cli-subprocess",
        "name": "Google Gemini (via gemini CLI)",
        # Replaced by the Antigravity preset: the gemini CLI no longer signs in
        # personal accounts. Kept so existing providers still resolve.
        "hidden": True,
        "base_url": "subprocess://gemini",  # pseudo URL — router short-circuits
        "default_model": "gemini-2.5-flash",
        "auth_source": "subprocess:gemini",
        "connect_via": "subprocess_check",
        "provider_type": "cloud",
        "status": "available",
        "known_models": [
            # Gemini 3 family (preview, 2026)
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-preview",
            "gemini-3.1-flash-lite-preview",
            # Gemini 2.5 family (GA)
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            # Gemini 2.0 family
            "gemini-2.0-flash",
        ],
        "setup_steps": [
            {
                "title": "Install the gemini CLI",
                "command": "brew install gemini-cli",
                "description": "Or `npm install -g @google/gemini-cli`.",
            },
            {
                "title": "Sign in with Google",
                "command": "gemini auth login",
                "description": "Browser sign-in. The session is stored in ~/.gemini/oauth_creds.json.",
            },
            {
                "title": "Check below",
                "description": "Click 'Verify CLI' to create the provider.",
            },
        ],
    },
    "codex-cli-subprocess": {
        "key": "codex-cli-subprocess",
        "name": "ChatGPT (via codex CLI)",
        "base_url": "subprocess://codex",
        "default_model": "gpt-5.5",
        "auth_source": "subprocess:codex",
        "connect_via": "subprocess_check",
        "provider_type": "cloud",
        "status": "available",
        "known_models": [
            # GPT-5.5 family (pricing-discovered/account-verified daily)
            "gpt-5.5",
            "gpt-5.5-chat",
            "gpt-5.5-mini",
            "gpt-5.5-nano",
            "gpt-5.5-pro",
            # GPT-5.4 family (latest)
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5.4-latest",
            # GPT-5 family
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5.2",
            "gpt-5.2-pro",
            # GPT-4 family (legacy but supported by Codex CLI)
            "gpt-4o",
            "gpt-4-turbo",
            "gpt-4",
            # Reasoning models
            "o4",
            "o3",
            "o1",
        ],
        "setup_steps": [
            {
                "title": "Install the Codex CLI",
                "command": "npm install -g @openai/codex",
                "description": "OpenAI's official Codex CLI.",
            },
            {
                "title": "Sign in with ChatGPT",
                "command": "codex",
                "description": "Choose 'Sign in with ChatGPT' on the first run.",
            },
            {"title": "Check below", "description": "Click 'Verify CLI'."},
        ],
    },
    "claude-cli-subprocess": {
        "key": "claude-cli-subprocess",
        "name": "Claude (via claude CLI)",
        "base_url": "subprocess://claude",
        "default_model": "claude-sonnet-5-5",
        "auth_source": "subprocess:claude",
        "connect_via": "subprocess_check",
        "provider_type": "cloud",
        "status": "available",
        "known_models": [
            # Claude 5 family (checked against the CLI, 2026-10)
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-1",
            # Claude 4 family (2025-2026)
            "claude-opus-4-7",
            "claude-opus-4-7-20260416",
            "claude-opus-4-6",
            "claude-opus-4-6-20260205",
            "claude-opus-4-5",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
            # Keep only currently validated models in CLI catalog.
        ],
        "setup_steps": [
            {
                "title": "Install the Claude Code CLI",
                "command": "npm install -g @anthropic-ai/claude-code",
                "description": "Anthropic's official Claude Code CLI.",
            },
            {
                "title": "Sign in to Anthropic",
                "command": "claude",
                "description": "Follow the prompts (Max account or API key).",
            },
            {"title": "Check below", "description": "Click 'Verify CLI'."},
        ],
    },
    "github-broker": {
        "key": "github-broker",
        "name": "GitHub Models (OAuth via dashboard)",
        "base_url": "https://models.github.ai/inference",
        "default_model": "gpt-4o",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "auth_source": "broker:github",
        "connect_via": "device_flow",
        "oauth_source": "github",
        "provider_type": "cloud",
        "status": "available",
        "known_models": ["gpt-4o", "gpt-4o-mini", "meta-llama-3.1-405b-instruct"],
        "setup_steps": [
            {
                "title": "Click 'Connect with GitHub' below",
                "description": "A one-time code is shown.",
            },
            {
                "title": "Open github.com/login/device",
                "description": "Paste the code and authorise access.",
            },
            {
                "title": "Back here automatically",
                "description": "The dashboard detects the authorisation and creates the provider.",
            },
        ],
    },
    "copilot-broker": {
        "key": "copilot-broker",
        "name": "GitHub Copilot (OAuth via dashboard)",
        "base_url": "https://api.githubcopilot.com",
        "default_model": "gpt-4o",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
        "auth_source": "broker:github+copilot",
        "connect_via": "device_flow",
        "oauth_source": "github",
        "provider_type": "cloud",
        "status": "available",
        "known_models": ["gpt-4o", "claude-3.5-sonnet", "gpt-4o-mini"],
        "extra_headers": {
            "Editor-Version": "vscode/1.99.3",
            "Copilot-Integration-Id": "vscode-chat",
        },
        "setup_steps": [
            {
                "title": "Copilot subscription required",
                "link": "https://github.com/features/copilot",
                "description": "Needs Copilot Individual, Business or Enterprise on your GitHub account.",
            },
            {
                "title": "Click 'Connect with GitHub' below",
                "description": "If GitHub is already connected through another dashboard preset, that connection is reused.",
            },
            {
                "title": "Provider created",
                "description": "The GitHub token is exchanged for a short-lived Copilot token (about 30 min), refreshed automatically.",
            },
        ],
    },
    # NOTE: chatgpt-broker (HTTP OAuth to chatgpt.com) was removed — it used a
    # reverse-engineered Codex backend endpoint that doesn't accept raw Bearer
    # tokens. Replaced by `codex-cli-subprocess` which spawns the official CLI.
}


def _public_fields(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": p["key"],
        "name": p["name"],
        "status": p.get("status", "available"),
        "base_url": p.get("base_url", ""),
        "default_model": p.get("default_model", ""),
        "provider_type": p.get("provider_type", "cloud"),
        "signup_url": p.get("signup_url", ""),
        "token_hint": p.get("token_hint", ""),
        "reason": p.get("reason", ""),
        "auth_source": p.get("auth_source", ""),
        "requires_cli": p.get("requires_cli", ""),
        "connect_via": p.get("connect_via", ""),
        "oauth_source": p.get("oauth_source", ""),
        "setup_steps": p.get("setup_steps", []),
        "known_models": p.get("known_models", []),
        "catalog": bool(p.get("catalog")),
        "verified": p.get("verified", True),
        "prices_known": p.get("prices_known", True),
    }


def _norm_url(url: str) -> str:
    return (url or "").rstrip("/").lower()


def list_presets() -> List[Dict[str, Any]]:
    """Built-in presets first, then catalog presets (no secrets).

    A catalog preset whose base_url matches a built-in one is hidden: the
    built-in preset is curated and verified.
    """
    from plugboard import models_dev

    builtin = [_public_fields(p) for p in PRESETS.values() if not p.get("hidden")]
    taken = {_norm_url(p["base_url"]) for p in builtin}
    catalog = [
        _public_fields(p)
        for p in models_dev.generated_presets()
        if _norm_url(p["base_url"]) not in taken
    ]
    return builtin + catalog


def get_preset(key: str) -> Optional[Dict[str, Any]]:
    """Get a preset config by key (built-in or catalog-<id>)."""
    if key in PRESETS:
        return PRESETS[key]
    from plugboard import models_dev

    return models_dev.get_generated_preset(key)


def is_oauth_preset(preset: Dict[str, Any]) -> bool:
    """True if this preset uses OAuth delegation (no api_key needed).

    Covers CLI delegation (cli:*), dashboard OAuth broker (broker:*),
    and subprocess CLI spawn (subprocess:*).
    """
    auth_source = preset.get("auth_source", "")
    return (
        auth_source.startswith("cli:")
        or auth_source.startswith("broker:")
        or auth_source.startswith("subprocess:")
    )


def is_broker_preset(preset: Dict[str, Any]) -> bool:
    """True if this preset uses the Plugboard OAuth broker (in-dashboard flow)."""
    return preset.get("auth_source", "").startswith("broker:")


def is_subprocess_preset(preset: Dict[str, Any]) -> bool:
    """True if this preset spawns a local CLI binary (OpenClaw-style)."""
    return preset.get("auth_source", "").startswith("subprocess:")


def create_from_preset(
    preset_key: str, api_key: str = "", is_active: bool = True
) -> Dict[str, Any]:
    """Create a Provider record from a preset.

    For API-key presets: api_key is required.
    For OAuth presets (auth_source=cli:*): api_key is ignored — auth is
    delegated to the CLI at request time.

    Returns the created provider record (with token masked).
    Raises ValueError if preset is unknown, on roadmap, or api_key missing.
    """
    from plugboard.config import create_provider, mask_token, update_provider

    preset = get_preset(preset_key)
    if preset is None:
        raise ValueError(f"Unknown preset: {preset_key}")
    if preset.get("status") == "roadmap":
        raise ValueError(
            f"Preset '{preset_key}' is on roadmap and not implementable yet: "
            f"{preset.get('reason', '')}"
        )

    if is_oauth_preset(preset):
        # OAuth preset: no bearer_token needed, auth delegated to CLI
        record = create_provider(
            name=preset["name"],
            base_url=preset["base_url"],
            bearer_token="",  # empty — CLI provides token at request time
            default_model=preset.get("default_model", ""),
            is_active=is_active,
            provider_type=preset.get("provider_type", "cloud"),
            auth_header_name=preset.get("auth_header_name", "Authorization"),
            auth_header_prefix=preset.get("auth_header_prefix", "Bearer"),
            auth_source=preset["auth_source"],
        )
        record = update_provider(record["id"], {"preset_key": preset_key}) or record
        return {**record, "bearer_token": "(via CLI)"}

    # API key preset: require api_key
    if not api_key or not api_key.strip():
        raise ValueError("api_key is required")

    record = create_provider(
        name=preset["name"],
        base_url=preset["base_url"],
        bearer_token=api_key.strip(),
        default_model=preset.get("default_model", ""),
        is_active=is_active,
        provider_type=preset.get("provider_type", "cloud"),
        auth_header_name=preset.get("auth_header_name", "Authorization"),
        auth_header_prefix=preset.get("auth_header_prefix", "Bearer"),
    )
    # The base_url above is frozen: a later catalog refresh never rewrites
    # an existing provider, so a changed catalog entry cannot redirect keys.
    extra: Dict[str, Any] = {"preset_key": preset_key}
    if preset.get("catalog"):
        extra["auto_discover"] = True
        extra["models"] = [{"id": m, "name": m} for m in preset.get("known_models", [])]
    record = update_provider(record["id"], extra) or {**record, **extra}
    # Return a copy with the token masked
    return {**record, "bearer_token": mask_token(api_key)}
