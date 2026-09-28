// Package spec implements derive_execution_spec (ARCHITECTURE §6.3):
// explicit user intent + grounded inspection facts → a bounded
// ExecutionSpec. No intent means no spec — the planner must not
// synthesize one (CLI.md). One bounded model call; degrade is the
// deterministic fallback; provenance and spec_hash ride the result.
package spec

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/synlace/vmfactory/internal/inspect"
	"github.com/synlace/vmfactory/internal/model"
	"github.com/synlace/vmfactory/internal/validate"
	"gopkg.in/yaml.v3"
)

// IntentPort extracts the port the user named ("run the web server on
// port 3100").
func IntentPort(phrase string) int {
	re := regexp.MustCompile(`(?i)\bport\s+(\d{1,5})\b`)
	m := re.FindStringSubmatch(phrase)
	if m == nil {
		return 0
	}
	n, _ := strconv.Atoi(m[1])
	if n < 1 || n > 65535 {
		return 0
	}
	return n
}

// Clamp bounds the raw spec to the vocabulary: junk drops or degrades,
// never guesses. The intent phrase is authoritative for the port.
// Mirrors _clamp_spec exactly (always-set serve/auth shapes included).
func Clamp(raw map[string]any, intent string) *map[string]any {
	if raw == nil {
		return nil
	}
	if intent == "" {
		intent = stringOf(raw["intent"])
	}
	intent = strings.TrimSpace(intent)
	d := map[string]any{"version": 1, "intent": truncate(intent, 200)}
	deliv := strings.ToLower(strings.TrimSpace(stringOf(raw["deliverable"])))
	if deliv != "web" && deliv != "cli" {
		deliv = ""
	}
	d["deliverable"] = deliv
	serve, _ := raw["serve"].(map[string]any)
	if serve == nil {
		serve = map[string]any{}
	}
	port := IntentPort(intent)
	if port == 0 {
		port, _ = strconv.Atoi(strings.TrimSpace(fmt.Sprintf("%v", serve["port"])))
		if port < 1 || port > 65535 {
			port = 0
		}
	}
	proto := strings.ToLower(strings.TrimSpace(stringOf(serve["proto"])))
	if proto != "http" && proto != "https" && proto != "tcp" {
		proto = "http"
	}
	path := stringOf(serve["path"])
	if !strings.HasPrefix(path, "/") {
		path = "/"
	}
	d["serve"] = map[string]any{"proto": proto, "port": port, "path": path}
	auth, _ := raw["auth"].(map[string]any)
	if auth == nil {
		auth = map[string]any{}
	}
	note := truncate(stringOf(auth["note"]), 120)
	req := false
	if b, ok := auth["required"].(bool); ok {
		req = b
	}
	d["auth"] = map[string]any{"required": req, "note": note}
	var env []string
	addEnv := func(k any) {
		s := strings.TrimSpace(fmt.Sprintf("%v", k))
		if validate.IsUpperWord(s) && len(s) <= 60 && !contains(env, s) {
			env = append(env, s)
		}
	}
	if rawEnv, ok := raw["env_required"].([]any); ok {
		for i, k := range rawEnv {
			if i >= 10 {
				break
			}
			addEnv(k)
		}
	} else if rawEnv, ok := raw["env_required"].([]string); ok {
		for i, k := range rawEnv {
			if i >= 10 {
				break
			}
			addEnv(k)
		}
	}
	d["env_required"] = env
	u := strings.ToLower(strings.TrimSpace(stringOf(raw["user"])))
	if u == "non-root" || u == "nonroot" || u == "!root" {
		d["user"] = "non-root"
	} else {
		d["user"] = validate.User(u)
	}
	hold, err := strconv.Atoi(strings.TrimSpace(fmt.Sprintf("%v", raw["hold"])))
	if err != nil {
		hold = 25
	}
	d["hold"] = minmax(hold, 1, 120)
	d["why"] = truncate(stringOf(raw["why"]), 60)
	return &d
}

// Fallback is the deterministic degrade when the model is unavailable:
// an intent port or compose-declared ports name a web deliverable;
// otherwise cli. Same policy as the derive prompt, minus the reading.
func Fallback(root, intent string) *map[string]any {
	port := IntentPort(intent)
	if port == 0 {
		for _, cand := range inspect.Names {
			p := filepath.Join(root, cand)
			if st, err := os.Stat(p); err != nil || !st.Mode().IsRegular() {
				continue
			}
			if cp := composePorts(p); len(cp) > 0 {
				port = cp[0]
				break
			}
		}
	}
	web := port > 0
	if re := regexp.MustCompile(`(?i)\bweb\b|\bserver\b|http`); re.MatchString(intent) {
		web = true
	}
	deliv := "cli"
	if web {
		deliv = "web"
	}
	return Clamp(map[string]any{
		"deliverable": deliv,
		"serve":       map[string]any{"port": port},
		"why":         "deterministic fallback",
	}, intent)
}

// composePorts reads the compose services' ports (the reference's
// meta(): strings or ints, clamped 1:1).
func composePorts(path string) []int {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc map[string]any
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	var ports []any
	svcs, _ := doc["services"].(map[string]any)
	for _, s := range svcs {
		sm, _ := s.(map[string]any)
		if sm == nil {
			continue
		}
		if pl, ok := sm["ports"].([]any); ok {
			ports = append(ports, pl...)
		}
	}
	return validate.Ports(ports)
}

// Derive is one bounded model call after the read: intent + evidence
// → the spec. Degrades deterministically when the transport is down.
func Derive(ctx context.Context, seam model.Seam, root, intent string) *map[string]any {
	return DeriveWithBundle(ctx, seam, root, intent, inspect.Bundle(root))
}

