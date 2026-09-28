// Package target implements resolve_target (ARCHITECTURE §6.1):
// normalise the input, determine the kind, resolve the ref, and hand
// back a plain per-run checkout — nothing downstream sees a shared
// tree.
//
// The reuse policy is the measured one (scripts/oci-run.sh): a durable
// shallow mirror per URL under the mirror root; a run pays one
// ls-remote round trip instead of a full clone; the mirror reuses on
// HEAD match, reuses as-is when the remote is unreachable (the mirror
// is yesterday's truth), fetch+resets when HEAD moved, and reclones
// when the update fails. A pinned SHA is a first-class alternative
// (the acceptance harness's need became a feature): full clone +
// checkout in a content-keyed cache, because a shallow mirror cannot
// materialise an arbitrary commit.
package target

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Source states why this checkout looks the way it does — provenance
// the spec and plan records carry forward.
type Source string

const (
	SourceCloned       Source = "cloned"
	SourceMirrorReuse  Source = "mirror-reuse"
	SourceUnreachable  Source = "mirror-unreachable-reuse"
	SourceMirrorUpdate Source = "mirror-updated"
	SourcePinned       Source = "pinned"
	SourceLocal        Source = "local"
)

// Typed failures. ErrUnreachable is internal (it degrades to reuse);
// the exported errors are real failures.
var (
	ErrCloneFailed = errors.New("target: clone failed")
	ErrPinNotFound = errors.New("target: pinned commit not found")
	ErrNoSource    = errors.New("target: no such directory")
	ErrUnsupported = errors.New("target: unsupported source form")
)

// Target is the resolved source.
type Target struct {
	URL         string // normalized clone URL ("" for a local dir)
	Dir         string // per-run plain copy; never a shared checkout
	SHA         string // full commit the tree is at
	ProjectHint string // path segments beyond host/org/repo (url#path convention)
	Source      Source
}

// Resolver creates or reuses target resources. MirrorRoot defaults to
// ~/.vmf/mirrors.
type Resolver struct {
	MirrorRoot string
}

// New returns a Resolver with the default mirror root.
func New() *Resolver {
	home, err := os.UserHomeDir()
	if err != nil {
		return &Resolver{}
	}
	return &Resolver{MirrorRoot: filepath.Join(home, ".vmf", "mirrors")}
}

// Resolve maps src (a local directory, or a git URL in the https/git@/
// file:// forms) plus an optional pinned SHA to a Target.
func (r *Resolver) Resolve(ctx context.Context, src, pin string) (*Target, error) {
	if !schemeRe(src) {
		// Local directory input: no clone, no mirror.
		abs, err := filepath.Abs(src)
		if err != nil || !isDir(abs) {
			return nil, fmt.Errorf("%w: %s", ErrNoSource, src)
		}
		return &Target{Dir: abs, Source: SourceLocal}, nil
	}
	url, hint, err := Normalize(src)
	if err != nil {
		return nil, err
	}
	if r.MirrorRoot == "" {
		r = New()
	}
	if err := os.MkdirAll(r.MirrorRoot, 0o755); err != nil {
		return nil, fmt.Errorf("target: mirror root: %w", err)
	}
	if pin != "" {
		return r.resolvePinned(ctx, url, pin, hint)
	}
	return r.resolveMirror(ctx, url, hint)
}

