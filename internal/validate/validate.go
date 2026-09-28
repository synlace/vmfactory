// Package validate is the clamp catalogue as code: the deterministic
// dispositions every model proposal passes through (drop over
// fabricate). Mirror of the reference's _clamp_* family in
// vmf_plan.py — same bounded vocabularies, same caps, same degrades.
package validate

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Ports clamps the guest tcp ports the app listens on; the host
// publishes each 1:1. Junk drops, not guesses.
func Ports(raw []any) []int {
	var ports []int
	for i, x := range raw {
		if i >= 8 {
			break
		}
		s := strings.TrimSpace(fmt.Sprintf("%v", x))
		d := strings.TrimPrefix(s, "-")
		// Python's str.isdigit on the stripped form; the signed value
		// must still parse (a negative port never clamps in).
		if p, err := strconv.Atoi(s); err == nil && isDigits(d) &&
			p >= 1 && p <= 65535 {
			if !contains(ports, p) {
				ports = append(ports, p)
			}
		}
	}
	return ports
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func contains[T comparable](xs []T, v T) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

// Images clamps the direct plan's host-supplied image refs (pull with
// the host's trust). Short names normalize the way the boot path does:
// the host builders refuse them.
func Images(raw []any) []string {
	var out []string
	for i, x := range raw {
		if i >= 4 {
			break
		}
		s := strings.TrimSpace(fmt.Sprintf("%v", x))
		if s == "" || strings.ContainsAny(s, " \n") || len(s) >= 200 {
			continue
		}
		switch {
		case strings.HasPrefix(s, "localhost/"), strings.HasPrefix(s, "vmf-"):
		case strings.Contains(s, "/"):
			head := strings.SplitN(s, "/", 2)[0]
			if !strings.Contains(head, ".") && !strings.Contains(head, ":") {
				s = "docker.io/" + s
			}
		default:
			s = "docker.io/library/" + s
		}
		if !contains(out, s) {
			out = append(out, s)
		}
	}
	return out
}

// Memory clamps the direct plan's VM sizing: needs_docker brings the
// docker daemons into one VM (measured ~150 MB before the app), so the
// floor is 2048 there; 1024 otherwise; ceiling 8192.
func Memory(raw any, needsDocker bool) int {
	mb, err := strconv.Atoi(strings.TrimSpace(fmt.Sprintf("%v", raw)))
	if err != nil {
		mb = 0
	}
	switch {
	case mb < 1024:
		mb = 1024
	case mb > 8192:
		mb = 8192
	}
	if needsDocker && mb < 2048 {
		mb = 2048
	}
	return mb
}

// Env clamps declared env: plain string pairs, upper-case keys (the
// env-var convention), bounded. Junk keys drop silently.
func Env(raw map[string]any) map[string]string {
	out := map[string]string{}
	n := 0
	for k, v := range raw {
		if n >= 20 {
			break
		}
		s := strings.TrimSpace(k)
		if s != "" && IsUpperWord(s) && len(s) <= 60 {
			out[s] = truncate(strings.TrimSpace(fmt.Sprintf("%v", v)), 200)
			n++
		}
	}
	return out
}

// IsUpperWord mirrors Python str.isupper(): at least one cased
// character and no lowercase ones.
func IsUpperWord(s string) bool {
	cased := false
	for _, c := range s {
		if c >= 'a' && c <= 'z' {
			return false
		}
		if c >= 'A' && c <= 'Z' {
			cased = true
		}
	}
	return cased
}

var userRe = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)

// User clamps the account the app runs as: the install list must
// create it; apps that refuse root need this.
func User(raw string) string {
	s := strings.ToLower(strings.TrimSpace(raw))
	if userRe.MatchString(s) {
		return s
	}
	return ""
}

// BaseImage clamps one OCI ref for a direct plan: a reference never
// contains whitespace — keep the first token only.
func BaseImage(raw any) string {
	s := strings.TrimSpace(fmt.Sprintf("%v", raw))
	if s == "" || s == "<nil>" {
		return ""
	}
	return truncate(strings.Fields(s)[0], 200)
}

// Checks clamps the model-declared success checks for direct plans.
// Bounded vocabulary: probe, exec, cmd — tcp checks derive from
// declared ports (a declared fact, never a model opinion). Junk drops,
// not guesses. Shapes mirror the reference's plan files.
func Checks(raw []any) []map[string]any {
	var out []map[string]any
	for i, c := range raw {
		if i >= 6 {
			break
		}
		m, ok := c.(map[string]any)
		if !ok {
			continue
		}
		if p, ok := m["probe"].(map[string]any); ok {
			e := clampProbe(p)
			if e != nil && !checkIn(out, e) {
				out = append(out, e)
			}
			continue
		}
		if x, ok := m["exec"].(map[string]any); ok {
			cmd, _ := x["cmd"].(string)
			if strings.TrimSpace(cmd) == "" {
				continue
			}
			e := map[string]any{"exec": map[string]any{
				"cmd": truncate(strings.TrimSpace(cmd), 300)}}
			if cont, ok := x["container"].(string); ok && strings.TrimSpace(cont) != "" {
				e["exec"].(map[string]any)["container"] =
					truncate(strings.TrimSpace(cont), 100)
			}
			if !checkIn(out, e) {
				out = append(out, e)
			}
			continue
		}
		if k, ok := m["cmd"].(map[string]any); ok {
			e := clampCmd(k)
			if e != nil && !checkIn(out, e) {
				out = append(out, e)
			}
		}
	}
	return out
}

