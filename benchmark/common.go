package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"
)

type exitErr struct {
	code int
	msg  string
}

func (e *exitErr) Error() string { return e.msg }

func usageErr(format string, a ...any) error { return &exitErr{2, fmt.Sprintf(format, a...)} }

// ---- flags shared by the client commands ----

type labelFlag map[string]string

func (l *labelFlag) String() string { return fmt.Sprint(map[string]string(*l)) }
func (l *labelFlag) Set(s string) error {
	k, v, ok := strings.Cut(s, "=")
	if !ok || k == "" {
		return fmt.Errorf("label must be key=value, got %q", s)
	}
	(*l)[k] = v
	return nil
}

type copts struct {
	run  bool    // duration, warmup, start-at, phase-key, out
	rate float64 // >0: add --rate with this default
	size bool    // --size
}

type common struct {
	fs       *flag.FlagSet
	target   *string
	dns      *string
	timeout  *time.Duration
	labels   labelFlag
	rate     *float64
	size     *int
	duration *time.Duration
	warmup   *time.Duration
	startAt  *float64
	phaseKey *string
	out      *string
}

func newCommon(name string, o copts) *common {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	c := &common{fs: fs, labels: labelFlag{}}
	c.target = fs.String("target", "", "host:port to connect to (name or IPv4)")
	c.dns = fs.String("dns", "once", "once: resolve at startup, dial the IP | each: dial by name every time (DNS cost included)")
	c.timeout = fs.Duration("timeout", 5*time.Second, "per-operation timeout")
	fs.Var(&c.labels, "label", "key=value copied into the JSON output (repeatable), e.g. setup=3, n=10, sandbox=s04, run=2")
	if o.rate > 0 {
		c.rate = fs.Float64("rate", o.rate, "requests per second, per sandbox")
	}
	if o.size {
		c.size = fs.Int("size", 64, "message size in bytes (min 16)")
	}
	if o.run {
		c.duration = fs.Duration("duration", 30*time.Second, "measured duration")
		c.warmup = fs.Duration("warmup", 5*time.Second, "warm-up before measuring; samples are discarded")
		c.startAt = fs.Float64("start-at", 0, "unix time in seconds (fractions ok) at which load starts; 0 = shortly after setup")
		c.phaseKey = fs.String("phase-key", "", "key hashed into a schedule offset within one interval (default: hostname; 'none' disables)")
		c.out = fs.String("out", "", "write JSON to this file instead of stdout")
	}
	return c
}

func (c *common) parse(args []string) error {
	if err := c.fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return &exitErr{0, ""}
		}
		return &exitErr{2, ""}
	}
	if *c.target == "" {
		return usageErr("--target is required")
	}
	if *c.dns != "once" && *c.dns != "each" {
		return usageErr("--dns must be once or each")
	}
	if c.rate != nil && *c.rate <= 0 {
		return usageErr("--rate must be > 0")
	}
	if c.size != nil && (*c.size < 16 || *c.size > maxSize) {
		return usageErr("--size must be between 16 and %d", maxSize)
	}
	return nil
}

// startTime returns the moment the schedule begins. Call it after connections are
// established so that setup time does not eat into the lead time.
func (c *common) startTime() (time.Time, error) {
	if *c.startAt == 0 {
		return time.Now().Add(300 * time.Millisecond), nil
	}
	sec, frac := math.Modf(*c.startAt)
	t := time.Unix(int64(sec), int64(frac*1e9))
	if time.Until(t) < -time.Second {
		return t, usageErr("--start-at is %.1fs in the past", -time.Until(t).Seconds())
	}
	return t, nil
}

// ---- counters and error classes ----

type counters struct {
	mu sync.Mutex
	m  map[string]uint64
}

func (c *counters) add(k string, n uint64) {
	c.mu.Lock()
	if c.m == nil {
		c.m = map[string]uint64{}
	}
	c.m[k] += n
	c.mu.Unlock()
}

func (c *counters) fail(err error) { c.add("err_"+classify(err), 1) }

func (c *counters) copyTo(dst map[string]uint64) {
	c.mu.Lock()
	for k, v := range c.m {
		dst[k] += v
	}
	c.mu.Unlock()
}

func classify(err error) string {
	var ne net.Error
	switch {
	case errors.Is(err, syscall.ECONNREFUSED):
		return "refused"
	case errors.Is(err, syscall.ECONNRESET), errors.Is(err, syscall.EPIPE):
		return "reset"
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
		return "eof"
	case errors.Is(err, os.ErrDeadlineExceeded):
		return "timeout"
	case errors.As(err, &ne) && ne.Timeout():
		return "timeout"
	}
	return "other"
}

