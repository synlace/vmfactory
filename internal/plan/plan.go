// Package plan is the pipeline orchestrator (ARCHITECTURE §6):
// resolve_target → inspect → derive_execution_spec →
// generate_candidates_by_method (validate rides the clamps) → the
// rendered plan surface. Nothing boots; the artifacts are exactly
// what a race consumes (plan-*.json under the content-keyed gen dir),
// so a run right after replays with zero model calls.
package plan

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/generate"
	"github.com/synlace/vmfactory/internal/inspect"
	"github.com/synlace/vmfactory/internal/model"
	"github.com/synlace/vmfactory/internal/spec"
	"github.com/synlace/vmfactory/internal/target"
	"github.com/synlace/vmfactory/internal/validate"
)

// Outcome is one planning run's full state — the grader and the
// renderer consume this, not stdout regexes.
type Outcome struct {
	Target     *target.Target
	Spec       *map[string]any
	Result     *generate.Result
	Approaches []map[string]any
	Blocked    map[string]string
	GenDir     string
	Found      []string
	Wall       time.Duration
}

// Emitter is the slice of the event contract the pipeline needs
// (ADR-0002: the renderer consumes envelopes, not internals).
type Emitter interface {
	Emit(typ, candidate string, data any, metrics map[string]float64) events.Envelope
}

// Plan runs the pipeline on one target (URL or local directory) with
// an optional intent phrase and optional spec override (JSON).
func Plan(ctx context.Context, seam model.Seam, src, intent,
	specOverride string, em Emitter) (*Outcome, error) {
	t0 := time.Now()
	tgt, err := target.New().Resolve(ctx, src, "")
	if err != nil {
		return nil, err
	}
	root := tgt.Dir
	if intent != "" {
		_ = os.Setenv("VMF_RUN_INTENT", intent)
	} else {
		_ = os.Unsetenv("VMF_RUN_INTENT")
	}
	if specOverride != "" {
		_ = os.Setenv("VMF_PLAN_SPEC", specOverride)
	} else {
		_ = os.Unsetenv("VMF_PLAN_SPEC")
	}
	genDir := generate.GenDir(root)
	sp := spec.LoadOrDerive(ctx, seam, root, genDir, intent)
	res := generate.Fanout(ctx, seam, root, sp, em)
	o := &Outcome{
		Target: tgt, Spec: sp, Result: res, Blocked: res.Blocked,
		GenDir: genDir, Found: inspect.Found(root),
		Wall: time.Since(t0),
	}
	o.Approaches = assemble(genDir, res)
	writeApproaches(genDir, o.Approaches)
	return o, nil
}

// assemble mirrors the reference's approaches file: per-method
// summaries in stable method order, direct payloads enriched from the
// plan files.
func assemble(genDir string, res *generate.Result) []map[string]any {
	var out []map[string]any
	for _, m := range generate.Methods {
		ap, ok := res.Plans[m]
		if !ok {
			continue
		}
		detail := stringOf(ap["notes"])
		if detail == "" {
			detail = stringOf(ap["image"])
		}
		if detail == "" {
			detail = stringOf(ap["compose_file"])
		}
		e := map[string]any{
			"kind":     stringOf(ap["kind"]),
			"evidence": truncate(fmt.Sprintf("fanout: %s %s", m, detail), 120),
			"cost":     generate.Cost[m],
			"method":   m,
		}
		for _, k := range []string{"image", "compose_file", "ports",
			"install", "checks", "env", "notes"} {
			if v, ok := ap[k]; ok && v != nil {
				if s, is := v.(string); is && s == "" {
					continue
				}
				e[k] = v
			}
		}
		if k := stringOf(ap["kind"]); k == "install_script" || k == "source_build" {
			e["direct"] = loadDirect(genDir, m)
		}
		out = append(out, e)
	}
	return out
}

func loadDirect(genDir, m string) map[string]any {
	b, err := os.ReadFile(filepath.Join(genDir, "plan-"+m+".json"))
	if err != nil {
		return map[string]any{}
	}
	var doc struct {
		Approach struct {
			Direct map[string]any `json:"direct"`
		} `json:"approach"`
	}
	if json.Unmarshal(b, &doc) != nil {
		return map[string]any{}
	}
	if doc.Approach.Direct == nil {
		return map[string]any{}
	}
	return doc.Approach.Direct
}

func writeApproaches(genDir string, approaches []map[string]any) {
	b, _ := json.MarshalIndent(map[string]any{"approaches": approaches},
		"", "  ")
	_ = os.WriteFile(filepath.Join(genDir, ".preview-approaches.json"),
		b, 0o644)
}

