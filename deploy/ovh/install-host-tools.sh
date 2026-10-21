#!/usr/bin/env bash
# Install native tools for the VPS ubuntu user, not inside the gateway.
# Usage: install-host-tools.sh codex|claude|agy|ollama
# Run as ubuntu; only global symlinks and Ollama service require sudo.
set -euo pipefail
IFS=$'\n\t'
umask 077
[[ "$(id -un)" == ubuntu ]] || { printf 'Run as ubuntu, not root.\n' >&2; exit 2; }
mkdir -p "$HOME/.cache" "$HOME/.local/bin"
work=$(mktemp -d "$HOME/.cache/plugboard-host-tools.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
link_cli() {
  local name="$1"
  local destination="/usr/local/bin/$name"
  local source="$HOME/.local/bin/$name"
  if [[ -e "$destination" || -L "$destination" ]]; then
    [[ "$(readlink "$destination")" == "$source" ]] || {
      printf 'Refusing to replace unrelated %s\n' "$destination" >&2; return 1;
    }
  else
    sudo ln -s "$source" "$destination"
  fi
}
fetch() { curl -fLsS --connect-timeout 15 --max-time 420 --retry 2 "$1" -o "$2"; }
case "${1:-}" in
  codex)
    fetch 'https://github.com/openai/codex/releases/download/rust-v0.163.0/codex-x86_64-unknown-linux-musl.tar.gz' "$work/codex.tar.gz"
    printf '%s  %s\n' 'beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4' "$work/codex.tar.gz" | sha256sum -c -
    tar --no-same-owner -xzf "$work/codex.tar.gz" -C "$work" codex-x86_64-unknown-linux-musl
    install -m 0755 "$work/codex-x86_64-unknown-linux-musl" "$HOME/.local/bin/codex"
    link_cli codex
    codex --version
    codex --help > /dev/null
    ;;
  claude)
    # This reviewed vendor installer verifies the downloaded native binary.
    bash "$HOME/claude-native-install.sh" 2.1.306
    link_cli claude
    claude --version
    claude --help > /dev/null
    ;;
  agy)
    fetch 'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.4-4783783166214144/linux-x64/cli_linux_x64.tar.gz' "$work/agy.tar.gz"
    printf '%s  %s\n' '7ce149d350344c1046c7b59b6659bcc5d659f351bf3a07c6c085887d93427ceb89d927752e4ccd0844b53ffb91d33d6a49f72835ceb276c7cffdd13cfb8023b8' "$work/agy.tar.gz" | sha512sum -c -
    tar --no-same-owner -xzf "$work/agy.tar.gz" -C "$work" antigravity
    install -m 0755 "$work/antigravity" "$HOME/.local/bin/agy"
    link_cli agy
    agy --version
    agy --help > /dev/null
    ;;
  ollama)
    [[ ! -e /usr/local/bin/ollama ]] || { printf 'Ollama already present; inspect before replacing.\n' >&2; exit 2; }
    fetch 'https://github.com/ollama/ollama/releases/download/v0.40.8/ollama-linux-amd64.tar.zst' "$work/ollama.tar.zst"
    printf '%s  %s\n' '0bb17cc19e9c1339ddbe0cba55148bf068f8f19dec8ec5fd531461aaa207282a' "$work/ollama.tar.zst" | sha256sum -c -
    tar --zstd -tf "$work/ollama.tar.zst" | python3 -c 'import pathlib,sys;names=[s.strip() for s in sys.stdin if s.strip()];assert names
for name in names:
 p=pathlib.PurePosixPath(name);s=str(p);assert not p.is_absolute() and ".." not in p.parts;assert s in {".","bin","lib","bin/ollama","lib/ollama"} or s.startswith("lib/ollama/"),s
print("Official Ollama archive paths verified:",len(names))'
    sudo tar --no-same-owner --zstd -xf "$work/ollama.tar.zst" -C /usr/local
    if ! id ollama > /dev/null 2>&1; then
      sudo useradd --system --user-group --home-dir /var/lib/ollama --shell /usr/sbin/nologin ollama
    fi
    sudo install -m 0644 "$HOME/ollama-private.service" /etc/systemd/system/ollama.service
    sudo systemd-analyze verify /etc/systemd/system/ollama.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now ollama.service
    python3 -c 'import urllib.request,time,json
for attempt in range(30):
 try:
  d=json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/version",timeout=2));print("OLLAMA_API",d);break
 except OSError:time.sleep(1)
else:raise RuntimeError("Ollama service did not become ready")
d=json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/tags",timeout=3));assert not d["models"];print("OLLAMA_MODELS_EMPTY_VERIFIED")'
    ollama --version
    ollama list
    ;;
  *) printf 'Usage: %s codex|claude|agy|ollama\n' "$0" >&2; exit 2 ;;
esac
