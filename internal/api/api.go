// Package api will serve the event envelope over SSE (the transport
// half of ADR-0002). The envelope and emitter are the contract; this
// package is a stub so the dependency direction exists before the
// transport does. Daemon/SSE work cannot change the envelope without
// an ADR.
package api

// Server is the future SSE endpoint that subscribes to an
// events.Emitter and streams envelopes. Nothing to serve yet.
type Server struct{}
