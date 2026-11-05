package main

// Wire protocol (all integers big-endian).
//
// Every connection starts with an 8-byte header:
//
//	[0] mode   'E' echo | 'S' sink | 'R' source
//	[1] version (1)
//	[2] flags  bit0: echo only, server closes right after the first reply
//	[3] reserved
//	[4:8] size  echo: message size; sink/source: chunk size
//
// echo:   client sends messages of exactly `size` bytes, server returns each unchanged.
// sink:   client sends bytes until it half-closes; server replies with an 8-byte total it received.
// source: server sends `size`-byte chunks until the client closes.
//
// Echo messages start with an 8-byte sequence number, followed by a fixed byte pattern.
// Sink/source chunks are a fixed byte pattern; the stream offset modulo chunk size selects the byte.

import (
	"encoding/binary"
	"errors"
	"io"
)

const (
	modeEcho   byte = 'E'
	modeSink   byte = 'S'
	modeSource byte = 'R'

	protoVersion = 1
	headerLen    = 8
	maxSize      = 16 << 20

	flagCloseAfterOne byte = 1 << 0

	patMod = 251 // prime, so the pattern does not line up with power-of-two buffers
)

type header struct {
	mode  byte
	flags byte
	size  uint32
}

func (h header) put(b []byte) {
	b[0] = h.mode
	b[1] = protoVersion
	b[2] = h.flags
	b[3] = 0
	binary.BigEndian.PutUint32(b[4:8], h.size)
}

func readHeader(r io.Reader) (header, error) {
	var b [headerLen]byte
	if _, err := io.ReadFull(r, b[:]); err != nil {
		return header{}, err
	}
	if b[1] != protoVersion {
		return header{}, errors.New("unsupported protocol version")
	}
	h := header{mode: b[0], flags: b[2], size: binary.BigEndian.Uint32(b[4:8])}
	if h.size == 0 || h.size > maxSize {
		return header{}, errors.New("bad size in header")
	}
	switch h.mode {
	case modeEcho, modeSink, modeSource:
		return h, nil
	}
	return header{}, errors.New("unknown mode")
}

// fillPattern writes the fixed pattern: byte i = i mod 251.
func fillPattern(b []byte) {
	for i := range b {
		b[i] = byte(i % patMod)
	}
}

// setSeq stamps the sequence number into an echo message whose pattern is already filled.
func setSeq(msg []byte, seq uint64) { binary.BigEndian.PutUint64(msg[:8], seq) }

// checkMsg verifies the sequence number and two sample bytes of an echoed message.
func checkMsg(msg []byte, seq uint64) bool {
	if len(msg) < 16 || binary.BigEndian.Uint64(msg[:8]) != seq {
		return false
	}
	last := len(msg) - 1
	mid := len(msg) / 2
	return msg[last] == byte(last%patMod) && msg[mid] == byte(mid%patMod)
}

// checkStream verifies three sample bytes of a read buffer that starts at stream offset off.
func checkStream(buf []byte, off uint64, chunk int) bool {
	n := len(buf)
	if n == 0 {
		return true
	}
	for _, i := range [3]int{0, n / 2, n - 1} {
		want := byte(((off + uint64(i)) % uint64(chunk)) % patMod)
		if buf[i] != want {
			return false
		}
	}
	return true
}
