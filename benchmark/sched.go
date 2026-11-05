package main

import (
	"context"
	"hash/fnv"
	"os"
	"time"
)

// schedule is the open-loop pacer. It calls fn at start + k*interval for k = 0,1,2,...
// while the scheduled time is before end.
//
//   - It never waits for fn's work to complete (callers hand work off or write without waiting for replies).
//   - It never skips a tick: if the loop is late (timer overshoot, a blocked fn), missed ticks fire
//     immediately and in order, so offered load stays correct. The lateness of every tick is
//     recorded in lag, and the run is flagged invalid if lag stays high.
//   - fn receives the *intended* time, so callers can measure from the schedule when that matters.
func schedule(ctx context.Context, start time.Time, interval time.Duration, end time.Time, lag *syncHist, fn func(k int64, sched time.Time)) {
	for k := int64(0); ; k++ {
		sched := start.Add(time.Duration(k) * interval)
		if !sched.Before(end) || ctx.Err() != nil {
			return
		}
		if !sleepUntil(ctx, sched) {
			return
		}
		if lag != nil {
			lag.record(time.Since(sched))
		}
		fn(k, sched)
	}
}

// sleepUntil returns false if ctx was cancelled first.
func sleepUntil(ctx context.Context, t time.Time) bool {
	d := time.Until(t)
	if d <= 0 {
		return ctx.Err() == nil
	}
	tm := time.NewTimer(d)
	defer tm.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-tm.C:
		return true
	}
}

// phaseFrac maps a key (default: hostname) to a stable fraction in [0,1). Clients use it to
// offset their schedule within one interval so that N sandboxes sharing --start-at do not
// all fire at the same instants. Key "none" disables the offset.
func phaseFrac(key string) float64 {
	if key == "none" {
		return 0
	}
	if key == "" {
		key, _ = os.Hostname()
	}
	h := fnv.New64a()
	h.Write([]byte(key))
	return float64(h.Sum64()%1000000) / 1e6
}
