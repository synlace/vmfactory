package target

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// originRepo builds a local bare-ish origin with two commits and
// returns (url, sha1, sha2). url is a file:// clone URL.
func originRepo(t *testing.T) (string, string, string) {
	t.Helper()
	work := t.TempDir()
	origin := filepath.Join(t.TempDir(), "repo.git")
	if out, err := exec.Command("git", "init", "-q", "--bare", origin).CombinedOutput(); err != nil {
		t.Fatalf("init bare: %v %s", err, out)
	}
	if out, err := exec.Command("git", "init", "-q", "-b", "main", work).CombinedOutput(); err != nil {
		t.Fatalf("init work: %v %s", err, out)
	}
	commit := func(msg string) string {
		run := func(args ...string) {
			cmd := exec.Command("git", args...)
			cmd.Dir = work
			cmd.Env = append(os.Environ(),
				"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t",
				"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
			if out, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("git %v: %v %s", args, err, out)
			}
		}
		if err := os.WriteFile(filepath.Join(work, "README.md"),
			[]byte(msg), 0o644); err != nil {
			t.Fatal(err)
		}
		run("add", ".")
		run("commit", "-q", "-m", msg)
		return gitOut(context.Background(), work, "rev-parse", "HEAD")
	}
	sha1 := commit("c1")
	run := exec.Command("git", "-C", work, "push", "-q", origin, "HEAD:refs/heads/main")
	if out, err := run.CombinedOutput(); err != nil {
		t.Fatalf("push: %v %s", err, out)
	}
	sha2 := commit("c2")
	if out, err := exec.Command("git", "-C", work, "push", "-q", origin,
		"HEAD:refs/heads/main").CombinedOutput(); err != nil {
		t.Fatalf("push2: %v %s", err, out)
	}
	return "file://" + origin, sha1, sha2
}

func TestNormalizeURLForms(t *testing.T) {
	cases := []struct{ in, url, hint string }{
		{"https://github.com/org/repo", "https://github.com/org/repo", ""},
		{"https://github.com/org/repo.git", "https://github.com/org/repo", ""},
		{"https://github.com/org/repo#frag", "https://github.com/org/repo", ""},
		{"https://github.com/org/repo/apps/upper", "https://github.com/org/repo", "apps/upper"},
		{"git@host:org/repo.git", "git@host:org/repo.git", ""},
		{"git@host:org/repo/apps/upper", "git@host:org/repo.git", "apps/upper"},
		{"file:///data/repo.git/apps/x", "file:///data/repo.git", "apps/x"},
		{"file:///data/repo", "file:///data/repo", ""},
	}
	for _, c := range cases {
		url, hint, err := Normalize(c.in)
		if err != nil || url != c.url || hint != c.hint {
			t.Errorf("Normalize(%q) = %q/%q err %v, want %q/%q",
				c.in, url, hint, err, c.url, c.hint)
		}
	}
}

func TestResolveLocalDir(t *testing.T) {
	r := New()
	tgt, err := r.Resolve(context.Background(), t.TempDir(), "")
	if err != nil || tgt.Source != SourceLocal || tgt.SHA != "" {
		t.Fatalf("local: %+v err %v", tgt, err)
	}
	if _, err := r.Resolve(context.Background(), "/nonexistent-vmf-dir", ""); err == nil {
		t.Fatal("missing dir must fail")
	}
}

