package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"time"
)

// probe checks whether a destination is reachable end to end: resolve, connect, one echo round trip.
// Run it from inside a sandbox before measuring: against an allowed target (--expect ok) and against
// something that must be blocked (--expect blocked). It catches bypasses and misconfigured setups.
// Exit code 1 means the expectation was not met.

type probeResult struct {
	Target    string  `json:"target"`
	Resolved  string  `json:"resolved,omitempty"`
	DNSMs     float64 `json:"dns_ms"`
	Outcome   string  `json:"outcome"` // ok | dns_error | refused | reset | eof | timeout | other
	Detail    string  `json:"detail,omitempty"`
	ConnectMs float64 `json:"connect_ms,omitempty"`
	RTTMs     float64 `json:"rtt_ms,omitempty"`
}

func runProbe(args []string) error {
	c := newCommon("probe", copts{})
	expect := c.fs.String("expect", "any", "ok | blocked | any")
	if err := c.parse(args); err != nil {
		return err
	}
	if *expect != "ok" && *expect != "blocked" && *expect != "any" {
		return usageErr("--expect must be ok, blocked or any")
	}
	res := probeResult{Target: *c.target}
	tg, err := resolveTarget(*c.target, *c.dns, *c.timeout)
	if tg != nil {
		res.DNSMs = float64(tg.dnsDur) / 1e6
		res.Resolved = tg.resolved
	}
	if err != nil {
		res.Outcome, res.Detail = "dns_error", err.Error()
	} else {
		res.Outcome, res.Detail = probeOnce(tg, *c.timeout, &res)
	}
	b, _ := json.Marshal(res)
	os.Stdout.Write(append(b, '\n'))

	ok := res.Outcome == "ok"
	switch {
	case *expect == "ok" && !ok:
		return &exitErr{1, fmt.Sprintf("expected reachable, got %s", res.Outcome)}
	case *expect == "blocked" && ok:
		return &exitErr{1, "expected blocked, but the destination is reachable"}
	}
	return nil
}

func probeOnce(tg *target, timeout time.Duration, res *probeResult) (string, string) {
	t0 := time.Now()
	conn, err := tg.dial(timeout)
	if err != nil {
		return classify(err), err.Error()
	}
	defer conn.Close()
	res.ConnectMs = float64(time.Since(t0)) / 1e6
	conn.SetDeadline(time.Now().Add(timeout))
	const size = 16
	msg := make([]byte, headerLen+size)
	header{mode: modeEcho, flags: flagCloseAfterOne, size: size}.put(msg)
	fillPattern(msg[headerLen:])
	setSeq(msg[headerLen:], 1)
	t1 := time.Now()
	if _, err := conn.Write(msg); err != nil {
		return classify(err), err.Error()
	}
	reply := make([]byte, size)
	if _, err := io.ReadFull(conn, reply); err != nil {
		return classify(err), err.Error()
	}
	if !checkMsg(reply, 1) {
		return "other", "reply did not match (something else answered)"
	}
	res.RTTMs = float64(time.Since(t1)) / 1e6
	return "ok", ""
}
