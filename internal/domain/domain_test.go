package domain

import (
	"encoding/json"
	"strings"
	"testing"
)

// The reference spec.json (measured shape, gen 88c87a8424db).
const paperclipSpec = `{
  "version": 1,
  "intent": "Run paperclip's web server on port 3100, authenticated mode",
  "deliverable": "web",
  "serve": {"proto": "http", "port": 3100, "path": "/"},
  "auth": {"required": true, "note": "authenticated mode per user intent"},
  "env_required": ["PORT", "HOST", "PAPERCLIP_HOME"],
  "user": "non-root",
  "hold": 25,
  "why": "web server on port 3100, authenticated"
}`

func TestSpecRoundtrip(t *testing.T) {
	var s Spec
	if err := json.Unmarshal([]byte(paperclipSpec), &s); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if s.Deliverable != "web" || s.Serve == nil || s.Serve.Port != 3100 {
		t.Fatalf("spec fields lost: %+v", s)
	}
	if s.Serve.Proto != "http" || s.Serve.Path != "/" || s.Hold != 25 {
		t.Fatalf("serve/hold lost: %+v", s.Serve)
	}
	if !s.Auth.Required || s.User != "non-root" || s.Why == "" {
		t.Fatalf("auth/user/why lost: %+v", s)
	}
	if len(s.EnvRequired) != 3 || s.EnvRequired[0] != "PORT" {
		t.Fatalf("env_required lost: %+v", s.EnvRequired)
	}
	out, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var back Spec
	if err := json.Unmarshal(out, &back); err != nil {
		t.Fatalf("roundtrip: %v", err)
	}
	if back.Serve.Port != 3100 || back.Auth.Required != true {
		t.Fatalf("roundtrip changed fields: %+v", back)
	}
}

func TestUnmarshalNativeCandidate(t *testing.T) {
	j := `{"version":"1","method":"native","config":{
		"base_image":"node:24-trixie-slim",
		"install":["apt-get install -y mvt"],
		"command":["mvt","ios","check","--help"],
		"environment":{"HOME":"/home/vmf"},
		"user":"vmf",
		"checks":[{"kind":"cmd","bin":"mvt","probes":[["mvt","--version"]]}],
		"hold":null,"memory_mb":2048}}`
	var c Candidate
	if err := json.Unmarshal([]byte(j), &c); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if c.Version != Version || c.Method != MethodNative {
		t.Fatalf("method/version: %+v", c)
	}
	cfg := c.Config
	if cfg.BaseImage != "node:24-trixie-slim" || len(cfg.Command) != 4 {
		t.Fatalf("native fields: %+v", cfg)
	}
	if len(cfg.Checks) != 1 || cfg.Checks[0].Kind != "cmd" ||
		cfg.Checks[0].Bin != "mvt" || len(cfg.Checks[0].Probes) != 1 {
		t.Fatalf("cmd check: %+v", cfg.Checks)
	}
	if cfg.User == nil || *cfg.User != "vmf" {
		t.Fatalf("user: %+v", cfg.User)
	}
}

func TestUnmarshalPrebuiltAndContainerCandidates(t *testing.T) {
	j := `{"version":"1","method":"prebuilt","config":{
		"artifact":{"kind":"image","source":"ghcr.io/x/y:1"},
		"command":["run"], "ports":[3100],
		"checks":[{"kind":"tcp","port":3100},
			{"kind":"http","protocol":"http","port":3100,"path":"/"}]}}`
	var p Candidate
	if err := json.Unmarshal([]byte(j), &p); err != nil {
		t.Fatalf("prebuilt unmarshal: %v", err)
	}
	if p.Config.Artifact == nil || p.Config.Artifact.Kind != "image" ||
		p.Config.Artifact.Source != "ghcr.io/x/y:1" {
		t.Fatalf("artifact: %+v", p.Config.Artifact)
	}
	if len(p.Config.Checks) != 2 || p.Config.Checks[1].Path != "/" {
		t.Fatalf("prebuilt checks: %+v", p.Config.Checks)
	}

	j2 := `{"version":"1","method":"container","config":{
		"image":"postgres:16","needs_docker":true,
		"image_provenance":{"kind":"pull","source":"docker.io/library/postgres:16"}}}`
	var c Candidate
	if err := json.Unmarshal([]byte(j2), &c); err != nil {
		t.Fatalf("container unmarshal: %v", err)
	}
	ip := c.Config.ImageProvenance
	if ip == nil || ip.Kind != "pull" || ip.Source == nil {
		t.Fatalf("image_provenance: %+v", ip)
	}
}

func TestUnmarshalComposeCandidate(t *testing.T) {
	j := `{"version":"1","method":"docker-compose","config":{
		"compose_files":["docker-compose.yml"],
		"services":[{"name":"app","ports":[80]}],
		"needs_docker":true}}`
	var c Candidate
	if err := json.Unmarshal([]byte(j), &c); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(c.Config.ComposeFiles) != 1 ||
		len(c.Config.Services) != 1 || c.Config.Services[0].Name != "app" {
		t.Fatalf("compose fields: %+v", c.Config)
	}
}

// The schema method names must match the wire exactly; a rename here
// is a break. The Methods slice holds the planner's lane names
// (fan-out order), a separate vocabulary from the wire methods.
func TestMethodNamesMatchSchemas(t *testing.T) {
	schemaMethods := map[string]string{
		MethodNative:        "native",
		MethodDockerfile:    "dockerfile",
		MethodContainer:     "container",
		MethodDockerCompose: "docker-compose",
		MethodPrebuilt:      "prebuilt",
	}
	if len(schemaMethods) != 5 {
		t.Fatalf("five schema methods expected, got %d", len(schemaMethods))
	}
	for k, v := range schemaMethods {
		if k != v {
			t.Errorf("constant %q must equal its schema name", k)
		}
	}
	lanes := map[string]bool{}
	for _, lane := range Methods {
		lanes[lane] = true
	}
	wantLanes := []string{"prebuilt", "compose", "build", "pkg", "source"}
	for _, l := range wantLanes {
		if !lanes[l] {
			t.Errorf("Methods lacks lane %q", l)
		}
	}
	if !strings.EqualFold(Version, "1") {
		t.Fatalf("Version must be \"1\"")
	}
}
