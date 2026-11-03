package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func codeOf(t *testing.T, raw string) string {
	t.Helper()
	var out bytes.Buffer
	run(strings.NewReader(raw), &out)
	var decoded struct {
		Version int `json:"version"`
		Error   struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("helper did not emit an envelope: %v", err)
	}
	if decoded.Version != protocolVersion {
		t.Fatalf("envelope version = %d, want %d", decoded.Version, protocolVersion)
	}
	return decoded.Error.Code
}

func TestUnimplementedOperationsAreReserved(t *testing.T) {
	for _, op := range []string{"sync", "message", "mark_read", "move"} {
		if got := codeOf(t, `{"version":1,"op":"`+op+`"}`); got != "unsupported_operation" {
			t.Errorf("op %s code = %q, want unsupported_operation", op, got)
		}
	}
}

func TestMalformedRequestsAreRejected(t *testing.T) {
	cases := map[string]string{
		"not json":        `{"version":`,
		"wrong shape":     `[]`,
		"missing version": `{"op":"sync"}`,
		"float version":   `{"version":1.0,"op":"sync"}`,
		"missing op":      `{"version":1}`,
		"unknown op":      `{"version":1,"op":"send"}`,
	}
	for name, raw := range cases {
		if got := codeOf(t, raw); got != "invalid_request" {
			t.Errorf("%s code = %q, want invalid_request", name, got)
		}
	}
}

func TestIncompatibleVersionNeverConnects(t *testing.T) {
	if got := codeOf(t, `{"version":2,"op":"login"}`); got != "unsupported_version" {
		t.Fatalf("code = %q, want unsupported_version", got)
	}
}

func TestOversizedInputIsRejected(t *testing.T) {
	if got := codeOf(t, `{"version":1,"op":"sync","pad":"`+strings.Repeat("x", maxRequestBytes)+`"}`); got != "invalid_request" {
		t.Fatalf("code = %q, want invalid_request", got)
	}
}

func TestReplyIsOneLine(t *testing.T) {
	var out bytes.Buffer
	run(strings.NewReader("  \n"+`{"version":1,"op":"sync"}`+"\n"), &out)
	if lines := strings.Count(out.String(), "\n"); lines != 1 {
		t.Fatalf("reply has %d lines, want exactly one", lines)
	}
}
