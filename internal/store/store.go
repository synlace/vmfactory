// Package store is persist_plan (ARCHITECTURE §5/§6.6): the port's
// store of record. SQLite holds the metadata — runs, candidate
// verdicts, provenance, the append-only event log, and the
// content-keyed cache facts — while heavy bytes stay on the
// filesystem (the plan artifacts under the content-keyed gen dir).
package store

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/synlace/vmfactory/internal/events"
	"github.com/synlace/vmfactory/internal/generate"
	"github.com/synlace/vmfactory/internal/inspect"
	"github.com/synlace/vmfactory/internal/plan"
	_ "modernc.org/sqlite" // pure-Go SQLite; no cgo
)

// Store wraps the SQLite database.
type Store struct {
	db *sql.DB
}

// DefaultPath resolves the database location: VMF_DB overrides, else
// ~/.local/share/vmfactory/vmfactory.db (the pack's layout).
func DefaultPath() string {
	if p := os.Getenv("VMF_DB"); p != "" {
		return p
	}
	base, err := os.UserHomeDir()
	if err != nil {
		return "vmfactory.db"
	}
	return filepath.Join(base, ".local", "share", "vmfactory", "vmfactory.db")
}

// Open creates the schema (idempotent) and returns the store.
func Open(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	for _, stmt := range schema {
		if _, err := db.Exec(stmt); err != nil {
			db.Close()
			return nil, fmt.Errorf("store: schema: %w", err)
		}
	}
	return &Store{db: db}, nil
}

// Close releases the database.
func (s *Store) Close() error { return s.db.Close() }

var schema = []string{
	`CREATE TABLE IF NOT EXISTS runs (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		created TEXT NOT NULL,
		url TEXT NOT NULL DEFAULT '',
		sha TEXT NOT NULL DEFAULT '',
		content_hash TEXT NOT NULL,
		spec_json TEXT NOT NULL DEFAULT '',
		provenance TEXT NOT NULL DEFAULT '',
		fresh_calls INTEGER NOT NULL DEFAULT 0,
		exit_code INTEGER NOT NULL DEFAULT 0)`,
	`CREATE TABLE IF NOT EXISTS candidates (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		run_id INTEGER NOT NULL REFERENCES runs(id),
		method TEXT NOT NULL,
		verdict TEXT NOT NULL,
		why TEXT NOT NULL DEFAULT '',
		approach_json TEXT NOT NULL DEFAULT '',
		cache_hit INTEGER NOT NULL DEFAULT 0,
		created TEXT NOT NULL,
		UNIQUE(run_id, method))`,
	`CREATE TABLE IF NOT EXISTS events (
		run_id INTEGER NOT NULL REFERENCES runs(id),
		seq INTEGER NOT NULL,
		ts TEXT NOT NULL,
		type TEXT NOT NULL,
		candidate TEXT NOT NULL DEFAULT '',
		data_json TEXT NOT NULL DEFAULT '',
		metrics_json TEXT NOT NULL DEFAULT '',
		PRIMARY KEY (run_id, seq))`,
	`CREATE INDEX IF NOT EXISTS idx_candidates_run
		ON candidates(run_id)`,
	`CREATE INDEX IF NOT EXISTS idx_runs_content
		ON runs(content_hash)`,
}

// Candidate is one recorded lane verdict.
type Candidate struct {
	Method       string
	Verdict      string // plan | blocked | skipped | transient
	Why          string
	ApproachJSON string
	CacheHit     bool
}

// RunInput is one planning run's record.
type RunInput struct {
	URL         string
	SHA         string
	ContentHash string
	SpecJSON    string
	Provenance  string // fresh | mixed | cached_replay
	ExitCode    int
	Candidates  []Candidate
	Events      []events.Envelope
}

