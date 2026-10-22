#!/usr/bin/env bash
# Connect this Docker gateway to native loopback Ollama, never to public ingress.
set -euo pipefail
IFS=$'\n\t'
[[ "$(id -u)" == 0 ]] || { printf 'Run as root.\n' >&2; exit 2; }
policy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -x /usr/lib/systemd/systemd-socket-proxyd ]]
if ! docker network inspect plugboard-ollama >/dev/null 2>&1; then
  python3 -c 'import ipaddress,json,subprocess;wanted=ipaddress.ip_network("172.30.114.0/29");routes=json.loads(subprocess.check_output(["ip","-j","-4","route"]));assert not any(wanted.overlaps(ipaddress.ip_network(r["dst"],strict=False)) for r in routes if r.get("dst") not in (None,"default","0.0.0.0/0")),"Private Ollama subnet conflicts with an existing route"'
  docker network create --internal --subnet 172.30.114.0/29 --gateway 172.30.114.1 \
    --opt com.docker.network.bridge.name=br-pb-ollama plugboard-ollama
fi
docker network inspect plugboard-ollama | python3 -c 'import sys,json;d=json.load(sys.stdin)[0];assert d["Internal"] is True;assert d["Options"]["com.docker.network.bridge.name"]=="br-pb-ollama";assert d["IPAM"]["Config"][0]["Subnet"]=="172.30.114.0/29";assert d["IPAM"]["Config"][0]["Gateway"]=="172.30.114.1"'
ufw allow in on br-pb-ollama proto tcp from 172.30.114.0/29 to 172.30.114.1 port 11434 comment plugboard-private-ollama
install -o root -g root -m 0644 "$policy_dir/plugboard-ollama.socket" /etc/systemd/system/plugboard-ollama.socket
install -o root -g root -m 0644 "$policy_dir/plugboard-ollama.service" /etc/systemd/system/plugboard-ollama.service
systemd-analyze verify /etc/systemd/system/plugboard-ollama.socket /etc/systemd/system/plugboard-ollama.service
systemctl daemon-reload
systemctl enable --now plugboard-ollama.socket
printf 'Ollama proxy enabled on the private Docker-only bridge; native loopback is unchanged.\n'
