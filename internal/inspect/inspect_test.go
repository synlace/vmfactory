package inspect

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func makeRepo(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	mk := func(rel, body string) {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	mk("README.md", strings.Repeat("r", 9000)) // over the 8192 cap
	mk("Dockerfile", "FROM node:24\n")
	mk("svc/Dockerfile.dev", "FROM node:24-slim\n")
	mk("a/b/deep-Dockerfile", "FROM deep\n") // depth 2: pruned
	mk("package.json", "{\"name\":\"x\"}")
	mk("go.mod", "module x\n")
	mk("compose.dev.yml", "services: {}\n") // a variant
	mk("units/app.service", "[Unit]\n")
	return root
}

func TestBundleShapeAndCaps(t *testing.T) {
	b := Bundle(makeRepo(t))
	if !strings.Contains(b, "=== readme: README.md ===") {
		t.Fatalf("readme section: %q", b)
	}
	// README truncated at 8192.
	if i := strings.Index(b, "=== readme: README.md ==="); i >= 0 {
		start := i + len("=== readme: README.md ===\n")
		chunk := b[start:min(len(b), start+8193)]
		if strings.Count(chunk, "r") > 8192 {
			t.Fatalf("readme cap not applied")
		}
	}
	for _, want := range []string{
		"=== dockerfile: Dockerfile ===",
		"=== dockerfile: svc" + string(os.PathSeparator) + "Dockerfile.dev ===",
		"=== manifest: package.json ===",
		"=== manifest: go.mod ===",
		"=== systemd-unit: units/app.service ===",
	} {
		if !strings.Contains(b, want) {
			t.Errorf("bundle lacks %q", want)
		}
	}
	if strings.Contains(b, "deep-Dockerfile") {
		t.Fatal("depth-2 file must be pruned")
	}
	if strings.Count(b, "=== dockerfile:") != 2 {
		t.Fatalf("dockerfile count: %d", strings.Count(b, "=== dockerfile:"))
	}
}

func TestComposeVariants(t *testing.T) {
	root := makeRepo(t)
	v := RootComposeVariants(root)
	if len(v) != 1 || v[0] != "compose.dev.yml" {
		t.Fatalf("variants: %v", v)
	}
}

func TestFoundOrder(t *testing.T) {
	f := Found(makeRepo(t))
	want := []string{"compose.dev.yml", "Dockerfile", "package.json",
		"go.mod", "README.md"}
	if len(f) != len(want) {
		t.Fatalf("found: %v", f)
	}
	for i := range want {
		if f[i] != want[i] {
			t.Fatalf("found[%d]: %s, want %s", i, f[i], want[i])
		}
	}
}

func TestContentHashStability(t *testing.T) {
	root := makeRepo(t)
	h1 := ContentHash(root)
	h2 := ContentHash(root)
	if h1 != h2 || len(h1) != 16 {
		t.Fatalf("hash unstable: %s vs %s", h1, h2)
	}
	// A plan-relevant edit moves the hash.
	if err := os.WriteFile(filepath.Join(root, "package.json"),
		[]byte("{\"name\":\"y\"}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if ContentHash(root) == h1 {
		t.Fatal("manifest edit must move the hash")
	}
}
