package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

// TTFB: a new connection per request. TTFB runs from the moment we start dialing until the
// first reply byte arrives, so it includes connection setup (and name resolution with --dns each).
// Dialing is open-loop: every scheduler tick starts a connection without waiting for earlier ones.
// The server closes the connection after replying, so the client never sits in TIME_WAIT.

type ttfbResult struct {
	ttfb, connect time.Duration
	err           error
	bad           bool
}

func oneTTFB(tg *target, size int, seq uint64, timeout time.Duration) ttfbResult {
	t0 := time.Now()
	c, err := tg.dial(timeout)
	if err != nil {
		return ttfbResult{err: err}
	}
	defer c.Close()
	tConn := time.Now()
	c.SetDeadline(t0.Add(timeout))

	msg := make([]byte, headerLen+size)
	header{mode: modeEcho, flags: flagCloseAfterOne, size: uint32(size)}.put(msg)
	fillPattern(msg[headerLen:])
	setSeq(msg[headerLen:], seq)
	if _, err := c.Write(msg); err != nil { // header and payload in one write: no extra round trip
		return ttfbResult{err: err}
	}
	reply := make([]byte, size)
	if _, err := io.ReadFull(c, reply[:1]); err != nil {
		return ttfbResult{err: err}
	}
	tFirst := time.Now()
	if _, err := io.ReadFull(c, reply[1:]); err != nil {
		return ttfbResult{err: err}
	}
	return ttfbResult{ttfb: tFirst.Sub(t0), connect: tConn.Sub(t0), bad: !checkMsg(reply, seq)}
}

func runTTFB(args []string) error {
	c := newCommon("ttfb", copts{run: true, rate: 100, size: true})
	maxInflight := c.fs.Int("max-inflight", 5000, "drop new attempts when this many connections are in flight")
	if err := c.parse(args); err != nil {
		return err
	}
	tg, err := resolveTarget(*c.target, *c.dns, *c.timeout)
	if err != nil {
		return &exitErr{2, err.Error()}
	}
	start, err := c.startTime()
	if err != nil {
		return err
	}
	interval := time.Duration(float64(time.Second) / *c.rate)
	tickStart := start.Add(time.Duration(phaseFrac(*c.phaseKey) * float64(interval)))
	measureFrom := start.Add(*c.warmup)
	end := measureFrom.Add(*c.duration)

	var (
		ttfb, conn, lag syncHist
		cnt             counters
		inflight        atomic.Int64
		attempts        atomic.Uint64
		wg              sync.WaitGroup
	)
	cpu0 := cpuNs()
	schedule(context.Background(), tickStart, interval, end, &lag, func(k int64, sched time.Time) {
		measured := !sched.Before(measureFrom)
		if measured {
			attempts.Add(1)
		}
		if inflight.Add(1) > int64(*maxInflight) {
			inflight.Add(-1)
			if measured {
				cnt.add("dropped", 1)
			}
			return
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer inflight.Add(-1)
			res := oneTTFB(tg, *c.size, uint64(k), *c.timeout)
			if !measured {
				return
			}
			switch {
			case res.err != nil:
				cnt.fail(res.err)
			case res.bad:
				cnt.add("bad_payload", 1)
			default:
				cnt.add("ok", 1)
				ttfb.record(res.ttfb)
				conn.record(res.connect)
			}
		}()
	})
	wg.Wait()

	r := newReport("ttfb", c, tg)
	r.StartUnixNs = start.UnixNano()
	r.Params["rate"], r.Params["size"] = *c.rate, *c.size
	r.Params["warmup_s"], r.Params["duration_s"] = c.warmup.Seconds(), c.duration.Seconds()
	cnt.copyTo(r.Counts)
	r.TTFB = ttfb.snapshot().summary(true)
	r.Connect = conn.snapshot().summary(true)
	r.SchedLag = lag.snapshot().summary(false)
	r.ClientCPUNs = cpuNs() - cpu0
	r.finalize(attempts.Load(), interval)
	fmt.Fprintf(os.Stderr, "ttfb  n=%d p50=%s p99=%s max=%s errors=%d lag_p99=%s valid=%v\n",
		r.TTFB.N, dur(r.TTFB.P50Ns), dur(r.TTFB.P99Ns), dur(r.TTFB.MaxNs), sumErrs(r.Counts), dur(r.SchedLag.P99Ns), r.Valid)
	return emit(r, *c.out)
}