// RenderPlain is the plain writer: same honesty as the planned rich
// board, nothing rendered that did not come out of the plan files.
func RenderPlain(o *Outcome) string {
	out := []string{"scout   " + joinFound(o.Found)}
	sp := o.Spec
	if sp != nil && stringOf((*sp)["deliverable"]) != "" {
		d := *sp
		deliv := stringOf(d["deliverable"])
		prov := "scout"
		if stringOf(d["intent"]) != "" {
			prov = "intent + scout"
		} else if stringOf(d["why"]) != "" {
			prov = stringOf(d["why"])
		}
		out = append(out, fmt.Sprintf(
			"spec    deliverable  %s          ← %s (--spec to override)",
			deliv, prov))
		serve, _ := d["serve"].(map[string]any)
		if deliv == "web" && serve != nil && intOf(serve["port"]) > 0 {
			proto := stringOf(serve["proto"])
			if proto == "" {
				proto = "http"
			}
			path := stringOf(serve["path"])
			if path == "" {
				path = "/"
			}
			out = append(out, fmt.Sprintf(
				"        serve        %s 0.0.0.0:%d · path %s",
				proto, intOf(serve["port"]), path))
		}
		if auth, ok := d["auth"].(map[string]any); ok {
			if req, _ := auth["required"].(bool); req {
				note := stringOf(auth["note"])
				if note != "" {
					note = " · " + note
				}
				out = append(out, "        auth         required"+note)
			}
		}
		if env, ok := d["env_required"].([]string); ok && len(env) > 0 {
			out = append(out, "        env          "+strings.Join(env, " · "))
		} else if envAny, ok := d["env_required"].([]any); ok && len(envAny) > 0 {
			var ss []string
			for _, e := range envAny {
				ss = append(ss, fmt.Sprintf("%v", e))
			}
			out = append(out, "        env          "+strings.Join(ss, " · "))
		}
		if u := stringOf(d["user"]); u != "" {
			out = append(out, "        user         "+u)
		}
		hold := intOf(d["hold"])
		if hold == 0 {
			hold = 25
		}
		out = append(out, fmt.Sprintf("        hold         %ds", hold))
	} else {
		out = append(out, "spec    (none — pass --intent to declare the target)")
	}
	if len(o.Approaches) > 0 {
		parts := []string{}
		for i, a := range o.Approaches {
			dm := stringOf(a["method"])
			if stringOf(a["kind"]) == "install_script" ||
				stringOf(a["kind"]) == "source_build" {
				dm = "direct"
			}
			parts = append(parts, fmt.Sprintf("%d %s/%s", i+1, dm,
				stringOf(a["cost"])))
		}
		out = append(out, "lanes   "+strings.Join(parts, ", "))
	}
	if len(o.Blocked) > 0 {
		out = append(out, "        blocked: "+joinBlocked(o.Blocked))
	}
	hold := 25
	if o.Spec != nil {
		if h := intOf((*o.Spec)["hold"]); h > 0 {
			hold = h
		}
	}
	for i, a := range o.Approaches {
		direct, _ := a["direct"].(map[string]any)
		if direct == nil {
			direct = map[string]any{}
		}
		dm := stringOf(a["method"])
		if stringOf(a["kind"]) == "install_script" ||
			stringOf(a["kind"]) == "source_build" {
			dm = "direct"
		}
		out = append(out, fmt.Sprintf("plan %d  %s · %s · %s", i+1, dm,
			stringOf(a["kind"]), stringOf(a["cost"])))
		if img := stringOf(a["image"]); img != "" {
			out = append(out, "        image     "+img)
		}
		if cf := stringOf(a["compose_file"]); cf != "" {
			out = append(out, "        compose   "+cf)
		}
		if bi := stringOf(direct["base_image"]); bi != "" {
			out = append(out, "        base      "+bi)
		}
		inst := installLines(a, direct)
		for j, line := range inst {
			if j >= 3 {
				out = append(out, fmt.Sprintf(
					"                  … +%d more", len(inst)-3))
				break
			}
			if j == 0 {
				out = append(out, "        install   "+line)
			} else {
				out = append(out, "                  "+line)
			}
		}
		if cmd := cmdStrings(direct["command"]); len(cmd) > 0 {
			ru := strings.Join(cmd, " ")
			if validate.IsKeepalive(cmd) {
				ru += "    keep-alive"
			}
			if u := stringOf(direct["user"]); u != "" && u != "root" {
				ru += "    as " + u
			}
			out = append(out, "        run       "+ru)
		}
		if env := envPairs(a, direct); len(env) > 0 {
			pairs := []string{}
			n := 0
			for k, v := range env {
				if n >= 6 {
					break
				}
				pairs = append(pairs, k+"="+v)
				n++
			}
			out = append(out, "        env       "+strings.Join(pairs, " · "))
		}
		words := checkWords(a, direct)
		if p := intSlice(a["ports"]); len(p) > 0 && !anyTCP(words) {
			for _, port := range p {
				words = append([]string{fmt.Sprintf("tcp:%d", port)}, words...)
			}
		}
		out = append(out, fmt.Sprintf("        checks    %s · hold %ds",
			strings.Join(words, " · "), hold))
		model_ := sortedNonTCP(words)
		out = append(out, fmt.Sprintf(
			"        verdict   winner needs %d/%d checks · floor: tcp + hold · model adds: %s",
			len(words), len(words), strings.Join(model_, " · ")))
		if n := stringOf(a["notes"]); n != "" {
			out = append(out, "        notes     "+n)
		}
	}
	out = append(out, "dry run — nothing booted · `just run <src> --intent …` executes")
	return strings.Join(out, "\n") + "\n"
}

