// Package generate implements generate_candidates_by_method
// (ARCHITECTURE §6.4): five parallel lanes — prebuilt, compose, build,
// pkg, source — each answers plan-or-blocked on paper. Every verdict
// clamps through the bounded vocabulary; a blocked verdict is durable
// and cached, a transport failure is transient and never cached (the
// next run retries the method). Prompts are byte-identical to the
// reference's fanout (vmf_plan.py) so the same evidence produces the
// same class of proposals.
package generate

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/inspect"
	"github.com/synlace/vmfactory/internal/model"
	"github.com/synlace/vmfactory/internal/spec"
)

// Lane vocabularies (the reference's FANOUT_* tables).
var (
	Methods = []string{"prebuilt", "compose", "build", "pkg", "source"}

	Kind = map[string]string{
		"prebuilt": "prebuilt_image", "compose": "compose",
		"build": "dockerfile", "pkg": "install_script",
		"source": "source_build"}

	Cost = map[string]string{
		"prebuilt": "fast", "compose": "medium", "build": "medium",
		"pkg": "slow", "source": "slowest"}

	Goal = map[string]string{
		"prebuilt": "an official container image: name the exact ref; the " +
			"host pulls it and the guest runs it with docker",
		"compose": "the repo's compose stack: name the compose file that is " +
			"COMPLETE (its services declare build or image); an " +
			"overlay variant that only patches a base file is " +
			"blocked, not runnable",
		"build": "building the repo's ROOT Dockerfile host-side; nested " +
			"Dockerfiles are blocked",
		"pkg": "installing runtime packages in the guest (apt/apk/npm/pip) " +
			"and exec'ing the app",
		"source": "building from source in the guest (make/cargo/go build) " +
			"and exec'ing the built artifact",
	}
)

// Verdict is one lane's landing state.
type Verdict struct {
	Method    string
	Plan      map[string]any // the clamped approach (nil when blocked/transient)
	Blocked   string         // durable blocked why (cached)
	Transient string         // transport/parse failure why (never cached)
	Cached    bool           // replayed from the per-method cache
}

// Result is the fan-out's outcome.
type Result struct {
	Plans      map[string]map[string]any
	Blocked    map[string]string
	Skipped    map[string]string
	Transients map[string]string
	// Cached marks the lanes replayed from the per-method cache (the
	// provenance the plan record and the replay rows carry).
	Cached   map[string]bool
	C7IDs    []string
	LLMCalls int
	Wall     time.Duration
}

// genDirRoot: the port's plan artifacts live beside the reference's
// (VMF_GENERATED overrides), keyed by the port's own content hash.
func genDirRoot() string {
	if d := os.Getenv("VMF_GENERATED"); d != "" {
		return d
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".vmf", "generated")
}

// GenDir maps a checked-out root to its content-keyed plan directory.
func GenDir(root string) string {
	return filepath.Join(genDirRoot(), inspect.ContentHash(root))
}

