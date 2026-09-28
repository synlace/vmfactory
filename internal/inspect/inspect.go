// Package inspect implements inspect_target (ARCHITECTURE §6.2): the
// bounded, deterministic evidence bundle, the root compose variants,
// the found-file summary, and the canonical content hash the plan
// cache keys on.
//
// The bundle shape is byte-identical to the reference's
// _gapfill_bundle (vmf_plan.py) — the same facts reach the planner
// prompts in the same format. One deliberate improvement: file
// collection walks in sorted order (the reference relies on readdir
// order), so the bundle is reproducible across filesystems.
package inspect

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// PromptV is the port's prompt version; part of the content hash so
// improved prompts invalidate stale cached plans (the reference's
// PROMPT_V discipline). v14 mirrors the reference's current prompts.
const PromptV = "14"

// Names are the four compose file names, checked in this order.
var Names = []string{"compose.yaml", "docker-compose.yaml",
	"compose.yml", "docker-compose.yml"}

// skipDirs mirrors the reference's SKIP_DIRS.
var skipDirs = map[string]bool{".git": true, "node_modules": true,
	".github": true, "__pycache__": true, ".idea": true, ".vscode": true}

// RootComposeVariants returns every root-level compose*.y*ml outside
// the canonical Names (dev overlays included), sorted, capped at six —
// the fan-out's compose evidence and part of the content key.
func RootComposeVariants(root string) []string {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		n := e.Name()
		if e.IsDir() || !strings.HasPrefix(n, "compose") {
			continue
		}
		if !strings.HasSuffix(n, ".yaml") && !strings.HasSuffix(n, ".yml") {
			continue
		}
		if !isName(n) {
			out = append(out, n)
		}
	}
	sort.Strings(out)
	if len(out) > 6 {
		out = out[:6]
	}
	return out
}

func isName(n string) bool {
	for _, m := range Names {
		if m == n {
			return true
		}
	}
	return false
}

// walkBounded walks root plus one directory level (pruned at depth two
// and inside skipDirs), calling visit per regular file. Sorted order
// keeps the bundle reproducible.
func walkBounded(root string, visit func(path string)) {
	visitTop := func(dir string) {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return
		}
		names := make([]string, 0, len(entries))
		byName := map[string]os.DirEntry{}
		for _, e := range entries {
			names = append(names, e.Name())
			byName[e.Name()] = e
		}
		sort.Strings(names)
		for _, n := range names {
			p := filepath.Join(dir, n)
			info, err := byName[n].Info()
			if err != nil || !info.Mode().IsRegular() {
				continue
			}
			visit(p)
		}
	}
	visitTop(root)
	entries, err := os.ReadDir(root)
	if err != nil {
		return
	}
	subs := []string{}
	for _, e := range entries {
		if !e.IsDir() || skipDirs[e.Name()] {
			continue
		}
		subs = append(subs, filepath.Join(root, e.Name()))
	}
	sort.Strings(subs)
	for _, s := range subs {
		visitTop(s)
	}
}

func readCap(path string, n int) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	buf := make([]byte, n)
	read, _ := f.Read(buf)
	return string(buf[:read])
}

// Bundle builds the deterministic, content-only evidence: README
// (8192), up to three Dockerfiles (4096 each, root + one level), the
// root manifests (4096 each), and up to three systemd units (4096).
// No git, no compose read, no LLM.
func Bundle(root string) string {
	var inputs []string
	add := func(kind, rel, text string) {
		inputs = append(inputs, fmt.Sprintf("=== %s: %s ===\n%s",
			kind, rel, text))
	}
	for _, rn := range []string{"README.md", "README.rst", "README.txt"} {
		p := filepath.Join(root, rn)
		if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() {
			add("readme", rn, readCap(p, 8192))
			break
		}
	}
	var dfs []string
	walkBounded(root, func(p string) {
		base := filepath.Base(p)
		if strings.HasPrefix(base, "Dockerfile") && len(dfs) < 3 {
			dfs = append(dfs, p)
		}
	})
	for _, p := range dfs {
		rel, _ := filepath.Rel(root, p)
		add("dockerfile", rel, readCap(p, 4096))
	}
	for _, f := range []string{"manifest.yaml", "pyproject.toml",
		"requirements.txt", "package.json", "go.mod", "Cargo.toml",
		"Gemfile", "Makefile"} {
		p := filepath.Join(root, f)
		if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() {
			add("manifest", f, readCap(p, 4096))
		}
	}
	var units []string
	walkBounded(root, func(p string) {
		if strings.HasSuffix(p, ".service") && len(units) < 3 {
			units = append(units, p)
		}
	})
	for _, p := range units {
		rel, _ := filepath.Rel(root, p)
		add("systemd-unit", rel, readCap(p, 4096))
	}
	return strings.Join(inputs, "\n")
}

