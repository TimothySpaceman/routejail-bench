package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"
)

func runServer(args []string) error {
	fs := flag.NewFlagSet("server", flag.ContinueOnError)
	listen := fs.String("listen", ":9000", "comma-separated listen addresses, e.g. :9001,:9002 or 10.0.0.5:9000")
	procs := fs.Int("procs", 0, "GOMAXPROCS (0 = Go default)")
	if err := fs.Parse(args); err != nil {
		return &exitErr{2, ""}
	}
	if *procs > 0 {
		runtime.GOMAXPROCS(*procs)
	}
	var lns []net.Listener
	for _, a := range strings.Split(*listen, ",") {
		a = strings.TrimSpace(a)
		if a == "" {
			continue
		}
		ln, err := net.Listen("tcp", a)
		if err != nil {
			return &exitErr{2, err.Error()}
		}
		lns = append(lns, ln)
		fmt.Fprintf(os.Stderr, "netbench server listening on %s\n", ln.Addr())
		go func() {
			for {
				c, err := ln.Accept()
				if err != nil {
					return
				}
				go serveConn(c)
			}
		}()
	}
	if len(lns) == 0 {
		return usageErr("no listen address")
	}
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	for _, ln := range lns {
		ln.Close()
	}
	return nil
}

func serveConn(c net.Conn) {
	defer c.Close()
	if tc, ok := c.(*net.TCPConn); ok {
		tc.SetNoDelay(true)
	}
	c.SetReadDeadline(time.Now().Add(10 * time.Second))
	h, err := readHeader(c)
	if err != nil {
		return
	}
	c.SetReadDeadline(time.Time{})
	switch h.mode {
	case modeEcho:
		serveEcho(c, h)
	case modeSink:
		serveSink(c)
	case modeSource:
		serveSource(c, h)
	}
}

// echo: read a message, write it back. With flagCloseAfterOne the server closes first,
// which keeps TIME_WAIT state on the server side during high connection-rate tests.
func serveEcho(c net.Conn, h header) {
	buf := make([]byte, h.size)
	for {
		if _, err := io.ReadFull(c, buf); err != nil {
			return
		}
		if _, err := c.Write(buf); err != nil {
			return
		}
		if h.flags&flagCloseAfterOne != 0 {
			return
		}
	}
}

// sink: discard everything until the client half-closes, then report the byte total.
func serveSink(c net.Conn) {
	buf := make([]byte, 256<<10)
	var total uint64
	for {
		n, err := c.Read(buf)
		total += uint64(n)
		if err != nil {
			break
		}
	}
	var out [8]byte
	binary.BigEndian.PutUint64(out[:], total)
	c.Write(out[:])
}

// source: write patterned chunks until the client goes away.
func serveSource(c net.Conn, h header) {
	chunk := make([]byte, h.size)
	fillPattern(chunk)
	for {
		if _, err := c.Write(chunk); err != nil {
			return
		}
	}
}
