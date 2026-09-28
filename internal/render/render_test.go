package render

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/generate"
	"github.com/synlace/vmfactory/internal/plan"
	"github.com/synlace/vmfactory/internal/target"
)

func testOutcome(t *testing.T) *plan.Outcome {
	t.Helper()
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "package.json"), []byte(`{"name":"x"}`), 0o644)
	direct := map[string]any{
		"command": []string{"sleep", "100000000"},
		"user":    "app",
		"checks": []any{map[string]any{
			"cmd": map[string]any{"bin": "app",
				"probes": []string{"app --version"}}}},
	}
	res := &generate.Result{
		Plans: map[string]map[string]any{
			"pkg": {"kind": "install_script", "method": "pkg",
				"direct": direct, "notes": "serves"}},
		Blocked: map[string]string{
			"prebuilt": "no official image ref in evidence",
			"compose":  "no standalone compose file"},
		Skipped:    map[string]string{"build": "no root Dockerfile"},
		Transients: map[string]string{},
		Cached:     map[string]bool{"pkg": true},
	}
	sp := map[string]any{"version": 1, "intent": "web on 3100",
		"deliverable":  "web",
		"serve":        map[string]any{"proto": "http", "port": 3100, "path": "/"},
		"auth":         map[string]any{"required": true, "note": "per intent"},
		"env_required": []string{"PORT"},
		"user":         "non-root", "hold": 25, "why": "serving"}
	return &plan.Outcome{
		Target: &target.Target{URL: "https://github.com/x/y", SHA: "abc123"},
		Spec:   &sp,
		Approaches: []map[string]any{
			{"kind": "install_script", "cost": "slow", "method": "pkg",
				"direct": direct, "notes": "serves"},
		},
		Blocked: res.Blocked, Result: res,
		Found: []string{"package.json"},
	}
}

// The styled surface keeps the mock's contract: same traced lines,
// ✗ blocked rows, floor/model split, dry-run footer, colors only as
// tags.
func TestStyledKeepsContract(t *testing.T) {
	o := testOutcome(t)
	s := Styled(o)
	plain := plan.RenderPlain(o)
	// Every plain fact line still appears (modulo the blocked line,
	// which lifts into ✗ rows).
	for _, want := range []string{
		"spec    deliverable",
		"checks", "hold", "verdict   winner needs",
		"dry run — nothing booted",
	} {
		if !strings.Contains(s, want) {
			t.Errorf("styled lacks %q", want)
		}
	}
	if !strings.Contains(s, "✗ prebuilt") || !strings.Contains(s, "✗ compose") {
		t.Errorf("blocked lanes must be ✗ rows: %q", s)
	}
	if strings.Contains(s, "blocked: prebuilt") {
		t.Errorf("the compact blocked line must lift into rows")
	}
	// ANSI tags present.
	for _, c := range []string{red, yellow, magenta, green, dim} {
		if !strings.Contains(s, c) {
			t.Errorf("styled missing an ANSI tag: %q", c)
		}
	}
	if plain == "" {
		t.Fatal("plain render empty")
	}
}

func TestStyledMatchesPlainFacts(t *testing.T) {
	o := testOutcome(t)
	p := plan.RenderPlain(o)
	s := Styled(o)
	// The traced facts survive styling: strip every ANSI sequence and
	// every plain line (except the lifted blocked line) must still appear.
	stripped := ansiRe.ReplaceAllString(s, "")
	for _, line := range strings.Split(strings.TrimRight(p, "\n"), "\n") {
		if strings.HasPrefix(line, "        blocked: ") {
			continue
		}
		core := line
		if !strings.Contains(stripped, strings.TrimSpace(core)) {
			t.Errorf("styled lost %q", core)
		}
	}
}

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*m`)

func TestProgressVocabulary(t *testing.T) {
	em := events.NewEmitter()
	id, ch := em.Subscribe()
	em.Emit("grounding", "", nil, nil)
	em.Emit("lane.plan", "", map[string]any{"method": "build", "cached": true}, nil)
	em.Emit("lane.blocked", "", map[string]any{"method": "compose",
		"why": "no standalone compose file"}, nil)
	em.Emit("lane.skipped", "", map[string]any{"method": "source",
		"why": "no build manifest"}, nil)
	em.Unsubscribe(id)

	var got []string
	for e := range ch {
		got = append(got, progressLine(e))
	}
	want := []string{
		"grounding",
		"lane build → plan (cached)",
		"lane compose → blocked — no standalone compose file",
		"lane source → skipped — no build manifest",
	}
	if len(got) != len(want) {
		t.Fatalf("lines: %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("line %d: %q, want %q", i, got[i], want[i])
		}
	}
}

func TestIsTTYUnderTest(t *testing.T) {
	// `go test` pipes stdout: not a terminal — the styled path stays
	// off by default in CI and pipes.
	if IsTTY() {
		t.Log("stdout is a char device in this environment")
	}
}
