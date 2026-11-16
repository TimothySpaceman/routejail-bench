# RouteJail Benchmark Setup

Measures what it costs to control a container sandbox's network, on gVisor (`runsc`) and plain Docker (`runc`),
with 5 to 50 sandboxes running at once.

| # | Setup | How a sandbox reaches its one allowed target |
|---|---|---|
| 1 | Kernel | host kernel routing; nftables allows only that connection |
| 2 | Envoy, network | Envoy on the host, over a network hop |
| 3 | Envoy, socket | no network; Unix socket to Envoy |
| 4 | Router | no network; router + sidecar over a Unix socket |

(`"0"` in `setups` adds a no-policy control.)

Two scenarios: **outbound** (sandbox → target) and **peer** (sandbox *i* → sandbox *i+1*, in a ring).
Metrics: time to first byte, request latency, throughput (up/down), plus CPU/RAM of the mediators.

## Layout

```
netbench/              load client, relay and test server (Go)
orchestrator/          runs everything (Python 3.11+): bench/, example.toml, host-tune.sh, cleanup.sh, report.py
results-viewer.html    opens the results folder: tables, charts, XLSX export
REFERENCE.md          formats, metrics, troubleshooting
```

## Quick start (Ubuntu, bare metal, root)

```bash
sudo apt install -y python3 iproute2 nftables conntrack iptables golang-go docker.io curl
# also needed: Envoy (setups 2, 3), gVisor runsc, a sandbox image — see REFERENCE.md

# build the load client
cd netbench && CGO_ENABLED=0 go build -o ../orchestrator/netbench . && cd ../orchestrator

# setup 4: publish your Router and Sidecar self-contained, then set router/sidecar in the config
# setups 3/4 on gVisor: add "--host-uds=all" to the runsc runtimeArgs in /etc/docker/daemon.json

cp example.toml my.toml                    # edit: experiment, docker_runtime, image, [cores]
sudo ./host-tune.sh --verify               # after every boot
sudo python3 -m bench check -c my.toml     # fix every FAIL
sudo python3 -m bench smoke -c my.toml     # short end-to-end test
python3 -m bench plan  -c my.toml          # estimated duration
sudo python3 -m bench run   -c my.toml     # resumable: run again after an interruption
```

Use a **new `experiment` name per runtime** (e.g. `docker`, `gvisor`). Don't change settings mid-experiment;
the tool refuses to mix results (`--force` overrides, e.g. to add benchmarks).

## Results

Open `results-viewer.html` and choose `orchestrator/results`. Raw data is in `results/<experiment>/`;
the log is `orchestrator.log` there.

## Troubleshooting

- `sudo ./cleanup.sh` removes leftover processes, containers, namespaces, interfaces and firewall rules.
- `python3 report.py results/<experiment>` prints a short diagnostic summary (no root needed) — send it when asking for help.
- Everything else: `REFERENCE.md`.