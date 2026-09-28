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
	"context"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/model"
	"github.com/synlace/vmfactory/internal/plan"
	"github.com/synlace/vmfactory/internal/store"
	"github.com/synlace/vmfactory/internal/target"
	"github.com/synlace/vmfactory/internal/validate"
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

// Grade runs the rows against the port surface: each row resolves at
// its pinned SHA (a moving tip re-plans and drifts; the pin reproduces
// the recorded content), the port pipeline plans on paper, and the
// verdict classes assert against the recorded baseline. A cold cache
// spends a bounded planning run and grades anyway; a warm cache
// replays at zero model calls. The report shape matches the reference
// grader (scripts/grade_reference.py) so the parity diff joins the
// two outputs line for line. Exit: 0 when no row failed, 1 otherwise.
func Grade(fx *Fixture, seam model.Seam, out io.Writer) int {
	em := events.NewEmitter()
	go drain(em)
	st, err := store.Open(store.DefaultPath())
	if err != nil {
		fmt.Fprintf(out, "note: store unavailable: %v\n", err)
		st = nil
	} else {
		defer st.Close()
	}
	ok, failed, pending := 0, 0, 0
	results := map[string]rowResult{}
	for _, row := range fx.Rows {
		r := gradeRow(context.Background(), row, results, seam, em, st)
		results[row.ID] = r
		switch {
		case r.pending:
			pending++
			fmt.Fprintf(out, "row %s: pending (%s)\n", row.ID, row.Scenario)
		case r.failed > 0:
			failed++
			fmt.Fprintf(out, "row %s: FAIL (%d checks, wall %.0fs, llm %d)\n",
				row.ID, len(r.checks), r.wall.Seconds(), r.llm)
		default:
			ok++
			fmt.Fprintf(out, "row %s: PASS (%d checks, wall %.0fs, llm %d)\n",
				row.ID, len(r.checks), r.wall.Seconds(), r.llm)
		}
		for _, c := range r.checks {
			mark := "ok "
			if !c.pass {
				mark = "FAIL"
			}
			detail := ""
			if c.detail != "" {
				detail = " — " + c.detail
			}
			fmt.Fprintf(out, "   %s %s%s\n", mark, c.name, detail)
		}
	}
	em.Emit("grade.done", "", map[string]any{"rows": len(fx.Rows)},
		map[string]float64{"ok": float64(ok), "failed": float64(failed),
			"pending": float64(pending)})
	fmt.Fprintf(out, "parity: %d ok, %d failed, %d pending\n", ok, failed, pending)
	if failed > 0 {
		return 1
	}
	return 0
}

func drain(em *events.Emitter) {
	id, ch := em.Subscribe()
	for range ch {
	}
	em.Unsubscribe(id)
}

type check struct {
	name   string
	pass   bool
	detail string
}

type rowResult struct {
	checks  []check
	failed  int
	pending bool
	wall    time.Duration
	llm     int
	outcome *plan.Outcome
}

func (r *rowResult) good(name string, pass bool, detail ...string) {
	c := check{name: name, pass: pass}
	if len(detail) > 0 {
		c.detail = detail[0]
	}
	if !pass {
		r.failed++
	}
	r.checks = append(r.checks, c)
}

