package target

import (
	"context"
	"os"
	"strings"
	"testing"
)

// TestSmokeResolveFixturePin proves resolve_target against a real
// fixture row: the mvt pin from fixtures/acceptance.yaml, full clone +
// checkout, one git round trip, no LLM spend. Skipped unless
// VMF_SMOKE=1 (it clones over the network).
func TestSmokeResolveFixturePin(t *testing.T) {
	if os.Getenv("VMF_SMOKE") != "1" {
		t.Skip("real clone smoke: set VMF_SMOKE=1")
	}
	r := New()
	tgt, err := r.Resolve(context.Background(),
		"https://github.com/mvt-project/mvt", "9c3db579ee73")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if !strings.HasPrefix(tgt.SHA, "9c3db579") {
		t.Fatalf("pin mismatch: %s", tgt.SHA)
	}
	if tgt.Source != SourcePinned {
		t.Fatalf("source: %s", tgt.Source)
	}
	if _, err := os.Stat(tgt.Dir + "/pyproject.toml"); err != nil {
		t.Fatalf("pinned tree incomplete: %v", err)
	}
	t.Logf("resolved %s at %s (%s) in %s", tgt.URL, tgt.SHA[:9],
		tgt.Source, tgt.Dir)
}
