// Command postcard-proton speaks the native Proton helper IPC without
// implementing any mail operation yet.
//
// One UTF-8 JSON object on stdin, one versioned envelope on stdout, exit 0
// always: {"version":1,"result":{...}} or {"version":1,"error":{"code":"..."}}.
// Framing, versions and error codes mirror the Tuta helper exactly so both
// backends stay symmetric; every mail op answers unsupported_operation until
// the go-proton-api implementation lands. Nothing is logged anywhere, since
// stdin carries passwords and session material.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
)

const protocolVersion = 1

const maxRequestBytes = 1024 * 1024

// Operations the real helper will implement, in the order they land.
var operations = map[string]bool{
	"login":     true,
	"sync":      true,
	"message":   true,
	"mark_read": true,
	"move":      true,
}

type request struct {
	Version *int   `json:"version"`
	Op      string `json:"op"`
}

type envelope struct {
	Version int            `json:"version"`
	Result  map[string]any `json:"result,omitempty"`
	Error   *codedError    `json:"error,omitempty"`
}

type codedError struct {
	Code string `json:"code"`
}

func (e *codedError) Error() string { return e.Code }

func dispatch(raw []byte) envelope {
	var decoded request
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return failure("invalid_request")
	}
	if decoded.Version == nil {
		return failure("invalid_request")
	}
	if *decoded.Version != protocolVersion {
		return failure("unsupported_version")
	}
	if !operations[decoded.Op] {
		return failure("invalid_request")
	}
	return failure("unsupported_operation")
}

func failure(code string) envelope {
	return envelope{Version: protocolVersion, Error: &codedError{Code: code}}
}

func run(stdin io.Reader, stdout io.Writer) {
	input, err := io.ReadAll(io.LimitReader(stdin, maxRequestBytes+1))
	if err != nil || len(input) > maxRequestBytes {
		write(stdout, failure("invalid_request"))
		return
	}
	write(stdout, dispatch(bytes.TrimSpace(input)))
}

func write(out io.Writer, response envelope) {
	encoded, err := json.Marshal(response)
	if err != nil {
		encoded = []byte(`{"version":1,"error":{"code":"helper_failed"}}`)
	}
	fmt.Fprintln(out, string(encoded))
}

func main() {
	run(os.Stdin, os.Stdout)
}