// ScanCompose finds compose files within the first two directory
// levels (one project per directory is the monorepo layout) — the
// reference's scan(): a nested compose still makes the compose lane
// run (the model then blocks or plans it honestly).
func ScanCompose(root string) bool {
	return composeIn(root) || composeDepth1(root)
}

func composeDepth1(root string) bool {
	entries, err := os.ReadDir(root)
	if err != nil {
		return false
	}
	var subs []string
	for _, e := range entries {
		if !e.IsDir() || skipDirs[e.Name()] {
			continue
		}
		subs = append(subs, filepath.Join(root, e.Name()))
	}
	sort.Strings(subs)
	for _, s := range subs {
		if composeIn(s) || composeInLevel2(s) {
			return true
		}
	}
	return false
}

// composeInLevel2 checks the level-2 directories' own files (the
// reference's walk prunes at two separators: a/b/... files count).
func composeInLevel2(dir string) bool {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		if !e.IsDir() || skipDirs[e.Name()] {
			continue
		}
		if composeIn(filepath.Join(dir, e.Name())) {
			return true
		}
	}
	return false
}

func composeIn(dir string) bool {
	list, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, e := range list {
		n := e.Name()
		if e.IsDir() || !strings.HasPrefix(n, "compose") {
			continue
		}
		if strings.HasSuffix(n, ".yaml") || strings.HasSuffix(n, ".yml") {
			return true
		}
	}
	return false
}

// ContentHash is the canonical cache-key input: the bundle plus every
// root-level compose file (dev overlays included — a compose edit is
// plan-relevant). Content-only: a commit move that changes nothing
// plan-relevant keeps the hash, so a solved fast-moving repo replays.
func ContentHash(root string) string {
	parts := []string{Bundle(root)}
	seen := map[string]bool{}
	var compose []string
	for _, n := range Names {
		compose = append(compose, n)
	}
	compose = append(compose, RootComposeVariants(root)...)
	sort.Strings(compose)
	for _, cand := range compose {
		if seen[cand] {
			continue
		}
		seen[cand] = true
		p := filepath.Join(root, cand)
		if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() {
			parts = append(parts, fmt.Sprintf("=== compose: %s ===\n%s",
				cand, readCap(p, 8192)))
		}
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("vmf-inspect-v1\n%s\n%s",
		PromptV, strings.Join(parts, "\n"))))
	return hex.EncodeToString(sum[:])[:16]
}

// Found is the deterministic read summary — what the scout would
// read, in the reference's cheapest-evidence order.
func Found(root string) []string {
	var found []string
	seen := map[string]bool{}
	add := func(n string) {
		p := filepath.Join(root, n)
		if st, err := os.Stat(p); err == nil && st.Mode().IsRegular() && !seen[n] {
			seen[n] = true
			found = append(found, n)
		}
	}
	for _, n := range Names {
		add(n)
	}
	for _, n := range RootComposeVariants(root) {
		add(n)
	}
	for _, f := range []string{"Dockerfile", "package.json", "pyproject.toml",
		"requirements.txt", "go.mod", "Cargo.toml", "Makefile",
		".env.example", "README.md"} {
		add(f)
	}
	return found
}
