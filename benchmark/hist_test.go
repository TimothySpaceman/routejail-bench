package main

import (
	"math/rand"
	"sort"
	"testing"
)

func TestBucketRoundTrip(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	for i := 0; i < 200000; i++ {
		v := uint64(r.Int63n(1 << uint(1+r.Intn(62))))
		idx := idxOf(v)
		if idx < 0 || idx >= histSlots {
			t.Fatalf("idx %d out of range for %d", idx, v)
		}
		low, width := bucketRange(idx)
		if v < low || v >= low+width {
			t.Fatalf("v=%d not in bucket %d [%d,%d)", v, idx, low, low+width)
		}
		if v >= 128 && float64(width)/float64(low) > 1.0/63 {
			t.Fatalf("bucket too wide at v=%d: width %d low %d", v, width, low)
		}
	}
}

func TestQuantilesAndMerge(t *testing.T) {
	r := rand.New(rand.NewSource(2))
	var a, b, all hist
	var vals []int64
	for i := 0; i < 100000; i++ {
		v := int64(100_000 + r.ExpFloat64()*400_000) // ~0.1 ms + exponential tail, in ns
		vals = append(vals, v)
		if i%2 == 0 {
			a.record(v)
		} else {
			b.record(v)
		}
		all.record(v)
	}
	a.merge(&b)
	sort.Slice(vals, func(i, j int) bool { return vals[i] < vals[j] })
	for _, q := range []float64{0.5, 0.9, 0.99, 0.999} {
		exact := float64(vals[int(q*float64(len(vals)))-1])
		for name, h := range map[string]*hist{"merged": &a, "single": &all} {
			got := float64(h.quantile(q))
			if d := (got - exact) / exact; d > 0.02 || d < -0.02 {
				t.Errorf("%s q=%v: got %v exact %v (%.2f%% off)", name, q, got, exact, d*100)
			}
		}
	}
	if a.n != all.n || a.max != all.max || a.min != all.min {
		t.Errorf("merge lost data: n %d/%d max %d/%d", a.n, all.n, a.max, all.max)
	}
}