// Fanout runs the lanes. spec may be nil (no intent). Events flow
// through the emitter when non-nil.
func Fanout(ctx context.Context, seam model.Seam, root string,
	specJSON *map[string]any,
	em interface {
		Emit(typ, candidate string, data any, metrics map[string]float64) events.Envelope
	}) *Result {
	t0 := time.Now()
	genDir := GenDir(root)
	res := &Result{
		Plans: map[string]map[string]any{}, Blocked: map[string]string{},
		Skipped: map[string]string{}, Transients: map[string]string{},
		Cached: map[string]bool{},
	}
	emit := func(typ string, data any) {
		if em != nil {
			em.Emit(typ, "", data, nil)
		}
	}
	_ = os.MkdirAll(genDir, 0o755)

	// The prefilters: declared facts, not model opinions.
	hasCompose := false
	for _, n := range inspect.Names {
		if fileExists(filepath.Join(root, n)) {
			hasCompose = true
			break
		}
	}
	variants := inspect.RootComposeVariants(root)
	hasCompose = hasCompose || len(variants) > 0 || inspect.ScanCompose(root)
	hasDockerfile := fileExists(filepath.Join(root, "Dockerfile"))
	hasMake := anyFileExists(root, "Makefile", "go.mod", "Cargo.toml")
	slots := []string{}
	for _, m := range Methods {
		switch {
		case m == "compose" && !hasCompose:
			res.Skipped[m] = "no compose file"
		case m == "build" && !hasDockerfile:
			res.Skipped[m] = "no root Dockerfile"
		case m == "source" && !hasMake:
			res.Skipped[m] = "no build manifest"
		default:
			slots = append(slots, m)
		}
	}
	for m, why := range res.Skipped {
		emit("lane.skipped", map[string]any{"method": m, "why": why})
	}

	sh := spec.Hash(specJSON)

	// Cache load: a plan or a blocked verdict under the same spec
	// replays; a stale spec replans.
	var todo []string
	for _, m := range slots {
		pj := filepath.Join(genDir, "plan-"+m+".json")
		bj := pj + ".blocked"
		if b, err := os.ReadFile(pj); err == nil {
			var doc struct {
				Approach map[string]any `json:"approach"`
				SpecH    string         `json:"spec_h"`
			}
			if json.Unmarshal(b, &doc) == nil && doc.SpecH == sh {
				res.Plans[m] = doc.Approach
				res.Cached[m] = true
				emit("lane.plan", map[string]any{"method": m, "cached": true})
				continue
			}
		}
		if b, err := os.ReadFile(bj); err == nil {
			var doc struct {
				Why   string `json:"why"`
				SpecH string `json:"spec_h"`
			}
			if json.Unmarshal(b, &doc) == nil && doc.SpecH == sh {
				res.Blocked[m] = doc.Why
				res.Cached[m] = true
				emit("lane.blocked", map[string]any{"method": m, "why": doc.Why, "cached": true})
				continue
			}
		}
		todo = append(todo, m)
	}

	if len(todo) > 0 {
		bundle := inspect.Bundle(root)
		for _, n := range variants {
			if b, err := os.ReadFile(filepath.Join(root, n)); err == nil {
				bundle += fmt.Sprintf("\n=== compose variant: %s ===\n%s",
					n, truncate(string(b), 6144))
			}
		}
		draft := "Draft a run plan for this repository inside a disposable " +
			"microVM. List up to 3 topics whose CURRENT facts matter " +
			"(package names, install steps, official images). Reply ONE " +
			`JSON object: {"lookup": ["<doc topic>"], ` +
			"\"why\": \"<max 8 words>\"}\nEvidence:\n" + truncate(bundle, 16384)
		emit("grounding", nil)
		res.LLMCalls++
		out, err := seam.LLMCall(ctx, "gapfill", draft, 180*time.Second)
		lookup := []string{}
		if err == nil {
			if j, perr := model.ParseLLMJSON(out); perr == nil {
				if ls, ok := j["lookup"].([]any); ok {
					for i, l := range ls {
						if i >= 3 {
							break
						}
						lookup = append(lookup, fmt.Sprintf("%v", l))
					}
				}
			}
		}
		grounded, ids := model.Ground(ctx, seam, lookup)
		res.C7IDs = ids
		emit("grounded", map[string]any{"ids": ids})

		deadline := 240 * time.Second
		if d := os.Getenv("VMF_PLAN_DEADLINE"); d != "" {
			if n, err := strconv.Atoi(d); err == nil && n > 0 {
				deadline = time.Duration(n) * time.Second
			}
		}
		var mu sync.Mutex
		var wg sync.WaitGroup
		for _, m := range todo {
			wg.Add(1)
			go func(m string) {
				defer wg.Done()
				prompt := buildPrompt(m, specJSON, grounded, bundle)
				o, err := seam.LLMCall(ctx, "gapfill", prompt, deadline)
				mu.Lock()
				res.LLMCalls++
				mu.Unlock()
				v := Verdict{Method: m, Cached: false}
				if err != nil {
					v.Transient = err.Error()
				} else if j, perr := model.ParseLLMJSON(o); perr != nil {
					v.Transient = "unparseable output"
				} else {
					plan, why := clampMethod(m, j, root, specJSON)
					switch {
					case plan != nil:
						v.Plan = plan
					case j != nil:
						// A parsed verdict is the model's answer:
						// status=blocked is honest, and a plan the
						// deterministic clamp rejected (a keep-alive
						// under a web target) is the spec's decision,
						// not the transport's — both cache as durable
						// blocked verdicts.
						v.Blocked = truncate(why, 40)
					default:
						v.Transient = why
					}
				}
				mu.Lock()
				defer mu.Unlock()
				land(genDir, m, v, sh, ids, emit)
				switch {
				case v.Plan != nil:
					res.Plans[m] = v.Plan
				case v.Blocked != "":
					res.Blocked[m] = v.Blocked
				default:
					res.Transients[m] = v.Transient
				}
			}(m)
		}
		wg.Wait()
	}
	res.Wall = time.Since(t0)
	// The tally: the run's cost story as a closing event (the
	// reference's stderr tally line, now an envelope metric the
	// renderer and the store consume).
	if em != nil {
		em.Emit("lanes.done", "", nil, map[string]float64{
			"runnable":     float64(len(res.Plans)),
			"not_runnable": float64(len(res.Blocked) + len(res.Skipped)),
			"llm":          float64(res.LLMCalls),
		})
	}
	return res
}

// land writes the per-method artifact the moment the lane resolves
// (an interrupt keeps the finished plans). Blocked verdicts — honest
// model verdicts and clamp-rejected plans alike — cache until the
// bundle or spec changes; transients never write.
func land(genDir, m string, v Verdict, sh string, ids []string,
	emit func(string, any)) {
	if v.Plan != nil {
		b, _ := json.MarshalIndent(map[string]any{
			"method": m, "approach": v.Plan,
			"context7": ids, "spec_h": sh}, "", "  ")
		_ = os.WriteFile(filepath.Join(genDir, "plan-"+m+".json"), b, 0o644)
		emit("lane.plan", map[string]any{"method": m, "cached": false})
		return
	}
	if v.Blocked != "" {
		b, _ := json.MarshalIndent(map[string]any{
			"why": v.Blocked, "spec_h": sh}, "", "  ")
		_ = os.WriteFile(filepath.Join(genDir, "plan-"+m+".json.blocked"),
			b, 0o644)
		emit("lane.blocked", map[string]any{"method": m, "why": v.Blocked, "cached": false})
	}
	// Transient: never written; the next run retries the method.
}

func fileExists(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.Mode().IsRegular()
}

func anyFileExists(root string, names ...string) bool {
	for _, n := range names {
		if fileExists(filepath.Join(root, n)) {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
