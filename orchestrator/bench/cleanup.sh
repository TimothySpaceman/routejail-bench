#!/usr/bin/env bash
# Removes everything a benchmark run can leave behind: processes that keep ports busy, containers, networks, network
# namespaces, interfaces, firewall rules, socket directories. Run it after a crash, or whenever a port is "busy".
#
#   sudo ./cleanup.sh [example.toml]
#
# The config is only read to find your router and sidecar programs (so their processes can be stopped).
# It refuses to run while an orchestrator is running. It never touches ip_forward or anything it did not create.
set -u
CONFIG="${1:-example.toml}"

[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0" >&2; exit 1; }
flock -n /tmp/bench-orchestrator.lock true 2>/dev/null || { echo "an orchestrator is running right now; stop it first (Ctrl-C)" >&2; exit 1; }

cfg() { [[ -f $CONFIG ]] && awk -F'"' -v k="$1" '$0 ~ "^"k"[[:space:]]*=" {print $2; exit}' "$CONFIG"; }
ROUTER="$(cfg router)"; SIDECAR="$(cfg sidecar)"

say() { printf '%-34s %s\n' "$1" "$2"; }

# 1. processes (they keep ports busy)
pkill -f '(^|/)netbench (server|relay) ' 2>/dev/null            # target servers, peer servers, relays
pkill -f 'envoy-(net|uds)\.yaml' 2>/dev/null                    # Envoy of setups 2 and 3
[[ -n $ROUTER ]]  && pkill -f -- "$ROUTER"  2>/dev/null         # router of setup 4
[[ -n $SIDECAR ]] && pkill -f -- "$SIDECAR" 2>/dev/null         # sidecars of setup 4 (namespace runtime)
sleep 0.5

# 2. Docker
if command -v docker >/dev/null 2>&1; then
  docker ps -aq --filter name=bench-sb --filter name=bench-check | xargs -r docker rm -f >/dev/null 2>&1
  docker network ls -q --filter name=bench-sb | xargs -r docker network rm >/dev/null 2>&1
fi

# 3. network namespaces and their resolv/hosts files
for ns in $(ip netns list 2>/dev/null | awk '{print $1}' | grep -E '^bench-'); do ip netns del "$ns" 2>/dev/null; done
rm -rf /etc/netns/bench-* 2>/dev/null

# 4. interfaces: sandbox veths and bridges, the target veth
for l in $(ip -br link 2>/dev/null | awk '{print $1}' | sed 's/@.*//' | grep -E '^(bsb|bsc)[0-9]+$|^bt-[ht]$'); do ip link del "$l" 2>/dev/null; done

# 5. firewall rules created by the orchestrator (nft table, and iptables rules tagged "bench")
nft delete table inet bench 2>/dev/null
for spec in "raw PREROUTING" "filter DOCKER-USER" "filter FORWARD"; do
  set -- $spec
  iptables -w 5 -t "$1" -S "$2" 2>/dev/null | grep -- '--comment bench' | sed 's/^-A/-D/' | while read -r rule; do iptables -w 5 -t "$1" $rule 2>/dev/null; done
done

# 6. files
rm -rf /run/bench /tmp/bench-check /tmp/bench-check2 2>/dev/null

# ---- what is left
echo "== after cleanup =="
left="$(pgrep -fc '(^|/)netbench (server|relay) |envoy-(net|uds)\.yaml' 2>/dev/null; true)"
say "bench processes:" "${left:-0} (should be 0)"
say "namespaces / bench links:" "$(ip netns list 2>/dev/null | grep -c '^bench-') / $(ip -br link 2>/dev/null | grep -cE '^(bsb|bsc)[0-9]+|^bt-[ht]')"
command -v docker >/dev/null 2>&1 && say "bench containers / networks:" "$(docker ps -aq --filter name=bench-sb 2>/dev/null | wc -l) / $(docker network ls -q --filter name=bench-sb 2>/dev/null | wc -l)"
busy="$(ss -Hltnup 2>/dev/null | grep -E ':(5010|9000|9100|9901|9998|9999)\b')"
if [[ -n $busy ]]; then echo "ports of the benchmark still in use (by something else?):"; echo "$busy"; else say "ports 5010 9000 9100 9901 9998 9999:" "free"; fi
