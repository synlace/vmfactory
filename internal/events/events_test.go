package events

import (
	"encoding/json"
	"testing"
	"time"
)

func TestEmitterSequencesMonotonically(t *testing.T) {
	em := NewEmitter()
	for want := uint64(1); want <= 3; want++ {
		env := em.Emit("test", "", nil, nil)
		if env.Seq != want {
			t.Fatalf("seq %d, want %d", env.Seq, want)
		}
		if env.Type != "test" || env.Time.IsZero() {
			t.Fatalf("envelope fields: %+v", env)
		}
	}
	if em.Sequence() != 3 {
		t.Fatalf("Sequence: %d", em.Sequence())
	}
}

func TestEmitterFansOutToSubscribers(t *testing.T) {
	em := NewEmitter()
	id1, ch1 := em.Subscribe()
	id2, ch2 := em.Subscribe()
	env := em.Emit("candidate.created", "build", map[string]string{"k": "v"}, map[string]float64{"llm": 1})
	for i, ch := range []<-chan Envelope{ch1, ch2} {
		select {
		case got := <-ch:
			if got.Seq != env.Seq || got.Type != "candidate.created" {
				t.Fatalf("sub %d: %+v", i, got)
			}
			if got.Candidate != "build" {
				t.Fatalf("sub %d candidate: %+v", i, got)
			}
		case <-time.After(time.Second):
			t.Fatalf("sub %d: no envelope", i)
		}
	}
	em.Unsubscribe(id1)
	em.Unsubscribe(id2)
}

func TestEmitterUnsubscribeClosesAndIsolates(t *testing.T) {
	em := NewEmitter()
	id, ch := em.Subscribe()
	_ = em.Emit("a", "", nil, nil)
	if got, open := <-ch; !open {
		t.Fatalf("expected one open event")
	} else if got.Type != "a" {
		t.Fatalf("event type: %+v", got)
	}
	select {
	case got, ok := <-ch:
		if ok {
			t.Fatalf("unexpected extra: %+v", got)
		}
	default:
		// empty: correct — one emit, one event
	}
	em.Unsubscribe(id)
	if _, ok := <-ch; ok {
		t.Fatalf("channel still open after unsubscribe")
	}
	// A later emit must not panic on the closed subscriber.
	em.Emit("b", "", nil, nil)
}

func TestEnvelopeHasNoTransportFields(t *testing.T) {
	// ADR-0002: no envelope field may encode transport shape. The
	// struct's JSON keys are the contract; a transport field (http,
	// sse, socket, stream) sneaking in fails the allowed-set check.
	em := NewEmitter()
	env := em.Emit("t", "build", nil, nil)
	b, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var keys map[string]any
	if err := json.Unmarshal(b, &keys); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	allowed := map[string]bool{
		"seq": true, "type": true, "time": true,
		"candidate": true, "data": true, "metrics": true,
	}
	if _, has := keys["seq"]; !has {
		t.Fatalf("seq must always serialize: %v", keys)
	}
	if _, has := keys["type"]; !has {
		t.Fatalf("type must always serialize: %v", keys)
	}
	for k := range keys {
		if !allowed[k] {
			t.Fatalf("unexpected envelope field %q — an envelope change needs an ADR (ADR-0002)", k)
		}
	}
}
