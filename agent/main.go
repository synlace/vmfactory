// vmf-agent: guest-side init supervisor and exec server for vmfactory
// microVMs. Mounted into the guest via virtiofs; runs as KRUN_INIT.
//
// Usage: vmf-agent <config.json>
//   config.json: {"cwd": "...", "env": {"K":"V"}, "argv": ["nginx","-g","..."],
//                 "listen": "0.0.0.0:7777"}
//
// Duties:
//   1. exec the app as a child (PID-1 style: forward signals, reap)
//   2. listen on TSI; per connection read {"argv":[...],"cwd":...},
//      exec it inside the guest, stream stdio both ways, then write
//      a final line "VMF-EXIT <n>" and close.
//
// Static build (no libc dependency): CGO_ENABLED=0 go build.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
)

type config struct {
	Cwd    string            `json:"cwd"`
	Env    map[string]string `json:"env"`
	Argv   []string          `json:"argv"`
	Listen string            `json:"listen"`
}

type request struct {
	Argv []string          `json:"argv"`
	Cwd  string            `json:"cwd"`
	Env  map[string]string `json:"env"`
}

func childEnv(base map[string]string, extra map[string]string) []string {
	env := make(map[string]string, len(base)+len(extra))
	for k, v := range base {
		env[k] = v
	}
	for k, v := range extra {
		env[k] = v
	}
	out := make([]string, 0, len(env))
	for k, v := range env {
		out = append(out, k+"="+v)
	}
	return out
}

// runReq spawns req with stdio wired to conn, so output streams live.
// Stdin is intentionally not connected: TSI forwarded connections do not
// support half-close, and cmd.Run() would block on the stdin copier until
// conn EOF. One-shot execs carry no stdin.
// Returns exit code.
func runReq(req request, baseEnv map[string]string, conn net.Conn) int {
	cmd := exec.Command(req.Argv[0], req.Argv[1:]...)
	if req.Cwd != "" {
		cmd.Dir = req.Cwd
	}
	cmd.Env = childEnv(baseEnv, req.Env)
	cmd.Stdout = conn
	cmd.Stderr = conn
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode()
		}
		fmt.Fprintf(os.Stderr, "vmf-agent: exec %v: %v\n", req.Argv, err)
		return 127
	}
	return 0
}

func serve(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			fmt.Fprintf(os.Stderr, "vmf-agent: accept: %v\n", err)
			return
		}
		fmt.Fprintf(os.Stderr, "vmf-agent: conn accepted\n")
		go func(c net.Conn) {
			defer c.Close()
			line, err := bufio.NewReader(c).ReadString('\n')
			if err != nil {
				fmt.Fprintf(os.Stderr, "vmf-agent: read: %v\n", err)
				return
			}
			fmt.Fprintf(os.Stderr, "vmf-agent: request: %s", line)
			var req request
			if err := json.Unmarshal([]byte(line), &req); err != nil || len(req.Argv) == 0 {
				fmt.Fprintf(c, "vmf-agent: bad request\nVMF-EXIT 2\n")
				return
			}
			code := runReq(req, envMap(os.Environ()), c)
			if _, werr := fmt.Fprintf(c, "VMF-EXIT %d\n", code); werr != nil {
				fmt.Fprintf(os.Stderr, "vmf-agent: write: %v\n", werr)
			} else {
				fmt.Fprintf(os.Stderr, "vmf-agent: response sent: VMF-EXIT %d\n", code)
			}
		}(conn)
	}
}

func envMap(environ []string) map[string]string {
	m := make(map[string]string, len(environ))
	for _, e := range environ {
		if i := strings.IndexByte(e, '='); i > 0 {
			m[e[:i]] = e[i+1:]
		}
	}
	return m
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	if ee, ok := err.(*exec.ExitError); ok && ee.ProcessState != nil {
		if ws, ok2 := ee.ProcessState.Sys().(syscall.WaitStatus); ok2 && ws.Signaled() {
			return 128 + int(ws.Signal())
		}
		return ee.ExitCode()
	}
	return 1
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: vmf-agent <config.json>")
		os.Exit(2)
	}
	raw, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, "vmf-agent:", err)
		os.Exit(2)
	}
	var cfg config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		fmt.Fprintln(os.Stderr, "vmf-agent: bad config:", err)
		os.Exit(2)
	}
	if len(cfg.Argv) == 0 {
		fmt.Fprintln(os.Stderr, "vmf-agent: empty argv")
		os.Exit(2)
	}

	// Start the application as our child; we are its PID 1.
	app := exec.Command(cfg.Argv[0], cfg.Argv[1:]...)
	app.Dir = cfg.Cwd
	app.Env = childEnv(cfg.Env, nil)
	app.Stdin = os.Stdin
	app.Stdout = os.Stdout
	app.Stderr = os.Stderr
	if err := app.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "vmf-agent: app:", err)
		os.Exit(127)
	}

	if cfg.Listen != "" {
		if l, lerr := net.Listen("tcp", cfg.Listen); lerr == nil {
			go serve(l)
		} else {
			fmt.Fprintf(os.Stderr, "vmf-agent: listen %s failed: %v\n", cfg.Listen, lerr)
		}
	}

	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs)
	go func() {
		for s := range sigs {
			app.Process.Signal(s)
		}
	}()
	st := app.Wait()
	os.Exit(exitCode(st))
}