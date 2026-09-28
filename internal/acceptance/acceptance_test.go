package acceptance

import (
	"bytes"
	"strings"
	"testing"

	"github.com/synlace/vmfactory/internal/events"
)

const inline = `version: 1
recorded: 2026-09-27
reference_commit: 607f9f7
freeze_commit: 0f751be
prompt_v: "14"
rows:
  - id: one
    scenario: minimal CLI target
    url: https://github.com/example/one
    sha: 9c3db579ee73
    intent: null
    expect:
      spec: null
      exit: 0
      min_runnable: 1
      blocked_contains:
        - {method: prebuilt, reason_contains: "official image"}
  - id: two
    scenario: explicit web intent
    url: https://github.com/example/two
    sha: 0f14d261233c
    intent: "serve web on 3100"
    expect:
      spec:
        deliverable: web
        serve_port: 3100
        user: non-root
  - id: replay
    scenario: cached replay
    url: https://github.com/example/two
    sha: 0f14d261233c
    intent: "serve web on 3100"
    repeat_of: two
    expect:
      fresh_model_calls: 0
`

func TestLoadParsesFixture(t *testing.T) {
	fx, err := Load(strings.NewReader(inline))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if fx.Version != 1 || fx.PromptV != "14" || fx.FreezeCommit != "0f751be" {
		t.Fatalf("header: %+v", fx)
	}
	if len(fx.Rows) != 3 {
		t.Fatalf("rows: %d", len(fx.Rows))
	}
	r := fx.Rows[0]
	if r.ID != "one" || r.SHA != "9c3db579ee73" {
		t.Fatalf("row one: %+v", r)
	}
	if r.Intent != "" {
		t.Fatalf("null intent must load as empty, got %q", r.Intent)
	}
	if exp := r.Expect; exp["exit"] != 0 || exp["min_runnable"] != 1 {
		t.Fatalf("expect: %+v", exp)
	}
	if bc, ok := r.Expect["blocked_contains"].([]any); !ok || len(bc) != 1 {
		t.Fatalf("blocked_contains: %+v", r.Expect["blocked_contains"])
	}
	if fx.Rows[2].RepeatOf != "two" {
		t.Fatalf("repeat_of: %+v", fx.Rows[2])
	}
}

func TestLoadRejectsBadFixtures(t *testing.T) {
	if _, err := Load(strings.NewReader("version: 2\nrows: []\n")); err == nil {
		t.Fatal("version 2 must reject")
	}
	if _, err := Load(strings.NewReader("version: 1\nrows: []\n")); err == nil {
		t.Fatal("no rows must reject")
	}
	dup := inline + "  - id: one\n    url: https://github.com/example/x\n    expect: {}\n"
	if _, err := Load(strings.NewReader(dup)); err == nil {
		t.Fatal("duplicate id must reject")
	}
}

func TestGradePendingReport(t *testing.T) {
	fx, err := Load(strings.NewReader(inline))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	em := events.NewEmitter()
	id, ch := em.Subscribe()
	var out bytes.Buffer
	code := Grade(fx, em, &out)
	if code != 0 {
		t.Fatalf("pending grade must exit 0, got %d", code)
	}
	lines := strings.Split(strings.TrimRight(out.String(), "\n"), "\n")
	if len(lines) != 4 {
		t.Fatalf("report lines: %q", lines)
	}
	for i, row := range fx.Rows {
		want := "row " + row.ID + ": pending"
		if !strings.HasPrefix(lines[i], want) {
			t.Fatalf("line %d: %q, want prefix %q", i, lines[i], want)
		}
	}
	if lines[3] != "parity: 0 ok, 0 failed, 3 pending" {
		t.Fatalf("parity line: %q", lines[3])
	}
	// The grader emits through the envelope (ADR-0002): the renderer
	// consumes the same stream a future UI would. Grade emitted
	// synchronously, so the events sit in the buffer already.
	n := 0
	for {
		select {
		case <-ch:
			n++
			if n > 100 {
				t.Fatal("emitter leaked events")
			}
			continue
		default:
		}
		break
	}
	if n != len(fx.Rows)+1 { // one per row + grade.done
		t.Fatalf("events: %d, want %d", n, len(fx.Rows)+1)
	}
	em.Unsubscribe(id)
}
