package main

// A small mergeable histogram with log-linear buckets (HDR-style, no dependencies).
//
// Values are non-negative nanoseconds. With subBits=6 every power of two is split into 64
// linear buckets, so the relative error is below ~1.6%. Values below 128 are exact.
//
//	idx(v):  n = bitlen(v); shift = max(0, n-7); idx = shift*64 + (v >> shift)
//	low(idx): idx < 128 -> idx;  else shift = idx/64 - 1, low = (idx%64 + 64) << shift
//
// The JSON output contains sparse [idx, count] pairs, so results from many sandboxes
// can be merged by adding counts per idx and then taking percentiles.

import (
	"math/bits"
	"sync"
	"time"
)

const (
	subBits   = 6
	subCount  = 1 << subBits
	histSlots = (63-subBits)*subCount + 2*subCount // 3712
)

type hist struct {
	counts [histSlots]uint64
	n      uint64
	sum    float64
	min    int64
	max    int64
}

func idxOf(v uint64) int {
	n := bits.Len64(v)
	shift := 0
	if n > subBits+1 {
		shift = n - (subBits + 1)
	}
	return shift*subCount + int(v>>uint(shift))
}

func bucketRange(idx int) (low, width uint64) {
	if idx < 2*subCount {
		return uint64(idx), 1
	}
	shift := uint(idx/subCount - 1)
	return uint64(idx%subCount+subCount) << shift, 1 << shift
}

func (h *hist) record(ns int64) {
	if ns < 0 {
		ns = 0
	}
	if h.n == 0 || ns < h.min {
		h.min = ns
	}
	if ns > h.max {
		h.max = ns
	}
	h.counts[idxOf(uint64(ns))]++
	h.n++
	h.sum += float64(ns)
}

func (h *hist) merge(o *hist) {
	if o.n == 0 {
		return
	}
	for i, c := range o.counts {
		h.counts[i] += c
	}
	if h.n == 0 || o.min < h.min {
		h.min = o.min
	}
	if o.max > h.max {
		h.max = o.max
	}
	h.n += o.n
	h.sum += o.sum
}

func (h *hist) quantile(q float64) int64 {
	if h.n == 0 {
		return 0
	}
	rank := uint64(q*float64(h.n) + 0.5)
	if rank < 1 {
		rank = 1
	}
	var cum uint64
	for i, c := range h.counts {
		cum += c
		if c > 0 && cum >= rank {
			low, width := bucketRange(i)
			v := int64(low + width/2)
			if v > h.max {
				v = h.max
			}
			if v < h.min {
				v = h.min
			}
			return v
		}
	}
	return h.max
}

type histSummary struct {
	N       uint64      `json:"n"`
	MeanNs  float64     `json:"mean_ns"`
	MinNs   int64       `json:"min_ns"`
	P50Ns   int64       `json:"p50_ns"`
	P90Ns   int64       `json:"p90_ns"`
	P99Ns   int64       `json:"p99_ns"`
	P999Ns  int64       `json:"p999_ns"`
	MaxNs   int64       `json:"max_ns"`
	SubBits int         `json:"sub_bits,omitempty"`
	Buckets [][2]uint64 `json:"buckets,omitempty"` // sparse [idx, count]
}

func (h *hist) summary(withBuckets bool) *histSummary {
	s := &histSummary{N: h.n, MinNs: h.min, MaxNs: h.max}
	if h.n > 0 {
		s.MeanNs = h.sum / float64(h.n)
		s.P50Ns, s.P90Ns, s.P99Ns, s.P999Ns = h.quantile(0.50), h.quantile(0.90), h.quantile(0.99), h.quantile(0.999)
	}
	if withBuckets {
		s.SubBits = subBits
		for i, c := range h.counts {
			if c > 0 {
				s.Buckets = append(s.Buckets, [2]uint64{uint64(i), c})
			}
		}
	}
	return s
}

// syncHist is a histogram safe for concurrent use.
type syncHist struct {
	mu sync.Mutex
	h  hist
}

func (s *syncHist) record(d time.Duration) {
	s.mu.Lock()
	s.h.record(int64(d))
	s.mu.Unlock()
}

func (s *syncHist) snapshot() *hist {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.h
	return &c
}
