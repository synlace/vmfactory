package model

import (
	"context"
	"os"
	"testing"
	"time"
)

// TestSmokeRealSeam proves the seam against the real scripts and the
// real transport: one cheap intent-role call, parsed through
// ParseLLMJSON. Skipped unless VMF_SMOKE=1 (it spends LLM budget and
// needs a configured key); the forced-failure tests above cover the
// contract otherwise.
func TestSmokeRealSeam(t *testing.T) {
	if os.Getenv("VMF_SMOKE") != "1" {
		t.Skip("real seam smoke: set VMF_SMOKE=1")
	}
	s := Seam{}
	out, err := s.LLMCall(context.Background(), "intent",
		`Reply with ONE JSON object: {"ok": true}`,
		60*time.Second)
	if err != nil {
		t.Fatalf("real call: %v", err)
	}
	m, err := ParseLLMJSON(out)
	if err != nil {
		t.Fatalf("real parse: %v — raw: %s", err, out)
	}
	if m["ok"] != true {
		t.Fatalf("unexpected payload: %v", m)
	}
	t.Logf("real seam ok: %s", out)
}