func joinFound(found []string) string {
	if len(found) == 0 {
		return "(no plan-relevant files)"
	}
	if len(found) > 8 {
		found = found[:8]
	}
	return strings.Join(found, " · ")
}

func joinBlocked(blocked map[string]string) string {
	keys := make([]string, 0, len(blocked))
	for k := range blocked {
		keys = append(keys, k)
	}
	sortStrings(keys)
	parts := []string{}
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s (%s)", k, blocked[k]))
	}
	return strings.Join(parts, ", ")
}

func installLines(a, direct map[string]any) []string {
	if l := stringSlice(a["install"]); len(l) > 0 {
		return l
	}
	return stringSlice(direct["install"])
}

func cmdStrings(raw any) []string {
	l, ok := raw.([]string)
	if ok {
		return l
	}
	return stringSlice(raw)
}

func envPairs(a, direct map[string]any) map[string]string {
	if m, ok := a["env"].(map[string]string); ok {
		return m
	}
	if m, ok := a["env"].(map[string]any); ok {
		out := map[string]string{}
		for k, v := range m {
			out[k] = fmt.Sprintf("%v", v)
		}
		return out
	}
	if m, ok := direct["env"].(map[string]any); ok {
		out := map[string]string{}
		for k, v := range m {
			out[k] = fmt.Sprintf("%v", v)
		}
		return out
	}
	return nil
}

func checkWords(a, direct map[string]any) []string {
	var raw []any
	if l, ok := a["checks"].([]any); ok {
		raw = l
	} else if l, ok := direct["checks"].([]any); ok {
		raw = l
	}
	var words []string
	for _, c := range raw {
		if w := checkWord(c); w != "" {
			words = append(words, w)
		}
	}
	return words
}

func checkWord(c any) string {
	m, ok := c.(map[string]any)
	if !ok {
		return ""
	}
	if p, ok := m["tcp"].(map[string]any); ok {
		return fmt.Sprintf("tcp:%d", intOf(p["port"]))
	}
	if p, ok := m["probe"].(map[string]any); ok {
		path := stringOf(p["path"])
		if path == "" {
			path = "/"
		}
		st := "2xx"
		if s, ok := p["expect_status"].(float64); ok {
			st = fmt.Sprintf("%d", int(s))
		}
		return fmt.Sprintf("probe:%s → %s", path, st)
	}
	if e, ok := m["exec"].(map[string]any); ok {
		words := strings.Fields(stringOf(e["cmd"]))
		if len(words) > 0 {
			return "exec:" + words[0]
		}
		return "exec:?"
	}
	if k, ok := m["cmd"].(map[string]any); ok {
		return "cmd:" + stringOf(k["bin"])
	}
	return ""
}

func anyTCP(words []string) bool {
	for _, w := range words {
		if strings.HasPrefix(w, "tcp:") {
			return true
		}
	}
	return false
}

func sortedNonTCP(words []string) []string {
	set := map[string]bool{}
	var out []string
	for _, w := range words {
		if strings.HasPrefix(w, "tcp:") {
			continue
		}
		head := w
		if i := strings.IndexByte(w, ':'); i >= 0 {
			head = w[:i]
		}
		if !set[head] {
			set[head] = true
			out = append(out, head)
		}
	}
	sortStrings(out)
	return out
}

func intSlice(raw any) []int {
	l, ok := raw.([]any)
	if !ok {
		return nil
	}
	var out []int
	for _, x := range l {
		if n := intOf(x); n > 0 {
			out = append(out, n)
		}
	}
	return out
}

func stringSlice(raw any) []string {
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

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
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

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// ExitCode translates an outcome to the CLI.md codes: 0 usable plan,
// 1 planning completed but no usable candidate.
func (o *Outcome) ExitCode() int {
	if len(o.Approaches) > 0 {
		return 0
	}
	return 1
}

// WriteJSON emits the outcome as the machine surface.
func (o *Outcome) WriteJSON(w io.Writer) {
	b, _ := json.MarshalIndent(map[string]any{
		"spec": o.Spec, "approaches": o.Approaches,
		"blocked": o.Blocked}, "", "  ")
	_, _ = w.Write(append(b, '\n'))
}
