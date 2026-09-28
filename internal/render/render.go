// Package render is the styled surface of the CLI (row 11): the same
// traced lines as the plain writer, ANSI-styled when stdout is a
// terminal — the mock's contract (mocks/plan/NOTES.md): every line
// traces to a plan file or the spec, the floor/model split stays
// visible, the footer never claims anything booted. Blocked lanes
// render as ✗ rows; the plain writer stays the fallback for pipes,
// tests, and --plain.
package render

import (
	"fmt"
	"os"
	"strings"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/plan"
)

// IsTTY reports whether stdout is a terminal.
func IsTTY() bool {
	st, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return st.Mode()&os.ModeCharDevice != 0
}

const (
	bold    = "\x1b[1m"
	dim     = "\x1b[2m"
	red     = "\x1b[31m"
	green   = "\x1b[32m"
	yellow  = "\x1b[33m"
	magenta = "\x1b[35m"
	reset   = "\x1b[0m"
)

// Styled renders the outcome for a terminal. Content-identical to the
// plain writer: it styles the same lines and lifts the blocked lanes
// into ✗ rows (the mock's shape).
func Styled(o *plan.Outcome) string {
	plain := plan.RenderPlain(o)
	lines := strings.Split(strings.TrimRight(plain, "\n"), "\n")
	var out []string
	for _, line := range lines {
		switch {
		case strings.HasPrefix(line, "scout "):
			out = append(out, bold+line+reset)
		case strings.HasPrefix(line, "spec "):
			out = append(out, styleSpec(line))
		case strings.HasPrefix(line, "lanes "):
			out = append(out, bold+line+reset)
		case strings.HasPrefix(line, "plan "):
			out = append(out, stylePlanHead(line))
		case strings.HasPrefix(line, "        blocked: "):
			out = append(out, blockedRows(line)...)
		case strings.HasPrefix(line, "        checks    "):
			out = append(out, styleChecks(line))
		case strings.HasPrefix(line, "        run       "):
			out = append(out, styleRun(line))
		case strings.HasPrefix(line, "        env       "):
			out = append(out, dim+line+reset)
		case strings.HasPrefix(line, "dry run"):
			out = append(out, dim+line+reset)
		default:
			out = append(out, line)
		}
	}
	return strings.Join(out, "\n") + "\n"
}

// styleSpec bolds the deliverable line; provenance and override dim.
func styleSpec(line string) string {
	if strings.Contains(line, "(none") {
		return dim + line + reset
	}
	// "spec    deliverable  web          ← intent + scout (--spec to
	// override)"
	arrow := strings.Index(line, "←")
	if arrow < 0 {
		return bold + line + reset
	}
	return bold + line[:arrow] + reset + dim + line[arrow:] + reset
}

// stylePlanHead dims the cost tail.
func stylePlanHead(line string) string {
	// "plan 1  build · dockerfile · medium"
	parts := strings.Split(line, " · ")
	if len(parts) < 2 {
		return bold + line + reset
	}
	return bold + strings.Join(parts[:len(parts)-1], " · ") + reset +
		dim + " · " + parts[len(parts)-1] + reset
}

// blockedRows lifts the compact blocked line into ✗ rows.
func blockedRows(line string) []string {
	body := strings.TrimPrefix(line, "        blocked: ")
	var rows []string
	for _, part := range splitBlocked(body) {
		m := part
		why := ""
		if i := strings.IndexByte(part, '('); i >= 0 && strings.HasSuffix(part, ")") {
			m = part[:i]
			why = part[i+1 : len(part)-1]
		}
		m = strings.TrimSpace(m)
		if why != "" {
			rows = append(rows, fmt.Sprintf("        %s✗ %s%s — %s",
				red, m, reset, why))
		} else {
			rows = append(rows, fmt.Sprintf("        %s✗ %s%s", red, m, reset))
		}
	}
	return rows
}

// splitBlocked splits "a (why), b (why2)" on the top-level commas.
func splitBlocked(s string) []string {
	var out []string
	depth := 0
	cur := strings.Builder{}
	for _, c := range s {
		switch c {
		case '(':
			depth++
		case ')':
			depth--
		case ',':
			if depth == 0 {
				out = append(out, strings.TrimSpace(cur.String()))
				cur.Reset()
				continue
			}
		}
		cur.WriteRune(c)
	}
	if strings.TrimSpace(cur.String()) != "" {
		out = append(out, strings.TrimSpace(cur.String()))
	}
	return out
}

// styleChecks keeps the check words green and the hold dim.
func styleChecks(line string) string {
	i := strings.LastIndex(line, "· hold ")
	if i < 0 {
		return line
	}
	return line[:i] + green + line[i:len(line)-len("s")] + reset +
		dim + "s" + reset
}

// styleRun colors the keep-alive tag yellow and the account magenta.
func styleRun(line string) string {
	line = strings.ReplaceAll(line, "keep-alive", yellow+"keep-alive"+reset)
	if i := strings.LastIndex(line, "as "); i >= 0 {
		line = line[:i] + magenta + line[i:] + reset
	}
	return line
}

// Progress prints one dim status line per pipeline event while the
// lanes assemble (the mock's replay vocabulary: spec → grounding →
// lanes landing as they finish). Append-only lines; no cursor games.
func Progress(ev <-chan events.Envelope, stop <-chan struct{}) {
	for {
		select {
		case <-stop:
			return
		case e, ok := <-ev:
			if !ok {
				return
			}
			fmt.Printf("· %s\n", progressLine(e))
		}
	}
}

func progressLine(e events.Envelope) string {
	d, _ := e.Data.(map[string]any)
	str := func(k string) string {
		if s, ok := d[k].(string); ok {
			return s
		}
		return ""
	}
	switch e.Type {
	case "grounding":
		return "grounding"
	case "grounded":
		if ids := str("ids"); ids != "" && ids != "[]" {
			return "grounded " + ids
		}
		return "grounded (context7 unavailable)"
	case "lane.plan":
		tag := ""
		if b, ok := d["cached"].(bool); ok && b {
			tag = " (cached)"
		}
		return fmt.Sprintf("lane %s → plan%s", str("method"), tag)
	case "lane.blocked":
		why := str("why")
		if why != "" {
			why = " — " + why
		}
		return fmt.Sprintf("lane %s → blocked%s", str("method"), why)
	case "lane.skipped":
		return fmt.Sprintf("lane %s → skipped — %s", str("method"), str("why"))
	default:
		return e.Type
	}
}
