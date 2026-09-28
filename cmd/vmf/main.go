// cmd/vmf is the Go port's CLI. The contract lives in
// docs/handover/CLI.md (normative): `vmf plan <target> [--intent ...]
// [--spec <path>]` with exit codes 0/1/2/3. This skeleton ships the
// `grade` surface (the acceptance harness) and honest stubs for the
// rest — the planner lands per docs/handover/ACCEPTANCE.md.
package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/synlace/vmfactory/internal/acceptance"
	"github.com/synlace/vmfactory/internal/events"
)

const usageText = `vmf — vmfactory Go port (skeleton)

  vmf grade  [-fixtures <path>]   grade the recorded acceptance rows
  vmf plan   <target> ...         not implemented yet (planner pending)
  vmf version                     print the build's stage

Exit codes (CLI.md, normative for plan):
  0  usable plan produced
  1  planning completed but no usable candidate exists
  2  usage error or ambiguous/invalid invocation
  3  required model capability unavailable or unconfigured
`

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usageText)
		return 2
	}
	switch args[0] {
	case "grade":
		return gradeCmd(args[1:])
	case "plan":
		// Exit 3 is the honest coarse bucket: the required model
		// capability (the planner) is unconfigured in this build.
		fmt.Fprintln(os.Stderr,
			"plan: not implemented (skeleton — the planner lands per docs/handover/ACCEPTANCE.md)")
		return 3
	case "version":
		fmt.Println("vmf skeleton (planner pending)")
		return 0
	default:
		fmt.Fprintf(os.Stderr, "vmf: unknown command %q\n\n", args[0])
		fmt.Fprint(os.Stderr, usageText)
		return 2
	}
}

// gradeCmd runs the acceptance harness. Exit 0 while rows are pending
// (harness healthy), 1 when a graded row fails, 2 on fixture errors.
func gradeCmd(args []string) int {
	fs := flag.NewFlagSet("grade", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fixtures := fs.String("fixtures", "fixtures/acceptance.yaml",
		"path to the recorded acceptance fixture document")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	f, err := os.Open(*fixtures)
	if err != nil {
		fmt.Fprintf(os.Stderr, "grade: %v\n", err)
		return 2
	}
	defer f.Close()
	fx, err := acceptance.Load(f)
	if err != nil {
		fmt.Fprintf(os.Stderr, "grade: %v\n", err)
		return 2
	}
	em := events.NewEmitter()
	return acceptance.Grade(fx, em, os.Stdout)
}