// ---- result report ----

type tputReport struct {
	Dir         string    `json:"dir"`
	Streams     int       `json:"streams"`
	Chunk       int       `json:"chunk"`
	Bytes       uint64    `json:"bytes"`
	Seconds     float64   `json:"seconds"`
	MbitPerSec  float64   `json:"mbit_s"`
	WindowMinMb float64   `json:"window_min_mbit_s"`
	WindowMaxMb float64   `json:"window_max_mbit_s"`
	WarmupSecs  int       `json:"warmup_seconds"`
	PerSecondMb []float64 `json:"per_second_mbit_s"` // from schedule start, warm-up included
}

type report struct {
	Schema       int               `json:"schema"`
	Mode         string            `json:"mode"`
	Labels       map[string]string `json:"labels"`
	Target       string            `json:"target"`
	Resolved     string            `json:"resolved,omitempty"`
	ResolvedN    int               `json:"resolved_count,omitempty"`
	DNSMode      string            `json:"dns_mode"`
	DNSMs        float64           `json:"dns_ms"`
	Params       map[string]any    `json:"params"`
	StartUnixNs  int64             `json:"start_unix_ns"`
	Counts       map[string]uint64 `json:"counts"`
	TTFB         *histSummary      `json:"ttfb,omitempty"`
	Latency      *histSummary      `json:"latency,omitempty"`       // reply time minus actual send time
	LatencySched *histSummary      `json:"latency_sched,omitempty"` // reply time minus scheduled send time (includes client lag)
	Connect      *histSummary      `json:"connect,omitempty"`
	SchedLag     *histSummary      `json:"sched_lag,omitempty"`
	Tput         *tputReport       `json:"tput,omitempty"`
	ClientCPUNs  int64             `json:"client_cpu_ns"`
	Valid        bool              `json:"valid"`
	Warnings     []string          `json:"warnings,omitempty"`
}

func newReport(mode string, c *common, tg *target) *report {
	return &report{
		Schema: 2, Mode: mode, Labels: c.labels, Target: tg.spec, Resolved: tg.resolved,
		ResolvedN: tg.nResolved, DNSMode: tg.mode, DNSMs: float64(tg.dnsDur) / 1e6,
		Params: map[string]any{}, Counts: map[string]uint64{},
	}
}

// finalize decides validity. A run is invalid if payloads were corrupted, more than 0.1% of
// attempts failed, or the client's own pacing lagged (the client, not the system, was the limit).
func (r *report) finalize(attempts uint64, interval time.Duration) {
	var errs uint64
	for k, v := range r.Counts {
		if strings.HasPrefix(k, "err_") || k == "dropped" || k == "lost" {
			errs += v
		}
	}
	if n := r.Counts["bad_payload"] + r.Counts["bad_total"]; n > 0 {
		r.Warnings = append(r.Warnings, fmt.Sprintf("%d corrupted payloads/totals", n))
	}
	if attempts > 0 && float64(errs)/float64(attempts) > 0.001 {
		r.Warnings = append(r.Warnings, fmt.Sprintf("%d errors/drops of %d attempts (>0.1%%)", errs, attempts))
	}
	if r.SchedLag != nil && interval > 0 {
		limit := 2 * time.Millisecond
		if q := interval / 4; q > limit {
			limit = q
		}
		if time.Duration(r.SchedLag.P99Ns) > limit {
			r.Warnings = append(r.Warnings, fmt.Sprintf("scheduler lag p99 %s exceeds %s: client is the bottleneck", dur(r.SchedLag.P99Ns), limit))
		}
	}
	r.Valid = len(r.Warnings) == 0
}

func emit(r *report, out string) error {
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	if out == "" {
		_, err = os.Stdout.Write(b)
		return err
	}
	return os.WriteFile(out, b, 0o644)
}

func cpuNs() int64 {
	var ru syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &ru) != nil {
		return 0
	}
	return ru.Utime.Nano() + ru.Stime.Nano()
}

func dur(ns int64) string { return fmt.Sprintf("%.3fms", float64(ns)/1e6) }

func sumErrs(m map[string]uint64) (n uint64) {
	for k, v := range m {
		if strings.HasPrefix(k, "err_") || k == "dropped" || k == "lost" {
			n += v
		}
	}
	return
}