func DeriveWithBundle(ctx context.Context, seam model.Seam, root, intent, bundle string) *map[string]any {
	intent = strings.TrimSpace(intent)
	prompt := "Derive the target state (the deliverable contract) for this " +
		"repository"
	if intent != "" {
		prompt += fmt.Sprintf(" — user intent: %s", intent)
	}
	prompt += ". Reply with ONE JSON object:\n" +
		`{"deliverable": "web"|"cli", ` +
		`"serve": {"proto": "http", "port": N, "path": "/"}, ` +
		`"auth": {"required": bool, "note": "<max 12 words>"}, ` +
		`"env_required": ["UPPER_KEYS"], ` +
		`"user": "non-root"|"<account>"|"", ` +
		`"hold": 25, "why": "<max 8 words>"}` + "\n" +
		"deliverable=web when the repo serves something on a port (a " +
		"web UI, an API); cli when the deliverable is a command-line " +
		"tool. Read the evidence: compose files, .env.example and " +
		"README quick-start lines name the port and the required env. " +
		"When an intent phrase is present it is authoritative.\n"
	if intent != "" {
		prompt += "Intent phrase (authoritative): " + intent + "\n"
	}
	prompt += "Evidence:\n" + truncate(bundle, 16384)
	out, err := seam.LLMCall(ctx, "gapfill", prompt, 120*time.Second)
	if err == nil {
		if j, perr := model.ParseLLMJSON(out); perr == nil {
			if s := Clamp(j, intent); s != nil && stringOf((*s)["deliverable"]) != "" {
				return s
			}
		}
	}
	return Fallback(root, intent)
}

// Block is the prompt fragment: the target state, authoritative.
// Empty when no spec — the planner prompts keep their exact prior
// shape then.
func Block(spec *map[string]any) string {
	if spec == nil {
		return ""
	}
	d := *spec
	deliv := stringOf(d["deliverable"])
	if deliv == "" {
		return ""
	}
	lines := []string{
		"Target state (the deliverable contract — authoritative, " +
			"from the user's intent and the repo evidence):",
		"- deliverable: " + deliv,
	}
	if deliv == "web" {
		if serve, ok := d["serve"].(map[string]any); ok {
			if p := intOf(serve["port"]); p > 0 {
				proto := stringOf(serve["proto"])
				if proto == "" {
					proto = "http"
				}
				path := stringOf(serve["path"])
				if path == "" {
					path = "/"
				}
				lines = append(lines, fmt.Sprintf(
					"- serve: %s://0.0.0.0:%d path %s", proto, p, path))
			}
		}
	}
	if auth, ok := d["auth"].(map[string]any); ok {
		if req, _ := auth["required"].(bool); req {
			note := stringOf(auth["note"])
			if note != "" {
				lines = append(lines, "- auth: required ("+note+")")
			} else {
				lines = append(lines, "- auth: required")
			}
		}
	}
	if env, ok := d["env_required"].([]string); ok && len(env) > 0 {
		lines = append(lines, "- required env keys: "+strings.Join(env, ", "))
	} else if envAny, ok := d["env_required"].([]any); ok && len(envAny) > 0 {
		var ss []string
		for _, e := range envAny {
			ss = append(ss, fmt.Sprintf("%v", e))
		}
		lines = append(lines, "- required env keys: "+strings.Join(ss, ", "))
	}
	if u := stringOf(d["user"]); u != "" {
		lines = append(lines, "- run user: "+u)
	}
	if deliv == "web" {
		lines = append(lines, "Every plan must serve this target: declare the "+
			"ports and a command that serves (never a keep-alive sleep), and "+
			"checks that prove the serve target answers (tcp on the port plus "+
			"an HTTP probe).")
	}
	return strings.Join(lines, "\n") + "\n"
}

// Hash is the spec fingerprint: a spec change invalidates the plans, a
// same-spec rerun replays them. Canonical JSON (sorted keys) → sha256
// → 8 hex chars. Empty spec hashes to "".
func Hash(spec *map[string]any) string {
	if spec == nil {
		return ""
	}
	b, err := json.Marshal(*spec)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])[:8]
}

// LoadOrDerive resolves the spec the way the reference does:
// VMF_PLAN_SPEC (JSON) overrides; else spec.json when the intent
// matches; else one derive call. No intent and no override means no
// spec. The spec lands in genDir/spec.json so the preview and the
// race consume the same contract.
func LoadOrDerive(ctx context.Context, seam model.Seam, root, genDir, intent string) *map[string]any {
	intent = strings.TrimSpace(intent)
	if override := strings.TrimSpace(os.Getenv("VMF_PLAN_SPEC")); override != "" {
		var raw map[string]any
		if err := json.Unmarshal([]byte(override), &raw); err == nil {
			if s := Clamp(raw, intent); s != nil && stringOf((*s)["deliverable"]) != "" {
				return s
			}
		}
	}
	if intent == "" {
		return nil
	}
	sp := filepath.Join(genDir, "spec.json")
	if b, err := os.ReadFile(sp); err == nil {
		var old map[string]any
		if err := json.Unmarshal(b, &old); err == nil {
			if strings.TrimSpace(stringOf(old["intent"])) == intent &&
				stringOf(old["deliverable"]) != "" &&
				stringOf(old["why"]) != "deterministic fallback" {
				return &old
			}
		}
	}
	s := Derive(ctx, seam, root, intent)
	_ = os.MkdirAll(genDir, 0o755)
	if b, err := json.MarshalIndent(*s, "", "  "); err == nil {
		_ = os.WriteFile(sp, b, 0o644)
	}
	return s
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

func minmax(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
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
