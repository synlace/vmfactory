package model

import (
	"context"
	"strings"
	"testing"
	"time"
)

// The grounding formats must stay byte-identical to the reference's
// vmf_llm.ground — the Go port's prompts grade against the reference's.
func TestGroundHappyPath(t *testing.T) {
	// A seam whose search/docs come from a scripted fake runner:
	// search returns one NDJSON hit, docs returns markdown.
	fr := &fakeRunner{}
	s := Seam{Runner: &scriptedRunner{fr: fr}}
	grounded, ids := Ground(context.Background(), s, []string{"uv install"})
	if len(ids) != 1 || ids[0] != "/a/uv [uv install]" {
		t.Fatalf("ids: %v", ids)
	}
	if !strings.Contains(grounded, "=== context7: /a/uv (uv install, updated 2026-01-01) ===") {
		t.Fatalf("block header: %q", grounded)
	}
	if !strings.Contains(grounded, "Grounding - CURRENT documentation fetched") {
		t.Fatalf("header: %q", grounded)
	}
}

func TestGroundDegradesSilently(t *testing.T) {
	s := Seam{Runner: &fakeRunner{rc: 4, stderr: "context7: down"}}
	grounded, ids := Ground(context.Background(), s, []string{"any topic"})
	if ids != nil {
		t.Fatalf("ids: %v", ids)
	}
	if !strings.Contains(grounded, "Grounding: context7 unavailable for this run; state facts conservatively and prefer the evidence below.") {
		t.Fatalf("conservative note: %q", grounded)
	}
	if GroundingNote(nil) != "NOT grounded (context7 unavailable)" {
		t.Fatal("note: empty case")
	}
	if GroundingNote([]string{"a [t]", "b [u]"}) != "grounded via context7: a [t], b [u]" {
		t.Fatal("note: grounded case")
	}
}

func TestGroundCapsTopicsAndDocs(t *testing.T) {
	fr := &fakeRunner{}
	s := Seam{Runner: &scriptedRunner{fr: fr, bigDocs: true}}
	grounded, ids := Ground(context.Background(), s,
		[]string{"t1", "t2", "t3", "t4", "t5"})
	if len(ids) != 3 {
		t.Fatalf("grounding must cap at 3 topics, got %d", len(ids))
	}
	if !strings.Contains(grounded, "GROUNDING_MARKER") {
		t.Fatalf("docs cap not applied: %d", len(grounded))
	}
	if strings.Contains(grounded, strings.Repeat("x", 3000)) {
		t.Fatalf("docs not truncated: %d", len(grounded))
	}
}

func TestGroundTriesFirstThreeTopicsOnly(t *testing.T) {
	fr := &fakeRunner{rc: 4, stderr: "down"}
	s := Seam{Runner: fr}
	// Five failing topics: the seam must be asked for exactly the
	// first three (search fails, docs never runs).
	_, ids := Ground(context.Background(), s,
		[]string{"t1", "t2", "t3", "t4", "t5"})
	if ids != nil {
		t.Fatalf("ids: %v", ids)
	}
	if len(fr.args) != 3 {
		t.Fatalf("seam calls: %d, want 3", len(fr.args))
	}
}

// scriptedRunner answers search/doc calls by shape of the args.
type scriptedRunner struct {
	fr      *fakeRunner
	bigDocs bool
}

func (s *scriptedRunner) Run(_ context.Context, _ string, args []string,
	_ string, _ time.Duration) (int, string, string, error) {
	if len(args) > 0 && strings.Contains(args[0], "context7.sh") {
		if len(args) > 1 && args[1] == "search" {
			return 0, "{\"id\":\"/a/uv\",\"title\":\"uv\",\"description\":\"d\",\"updated\":\"2026-01-01\"}\n", "", nil
		}
		if len(args) > 1 && args[1] == "docs" {
			if s.bigDocs {
				// Marker sits inside the first 3000 chars; the
				// tail proves the cap truncated.
				return 0, strings.Repeat("x", 2500) + "GROUNDING_MARKER" + strings.Repeat("x", 9000), "", nil
			}
			return 0, "doc body", "", nil
		}
	}
	return s.fr.rc, s.fr.stdout, s.fr.stderr, s.fr.err
}
