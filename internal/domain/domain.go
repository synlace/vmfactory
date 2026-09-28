// Package domain holds the port's vocabulary: the ExecutionSpec and
// the candidate union, matching the pack's openapi contract
// (docs/handover/api/schemas/candidates, version 1). The structs are
// the wire shapes; per-method required fields and clamps are enforced
// by the planner's validate stage, not by the type (Go has no closed
// unions, so one config struct carries the five method shapes — the
// method field discriminates).
package domain

// Version is the contract version the port speaks.
const Version = "1"

// Spec is the ExecutionSpec: the target-state contract derived from an
// explicit user intent (the planner must not synthesize one — CLI.md).
// Field names follow the reference's spec.json (measured shape).
type Spec struct {
	Version     int      `json:"version,omitempty"`
	Intent      string   `json:"intent,omitempty"`
	Deliverable string   `json:"deliverable,omitempty"` // cli | web | ...
	Serve       *Serve   `json:"serve,omitempty"`
	Auth        *Auth    `json:"auth,omitempty"`
	EnvRequired []string `json:"env_required,omitempty"`
	User        string   `json:"user,omitempty"`
	Hold        int      `json:"hold,omitempty"`
	Why         string   `json:"why,omitempty"`
}

// Serve is the web deliverable's surface contract.
type Serve struct {
	Proto string `json:"proto,omitempty"`
	Port  int    `json:"port,omitempty"`
	Path  string `json:"path,omitempty"`
}

// Auth is the authentication requirement stated by the user intent.
type Auth struct {
	Required bool   `json:"required"`
	Note     string `json:"note,omitempty"`
}

// Candidate is the versioned union over the five candidate schemas.
// The wire shape is {"version":"1","method":"<name>","config":{...}}.
type Candidate struct {
	Version string          `json:"version"`
	Method  string          `json:"method"`
	Config  CandidateConfig `json:"config"`
}

// Methods are the five planner lanes, in fan-out order.
var Methods = []string{"prebuilt", "compose", "build", "pkg", "source"}

// Method names as they appear in the schemas.
const (
	MethodNative        = "native"         // the pkg lane
	MethodDockerfile    = "dockerfile"     // the build lane
	MethodContainer     = "container"      // prebuilt container lane
	MethodDockerCompose = "docker-compose" // the compose lane
	MethodPrebuilt      = "prebuilt"       // archive/binary/image lane
)

// CandidateConfig carries the five method configs as named optionals.
// A valid candidate sets exactly the fields its method requires; the
// validate stage disposes (drop over fabricate).
type CandidateConfig struct {
	// native (method native)
	BaseImage   string            `json:"base_image,omitempty"`
	Install     []string          `json:"install,omitempty"`
	Command     []string          `json:"command,omitempty"`
	Environment map[string]string `json:"environment,omitempty"`
	User        *string           `json:"user,omitempty"`
	Checks      []Check           `json:"checks,omitempty"`
	Hold        *bool             `json:"hold,omitempty"`
	MemoryMB    *int              `json:"memory_mb,omitempty"`

	// dockerfile (method dockerfile)
	Dockerfile  string            `json:"dockerfile,omitempty"`
	Context     string            `json:"context,omitempty"`
	Target      *string           `json:"target,omitempty"`
	BuildArgs   map[string]string `json:"build_args,omitempty"`
	NeedsDocker *bool             `json:"needs_docker,omitempty"`

	// container (method container)
	Image           string           `json:"image,omitempty"`
	ImageProvenance *ImageProvenance `json:"image_provenance,omitempty"`

	// prebuilt (method prebuilt)
	Artifact *Artifact `json:"artifact,omitempty"`

	// docker-compose (method docker-compose)
	ComposeFiles     []string  `json:"compose_files,omitempty"`
	ProjectDirectory *string   `json:"project_directory,omitempty"`
	Services         []Service `json:"services,omitempty"`
}

// Check is the verify surface union: cmd, exec, tcp, or http.
type Check struct {
	Kind string `json:"kind"` // cmd | exec | tcp | http

	// cmd: a CLI-shape check — the binary plus a probe ladder.
	Bin    string     `json:"bin,omitempty"`
	Probes [][]string `json:"probes,omitempty"`

	// exec: a command that must run and assert.
	ExecCommand string `json:"command,omitempty"`

	// tcp: a port must accept connections.
	Port int `json:"port,omitempty"`

	// http: an endpoint must answer.
	Protocol string `json:"protocol,omitempty"` // http | https
	Path     string `json:"path,omitempty"`     // must start with /
}

// ImageProvenance is required for container candidates: where the
// image came from (pull from a registry ref, or build from a bounded
// source identifier). A container candidate without provenance is
// invalid (ARCHITECTURE §26).
type ImageProvenance struct {
	Kind   string  `json:"kind"` // pull | build
	Source *string `json:"source,omitempty"`
}

// Artifact is the prebuilt lane's resolved artefact. kind=image means
// the image is already resolved — the candidate must not define a
// build path (prebuilt-v1 schema description).
type Artifact struct {
	Kind   string  `json:"kind"` // image | archive | binary
	Source string  `json:"source"`
	SHA256 *string `json:"sha256,omitempty"`
}

// Service is one compose service's plan-level summary.
type Service struct {
	Name        string            `json:"name"`
	Image       *string           `json:"image,omitempty"`
	Ports       []int             `json:"ports,omitempty"`
	Environment map[string]string `json:"environment,omitempty"`
}