// RecordRun appends the run, its candidate verdicts, and its events.
// Returns the run id.
func (s *Store) RecordRun(in RunInput) (int64, error) {
	now := time.Now().UTC().Format(time.RFC3339)
	tx, err := s.db.Begin()
	if err != nil {
		return 0, err
	}
	res, err := tx.Exec(`INSERT INTO runs
		(created, url, sha, content_hash, spec_json, provenance,
		 fresh_calls, exit_code) VALUES (?,?,?,?,?,?,?,?)`,
		now, in.URL, in.SHA, in.ContentHash, in.SpecJSON, in.Provenance,
		countFresh(in.Candidates), in.ExitCode)
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	runID, err := res.LastInsertId()
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	for _, c := range in.Candidates {
		hit := 0
		if c.CacheHit {
			hit = 1
		}
		if _, err := tx.Exec(`INSERT INTO candidates
			(run_id, method, verdict, why, approach_json, cache_hit, created)
			VALUES (?,?,?,?,?,?,?)`,
			runID, c.Method, c.Verdict, c.Why, c.ApproachJSON, hit, now); err != nil {
			tx.Rollback()
			return 0, err
		}
	}
	for _, e := range in.Events {
		dj, mj := "", ""
		if e.Data != nil {
			b, _ := json.Marshal(e.Data)
			dj = string(b)
		}
		if e.Metrics != nil {
			b, _ := json.Marshal(e.Metrics)
			mj = string(b)
		}
		if _, err := tx.Exec(`INSERT INTO events
			(run_id, seq, ts, type, candidate, data_json, metrics_json)
			VALUES (?,?,?,?,?,?,?)`,
			runID, int64(e.Seq), e.Time.Format(time.RFC3339Nano),
			e.Type, e.Candidate, dj, mj); err != nil {
			tx.Rollback()
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return runID, nil
}

func countFresh(cs []Candidate) int {
	n := 0
	for _, c := range cs {
		if !c.CacheHit {
			n++
		}
	}
	return n
}

// RunInfo is the replay facts the acceptance rows grade.
type RunInfo struct {
	ID          int64
	Created     string
	ContentHash string
	Provenance  string
	FreshCalls  int
	Candidates  []Candidate
}

// RunProvenance reads one run's facts back.
func (s *Store) RunProvenance(runID int64) (*RunInfo, error) {
	row := s.db.QueryRow(`SELECT id, created, content_hash, provenance,
		fresh_calls FROM runs WHERE id = ?`, runID)
	info := &RunInfo{}
	if err := row.Scan(&info.ID, &info.Created, &info.ContentHash,
		&info.Provenance, &info.FreshCalls); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(`SELECT method, verdict, why, approach_json,
		cache_hit FROM candidates WHERE run_id = ? ORDER BY id`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var c Candidate
		var hit int
		if err := rows.Scan(&c.Method, &c.Verdict, &c.Why,
			&c.ApproachJSON, &hit); err != nil {
			return nil, err
		}
		c.CacheHit = hit == 1
		info.Candidates = append(info.Candidates, c)
	}
	return info, rows.Err()
}

// FromOutcome adapts a planning outcome into a run record: provenance
// class from the cache facts (all cached → cached_replay; some →
// mixed; none → fresh), one candidate row per lane disposition.
func FromOutcome(o *plan.Outcome, exit int) RunInput {
	in := RunInput{
		ContentHash: inspect.ContentHash(o.Target.Dir),
		ExitCode:    exit,
	}
	if o.Target != nil {
		in.URL = o.Target.URL
		in.SHA = o.Target.SHA
	}
	if o.Spec != nil {
		if b, err := json.Marshal(*o.Spec); err == nil {
			in.SpecJSON = string(b)
		}
	}
	for _, m := range generate.Methods {
		c := Candidate{Method: m}
		if p, ok := o.Result.Plans[m]; ok {
			c.Verdict = "plan"
			c.CacheHit = o.Result.Cached[m]
			if b, err := json.Marshal(p); err == nil {
				c.ApproachJSON = string(b)
			}
		} else if why, ok := o.Result.Blocked[m]; ok {
			c.Verdict = "blocked"
			c.Why = why
			c.CacheHit = o.Result.Cached[m]
		} else if why, ok := o.Result.Skipped[m]; ok {
			c.Verdict = "skipped"
			c.Why = why
		} else if why, ok := o.Result.Transients[m]; ok {
			c.Verdict = "transient"
			c.Why = why
		} else {
			continue
		}
		in.Candidates = append(in.Candidates, c)
	}
	cached, fresh := 0, 0
	for _, c := range in.Candidates {
		switch {
		case c.CacheHit:
			cached++
		case c.Verdict != "skipped":
			fresh++
		}
	}
	switch {
	case fresh == 0 && cached > 0:
		in.Provenance = "cached_replay"
	case cached > 0:
		in.Provenance = "mixed"
	default:
		in.Provenance = "fresh"
	}
	return in
}
