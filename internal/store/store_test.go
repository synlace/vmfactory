package store

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/synlace/vmfactory/internal/events"
)

func testInput(prov string, cached []bool) RunInput {
	in := RunInput{
		URL:         "https://github.com/example/repo",
		SHA:         "9c3db579ee73",
		ContentHash: "abc123def4567890",
		SpecJSON:    `{"deliverable":"cli"}`,
		Provenance:  prov,
		ExitCode:    0,
	}
	methods := []string{"prebuilt", "compose", "build"}
	verdicts := []string{"blocked", "skipped", "plan"}
	for i, m := range methods {
		in.Candidates = append(in.Candidates, Candidate{
			Method: m, Verdict: verdicts[i], Why: "why-" + m,
			ApproachJSON: `{"kind":"x"}`, CacheHit: cached[i],
		})
	}
	em := events.NewEmitter()
	e1 := em.Emit("stage", "", "resolve", nil)
	e2 := em.Emit("lane.plan", "build", map[string]string{"k": "v"}, nil)
	in.Events = []events.Envelope{e1, e2}
	em.Unsubscribe(0)
	// Drain the closed subscriber side cleanly: the envelopes above
	// were collected before subscription, which is fine for the test.
	return in
}

func TestRecordAndReadBack(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "vmfactory.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer s.Close()

	runID, err := s.RecordRun(testInput("mixed", []bool{true, false, false}))
	if err != nil {
		t.Fatalf("record: %v", err)
	}
	info, err := s.RunProvenance(runID)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if info.Provenance != "mixed" || info.ContentHash != "abc123def4567890" {
		t.Fatalf("info: %+v", info)
	}
	if len(info.Candidates) != 3 {
		t.Fatalf("candidates: %d", len(info.Candidates))
	}
	if !info.Candidates[0].CacheHit || info.Candidates[1].CacheHit {
		t.Fatalf("cache flags: %+v", info.Candidates)
	}
	if info.Candidates[2].Verdict != "plan" ||
		info.Candidates[2].ApproachJSON != `{"kind":"x"}` {
		t.Fatalf("plan candidate: %+v", info.Candidates[2])
	}
}

func TestProvenanceClasses(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "vmfactory.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	// All cached: cached_replay. Some cached: mixed. None: fresh.
	id1, _ := s.RecordRun(testInput("cached_replay", []bool{true, true, true}))
	id2, _ := s.RecordRun(testInput("fresh", []bool{false, false, false}))
	i1, _ := s.RunProvenance(id1)
	i2, _ := s.RunProvenance(id2)
	if i1.Provenance != "cached_replay" || i2.Provenance != "fresh" {
		t.Fatalf("classes: %s / %s", i1.Provenance, i2.Provenance)
	}
}

func TestEventsAppendOnly(t *testing.T) {
	s, err := Open(filepath.Join(t.TempDir(), "vmfactory.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	in := testInput("fresh", []bool{false, false, false})
	runID, err := s.RecordRun(in)
	if err != nil {
		t.Fatal(err)
	}
	var n, seqMax int
	row := s.db.QueryRow(
		`SELECT COUNT(*), MAX(seq) FROM events WHERE run_id = ?`, runID)
	if err := row.Scan(&n, &seqMax); err != nil {
		t.Fatal(err)
	}
	if n != 2 || seqMax < 2 {
		t.Fatalf("events: n=%d seqMax=%d", n, seqMax)
	}
	// Timestamps parse as RFC3339Nano.
	info, _ := s.RunProvenance(runID)
	if info.Created == "" {
		t.Fatal("created missing")
	}
	if _, err := time.Parse(time.RFC3339, info.Created); err != nil {
		t.Fatalf("created format: %v", err)
	}
}

func TestDefaultPathEnv(t *testing.T) {
	t.Setenv("VMF_DB", "/tmp/vmf-test.db")
	if DefaultPath() != "/tmp/vmf-test.db" {
		t.Fatal("VMF_DB override")
	}
}
