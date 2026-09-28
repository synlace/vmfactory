package generate

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/model"
	"github.com/synlace/vmfactory/internal/spec"
)

// scriptRunner serves a per-marker canned reply; the prompt rides
// stdin (the measured "-" contract), so markers are stdin markers.
// context7 calls always fail (grounding degrades).
type scriptRunner struct {
	markers map[string]string
	calls   int
}

func (s *scriptRunner) Run(_ context.Context, _ string, args []string,
	stdin string, _ time.Duration) (int, string, string, error) {
	s.calls++
	if strings.Contains(args[0], "context7.sh") {
		return 4, "", "", nil
	}
	for marker, reply := range s.markers {
		if strings.Contains(stdin, marker) {
			return 0, reply, "", nil
		}
	}
	return 0, `{"status":"blocked","why":"no reply scripted"}`, "", nil
}

const pkgPlan = `{"status":"plan","install":["apt-get install -y app"],"command":["app","--serve"],"ports":[8080],"notes":"serves"}`
const pkgKeepalive = `{"status":"plan","install":[],"command":["sleep","100000000"],"ports":[],"notes":"cli"}`
const sourceBlocked = `{"status":"blocked","why":"no build target"}`

func manifestRepo(t *testing.T) string {
	t.Helper()
	t.Setenv("VMF_GENERATED", t.TempDir()) // per-test gen isolation
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "package.json"),
		[]byte(`{"name":"x"}`), 0o644)
	// The source lane's prefilter wants a build manifest
	// (Makefile/go.mod/Cargo.toml); package.json alone does not qualify.
	os.WriteFile(filepath.Join(root, "Makefile"), []byte("all:\n\techo x\n"), 0o644)
	return root
}

func specClamp(t *testing.T, intent string) *map[string]any {
	t.Helper()
	deliv := "cli"
	if strings.Contains(intent, "web") {
		deliv = "web"
	}
	s := spec.Clamp(map[string]any{"deliverable": deliv}, intent)
	if s == nil {
		t.Fatal("clamp")
	}
	return s
}

func TestFanoutSkipsAndLanes(t *testing.T) {
	root := manifestRepo(t)
	r := &scriptRunner{markers: map[string]string{
		"installing runtime packages": pkgPlan,
		"building from source":        sourceBlocked,
	}}
	em := events.NewEmitter()
	id, ch := em.Subscribe()
	res := Fanout(context.Background(), model.Seam{Runner: r}, root, nil, em)
	em.Unsubscribe(id)
	var last events.Envelope
	n := 0
	for e := range ch {
		last = e
		n++
	}
	// The closing tally rides the envelope: the run's cost story.
	// two skips + two blocked = 4 not runnable; grounding + three
	// lanes = 4 calls.
	if last.Type != "lanes.done" {
		t.Fatalf("last event: %s", last.Type)
	}
	if last.Metrics["runnable"] != 1 || last.Metrics["not_runnable"] != 4 ||
		last.Metrics["llm"] != 4 {
		t.Fatalf("tally metrics: %v", last.Metrics)
	}
	if res.Skipped["compose"] != "no compose file" ||
		res.Skipped["build"] != "no root Dockerfile" {
		t.Fatalf("skips: %v", res.Skipped)
	}
	if res.Plans["pkg"] == nil {
		t.Fatalf("pkg plan: %v", res.Plans)
	}
	if res.Blocked["source"] != "no build target" {
		t.Fatalf("source blocked: %v", res.Blocked)
	}
	if len(res.Transients) != 0 {
		t.Fatalf("transient leak: %v", res.Transients)
	}
}

