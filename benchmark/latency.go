package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

// Latency: C persistent connections, each doing open-loop ping-pong. A writer sends a message at
// every scheduler tick without waiting for replies; a reader matches replies to sends in order
// (TCP preserves order).
//
// Two latencies are recorded for every reply:
//
//	latency        reply time minus the moment the message was actually written. This is the request's
//	               own round trip and the number to compare setups with. It does not include the
//	               client's timer lateness (Go timers in an idle process fire up to ~1 ms late).
//	latency_sched  reply time minus the moment the message was *supposed* to be sent. It also counts
//	               time lost to a late or blocked writer, so it is what a user of a saturated system
//	               would have experienced (coordinated-omission aware). Under light load it is
//	               dominated by timer lateness; under overload it is the honest one.
//
// The gap between the two is the client's own lag. It is also reported on its own as sched_lag.

var epoch = time.Now() // monotonic reference for send timestamps

type pending struct {
	sched    time.Time
	sent     atomic.Int64 // ns since epoch, stored by the writer right before the write
	seq      uint64
	measured bool
}

type latConn struct {
	c      net.Conn
	pend   chan *pending
	ctx    context.Context
	cancel context.CancelFunc
	h      hist   // from the actual send
	hs     hist   // from the scheduled send
	sent   uint64 // measured sends (writer goroutine only)
	ok     uint64 // reader goroutine only
	bad    uint64
	lost   uint64
}

func (lc *latConn) fail() {
	lc.cancel()
	lc.c.Close()
}

func runLatency(args []string) error {
	c := newCommon("latency", copts{run: true, rate: 200, size: true})
	conns := c.fs.Int("conns", 1, "persistent connections per sandbox (--rate is split across them)")
	if err := c.parse(args); err != nil {
		return err
	}
	if *conns < 1 {
		return usageErr("--conns must be >= 1")
	}
	tg, err := resolveTarget(*c.target, *c.dns, *c.timeout)
	if err != nil {
		return &exitErr{2, err.Error()}
	}

	// Connect first (setup is not measured), send the protocol header, then wait for the start time.
	lcs := make([]*latConn, *conns)
	for i := range lcs {
		conn, err := tg.dial(*c.timeout)
		if err == nil {
			hdr := make([]byte, headerLen)
			header{mode: modeEcho, size: uint32(*c.size)}.put(hdr)
			_, err = conn.Write(hdr)
		}
		if err != nil {
			for _, o := range lcs[:i] {
				o.c.Close()
			}
			return &exitErr{2, fmt.Sprintf("connection %d/%d: %v (use `probe` to check reachability)", i+1, *conns, err)}
		}
		ctx, cancel := context.WithCancel(context.Background())
		lcs[i] = &latConn{c: conn, pend: make(chan *pending, 1<<16), ctx: ctx, cancel: cancel}
	}
	start, err := c.startTime()
	if err != nil {
		return err
	}
	perConn := time.Duration(float64(time.Second) * float64(*conns) / *c.rate)
	measureFrom := start.Add(*c.warmup)
	end := measureFrom.Add(*c.duration)
	phase := phaseFrac(*c.phaseKey)

	var (
		lag syncHist
		cnt counters
		wg  sync.WaitGroup
	)
	cpu0 := cpuNs()
	for i, lc := range lcs {
		// stagger: connection i of this client starts (i+phase)/C of the way into the interval
		off := time.Duration((float64(i) + phase) / float64(*conns) * float64(perConn))
		wg.Add(2)
		go func() { // writer
			defer wg.Done()
			msg := make([]byte, *c.size)
			fillPattern(msg)
			schedule(lc.ctx, start.Add(off), perConn, end, &lag, func(k int64, sched time.Time) {
				setSeq(msg, uint64(k))
				p := &pending{sched: sched, seq: uint64(k), measured: !sched.Before(measureFrom)}
				// Queue the entry first so the reader is already waiting on the socket when the reply
				// arrives (a reader that wakes up late would inflate the round trip). A full queue
				// blocks here, which then shows up as scheduler lag.
				select {
				case lc.pend <- p:
				case <-lc.ctx.Done():
					return
				}
				if p.measured {
					lc.sent++
				}
				lc.c.SetWriteDeadline(time.Now().Add(*c.timeout))
				p.sent.Store(int64(time.Since(epoch))) // stamped as late as possible, right before the write
				if _, err := lc.c.Write(msg); err != nil {
					if lc.ctx.Err() == nil {
						cnt.fail(err)
					}
					lc.fail()
				}
			})
			close(lc.pend)
		}()
		go func() { // reader
			defer wg.Done()
			buf := make([]byte, *c.size)
			for p := range lc.pend {
				lc.c.SetReadDeadline(time.Now().Add(*c.timeout))
				if _, err := io.ReadFull(lc.c, buf); err != nil {
					if lc.ctx.Err() == nil && p.measured {
						cnt.fail(err)
					}
					if p.measured {
						lc.lost++
					}
					lc.fail()
					for q := range lc.pend { // account for everything still outstanding
						if q.measured {
							lc.lost++
						}
					}
					return
				}
				now := time.Since(epoch)
				if !checkMsg(buf, p.seq) {
					if p.measured {
						lc.bad++
					}
					continue
				}
				if p.measured {
					sent := time.Duration(p.sent.Load())
					if sent == 0 { // writer never stamped it (cannot happen on a successful reply); fall back
						sent = p.sched.Sub(epoch)
					}
					lc.h.record(int64(now - sent))
					lc.hs.record(int64(now - p.sched.Sub(epoch)))
					lc.ok++
				}
			}
		}()
	}
	wg.Wait()
	var total, totalSched hist
	var attempts uint64
	for _, lc := range lcs {
		lc.c.Close()
		total.merge(&lc.h)
		totalSched.merge(&lc.hs)
		attempts += lc.sent
		cnt.add("ok", lc.ok)
		cnt.add("bad_payload", lc.bad)
		cnt.add("lost", lc.lost)
	}

	r := newReport("latency", c, tg)
	r.StartUnixNs = start.UnixNano()
	r.Params["rate"], r.Params["size"], r.Params["conns"] = *c.rate, *c.size, *conns
	r.Params["warmup_s"], r.Params["duration_s"] = c.warmup.Seconds(), c.duration.Seconds()
	cnt.copyTo(r.Counts)
	r.Latency = total.summary(true)
	r.LatencySched = totalSched.summary(true)
	r.SchedLag = lag.snapshot().summary(false)
	r.ClientCPUNs = cpuNs() - cpu0
	r.finalize(attempts, perConn)
	fmt.Fprintf(os.Stderr, "latency  n=%d p50=%s p99=%s max=%s | from schedule: p50=%s p99=%s | errors=%d lag_p99=%s valid=%v\n",
		r.Latency.N, dur(r.Latency.P50Ns), dur(r.Latency.P99Ns), dur(r.Latency.MaxNs),
		dur(r.LatencySched.P50Ns), dur(r.LatencySched.P99Ns), sumErrs(r.Counts), dur(r.SchedLag.P99Ns), r.Valid)
	return emit(r, *c.out)
}
