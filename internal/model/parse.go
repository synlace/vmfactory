package model

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// ParseLLMJSON is the fence- and double-encoding-tolerant parser: LLM
// payloads arrive wrapped in markdown fences (optionally tagged
// ```json), and occasionally double-encoded as a JSON string. Mirrors
// the reference's parse_llm_json exactly.
func ParseLLMJSON(raw string) (map[string]any, error) {
	raw = strings.TrimSpace(raw)
	if strings.HasPrefix(raw, "`") && strings.HasSuffix(raw, "`") {
		raw = strings.Trim(raw, "`")
		raw = strings.TrimSpace(raw)
		if len(raw) >= 4 && strings.EqualFold(raw[:4], "json") {
			raw = strings.TrimLeft(raw[4:], " \t\r\n")
		}
	}
	var v any
	if err := json.Unmarshal([]byte(raw), &v); err != nil {
		return nil, fmt.Errorf("parse_llm_json: %w", err)
	}
	// Double-encoded: the model wrapped the object in a JSON string.
	if s, ok := v.(string); ok {
		if err := json.Unmarshal([]byte(s), &v); err != nil {
			return nil, fmt.Errorf("parse_llm_json: double-encoded: %w", err)
		}
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("parse_llm_json: not a JSON object (got %T)", v)
	}
	return m, nil
}

// parseSearchLines parses context7.sh search output: one JSON object
// per line, up to four results (the script truncates).
func parseSearchLines(out string) ([]SearchHit, error) {
	var hits []SearchHit
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var h SearchHit
		dec := json.NewDecoder(bytes.NewReader([]byte(line)))
		if err := dec.Decode(&h); err != nil {
			return nil, err
		}
		hits = append(hits, h)
	}
	return hits, nil
}
