package main

import (
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

// Throughput: P parallel streams moving bulk data for the measured window.
//
//	up:   sandbox -> target. The target sinks the data and, after the client half-closes,
//	      reports how many bytes it received; a mismatch marks the run invalid.
//	down: target -> sandbox. Sampled bytes of the incoming stream are verified.
//
// The rate is bytes moved between the end of warm-up and the end of the window, divided by the
// window length. A per-second series (from the start of the schedule) shows whether it was steady.

const tputGrace = 2 * time.Second

func runTput(args []string) error {
	c := newCommon("tput", copts{run: true})
	dir := c.fs.String("dir", "up", "up: sandbox->target | down: target->sandbox")
	streams := c.fs.Int("streams", 1, "parallel streams")
	chunk := c.fs.Int("chunk", 64<<10, "chunk size in bytes")
	if err := c.parse(args); err != nil {
		return err
	}
	if *dir != "up" && *dir != "down" {
		return usageErr("--dir must be up or down")
	}
	if *streams < 1 || *chunk < 16 || *chunk > maxSize {
		return usageErr("bad --streams or --chunk")
	}
	tg, err := resolveTarget(*c.target, *c.dns, *c.timeout)
	if err != nil {
		return &exitErr{2, err.Error()}
	}

	mode := modeSink
	if *dir == "down" {
		mode = modeSource
	}
	conns := make([]net.Conn, *streams)
	for i := range conns {
		conn, err := tg.dial(*c.timeout)
		if err == nil {
			hdr := make([]byte, headerLen)
			header{mode: mode, size: uint32(*chunk)}.put(hdr)
			_, err = conn.Write(hdr)
		}
		if err != nil {
			for _, o := range conns[:i] {
				o.Close()
			}
			return &exitErr{2, fmt.Sprintf("stream %d/%d: %v (use `probe` to check reachability)", i+1, *streams, err)}
		}
		conns[i] = conn
	}
	start, err := c.startTime()
	if err != nil {
		return err
	}
	measureFrom := start.Add(*c.warmup)
	end := measureFrom.Add(*c.duration)

	var (
		moved  atomic.Uint64
		b0, b1 atomic.Uint64
		cnt    counters
		wg     sync.WaitGroup
	)
	cpu0 := cpuNs()
	time.AfterFunc(time.Until(measureFrom), func() { b0.Store(moved.Load()) })
	time.AfterFunc(time.Until(end), func() { b1.Store(moved.Load()) })

	// per-second series, sampled from the start of the schedule
	var series []float64
	sampled := make(chan struct{})
	go func() {
		defer close(sampled)
		var prev uint64
		for i := 1; ; i++ {
			t := start.Add(time.Duration(i) * time.Second)
			if t.After(end) {
				return
			}
			time.Sleep(time.Until(t))
			cur := moved.Load()
			series = append(series, float64(cur-prev)*8/1e6)
			prev = cur
		}
	}()

	for _, conn := range conns {
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer conn.Close()
			time.Sleep(time.Until(start))
			if *dir == "up" {
				buf := make([]byte, *chunk)
				fillPattern(buf)
				var total uint64
				for time.Now().Before(end) {
					conn.SetWriteDeadline(end.Add(tputGrace))
					n, err := conn.Write(buf)
					total += uint64(n)
					moved.Add(uint64(n))
					if err != nil {
						if time.Now().Before(end) {
							cnt.fail(err)
						}
						break
					}
				}
				conn.(*net.TCPConn).CloseWrite()
				conn.SetReadDeadline(time.Now().Add(*c.timeout))
				var rep [8]byte
				if _, err := io.ReadFull(conn, rep[:]); err != nil {
					cnt.fail(err)
				} else if binary.BigEndian.Uint64(rep[:]) != total {
					cnt.add("bad_total", 1)
				}
				return
			}
			buf := make([]byte, 256<<10)
			var off uint64
			for {
				conn.SetReadDeadline(end.Add(tputGrace))
				n, err := conn.Read(buf)
				if n > 0 {
					moved.Add(uint64(n))
					if !checkStream(buf[:n], off, *chunk) {
						cnt.add("bad_payload", 1)
					}
					off += uint64(n)
				}
				if err != nil {
					if time.Now().Before(end) {
						cnt.fail(err)
					}
					return
				}
				if !time.Now().Before(end) {
					return
				}
			}
		}()
	}
	wg.Wait()
	<-sampled
	if b1.Load() == 0 { // stream failed before the window ended
		b1.Store(moved.Load())
	}

	bytesMoved := b1.Load() - b0.Load()
	secs := c.duration.Seconds()
	tr := &tputReport{
		Dir: *dir, Streams: *streams, Chunk: *chunk, Bytes: bytesMoved, Seconds: secs,
		MbitPerSec: float64(bytesMoved) * 8 / 1e6 / secs, PerSecondMb: series, WarmupSecs: int(c.warmup.Seconds()),
	}
	first := true
	for i := tr.WarmupSecs; i < len(series); i++ {
		v := series[i]
		if first || v < tr.WindowMinMb {
			tr.WindowMinMb = v
		}
		if first || v > tr.WindowMaxMb {
			tr.WindowMaxMb = v
		}
		first = false
	}

	r := newReport("tput", c, tg)
	r.StartUnixNs = start.UnixNano()
	r.Params["dir"], r.Params["streams"], r.Params["chunk"] = *dir, *streams, *chunk
	r.Params["warmup_s"], r.Params["duration_s"] = c.warmup.Seconds(), secs
	cnt.copyTo(r.Counts)
	r.Counts["ok"] = uint64(*streams) - min(uint64(*streams), sumErrs(r.Counts))
	r.Tput = tr
	r.ClientCPUNs = cpuNs() - cpu0
	r.finalize(uint64(*streams), 0)
	fmt.Fprintf(os.Stderr, "tput %s  streams=%d  %.1f Mbit/s (%.2f Gbit/s)  window min/max %.0f/%.0f  errors=%d valid=%v\n",
		*dir, *streams, tr.MbitPerSec, tr.MbitPerSec/1000, tr.WindowMinMb, tr.WindowMaxMb, sumErrs(r.Counts), r.Valid)
	return emit(r, *c.out)
}
