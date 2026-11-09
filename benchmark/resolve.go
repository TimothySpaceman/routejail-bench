package main

import (
	"context"
	"fmt"
	"net"
	"strings"
	"time"
)

// target is a host:port that may be a name or an IPv4 literal. The tool is IPv4-only:
// it asks the resolver for A records only (so a DNS stub does not need to answer AAAA)
// and dials tcp4.
//
//	--dns once  resolve at startup, then dial the resolved IP. Name resolution is exercised
//	            (and timed) once, and does not pollute TTFB.
//	--dns each  dial by name every time. The resolver is hit on every connection and its cost
//	            is part of connect/TTFB. Build with CGO_ENABLED=0 so Go's own resolver reads
//	            /etc/resolv.conf directly, the same in every sandbox.
type target struct {
	spec      string
	network   string // "tcp4", or "unix" for targets written as unix:/path
	dialAddr  string
	resolved  string
	nResolved int
	dnsDur    time.Duration
	mode      string
}

func resolveTarget(spec, mode string, timeout time.Duration) (*target, error) {
	if path, ok := strings.CutPrefix(spec, "unix:"); ok { // a Unix socket: no name resolution
		return &target{spec: spec, network: "unix", dialAddr: path, resolved: spec, nResolved: 1, mode: "unix"}, nil
	}
	host, port, err := net.SplitHostPort(spec)
	if err != nil {
		return nil, fmt.Errorf("bad --target %q: %w", spec, err)
	}
	t := &target{spec: spec, network: "tcp4", dialAddr: spec, mode: mode}
	if net.ParseIP(host) != nil {
		t.mode, t.resolved, t.nResolved = "literal", spec, 1
		return t, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	t0 := time.Now()
	ips, err := net.DefaultResolver.LookupIP(ctx, "ip4", host)
	t.dnsDur = time.Since(t0)
	if err != nil {
		return t, fmt.Errorf("resolve %q: %w", host, err)
	}
	if len(ips) == 0 {
		return t, fmt.Errorf("resolve %q: no A records", host)
	}
	t.nResolved = len(ips)
	t.resolved = net.JoinHostPort(ips[0].String(), port)
	if mode == "once" {
		t.dialAddr = t.resolved
	}
	return t, nil
}

func (t *target) dial(timeout time.Duration) (net.Conn, error) {
	d := net.Dialer{Timeout: timeout}
	c, err := d.Dial(t.network, t.dialAddr)
	if err == nil {
		if tc, ok := c.(*net.TCPConn); ok {
			tc.SetNoDelay(true)
		}
	}
	return c, err
}
