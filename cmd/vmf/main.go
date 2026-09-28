// cmd/vmf is the Go port's CLI. The contract lives in
// docs/handover/CLI.md (normative): `vmf plan <target> [--intent ...]
// [--spec <path>]` with exit codes 0/1/2/3. This skeleton ships the
// `grade` surface (the acceptance harness) and honest stubs for the
// rest — the planner lands per docs/handover/ACCEPTANCE.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"

	"github.com/synlace/vmfactory/internal/acceptance"
	"github.com/synlace/vmfactory/internal/model"
	"github.com/synlace/vmfactory/internal/plan"
	"github.com/synlace/vmfactory/internal/target"
)

const usageText = `vmf — vmfactory Go port

  vmf plan   <target> [--intent <text>] [--spec <path>] [--json]
                             plan on paper: spec + per-lane candidates
  vmf grade  [-fixtures <path>]
                             grade the recorded acceptance rows
  vmf version                print the build's stage

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
		return planCmd(args[1:])
	case "version":
		fmt.Println("vmf (go port; planner live)")
		return 0
	default:
		fmt.Fprintf(os.Stderr, "vmf: unknown command %q\n\n", args[0])
		fmt.Fprint(os.Stderr, usageText)
		return 2
	}
}

// planCmd runs the planning pipeline: resolve → inspect → derive →
// generate. Exit codes are CLI.md's: 0 usable plan, 1 no usable
// candidate, 2 usage, 3 model unconfigured.
func planCmd(args []string) int {
	fs := flag.NewFlagSet("plan", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	intent := fs.String("intent", "", "explicit user intent (never synthesized)")
	specPath := fs.String("spec", "", "human-authored ExecutionSpec override (JSON path)")
	asJSON := fs.Bool("json", false, "machine surface: spec, approaches, blocked")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "plan: exactly one target (URL or directory) required")
		return 2
	}
	override := ""
	if *specPath != "" {
		b, err := os.ReadFile(*specPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "plan: --spec: %v\n", err)
			return 2
		}
		override = string(b)
	}
	o, err := plan.Plan(context.Background(), model.Seam{}, fs.Arg(0),
		*intent, override, nil)
	if err != nil {
		if errors.Is(err, target.ErrNoSource) {
			fmt.Fprintf(os.Stderr, "plan: %v\n", err)
			return 2
		}
		fmt.Fprintf(os.Stderr, "plan: %v\n", err)
		return 3
	}
	if *asJSON {
		o.WriteJSON(os.Stdout)
	} else {
		fmt.Print(plan.RenderPlain(o))
	}
	return o.ExitCode()
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
	return acceptance.Grade(fx, model.Seam{}, os.Stdout)
}
