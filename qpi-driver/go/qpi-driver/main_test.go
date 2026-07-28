package main

import (
	"bytes"
	"strings"
	"testing"
)

// run executes the CLI with args and returns what it wrote plus the error it
// returned, without ever starting a driver — every case here fails before the
// device runner would be called.
func run(t *testing.T, args ...string) (string, error) {
	t.Helper()
	root := newRootCmd()
	out := &bytes.Buffer{}
	root.SetOut(out)
	root.SetErr(out)
	root.SetArgs(args)
	err := root.Execute()
	return out.String(), err
}

func TestOperationIsRequired(t *testing.T) {
	// `start` alone cannot know what to run: the operation is the one thing that
	// is not defaultable.
	_, err := run(t, "start", "--token", "t", "--ca-fingerprint", "fp")
	if err == nil {
		t.Fatal("expected start without --operation to fail")
	}
	for _, want := range []string{"--operation is required", "monitor", "process"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("expected the error to mention %q, got %q", want, err)
		}
	}
}

func TestUnknownOperationListsTheValidOnes(t *testing.T) {
	// Operations are a closed set — QPI-UI must have a handler for each — so the
	// error can name all of them (RFC 0003 §13.1).
	_, err := run(t, "start", "--operation", "procces", "--token", "t")
	if err == nil {
		t.Fatal("expected an unknown operation to fail")
	}
	if !strings.Contains(err.Error(), `unknown operation "procces"`) {
		t.Errorf("expected the operation named, got %q", err)
	}
	if !strings.Contains(err.Error(), "process, monitor") &&
		!strings.Contains(err.Error(), "monitor, process") {
		t.Errorf("expected the valid operations listed, got %q", err)
	}
}

func TestOperationComesFromTheEnvironment(t *testing.T) {
	// Flags are registered with the environment already read, so a unit file can
	// be nothing but Environment= lines.
	t.Setenv("QPI_OPERATION", "monitor")
	t.Setenv("QPI_ACCESS_TOKEN", "t")

	_, err := run(t, "start", "--device", "nope")
	if err == nil || !strings.Contains(err.Error(), "unknown monitor device") {
		t.Fatalf("expected QPI_OPERATION to select monitor, got %v", err)
	}
}

func TestEmptyOperationSaysSoHonestly(t *testing.T) {
	// The Go SDK registers no process devices. With one verb, `--operation
	// process` has to say that plainly rather than reporting an unknown device
	// with an empty list of known ones (RFC 0003 §8).
	_, err := run(t, "start", "--operation", "process", "--token", "t")
	if err == nil {
		t.Fatal("expected an operation with no devices to fail")
	}
	message := err.Error()
	if !strings.Contains(message, "ships no process devices") {
		t.Errorf("expected an honest message, got %q", message)
	}
	if strings.Contains(message, "known devices: \"\"") ||
		strings.HasSuffix(message, "known devices: ") {
		t.Errorf("expected no empty device list, got %q", message)
	}
	if !strings.Contains(message, "qpi-driver[cli]") {
		t.Errorf("expected the message to say where to find one, got %q", message)
	}
}

func TestDeviceAndNameDefaultPerOperation(t *testing.T) {
	// One verb, but the defaults still differ by operation. Asserted by giving
	// monitor a runner that records what it was handed, since resolution happens
	// only when the flags are omitted.
	var got *commonFlags
	spec := operations["monitor"]
	original := spec.devices["bluefors_gen1"]
	spec.devices["bluefors_gen1"] = func(cf *commonFlags, _ map[string]string) error {
		got = cf
		return nil
	}
	operations["monitor"] = spec
	t.Cleanup(func() {
		spec.devices["bluefors_gen1"] = original
		operations["monitor"] = spec
	})

	if _, err := run(t, "start", "--operation", "monitor", "--token", "t"); err != nil {
		t.Fatalf("expected the default device to run, got %v", err)
	}
	if got == nil {
		t.Fatal("expected the device runner to be called")
	}
	if got.device != "bluefors_gen1" {
		t.Errorf("expected the operation's default device, got %q", got.device)
	}
	if got.name != "qpi-monitor" {
		t.Errorf("expected the operation's default name, got %q", got.name)
	}
}

func TestTheOldSubcommandsAreGone(t *testing.T) {
	// The grammar broke once, deliberately (RFC 0003 §11). A subcommand that
	// still worked would make the migration optional and the docs wrong.
	for _, operation := range []string{"process", "monitor"} {
		if _, err := run(t, operation, "--token", "t"); err == nil {
			t.Errorf("expected %q to no longer be a subcommand", operation)
		}
	}
}

func TestStartHelpListsOperationsAndDevices(t *testing.T) {
	// Generated from the operation table, so a new device appears with no change
	// to the help text.
	out, err := run(t, "start", "--help")
	if err != nil {
		t.Fatalf("expected --help to succeed, got %v", err)
	}
	for _, want := range []string{"process", "monitor", "bluefors_gen1", "--operation"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected --help to mention %q, got:\n%s", want, out)
		}
	}
}

func TestParseOptionsRejectsAPairWithoutEquals(t *testing.T) {
	if _, err := parseOptions([]string{"channels=a:K"}); err != nil {
		t.Fatalf("expected a valid pair to parse, got %v", err)
	}
	if _, err := parseOptions([]string{"not-a-pair"}); err == nil {
		t.Error("expected a pair without '=' to be rejected")
	}
}
