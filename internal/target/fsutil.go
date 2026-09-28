package target

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

func keyHash(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])[:12]
}

// ---- git plumbing (os/exec; local and network git only) ----

func git(ctx context.Context, dir string, args ...string) error {
	if dir != "" {
		args = append([]string{"-C", dir}, args...)
	}
	cmd := exec.CommandContext(ctx, "git", args...)
	var errb strings.Builder
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("git %s: %s (%w)",
			strings.Join(args, " "),
			strings.TrimSpace(errb.String()), err)
	}
	return nil
}

func gitOut(ctx context.Context, dir string, args ...string) string {
	if dir != "" {
		args = append([]string{"-C", dir}, args...)
	}
	cmd := exec.CommandContext(ctx, "git", args...)
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// lsRemoteHead returns the remote HEAD sha, or "" on any failure
// (unreachable remote — the mirror reuse path consumes the emptiness).
func lsRemoteHead(ctx context.Context, url string) string {
	out := gitOut(ctx, "", "ls-remote", url, "HEAD")
	if out == "" {
		return ""
	}
	line, _, _ := strings.Cut(out, "\n")
	sha, _, _ := strings.Cut(line, "\t")
	return strings.TrimSpace(sha)
}

// ---- mirror lock (the measured flock -x) ----

type lock struct{ f *os.File }

func acquireLock(path string) (*lock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		return nil, err
	}
	return &lock{f: f}, nil
}

func releaseLock(l *lock) {
	if l == nil || l.f == nil {
		return
	}
	_ = syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
	_ = l.f.Close()
}

// ---- per-run plain copy (the measured `cp -a`) ----

// copyTree copies src's contents into dst, preserving file modes and
// symlinks. Directories get the source mode; a symlink is re-linked,
// never followed.
func copyTree(dst, src string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if rel == "." {
			return os.MkdirAll(target, 0o755)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			_ = os.Remove(target)
			return os.Symlink(link, target)
		}
		if d.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		if !info.Mode().IsRegular() {
			return nil // devices/fifos: not plan-relevant
		}
		return copyFile(target, path, info.Mode().Perm())
	})
}

func copyFile(dst, src string, mode fs.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