func TestFanoutCacheReplaysAndInvalidates(t *testing.T) {
	root := manifestRepo(t)
	specJSON := specClamp(t, "a cli tool")
	r := &scriptRunner{markers: map[string]string{
		"installing runtime packages": pkgPlan,
		"building from source":        sourceBlocked,
	}}
	res := Fanout(context.Background(), model.Seam{Runner: r}, root, specJSON, nil)
	// prebuilt (no marker → canned blocked), pkg plan, source blocked.
	if len(res.Plans) != 1 || len(res.Blocked) != 2 {
		t.Fatalf("first run: %+v", res)
	}

	// Second run: full replay, zero fresh calls.
	r2 := &scriptRunner{}
	res2 := Fanout(context.Background(), model.Seam{Runner: r2}, root, specJSON, nil)
	if r2.calls != 0 || res2.LLMCalls != 0 {
		t.Fatalf("replay spent calls: %d", r2.calls)
	}
	if len(res2.Plans) != 1 || len(res2.Blocked) != 2 {
		t.Fatalf("replay: %+v", res2)
	}

	// A spec change invalidates: replan. Under a web spec the
	// keep-alive pkg plan is the spec's decision — durable blocked.
	r3 := &scriptRunner{markers: map[string]string{
		"installing runtime packages": pkgKeepalive,
		"building from source":        sourceBlocked,
	}}
	res3 := Fanout(context.Background(), model.Seam{Runner: r3}, root,
		specClamp(t, "web on port 3100"), nil)
	if r3.calls == 0 {
		t.Fatal("spec change must replan")
	}
	if res3.Blocked["pkg"] != "plan ignores the web target" {
		t.Fatalf("web guard: %v", res3)
	}
}

func TestTransientNeverCaches(t *testing.T) {
	root := manifestRepo(t)
	// Transport down: every lane is transient.
	fail := &failingRunner{}
	res := Fanout(context.Background(), model.Seam{Runner: fail}, root, nil, nil)
	if len(res.Transients) == 0 {
		t.Fatalf("transients: %v", res)
	}
	gen := GenDir(root)
	entries, _ := os.ReadDir(gen)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".blocked") ||
			strings.HasSuffix(e.Name(), ".json") {
			t.Fatalf("transient leaked to disk: %s", e.Name())
		}
	}
	// The next run retries (spends again) — never cached.
	fail2 := &failingRunner{}
	Fanout(context.Background(), model.Seam{Runner: fail2}, root, nil, nil)
	if fail2.calls == 0 {
		t.Fatal("transient must retry next run")
	}
}

type failingRunner struct{ calls int }

func (f *failingRunner) Run(_ context.Context, _ string, _ []string,
	_ string, _ time.Duration) (int, string, string, error) {
	f.calls++
	return 3, "", "seam: unconfigured", nil
}

func TestComposeFileClamp(t *testing.T) {
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "compose.dev.yml"),
		[]byte("services: {}\n"), 0o644)
	if composeFile("compose.dev.yml", root) != "compose.dev.yml" {
		t.Fatal("valid root variant accepted")
	}
	for _, bad := range []string{"sub/compose.yml", "../escape.yml",
		"compose.txt", "missing.yml"} {
		if composeFile(bad, root) != "" {
			t.Fatalf("%q must reject", bad)
		}
	}
}

func TestPrebuiltClamp(t *testing.T) {
	plan, why := clampMethod("prebuilt", map[string]any{
		"image": "ghost:5", "ports": []any{2368.0}}, t.TempDir(), nil)
	if plan == nil || plan["image"] != "docker.io/library/ghost:5" {
		t.Fatalf("prebuilt plan: %v / %s", plan, why)
	}
	plan2, why2 := clampMethod("prebuilt", map[string]any{"image": " "},
		t.TempDir(), nil)
	if plan2 != nil || why2 != "no exact image ref" {
		t.Fatalf("prebuilt junk: %v / %s", plan2, why2)
	}
}

func TestBuildClamp(t *testing.T) {
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "Dockerfile"), []byte("FROM x\n"), 0o644)
	plan, _ := clampMethod("build", map[string]any{}, root, nil)
	if plan == nil || plan["kind"] != "dockerfile" {
		t.Fatalf("build plan: %v", plan)
	}
	_, why := clampMethod("build", map[string]any{}, t.TempDir(), nil)
	if why != "no root Dockerfile" {
		t.Fatalf("build blocked: %s", why)
	}
}

func TestGenDirKeysOnContent(t *testing.T) {
	t.Setenv("VMF_GENERATED", t.TempDir())
	root := manifestRepo(t)
	g1 := GenDir(root)
	os.WriteFile(filepath.Join(root, "package.json"), []byte(`{"a":2}`), 0o644)
	if GenDir(root) == g1 {
		t.Fatal("content edit must move the gen dir")
	}
}
