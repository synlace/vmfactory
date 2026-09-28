package model

import (
	"context"
	"fmt"
	"strings"
)

// docCap is the grounding doc truncation (the reference's ground cap).
const docCap = 3000

// Ground is phase 2 of the propose flow: fetch current docs per lookup
// topic. Every failure degrades silently (a topic that yields no docs
// is skipped; the caller's prompt then says to state facts
// conservatively). Byte-identical formats to the reference's vmf_llm.
// ground — the Go port's prompts must be indistinguishable from the
// reference's for the rows to grade.
func Ground(ctx context.Context, s Seam, lookup []string) (string, []string) {
	var grounding, ids []string
	for i, topic := range lookup {
		// The reference grounds at most the first three topics; a
		// failed topic is skipped, not retried.
		if i >= 3 {
			break
		}
		hits := s.C7Search(ctx, topic)
		if len(hits) == 0 {
			continue
		}
		lib := hits[0].ID
		docs := s.C7Docs(ctx, lib, topic, docCap)
		if docs == "" {
			continue
		}
		updated := hits[0].Updated
		if updated == "" {
			updated = "?"
		}
		if len(docs) > docCap {
			docs = docs[:docCap]
		}
		grounding = append(grounding, fmt.Sprintf(
			"=== context7: %s (%s, updated %s) ===\n%s",
			lib, topic, updated, docs))
		ids = append(ids, fmt.Sprintf("%s [%s]", lib, topic))
	}
	if len(grounding) == 0 {
		return "\nGrounding: context7 unavailable for this run; state facts conservatively and prefer the evidence below.\n", nil
	}
	return "\nGrounding - CURRENT documentation fetched for the lookup topics; prefer these facts over your recall:\n" +
		strings.Join(grounding, "\n"), ids
}

// GroundingNote renders the provenance fragment the fanout logs
// (grounded via context7: … / NOT grounded).
func GroundingNote(ids []string) string {
	if len(ids) > 0 {
		return "grounded via context7: " + strings.Join(ids[:min(len(ids), 3)], ", ")
	}
	return "NOT grounded (context7 unavailable)"
}
