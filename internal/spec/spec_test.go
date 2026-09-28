package spec

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/synlace/vmfactory/internal/model"
)

// staticRunner answers every llm.sh call with a fixed payload.
type staticRunner struct {
	out string
	rc  int
}

func (s *staticRunner) Run(_ context.Context, _ string, _ []string,
	_ string, _ time.Duration) (int, string, string, error) {
	return s.rc, s.out, "", nil
}

func TestIntentPort(t *testing.T) {
	if IntentPort("run the web server on port 3100, authenticated") != 3100 {
		t.Fatal("intent port")
	}
	if IntentPort("no port here") != 0 || IntentPort("port 99999") != 0 {
		t.Fatal("absent or out-of-range port")
	}
}

func TestClampVocabulary(t *testing.T) {
	s := Clamp(map[string]any{
		"deliverable": " WEB ", "serve": map[string]any{
			"port": 9999, "proto": "gopher", "path": "no"},
		"auth":         map[string]any{"required": true, "note": "n"},
		"env_required": []any{"PORT", "lower", "PORT", "OK_KEY"},
		"user":         "!ROOT", "hold": 9999, "why": "w",
	}, "serve web on port 3100")
	d := *s
	if d["deliverable"] != "web" {
		t.Fatalf("deliverable: %v", d["deliverable"])
	}
	serve := d["serve"].(map[string]any)
	// The intent port is authoritative over the junk serve port.
	if serve["port"] != 3100 || serve["proto"] != "http" || serve["path"] != "/" {
		t.Fatalf("serve: %v", serve)
	}
	if got := d["env_required"].([]string); len(got) != 2 || got[0] != "PORT" || got[1] != "OK_KEY" {
		t.Fatalf("env: %v", got)
	}
	if d["user"] != "non-root" {
		t.Fatalf("user: %v", d["user"])
	}
	if d["hold"] != 120 || d["version"] != 1 {
		t.Fatalf("hold/version: %v %v", d["hold"], d["version"])
	}
}

func TestFallbackWebAndCli(t *testing.T) {
	root := t.TempDir()
	// A compose file with ports names web even without intent words.
	os.WriteFile(filepath.Join(root, "compose.yaml"),
		[]byte("services:\n  app:\n    image: x\n    ports:\n      - 8080\n"), 0o644)
	f := *Fallback(root, "")
	if f["deliverable"] != "web" {
		t.Fatalf("compose ports → web: %v", f)
	}
	if f["why"] != "deterministic fallback" {
		t.Fatalf("why: %v", f["why"])
	}
	if f["intent"] != "" {
		t.Fatalf("no intent must stay absent: %v", f["intent"])
	}
	// An intent with web words but no port: web, port 0.
	f2 := *Fallback(t.TempDir(), "serve the web thing")
	if f2["deliverable"] != "web" {
		t.Fatalf("web keyword: %v", f2)
	}
	// No signal: cli.
	f3 := *Fallback(t.TempDir(), "")
	if f3["deliverable"] != "cli" {
		t.Fatalf("no signal: %v", f3)
	}
}

func TestDeriveUsesModelThenClamps(t *testing.T) {
	seam := model.Seam{Runner: &staticRunner{out: `{"deliverable": "web",
		"serve": {"proto": "http", "port": 3100, "path": "/"},
		"why": "serving"}`}}
	s := DeriveWithBundle(context.Background(), seam, t.TempDir(),
		"web on port 3100", "EVIDENCE")
	d := *s
	if d["deliverable"] != "web" || d["why"] != "serving" {
		t.Fatalf("derive: %v", d)
	}
}

func TestDeriveDegradesToFallback(t *testing.T) {
	seam := model.Seam{Runner: &staticRunner{rc: 3}}
	s := DeriveWithBundle(context.Background(), seam, t.TempDir(),
		"run the server on port 8080", "EVIDENCE")
	d := *s
	if d["deliverable"] != "web" || d["why"] != "deterministic fallback" {
		t.Fatalf("degrade: %v", d)
	}
}

func TestBlockShape(t *testing.T) {
	s := Clamp(map[string]any{
		"deliverable":  "web",
		"serve":        map[string]any{"port": 3100.0, "proto": "http"},
		"auth":         map[string]any{"required": true, "note": "per intent"},
		"env_required": []string{"PORT", "HOST"},
		"user":         "non-root",
	}, "intent phrase")
	b := Block(s)
	for _, want := range []string{
		"Target state (the deliverable contract — authoritative,",
		"- deliverable: web",
		"- serve: http://0.0.0.0:3100 path /",
		"- auth: required (per intent)",
		"- required env keys: PORT, HOST",
		"- run user: non-root",
		"Every plan must serve this target",
	} {
		if !strings.Contains(b, want) {
			t.Errorf("block lacks %q", want)
		}
	}
	if Block(nil) != "" || Block(mapPtr(map[string]any{"deliverable": ""})) != "" {
		t.Fatal("no spec → empty block")
	}
}

func mapPtr(m map[string]any) *map[string]any { return &m }

func TestHashDifferentiates(t *testing.T) {
	a := Clamp(map[string]any{"deliverable": "cli"}, "")
	b := Clamp(map[string]any{"deliverable": "web"}, "")
	if Hash(a) == "" || Hash(a) == Hash(b) {
		t.Fatalf("hash: %s vs %s", Hash(a), Hash(b))
	}
	if Hash(nil) != "" {
		t.Fatal("no spec hashes empty")
	}
}

func TestLoadOrDeriveCache(t *testing.T) {
	root := t.TempDir()
	gen := filepath.Join(root, "gen")
	seam := model.Seam{Runner: &staticRunner{rc: 3}}
	intent := "serve on port 3100"

	// First call: derives (fallback), writes spec.json.
	s1 := LoadOrDerive(context.Background(), seam, root, gen, intent)
	if s1 == nil {
		t.Fatal("derive produced nothing")
	}
	if _, err := os.Stat(filepath.Join(gen, "spec.json")); err != nil {
		t.Fatalf("spec.json not written: %v", err)
	}

	// No intent: no spec, never invented.
	if LoadOrDerive(context.Background(), seam, root, gen, "") != nil {
		t.Fatal("no intent must produce no spec")
	}

	// A matching intent replays spec.json (the seam would fail if
	// called; a different intent string forces a re-derive instead).
	s2 := LoadOrDerive(context.Background(), seam, root, gen, intent)
	if Hash(s1) != Hash(s2) {
		t.Fatal("cache replay mismatch")
	}
	if LoadOrDerive(context.Background(), seam, root, gen, "other intent") == nil {
		t.Fatal("different intent must re-derive (fallback still answers)")
	}
}
