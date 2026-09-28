// Package acceptance is the port's grading harness over
// fixtures/acceptance.yaml, the recorded baseline (docs/handover/
// ACCEPTANCE.md; authority rule in docs/adr/0001). The report shape
// matches the reference grader (scripts/grade_reference.py) so the
// parity diff joins the two outputs line for line.
//
// This is the skeleton grade: the planner has not landed, so every
// row reports pending honestly. Rows report PASS/FAIL only when the
// planner exists (the generate/validate slices); the harness itself
// exits 0 while pending, and non-zero when a graded row fails.
package acceptance

import (
	"fmt"
	"io"

	"github.com/synlace/vmfactory/internal/events"
	"gopkg.in/yaml.v3"
)

// Fixture is the parsed fixtures/acceptance.yaml.
type Fixture struct {
	Version         int    `yaml:"version"`
	Recorded        string `yaml:"recorded"`
	ReferenceCommit string `yaml:"reference_commit"`
	FreezeCommit    string `yaml:"freeze_commit"`
	PromptV         string `yaml:"prompt_v"`
	Rows            []Row  `yaml:"rows"`
}

// Row is one recorded acceptance row.
type Row struct {
	ID       string         `yaml:"id"`
	Scenario string         `yaml:"scenario"`
	URL      string         `yaml:"url"`
	SHA      string         `yaml:"sha"`
	Intent   string         `yaml:"intent"`
	RepeatOf string         `yaml:"repeat_of"`
	Expect   map[string]any `yaml:"expect"`
}

// Load parses and sanity-checks the fixture document.
func Load(r io.Reader) (*Fixture, error) {
	var fx Fixture
	dec := yaml.NewDecoder(r)
	if err := dec.Decode(&fx); err != nil {
		return nil, fmt.Errorf("fixtures: %w", err)
	}
	if fx.Version != 1 {
		return nil, fmt.Errorf("fixtures: unsupported version %d", fx.Version)
	}
	if len(fx.Rows) == 0 {
		return nil, fmt.Errorf("fixtures: no rows")
	}
	seen := map[string]bool{}
	for _, row := range fx.Rows {
		if row.ID == "" || row.URL == "" {
			return nil, fmt.Errorf("fixtures: row needs id and url")
		}
		if seen[row.ID] {
			return nil, fmt.Errorf("fixtures: duplicate row id %q", row.ID)
		}
		seen[row.ID] = true
	}
	return &fx, nil
}

// RowStatus is a row's grading state.
type RowStatus string

const (
	StatusPending RowStatus = "pending"
	StatusPass    RowStatus = "PASS"
	StatusFail    RowStatus = "FAIL"
)

// Grade runs the rows against the port surface, emits one event per
// row plus a grade.done summary, and writes the parity report. It
// returns the CLI exit code: 0 while the harness is healthy (pending
// rows are honest absences, not failures), 1 when a graded row fails.
func Grade(fx *Fixture, em *events.Emitter, out io.Writer) int {
	ok, failed, pending := 0, 0, 0
	for _, row := range fx.Rows {
		// The planner surface does not exist yet; a pending row is
		// the honest report, not a placeholder failure.
		em.Emit("row.pending", "", map[string]any{
			"id":       row.ID,
			"scenario": row.Scenario,
			"sha":      row.SHA,
		}, nil)
		fmt.Fprintf(out, "row %s: pending (%s)\n", row.ID, row.Scenario)
		pending++
	}
	em.Emit("grade.done", "", map[string]any{
		"rows": len(fx.Rows),
	}, map[string]float64{
		"ok": float64(ok), "failed": float64(failed), "pending": float64(pending),
	})
	fmt.Fprintf(out, "parity: %d ok, %d failed, %d pending\n", ok, failed, pending)
	if failed > 0 {
		return 1
	}
	return 0
}
