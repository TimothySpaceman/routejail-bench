package main

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// relay runs INSIDE a sandbox that has no network and forwards every connection, byte for byte, between two
// addresses, each either TCP (host:port) or a Unix socket (unix:/path):
//
//	out:  --listen 127.0.0.1:9000        --to unix:/sock/proxy.sock   the program in the sandbox keeps using plain TCP;
//	                                                                  the host side of the socket is the proxy
//	in:   --listen unix:/sock/in.sock    --to 127.0.0.1:9100          the host reaches a server inside the sandbox
//
// With --stats FILE the relay appends one JSON line per second about itself (CPU time, memory, open
// connections). The orchestrator reads that file from the host, so the relay's cost can be recorded
// without entering the sandbox.

func runRelay(args []string) error {
	fs := flag.NewFlagSet("relay", flag.ContinueOnError)
	listen := fs.String("listen", "127.0.0.1:9000", "address to listen on: host:port, or unix:/path")
	to := fs.String("to", "", "address every connection is forwarded to: host:port, or unix:/path")
	unixPath := fs.String("unix", "", "same as --to unix:PATH (kept for older callers)")
	stats := fs.String("stats", "", "append one JSON line per second with this process's CPU time and memory to FILE")
	procs := fs.Int("procs", 0, "GOMAXPROCS (0 = Go default)")
	if err := fs.Parse(args); err != nil {
		return &exitErr{2, ""}
	}
	target := *to
	if target == "" && *unixPath != "" {
		target = "unix:" + *unixPath
	}
	if target == "" {
		return usageErr("--to (or --unix) is required")
	}
	if *procs > 0 {
		runtime.GOMAXPROCS(*procs)
	}
	network, addr := "tcp4", *listen
	if path, ok := strings.CutPrefix(*listen, "unix:"); ok {
		os.Remove(path) // a stale socket file from an earlier run
		network, addr = "unix", path
	}
	ln, err := net.Listen(network, addr)
	if err != nil {
		return &exitErr{2, err.Error()}
	}
	if network == "unix" {
		os.Chmod(addr, 0o666)
	}
	var active atomic.Int64
	if *stats != "" {
		go statsLoop(*stats, &active)
	}
	fmt.Fprintf(os.Stderr, "netbench relay listening on %s -> %s\n", *listen, target)
	dialNet, dialAddr := "tcp4", target
	if path, ok := strings.CutPrefix(target, "unix:"); ok {
		dialNet, dialAddr = "unix", path
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go relayConn(c, dialNet, dialAddr, &active)
	}
}

type closeWriter interface{ CloseWrite() error }

func relayConn(c net.Conn, network, addr string, active *atomic.Int64) {
	defer c.Close()
	active.Add(1)
	defer active.Add(-1)
	u, err := net.DialTimeout(network, addr, 5*time.Second)
	if err != nil {
		return
	}
	defer u.Close()
	if tc, ok := c.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	if tc, ok := u.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	done := make(chan struct{}, 2)
	go func() { // listening side -> destination
		io.Copy(u, c)
		if w, ok := u.(closeWriter); ok {
			w.CloseWrite()
		}
		done <- struct{}{}
	}()
	go func() { // destination -> listening side
		io.Copy(c, u)
		if w, ok := c.(closeWriter); ok {
			w.CloseWrite()
		}
		done <- struct{}{}
	}()
	<-done
	<-done
}

func statsLoop(path string, active *atomic.Int64) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "relay: cannot open stats file:", err)
		return
	}
	defer f.Close()
	for range time.Tick(time.Second) {
		rss, hwm := memKB()
		fmt.Fprintf(f, "{\"t\":%.3f,\"cpu_ns\":%d,\"rss_kb\":%d,\"hwm_kb\":%d,\"conns\":%d}\n",
			float64(time.Now().UnixNano())/1e9, cpuNs(), rss, hwm, active.Load())
	}
}

// memKB returns the resident set size and its peak from /proc/self/status.
func memKB() (rss, hwm int64) {
	f, err := os.Open("/proc/self/status")
	if err != nil {
		return 0, 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		switch {
		case strings.HasPrefix(line, "VmRSS:"):
			rss = parseKB(line)
		case strings.HasPrefix(line, "VmHWM:"):
			hwm = parseKB(line)
		}
	}
	return
}

func parseKB(line string) int64 {
	fields := strings.Fields(line)
	if len(fields) < 2 {
		return 0
	}
	v, _ := strconv.ParseInt(fields[1], 10, 64)
	return v
}
