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

// relay runs INSIDE a sandbox that has no network. It listens on a loopback TCP port and forwards every
// connection, byte for byte, to a Unix socket that the host mounted into the sandbox. The program inside
// the sandbox keeps using ordinary TCP (to 127.0.0.1); the host side of the socket is the proxy.
//
// With --stats FILE the relay appends one JSON line per second about itself (CPU time, memory, open
// connections). The orchestrator reads that file from the host, so the relay's cost can be recorded
// without entering the sandbox.

func runRelay(args []string) error {
	fs := flag.NewFlagSet("relay", flag.ContinueOnError)
	listen := fs.String("listen", "127.0.0.1:9000", "TCP address to listen on inside the sandbox")
	unixPath := fs.String("unix", "", "Unix socket every connection is forwarded to (required)")
	stats := fs.String("stats", "", "append one JSON line per second with this process's CPU time and memory to FILE")
	procs := fs.Int("procs", 0, "GOMAXPROCS (0 = Go default)")
	if err := fs.Parse(args); err != nil {
		return &exitErr{2, ""}
	}
	if *unixPath == "" {
		return usageErr("--unix is required")
	}
	if *procs > 0 {
		runtime.GOMAXPROCS(*procs)
	}
	ln, err := net.Listen("tcp4", *listen)
	if err != nil {
		return &exitErr{2, err.Error()}
	}
	var active atomic.Int64
	if *stats != "" {
		go statsLoop(*stats, &active)
	}
	fmt.Fprintf(os.Stderr, "netbench relay listening on %s -> unix:%s\n", ln.Addr(), *unixPath)
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go relayConn(c, *unixPath, &active)
	}
}

func relayConn(c net.Conn, path string, active *atomic.Int64) {
	defer c.Close()
	active.Add(1)
	defer active.Add(-1)
	u, err := net.DialTimeout("unix", path, 5*time.Second)
	if err != nil {
		return
	}
	defer u.Close()
	if tc, ok := c.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	done := make(chan struct{}, 2)
	go func() { // sandbox -> host
		io.Copy(u, c)
		if uc, ok := u.(*net.UnixConn); ok {
			uc.CloseWrite()
		}
		done <- struct{}{}
	}()
	go func() { // host -> sandbox
		io.Copy(c, u)
		if tc, ok := c.(*net.TCPConn); ok {
			tc.CloseWrite()
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
