// Package model is the LLM seam: the port's single touchpoint for the
// model and the doc-grounding transport (ADR-0003: seam-first —
// internal/model calls the measured bash scripts, scripts/llm.sh and
// scripts/context7.sh, before any HTTP client exists in Go).
//
// The seam's essence is degrade-as-code: the model is an accelerator,
// never a dependency. Every failure mode is a typed error or an empty
// result the caller can honestly report, mirroring the reference's
// vmf_llm.py.
package model

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"
)

// Typed degrade errors. Exit codes follow the measured scripts:
// llm.sh exits 2 on usage, 3 on unconfigured/transport; context7.sh
// exits 4 on transport; the runner maps a deadline to Timeout.
var (
	ErrUsage        = errors.New("seam: usage error (exit 2)")
	ErrUnconfigured = errors.New("seam: unconfigured or transport failure (exit 3)")
	ErrC7Failed     = errors.New("seam: context7 failure (exit 4)")
	ErrTimeout      = errors.New("seam: deadline exceeded")
)

// SearchHit is one context7 search result line
// ({"id","title","description","updated"}).
type SearchHit struct {
	ID          string `json:"id"`
	Title       string `json:"title"`
	Description string `json:"description"`
	Updated     string `json:"updated"`
}

// Runner executes one seam command. It exists so tests force every
// failure mode without bash or network.
type Runner interface {
	Run(ctx context.Context, name string, args []string, stdin string,
		timeout time.Duration) (rc int, stdout, stderr string, err error)
}

// ExecRunner runs real processes: bash <script> plus args, prompt on
// stdin (the measured "-" contract: grounded prompts exceed the per-arg
// exec limit).
type ExecRunner struct{}

// Run implements Runner.
func (ExecRunner) Run(ctx context.Context, name string, args []string,
	stdin string, timeout time.Duration) (int, string, string, error) {
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(cctx, name, args...)
	cmd.Stdin = bytes.NewBufferString(stdin)
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errb
	err := cmd.Run()
	rc := 0
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			rc = ee.ExitCode()
		} else {
			if cctx.Err() == context.DeadlineExceeded {
				return 99, out.String(), errb.String(), ErrTimeout
			}
			return 99, out.String(), errb.String(), err
		}
	}
	return rc, out.String(), errb.String(), nil
}

// Seam is the transport over the measured scripts. ScriptsDir resolves
// per call: an explicit dir wins, then VMF_SCRIPTS_DIR, else the repo
// root found by walking up from the working directory (the binary may
// run from any depth of the monorepo).
type Seam struct {
	ScriptsDir string
	Runner     Runner
}

func (s Seam) scripts() string {
	if s.ScriptsDir != "" {
		return s.ScriptsDir
	}
	if d := os.Getenv("VMF_SCRIPTS_DIR"); d != "" {
		return d
	}
	if root, ok := findRepoRoot(); ok {
		return root + "/scripts"
	}
	return "scripts"
}

// findRepoRoot walks up from cwd until a directory contains
// scripts/llm.sh — the measured seam's anchor file.
func findRepoRoot() (string, bool) {
	wd, err := os.Getwd()
	if err != nil {
		return "", false
	}
	for {
		if _, err := os.Stat(wd + "/scripts/llm.sh"); err == nil {
			return wd, true
		}
		parent := filepath.Dir(wd)
		if parent == wd {
			return "", false
		}
		wd = parent
	}
}

func (s Seam) runner() Runner {
	if s.Runner != nil {
		return s.Runner
	}
	return ExecRunner{}
}

// DefaultTimeout mirrors llm.sh's role-aware curl ceilings: planning
// calls carry repo evidence and doc excerpts, so they get the wide
// window; the caller may still tighten per call.
func DefaultTimeout(role string) time.Duration {
	switch role {
	case "gapfill":
		return 420 * time.Second
	case "agent":
		return 220 * time.Second
	default:
		return 60 * time.Second
	}
}

// LLMCall runs llm.sh --role <role> - with the prompt on stdin and
// returns the assistant text. Degrades: ErrUsage (exit 2),
// ErrUnconfigured (exit 3 — llm.sh exits 3 for both unconfigured and
// transport failures, with the why on stderr), ErrTimeout.
func (s Seam) LLMCall(ctx context.Context, role, prompt string,
	timeout time.Duration) (string, error) {
	rc, out, errb, err := s.runner().Run(ctx, "bash",
		[]string{s.scripts() + "/llm.sh", "--role", role, "-"},
		prompt, timeout)
	if err != nil {
		return "", err
	}
	switch rc {
	case 0:
		return out, nil
	case 2:
		return "", fmt.Errorf("%w: %s", ErrUsage, firstLine(errb))
	case 3:
		return "", fmt.Errorf("%w: %s", ErrUnconfigured, firstLine(errb))
	default:
		return "", fmt.Errorf("seam: llm.sh exit %d: %s", rc, firstLine(errb))
	}
}

// C7Search runs context7.sh search and returns the parsed hits; a
// dead transport or unparseable output degrades to zero hits (the
// reference's c7_search contract).
func (s Seam) C7Search(ctx context.Context, topic string) []SearchHit {
	rc, out, _, _ := s.runner().Run(ctx, "bash",
		[]string{s.scripts() + "/context7.sh", "search", topic},
		"", 30*time.Second)
	if rc != 0 {
		return nil
	}
	hits, err := parseSearchLines(out)
	if err != nil {
		return nil
	}
	return hits
}

// C7Docs runs context7.sh docs and returns the markdown; a dead
// transport or empty body degrades to an empty string (the reference's
// c7_docs contract).
func (s Seam) C7Docs(ctx context.Context, lib, topic string,
	tokens int) string {
	args := []string{s.scripts() + "/context7.sh", "docs", lib}
	if topic != "" {
		args = append(args, topic)
	}
	if tokens > 0 {
		args = append(args, strconv.Itoa(tokens))
	}
	rc, out, _, _ := s.runner().Run(ctx, "bash", args, "", 40*time.Second)
	if rc != 0 || out == "" {
		return ""
	}
	return out
}

func firstLine(s string) string {
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			return s[:i]
		}
	}
	return s
}