func gradeRow(ctx context.Context, row Row, results map[string]rowResult,
	seam model.Seam, em *events.Emitter, st *store.Store) rowResult {
	var r rowResult
	// resolve: pinned checkout of the recorded SHA.
	tgt, err := target.New().Resolve(ctx, row.URL, row.SHA)
	if err != nil {
		r.pending = true
		return r
	}
	r.good("resolve", strings.HasPrefix(tgt.SHA, row.SHA), tgt.SHA[:9])

	intent := strings.TrimSpace(row.Intent)
	o, err := plan.Plan(ctx, seam, tgt.Dir, intent, "", em)
	if err != nil {
		r.good("plan", false, err.Error())
		return r
	}
	r.outcome = o
	if st != nil {
		if runID, err := st.RecordRun(store.FromOutcome(o, o.ExitCode())); err == nil {
			if info, ierr := st.RunProvenance(runID); ierr == nil {
				o.RecordedProvenance = info.Provenance
			}
		}
	}
	r.wall = o.Wall
	r.llm = o.Result.LLMCalls
	em.Emit("row.graded", "", map[string]any{
		"id": row.ID, "llm": r.llm, "wall": r.wall.Seconds()}, nil)

	want := row.Expect
	wantSpec, _ := want["spec"].(map[string]any)
	if wantSpec == nil {
		r.good("spec none", o.Spec == nil, specSummary(o.Spec))
	} else {
		deliv := wantSpec["deliverable"]
		got := ""
		if o.Spec != nil {
			got = stringOf((*o.Spec)["deliverable"])
		}
		r.good("spec deliverable", got == fmt.Sprintf("%v", deliv), got)
		if port := wantSpec["serve_port"]; port != nil {
			gp := 0
			if o.Spec != nil {
				if serve, ok := (*o.Spec)["serve"].(map[string]any); ok {
					gp = intOf(serve["port"])
				}
			}
			r.good("serve port", gp == intOf(port), strconv.Itoa(gp))
		}
		if user := wantSpec["user"]; user != nil {
			gu := ""
			if o.Spec != nil {
				gu = stringOf((*o.Spec)["user"])
			}
			// The class is "not root": the recorded roll said
			// non-root, the vocabulary also allows a named account
			// (the install list must create it either way).
			pass := gu != "" && gu != "root"
			r.good("spec user", pass, gu)
		}
		if b, ok := wantSpec["provenance_fresh"].(bool); ok && b {
			why := ""
			if o.Spec != nil {
				why = stringOf((*o.Spec)["why"])
			}
			r.good("provenance fresh", why != "deterministic fallback", why)
		}
	}
	r.good("exit", o.ExitCode() == intOf(want["exit"]),
		strconv.Itoa(o.ExitCode()))
	if mr := intOf(want["min_runnable"]); mr > 0 {
		r.good("min runnable", len(o.Approaches) >= mr,
			"runnable="+strconv.Itoa(len(o.Approaches)))
	}
	if b, ok := want["runnable_with_cmd_check"].(bool); ok && b {
		r.good("cmd check in runnables", hasCmdCheck(o.Approaches))
	}
	if b, ok := want["runnable_serves_port"].(bool); ok && b {
		port := intOf(wantSpec["serve_port"])
		found := false
		for _, a := range o.Approaches {
			for _, p := range portsOf(a["ports"]) {
				if p == port {
					found = true
				}
			}
		}
		r.good("serve port in runnables", found, "port="+strconv.Itoa(port))
	}
	if b, ok := want["runnable_keepalive_forbidden"].(bool); ok && b {
		bad := []string{}
		for _, a := range o.Approaches {
			if d, ok := a["direct"].(map[string]any); ok {
				if cmd := cmdOf(d["command"]); len(cmd) > 0 && validate.IsKeepalive(cmd) {
					bad = append(bad, stringOf(a["method"]))
				}
			}
		}
		r.good("no keep-alive runnables", len(bad) == 0, strings.Join(bad, ","))
	}
	for _, bc := range blockedContains(want["blocked_contains"]) {
		bm, _ := bc["method"].(string)
		frag, _ := bc["reason_contains"].(string)
		// The class is "not runnable": a durable blocked verdict with
		// a matching reason, or an honest prefilter skip. Which one
		// depends on the checkout state (a nested compose file makes
		// the lane run and block; its absence skips it — see
		// ADR-0005); both keep the lane off the runnable list.
		runnable := false
		for _, a := range o.Approaches {
			if stringOf(a["method"]) == bm {
				runnable = true
			}
		}
		why, blockedHere := o.Blocked[bm]
		skipWhy, skippedHere := o.Result.Skipped[bm]
		pass := !runnable && ((blockedHere && strings.Contains(why, frag)) || skippedHere)
		disp := why
		if skippedHere {
			disp = "skipped: " + skipWhy
		}
		r.good("not-runnable "+bm, pass, disp)
	}
	if row.RepeatOf != "" {
		prev, ok := results[row.RepeatOf]
		if ok {
			r.good("verdicts match repeat",
				verdictsOf(o) == verdictsOf(prev.outcome),
				verdictsOf(o))
			// Replayed candidates cost zero fresh calls; prior
			// transients legitimately retry (the measured row-4
			// semantics: a retry pays the grounding draft once).
			prevT := 0
			if prev.outcome != nil {
				prevT = len(prev.outcome.Result.Transients)
			}
			want0 := intOf(want["fresh_model_calls"]) == 0
			if want0 {
				expected := 0
				if prevT > 0 {
					expected = prevT + 1
				}
				r.good("replay fresh calls", r.llm == 0 || r.llm == expected,
					fmt.Sprintf("llm=%d (prior transients %d)", r.llm, prevT))
			}
			// The store's provenance class: the recorded row grades
			// mixed or cached_replay (persist_plan, ARCHITECTURE §6.6).
			if o.RecordedProvenance != "" {
				r.good("provenance class",
					o.RecordedProvenance == "mixed" ||
						o.RecordedProvenance == "cached_replay",
					o.RecordedProvenance)
			}
		}
	}
	return r
}

func verdictsOf(o *plan.Outcome) string {
	if o == nil {
		return ""
	}
	var methods, blocked []string
	for _, a := range o.Approaches {
		methods = append(methods, stringOf(a["method"]))
	}
	for m := range o.Blocked {
		blocked = append(blocked, m)
	}
	sort.Strings(methods)
	sort.Strings(blocked)
	return strings.Join(methods, ",") + "|" + strings.Join(blocked, ",")
}

func hasCmdCheck(approaches []map[string]any) bool {
	for _, a := range approaches {
		var checks []any
		if l, ok := a["checks"].([]any); ok {
			checks = l
		} else if d, ok := a["direct"].(map[string]any); ok {
			if l, ok := d["checks"].([]any); ok {
				checks = l
			}
		}
		for _, c := range checks {
			if m, ok := c.(map[string]any); ok {
				if _, has := m["cmd"]; has {
					return true
				}
			}
		}
	}
	return false
}

func blockedContains(raw any) []map[string]any {
	l, ok := raw.([]any)
	if !ok {
		return nil
	}
	var out []map[string]any
	for _, x := range l {
		if m, ok := x.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func specSummary(spec *map[string]any) string {
	if spec == nil {
		return "None"
	}
	return stringOf((*spec)["deliverable"])
}

func stringOf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func intOf(v any) int {
	switch x := v.(type) {
	case float64:
		return int(x)
	case int:
		return x
	}
	return 0
}

// portsOf reads the ports list from either shape ([]int from the
// in-memory fanout, []any from JSON artifacts).
func portsOf(raw any) []int {
	switch l := raw.(type) {
	case []int:
		return l
	case []any:
		var out []int
		for _, x := range l {
			if n := intOf(x); n > 0 {
				out = append(out, n)
			}
		}
		return out
	}
	return nil
}

func cmdOf(raw any) []string {
	l, ok := raw.([]any)
	if !ok {
		return nil
	}
	var out []string
	for _, x := range l {
		out = append(out, fmt.Sprintf("%v", x))
	}
	return out
}
