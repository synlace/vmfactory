package model

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeRunner forces every failure mode without bash or network.
type fakeRunner struct {
	rc     int
	stdout string
	stderr string
	err    error
	args   [][]string
}

func (f *fakeRunner) Run(_ context.Context, name string, args []string,
	stdin string, timeout time.Duration) (int, string, string, error) {
	f.args = append(f.args, args)
	_ = name
	_ = stdin
	_ = timeout
	if f.err != nil {
		return 99, f.stdout, f.stderr, f.err
	}
	return f.rc, f.stdout, f.stderr, nil
}

func TestLLMCallSuccess(t *testing.T) {
	fr := &fakeRunner{rc: 0, stdout: "{\"status\": \"plan\"}\n"}
	s := Seam{Runner: fr}
	got, err := s.LLMCall(context.Background(), "gapfill", "prompt body", 0)
	if err != nil || got != "{\"status\": \"plan\"}\n" {
		t.Fatalf("got %q err %v", got, err)
	}
	// The seam's exec shape is the measured one: bash <scripts>/llm.sh
	// --role R - with the prompt on stdin (E2BIG ceiling). The dir is
	// resolved (VMF_SCRIPTS_DIR or repo-root walk-up); assert the tail.
	if len(fr.args) != 1 {
		t.Fatalf("one call expected: %v", fr.args)
	}
	argv := fr.args[0]
	if !strings.HasSuffix(argv[0], "llm.sh") ||
		strings.Join(argv[1:], " ") != "--role gapfill -" {
		t.Fatalf("args: %v", argv)
	}
}

func TestLLMCallDegradesTyped(t *testing.T) {
	cases := []struct {
		rc     int
		stderr string
		want   error
	}{
		{2, "llm.sh: no prompt given", ErrUsage},
		{3, "llm.sh: no API key; set VMF_LLM_API_KEY in ~/.vmf/env", ErrUnconfigured},
	}
	for _, c := range cases {
		s := Seam{Runner: &fakeRunner{rc: c.rc, stderr: c.stderr + "\n"}}
		_, err := s.LLMCall(context.Background(), "intent", "p", 0)
		if !errors.Is(err, c.want) {
			t.Fatalf("rc %d: got %v, want %v", c.rc, err, c.want)
		}
		if !strings.Contains(err.Error(), c.stderr) {
			t.Fatalf("stderr not carried: %v", err)
		}
	}
}

func TestLLMCallRunnerTimeout(t *testing.T) {
	s := Seam{Runner: &fakeRunner{err: ErrTimeout}}
	_, err := s.LLMCall(context.Background(), "gapfill", "p", 0)
	if !errors.Is(err, ErrTimeout) {
		t.Fatalf("got %v, want ErrTimeout", err)
	}
}

// The real exec runner, driven against fake scripts in a temp dir:
// proves the bash + stdin + exit-code wiring end to end, no network.
func TestExecRunnerAgainstFakeScripts(t *testing.T) {
	dir := t.TempDir()
	write := func(name, body string) {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	write("llm.sh", "#!/usr/bin/env bash\ncat\n")
	write("context7.sh", "#!/usr/bin/env bash\nexit 4\n")
	s := Seam{ScriptsDir: dir}

	got, err := s.LLMCall(context.Background(), "intent", "echo me", 10*time.Second)
	if err != nil || got != "echo me" {
		t.Fatalf("llm.sh stdin roundtrip: %q err %v", got, err)
	}

	if d := s.C7Docs(context.Background(), "lib", "", 0); d != "" {
		t.Fatalf("failed docs must degrade to empty, got %q", d)
	}
	if hits := s.C7Search(context.Background(), "topic"); hits != nil {
		t.Fatalf("failed search must degrade to nil, got %v", hits)
	}
}

func TestParseLLMJSONShapes(t *testing.T) {
	cases := []struct {
		name, raw string
		wantKey   string
		wantFail  bool
	}{
		{"plain", `{"status":"plan"}`, "status", false},
		{"fenced tagged", "```json\n{\"status\":\"plan\"}\n```", "status", false},
		{"fenced untagged", "```\n{\"status\":\"plan\"}\n```", "status", false},
		{"double encoded", `"{\"status\":\"plan\"}"`, "status", false},
		{"garbage", "not json at all", "", true},
		{"array not object", `[1,2]`, "", true},
	}
	for _, c := range cases {
		m, err := ParseLLMJSON(c.raw)
		if c.wantFail {
			if err == nil {
				t.Fatalf("%s: expected failure", c.name)
			}
			continue
		}
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if _, ok := m[c.wantKey]; !ok {
			t.Fatalf("%s: key %q lost: %v", c.name, c.wantKey, m)
		}
	}
}

func TestParseSearchLines(t *testing.T) {
	out := "{\"id\":\"/a/b\",\"title\":\"B\",\"description\":\"d\",\"updated\":\"2026-01-01\"}\n\n"
	hits, err := parseSearchLines(out)
	if err != nil || len(hits) != 1 || hits[0].ID != "/a/b" {
		t.Fatalf("hits: %+v err %v", hits, err)
	}
	if _, err := parseSearchLines("{broken\n"); err == nil {
		t.Fatal("broken line must error (caller degrades to nil)")
	}
}
