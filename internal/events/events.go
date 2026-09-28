// Package events holds the event envelope and the in-process emitter
// (ADR-0002: the event contract ships before the daemon). The envelope
// is append-only, monotonically sequenced, and carries no transport
// shape: the same emitter serves the CLI renderer now and SSE later,
// and no field may encode a transport. Daemon/SSE work cannot change
// the envelope without an ADR.
package events

import (
	"sync"
	"time"
)

// Envelope is one event record. Seq is assigned by the emitter and is
// monotonic per emitter; a consumer detects loss by spotting gaps.
type Envelope struct {
	Seq       uint64             `json:"seq"`
	Type      string             `json:"type"`
	Time      time.Time          `json:"time"`
	Candidate string             `json:"candidate,omitempty"` // candidate-level correlation
	Data      any                `json:"data,omitempty"`
	Metrics   map[string]float64 `json:"metrics,omitempty"`
}

// bufferSize bounds each subscriber's queue. A full queue drops the
// event (non-blocking send); the consumer recovers via Seq gaps.
const bufferSize = 256

// Emitter fans envelopes out to subscribers. MVP-1 runs in-process;
// a later transport (SSE) subscribes the same way a renderer does.
type Emitter struct {
	mu   sync.Mutex
	seq  uint64
	next int
	subs map[int]chan Envelope
}

// NewEmitter returns an emitter whose first envelope carries Seq 1.
func NewEmitter() *Emitter {
	return &Emitter{subs: map[int]chan Envelope{}}
}

// Subscribe registers a consumer and returns its handle plus a receive
// channel. Call Unsubscribe when done.
func (e *Emitter) Subscribe() (int, <-chan Envelope) {
	e.mu.Lock()
	defer e.mu.Unlock()
	id := e.next
	e.next++
	ch := make(chan Envelope, bufferSize)
	e.subs[id] = ch
	return id, ch
}

// Unsubscribe removes a consumer and closes its channel.
func (e *Emitter) Unsubscribe(id int) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if ch, ok := e.subs[id]; ok {
		delete(e.subs, id)
		close(ch)
	}
}

// Emit stamps the envelope and fans it out. Returns the envelope as
// appended (the caller may log it; the log's Seq proves ordering).
func (e *Emitter) Emit(typ, candidate string, data any, metrics map[string]float64) Envelope {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.seq++
	env := Envelope{
		Seq:       e.seq,
		Type:      typ,
		Time:      time.Now().UTC(),
		Candidate: candidate,
		Data:      data,
		Metrics:   metrics,
	}
	for _, ch := range e.subs {
		select {
		case ch <- env:
		default: // slow consumer: drop; Seq gaps expose it
		}
	}
	return env
}

// Sequence returns the last assigned Seq.
func (e *Emitter) Sequence() uint64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.seq
}
