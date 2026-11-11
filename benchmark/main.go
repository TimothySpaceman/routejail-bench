// netbench: one binary for the benchmark client (runs inside each sandbox) and the target server.
//
//	netbench server  --listen :9001,:9002
//	netbench relay   --listen 127.0.0.1:9000 --to unix:/sock/proxy.sock [--stats FILE]   (either side: host:port or unix:/path)
//	netbench probe   --target db.sandbox:9001 --expect ok|blocked  (targets may be unix:/path)
//	netbench ttfb    --target HOST:PORT --rate 200 --size 64
//	netbench latency --target HOST:PORT --rate 500 --conns 2 --size 64
//	netbench tput    --target HOST:PORT --dir up|down --streams 4
//
// Common client flags: --dns once|each, --duration, --warmup, --start-at, --label k=v, --phase-key, --out.
// Results are JSON (one object per run) on stdout; a one-line summary goes to stderr.
// Build static so the Go resolver is used in every sandbox:  CGO_ENABLED=0 go build -o netbench .
package main

import (
	"fmt"
	"os"
)

func usage() {
	fmt.Fprint(os.Stderr, `usage: netbench <command> [flags]

commands:
  server    target server: echo, sink and source on any number of ports
  relay     inside a network-less sandbox: forwards TCP <-> Unix socket, byte for byte (either direction)
  probe     one connect + echo round trip; --expect ok|blocked checks reachability
  ttfb      new connection per request, open loop; reports TTFB and connect-time histograms
  latency   persistent connections, open-loop ping-pong; reports latency histogram
  tput      bulk transfer, up or down, N parallel streams; reports Mbit/s

run "netbench <command> -h" for flags.
`)
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "server":
		err = runServer(os.Args[2:])
	case "relay":
		err = runRelay(os.Args[2:])
	case "probe":
		err = runProbe(os.Args[2:])
	case "ttfb":
		err = runTTFB(os.Args[2:])
	case "latency":
		err = runLatency(os.Args[2:])
	case "tput":
		err = runTput(os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		code := 2
		if ee, ok := err.(*exitErr); ok {
			code = ee.code
			if ee.msg != "" {
				fmt.Fprintln(os.Stderr, "netbench:", ee.msg)
			}
		} else {
			fmt.Fprintln(os.Stderr, "netbench:", err)
		}
		os.Exit(code)
	}
}