func clampProbe(p map[string]any) map[string]any {
	port, err := strconv.Atoi(strings.TrimSpace(fmt.Sprintf("%v", p["port"])))
	if err != nil || port < 1 || port > 65535 {
		return nil
	}
	inner := map[string]any{"port": port}
	if path, ok := p["path"].(string); ok && strings.HasPrefix(path, "/") {
		inner["path"] = truncate(path, 200)
	}
	if st, ok := p["expect_status"].(float64); ok && st >= 100 && st <= 599 {
		inner["expect_status"] = int(st)
	}
	if ec, ok := p["expect_contains"].(string); ok && ec != "" {
		inner["expect_contains"] = truncate(ec, 200)
	}
	return map[string]any{"probe": inner}
}

func clampCmd(k map[string]any) map[string]any {
	var probes []string
	rawProbes, _ := k["probes"].([]any)
	for i, p := range rawProbes {
		if i >= 4 {
			break
		}
		s := strings.TrimSpace(fmt.Sprintf("%v", p))
		if s != "" && len(s) <= 200 && !contains(probes, s) {
			probes = append(probes, s)
		}
	}
	bin := truncate(strings.TrimSpace(fmt.Sprintf("%v", k["bin"])), 60)
	if bin == "<nil>" {
		bin = ""
	}
	if len(probes) == 0 && bin != "" {
		probes = []string{bin + " --version", bin + " --help"}
	}
	if len(probes) == 0 {
		return nil
	}
	if bin == "" {
		words := strings.Fields(probes[0])
		if len(words) > 0 {
			bin = truncate(words[0], 60)
		}
	}
	if bin == "" {
		return nil
	}
	inner := map[string]any{"bin": bin, "probes": probes}
	if ex, ok := k["expect_exit"].(float64); ok && ex >= 0 && ex <= 255 {
		inner["expect_exit"] = int(ex)
	}
	if eo, ok := k["expect_out"].(string); ok && strings.TrimSpace(eo) != "" {
		if _, err := regexp.Compile(eo); err == nil {
			inner["expect_out"] = truncate(eo, 200)
		}
	}
	return map[string]any{"cmd": inner}
}

func checkIn(out []map[string]any, e map[string]any) bool {
	for _, x := range out {
		if fmt.Sprintf("%v", x) == fmt.Sprintf("%v", e) {
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

// KeepaliveCmds is the keep-alive vocabulary.
var KeepaliveCmds = map[string]bool{"sleep": true, "true": true,
	"false": true, "tail": true, "top": true, "yes": true, "watch": true,
	"cat": true, "sh": true, "bash": true, "ash": true, "dash": true,
	"read": true}

// IsKeepalive judges the keep-alive shape: a command that serves
// nothing and exists only to keep the VM alive for the verify. A bare
// shell is keep-alive; a shell carrying a payload (`bash -c 'service …
// && exec apache2ctl …'`) is the app — judge the payload's first word
// (measured: DVWA's serving plan wore a false keep-alive tag).
func IsKeepalive(cmd []string) bool {
	if len(cmd) == 0 {
		return false
	}
	a0 := strings.TrimSpace(cmd[0])
	if !KeepaliveCmds[a0] {
		return false
	}
	if (a0 == "sh" || a0 == "bash" || a0 == "ash" || a0 == "dash") &&
		len(cmd) >= 3 && strings.TrimSpace(cmd[1]) == "-c" {
		words := strings.Fields(strings.TrimSpace(cmd[2]))
		if len(words) == 0 {
			return true
		}
		return KeepaliveCmds[words[0]]
	}
	return true
}

// Direct clamps the pkg/release/source payload the guest boot replays
// (the gap-fill direct shape). A command is mandatory; everything else
// clamps or degrades to its default. Returns nil when the command is
// missing.
func Direct(j map[string]any, ports []int) map[string]any {
	var cmd []string
	if rawCmd, ok := j["command"].([]any); ok {
		for i, x := range rawCmd {
			if i >= 16 {
				break
			}
			cmd = append(cmd, fmt.Sprintf("%v", x))
		}
	}
	if len(cmd) == 0 {
		return nil
	}
	var install []string
	if rawInst, ok := j["install"].([]any); ok {
		for i, x := range rawInst {
			if i >= 20 {
				break
			}
			install = append(install, fmt.Sprintf("%v", x))
		}
	}
	needsDocker := false
	if b, ok := j["needs_docker"].(bool); ok {
		needsDocker = b
	}
	env := map[string]string{}
	if rawEnv, ok := j["env"].(map[string]any); ok {
		for k, v := range rawEnv {
			env[k] = fmt.Sprintf("%v", v)
		}
	}
	var rawChecks, rawImages []any
	if cs, ok := j["checks"].([]any); ok {
		rawChecks = cs
	}
	if is, ok := j["images"].([]any); ok {
		rawImages = is
	}
	return map[string]any{
		"base_image":   BaseImage(j["base_image"]),
		"install":      install,
		"command":      cmd,
		"ports":        ports,
		"checks":       Checks(rawChecks),
		"images":       Images(rawImages),
		"env":          env,
		"user":         User(stringOf(j["user"])),
		"needs_docker": needsDocker,
		"memory_mb":    Memory(j["memory_mb"], needsDocker),
		"notes":        truncate(stringOf(j["notes"]), 120),
	}
}

func stringOf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