func isDir(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

// resolveMirror is the measured policy, under one exclusive lock per
// mirror.
func (r *Resolver) resolveMirror(ctx context.Context, url, hint string) (*Target, error) {
	key := hashKey(url)
	mirror := filepath.Join(r.MirrorRoot, key)
	lock, err := acquireLock(mirror + ".lock")
	if err != nil {
		return nil, fmt.Errorf("target: lock: %w", err)
	}
	defer releaseLock(lock)

	src := SourceCloned
	if isDir(mirror) && isDir(filepath.Join(mirror, ".git")) {
		headRemote := lsRemoteHead(ctx, url)
		headLocal := gitOut(ctx, mirror, "rev-parse", "HEAD")
		switch {
		case headRemote == "":
			// Unreachable remote: the mirror is still yesterday's
			// truth — reuse it as-is.
			src = SourceUnreachable
		case headRemote == headLocal:
			src = SourceMirrorReuse
		default:
			if git(ctx, mirror, "fetch", "--depth", "1", "origin", "HEAD") == nil &&
				git(ctx, mirror, "reset", "--hard", "FETCH_HEAD") == nil {
				src = SourceMirrorUpdate
			} else {
				// Mirror update failed: reclone from scratch.
				_ = os.RemoveAll(mirror)
				if err := git(ctx, r.MirrorRoot, "clone", "-q",
					"--depth", "1", url, mirror); err != nil {
					return nil, fmt.Errorf("%w: %s: %v",
						ErrCloneFailed, url, err)
				}
				src = SourceCloned
			}
		}
	} else {
		_ = os.RemoveAll(mirror)
		if err := git(ctx, r.MirrorRoot, "clone", "-q", "--depth", "1",
			url, mirror); err != nil {
			return nil, fmt.Errorf("%w: %s: %v", ErrCloneFailed, url, err)
		}
	}
	return r.perRunCopy(url, mirror, hint, src)
}

// resolvePinned materialises an exact commit: a shallow mirror cannot
// hold an arbitrary sha, so the pin cache is a full clone keyed by the
// sha (a pinned run never replans against a moved tip).
func (r *Resolver) resolvePinned(ctx context.Context, url, pin, hint string) (*Target, error) {
	cache := filepath.Join(r.MirrorRoot, "pin-"+pin)
	if !isDir(filepath.Join(cache, ".git")) ||
		gitOut(ctx, cache, "cat-file", "-e", pin+"^{commit}") == "" {
		_ = os.RemoveAll(cache)
		if err := git(ctx, r.MirrorRoot, "clone", url, cache); err != nil {
			return nil, fmt.Errorf("%w: %s@%s: %v",
				ErrCloneFailed, url, pin, err)
		}
	}
	if err := git(ctx, cache, "checkout", "-q", pin); err != nil {
		_ = os.RemoveAll(cache)
		return nil, fmt.Errorf("%w: %s@%s: %v", ErrPinNotFound, url, pin, err)
	}
	return r.perRunCopy(url, cache, hint, SourcePinned)
}

// perRunCopy hands back a plain copy of the resolved tree, and records
// the exact commit from the copy itself.
func (r *Resolver) perRunCopy(url, from, hint string, src Source) (*Target, error) {
	run, err := os.MkdirTemp("", "vmf-target-")
	if err != nil {
		return nil, fmt.Errorf("target: run dir: %w", err)
	}
	if err := copyTree(run, from); err != nil {
		return nil, fmt.Errorf("target: copy: %w", err)
	}
	sha := gitOut(context.Background(), run, "rev-parse", "HEAD")
	return &Target{
		URL: url, Dir: run, SHA: sha, ProjectHint: hint, Source: src,
	}, nil
}

// ---- measured URL normalisation (oci-run.sh) ----

// Normalize strips fragments and .git suffixes and extracts the
// project hint: URL segments beyond host/org/repo select a project
// inside the repo (the compose-spec url#path convention).
func Normalize(src string) (url, hint string, err error) {
	frag := src
	if i := strings.IndexByte(frag, '#'); i >= 0 {
		frag = frag[:i]
	}
	switch {
	case strings.HasPrefix(frag, "https://"), strings.HasPrefix(frag, "http://"):
		scheme := frag[:strings.IndexByte(frag, '/')+2] // "https://" / "http://"
		p := strings.TrimSuffix(frag[len(scheme):], ".git")
		segs := strings.Split(p, "/")
		if len(segs) > 3 {
			url = scheme + strings.Join(segs[:3], "/")
			hint = strings.Join(segs[3:], "/")
			return url, hint, nil
		}
		return scheme + p, "", nil
	case strings.HasPrefix(frag, "git@"):
		rest := strings.TrimPrefix(frag, "git@")
		host, repo, ok := strings.Cut(rest, ":")
		if !ok {
			return "", "", fmt.Errorf("%w: %s", ErrUnsupported, src)
		}
		repo = strings.TrimSuffix(repo, ".git")
		segs := strings.Split(repo, "/")
		if len(segs) > 2 {
			url = "git@" + host + ":" + strings.Join(segs[:2], "/") + ".git"
			hint = strings.Join(segs[2:], "/")
			return url, hint, nil
		}
		return "git@" + host + ":" + repo + ".git", "", nil
	case strings.HasPrefix(frag, "file://"):
		p := strings.TrimPrefix(frag, "file://")
		if i := strings.Index(p, ".git/"); i >= 0 {
			url = "file://" + p[:i+len(".git")]
			hint = p[i+len(".git/"):]
			return url, hint, nil
		}
		return frag, "", nil
	default:
		return "", "", fmt.Errorf("%w: %s", ErrUnsupported, src)
	}
}

// schemeRe reports whether src carries a supported scheme (anything
// else is treated as a local directory, as oci-run.sh does).
func schemeRe(src string) bool {
	for _, p := range []string{"https://", "http://", "git@", "file://"} {
		if strings.HasPrefix(src, p) {
			return true
		}
	}
	return false
}

func hashKey(s string) string {
	// The mirror key is internal; sha256-truncated matches the
	// project's other content keys.
	return keyHash(s)
}
