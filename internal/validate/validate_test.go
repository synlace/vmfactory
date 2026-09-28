package validate

import (
	"reflect"
	"testing"
)

func TestPortsClamp(t *testing.T) {
	got := Ports([]any{"3000", "-5", 80.0, "0", "65536", 3000, "junk", "8080"})
	want := []int{3000, 80, 8080}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestImagesNormalize(t *testing.T) {
	// The reference caps at the first four entries, junk included.
	got := Images([]any{"ghost:5", "library/redis", "localhost/x:1", "vmf-y",
		"docker.io/a/b:2", "has space", ""})
	want := []string{"docker.io/library/ghost:5", "docker.io/library/redis",
		"localhost/x:1", "vmf-y"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	if g := Images([]any{"docker.io/a/b:2"}); !reflect.DeepEqual(g, []string{"docker.io/a/b:2"}) {
		t.Fatalf("qualified ref passthrough: %v", g)
	}
}

func TestMemoryFloors(t *testing.T) {
	if Memory("junk", false) != 1024 || Memory(512, false) != 1024 {
		t.Fatal("floor 1024")
	}
	if Memory(512, true) != 2048 {
		t.Fatal("docker floor 2048")
	}
	if Memory(99999, true) != 8192 {
		t.Fatal("ceiling 8192")
	}
}

func TestEnvAndUserAndBaseImage(t *testing.T) {
	env := Env(map[string]any{"PORT": "3100", "lower": "x", "TOO_LONG_KEY_" +
		"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": "y"})
	if len(env) != 1 || env["PORT"] != "3100" {
		t.Fatalf("env: %v", env)
	}
	// The reference lowers then fullmatches: "Non-Root" IS a valid
	// account-name shape ("non-root"); the spec clamp's alias table
	// (non-root|nonroot|!root) handles the deliverable-level meaning.
	if User("Non-Root") != "non-root" || User("Bad Upper!") != "" ||
		User("app-user_2") != "app-user_2" {
		t.Fatal("user clamp")
	}
	if BaseImage("ref timestamp junk") != "ref" || BaseImage(nil) != "" {
		t.Fatal("base image first token")
	}
}

func TestChecksShapes(t *testing.T) {
	raw := []any{
		map[string]any{"probe": map[string]any{"port": 3100.0,
			"path": "/", "expect_status": 399.0}},
		map[string]any{"exec": map[string]any{"cmd": "  curl -fsS /health  "}},
		map[string]any{"cmd": map[string]any{"bin": "mvt"}},
		map[string]any{"probe": map[string]any{"port": 0.0}}, // junk: dropped
		map[string]any{"unknown": 1},                         // junk: dropped
	}
	got := Checks(raw)
	if len(got) != 3 {
		t.Fatalf("checks: %v", got)
	}
	probe := got[0]["probe"].(map[string]any)
	if probe["port"] != 3100 || probe["path"] != "/" || probe["expect_status"] != 399 {
		t.Fatalf("probe: %v", probe)
	}
	exec := got[1]["exec"].(map[string]any)
	if exec["cmd"] != "curl -fsS /health" {
		t.Fatalf("exec: %v", exec)
	}
	cmd := got[2]["cmd"].(map[string]any)
	if cmd["bin"] != "mvt" {
		t.Fatalf("cmd: %v", cmd)
	}
	probes := cmd["probes"].([]string)
	if len(probes) != 2 || probes[0] != "mvt --version" {
		t.Fatalf("probe ladder synthesized: %v", probes)
	}
}

// The measured DVWA case: a serving payload inside a shell must not
// wear the keep-alive tag.
func TestIsKeepalive(t *testing.T) {
	cases := []struct {
		cmd  []string
		want bool
	}{
		{[]string{"sleep", "100000000"}, true},
		{[]string{"bash"}, true},
		{[]string{"bash", "-c", "sleep 30"}, true},
		{[]string{"bash", "-c", "apache2ctl -DFOREGROUND"}, false},
		{[]string{"bash", "-c", "service mysql start && exec apache2ctl -DFOREGROUND"}, false},
		{[]string{"mvt", "ios", "check"}, false},
		{nil, false},
	}
	for _, c := range cases {
		if got := IsKeepalive(c.cmd); got != c.want {
			t.Errorf("IsKeepalive(%v) = %v, want %v", c.cmd, got, c.want)
		}
	}
}

func TestDirectNeedsCommand(t *testing.T) {
	if got := Direct(map[string]any{"install": []any{"apt-get update"}}, []int{80}); got != nil {
		t.Fatalf("no command must return nil, got %v", got)
	}
	got := Direct(map[string]any{
		"command": []any{"mvt", "ios", "check"},
		"user":    "mvt", "memory_mb": 512.0,
		"checks": []any{map[string]any{
			"cmd": map[string]any{"bin": "mvt",
				"probes": []any{"mvt --version"}}}},
		"base_image": "node:24 junk", "images": []any{"mvt:1"},
	}, []int{})
	if got == nil || got["base_image"] != "node:24" {
		t.Fatalf("direct: %v", got)
	}
	if got["memory_mb"] != 1024 || got["user"] != "mvt" {
		t.Fatalf("direct clamps: %v", got)
	}
}