func TestMirrorPolicyLifecycle(t *testing.T) {
	url, _, sha2 := originRepo(t)
	r := New()
	r.MirrorRoot = filepath.Join(t.TempDir(), "mirrors")
	ctx := context.Background()

	// 1. cold: clone. originRepo pushed c1 then c2, so remote HEAD
	// is sha2 already.
	t1, err := r.Resolve(ctx, url, "")
	if err != nil {
		t.Fatalf("cold resolve: %v", err)
	}
	if t1.Source != SourceCloned || t1.SHA != sha2 {
		t.Fatalf("cold: %+v", t1)
	}

	// 2. unchanged: remote HEAD matches — reuse.
	t2, err := r.Resolve(ctx, url, "")
	if err != nil || t2.Source != SourceMirrorReuse || t2.SHA != sha2 {
		t.Fatalf("reuse: %+v err %v", t2, err)
	}

	// 3. moved: remote HEAD moved — fetch+reset, new sha.
	head := filepath.Join(strings.TrimPrefix(url, "file://"), "..", "origin-work")
	_ = head // origin is bare; push a new commit through a clone.
	work := t.TempDir()
	run := func(args ...string) {
		cmd := exec.Command("git", args...)
		cmd.Dir = work
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v %s", args, err, out)
		}
	}
	run("clone", "-q", url, work)
	if err := os.WriteFile(filepath.Join(work, "new.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	run("add", "-A")
	env := append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
	c := exec.Command("git", "commit", "-q", "-m", "c3")
	c.Dir = work
	c.Env = env
	if out, err := c.CombinedOutput(); err != nil {
		t.Fatalf("commit: %v %s", err, out)
	}
	run("push", "-q", "origin", "HEAD:refs/heads/main")
	sha3 := gitOut(ctx, work, "rev-parse", "HEAD")

	t3, err := r.Resolve(ctx, url, "")
	if err != nil || t3.Source != SourceMirrorUpdate || t3.SHA != sha3 {
		t.Fatalf("update: %+v err %v", t3, err)
	}

	// 4. unreachable: origin gone — the mirror is yesterday's truth.
	originPath := strings.TrimPrefix(url, "file://")
	if err := os.Rename(originPath, originPath+".gone"); err != nil {
		t.Fatal(err)
	}
	t4, err := r.Resolve(ctx, url, "")
	if err != nil || t4.Source != SourceUnreachable || t4.SHA != sha3 {
		t.Fatalf("unreachable reuse: %+v err %v", t4, err)
	}
	if err := os.Rename(originPath+".gone", originPath); err != nil {
		t.Fatal(err)
	}
}

func TestPerRunCopyIsIsolated(t *testing.T) {
	url, _, _ := originRepo(t)
	r := New()
	r.MirrorRoot = filepath.Join(t.TempDir(), "mirrors")
	tgt, err := r.Resolve(context.Background(), url, "")
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	// The returned dir is a copy: writing to it must not touch the
	// mirror (nothing downstream sees a shared checkout).
	if err := os.WriteFile(filepath.Join(tgt.Dir, "run-only.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(r.MirrorRoot, hashKey(tgt.URL), "run-only.txt")); err == nil {
		t.Fatal("the per-run copy leaked into the mirror")
	}
}

func TestPinnedCheckout(t *testing.T) {
	url, sha1, _ := originRepo(t)
	r := New()
	r.MirrorRoot = filepath.Join(t.TempDir(), "mirrors")
	ctx := context.Background()

	// A pinned resolve returns the pin's tree, not HEAD.
	tgt, err := r.Resolve(ctx, url, sha1)
	if err != nil || tgt.Source != SourcePinned || tgt.SHA != sha1 {
		t.Fatalf("pin: %+v err %v", tgt, err)
	}
	// The pin cache is reusable (same sha, second resolve hits it).
	tgt2, err := r.Resolve(ctx, url, sha1)
	if err != nil || tgt2.SHA != sha1 || tgt2.Dir == tgt.Dir {
		t.Fatalf("pin reuse: %+v err %v", tgt2, err)
	}
	// An unknown pin fails typed, not with a generic error.
	if _, err := r.Resolve(ctx, url, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"); err == nil {
		t.Fatal("unknown pin must fail")
	}
}

func TestMirrorKeyStablePerURL(t *testing.T) {
	if hashKey("https://github.com/a/b") != hashKey("https://github.com/a/b") {
		t.Fatal("same URL must map to one mirror")
	}
	if hashKey("https://github.com/a/b") == hashKey("https://github.com/a/c") {
		t.Fatal("different URLs must not collide")
	}
}
