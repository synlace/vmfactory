package generate

import (
	"fmt"
	"path/filepath"
	"strings"

	"github.com/synlace/vmfactory/internal/validate"
)

// clampMethod disposes one lane's raw model JSON through the bounded
// vocabulary: the plan, a durable blocked why, or a transient why.
// Mirror of the reference's clamp_method.
func clampMethod(m string, j map[string]any, root string,
	specJSON *map[string]any) (map[string]any, string) {
	if j == nil {
		return nil, "bad output shape"
	}
	if strings.EqualFold(stringOf(j["status"]), "blocked") {
		return nil, truncate(stringOf(j["why"]), 40)
	}
	notes := truncate(stringOf(j["notes"]), 120)
	ports := clampPortsAny(j["ports"])
	switch m {
	case "prebuilt":
		var rawImgs []any
		if imgs, ok := j["images"].([]any); ok {
			rawImgs = imgs
		} else if img := stringOf(j["image"]); img != "" {
			rawImgs = []any{img}
		}
		imgs := validate.Images(rawImgs)
		if len(imgs) == 0 {
			return nil, "no exact image ref"
		}
		return map[string]any{
			"kind": Kind[m], "image": imgs[0], "ports": ports,
			"notes": notes, "install": stringList(j["install"], 20),
			"env":    validate.Env(envAny(j)),
			"checks": validate.Checks(checksAny(j)),
		}, ""
	case "compose":
		cf := composeFile(j["compose_file"], root)
		if cf == "" {
			return nil, "no standalone compose file"
		}
		return map[string]any{
			"kind": Kind[m], "compose_file": cf, "ports": ports,
			"notes": notes, "install": stringList(j["install"], 20),
			"checks": validate.Checks(checksAny(j)),
		}, ""
	case "build":
		df := stringOf(j["dockerfile"])
		if df == "" {
			df = "Dockerfile"
		}
		if strings.Contains(df, "/") || !fileExists(filepath.Join(root, df)) {
			return nil, "no root Dockerfile"
		}
		return map[string]any{
			"kind": Kind[m], "ports": ports,
			"env":   validate.Env(envAny(j)),
			"notes": notes,
		}, ""
	default: // pkg, source
		direct := validate.Direct(j, ports)
		if direct == nil {
			return nil, "no command"
		}
		ap := map[string]any{
			"kind": Kind[m], "ports": ports,
			"direct": direct, "notes": notes,
		}
		if isWeb(specJSON) {
			if cmd, ok := direct["command"].([]string); ok && validate.IsKeepalive(cmd) {
				return nil, "plan ignores the web target"
			}
		}
		return ap, ""
	}
}

// composeFile clamps the model-identified compose file: repo ROOT
// basename only, must exist, never a path with separators.
func composeFile(raw any, root string) string {
	s := strings.TrimSpace(stringOf(raw))
	if s == "" || strings.ContainsAny(s, "/\\") ||
		!(strings.HasSuffix(s, ".yaml") || strings.HasSuffix(s, ".yml")) {
		return ""
	}
	if len(s) > 120 || !fileExists(filepath.Join(root, s)) {
		return ""
	}
	return s
}

func clampPortsAny(raw any) []int {
	if l, ok := raw.([]any); ok {
		return validate.Ports(l)
	}
	return nil
}

func checksAny(j map[string]any) []any {
	if l, ok := j["checks"].([]any); ok {
		return l
	}
	return nil
}

func envAny(j map[string]any) map[string]any {
	if m, ok := j["env"].(map[string]any); ok {
		return m
	}
	return nil
}

func stringList(raw any, cap int) []string {
	l, ok := raw.([]any)
	if !ok {
		return nil
	}
	var out []string
	for i, x := range l {
		if i >= cap {
			break
		}
		s := strings.TrimSpace(fmt.Sprintf("%v", x))
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

func stringOf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
