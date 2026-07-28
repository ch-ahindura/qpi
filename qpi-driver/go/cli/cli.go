// Package cli is the `qpi-driver` command line, built over the [devices] registry
// (RFC 0003 §4, §6).
//
// It is a package rather than a `main` so that a build of your own can be the CLI:
// register your device with [devices.Register] and call [Execute], and your binary
// has the same `start`/`devices`/`catalog` commands, the same generated help and
// the same option validation as the officially maintained one. Go resolves imports
// at compile time, so this — not a device named in a string — is how a device from
// outside the SDK reaches the CLI. See the module README.
//
// The command that ships with the SDK is exactly that: qpi-driver/main.go
// registers the built-in devices and calls Execute.
package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// Version is the CLI version; overridable at build time with
// -ldflags "-X github.com/sopherapps/qpi/qpi-driver/go/cli.Version=…".
var Version = "0.1.2"

// commonFlags are the universal options `start` shares across every operation,
// mirroring the Python CLI. A device's own settings go through -o instead.
type commonFlags struct {
	operation     string
	qpiAddr       string
	token         string
	device        string
	caFile        string
	caFingerprint string
	options       []string
	recvTimeoutMs int
}

// Execute runs the CLI and exits non-zero on failure. A main that has registered
// its own devices calls this and is done.
func Execute() {
	if err := NewRootCmd().Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}

// NewRootCmd builds the whole command tree over whatever is in
// [devices.Default] — so a binary that registered a device of its own gets the
// same commands, the same generated help and the same option validation.
func NewRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:           "qpi-driver",
		Short:         "Quantum Processing Interface (QPI) Driver CLI",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.AddCommand(newStartCmd(), newDevicesCmd(), newCatalogCmd(), newVersionCmd())
	return root
}

// newStartCmd builds the one verb that runs a driver. One command rather than a
// subcommand per operation, because everything about launching a driver is the
// same whichever operation it is (RFC 0003 §4).
func newStartCmd() *cobra.Command {
	cf := &commonFlags{}
	cmd := &cobra.Command{
		Use:   "start",
		Short: "Run a driver: one --operation, on one --device within it (RFC 0001 §4).",
		RunE: func(_ *cobra.Command, _ []string) error {
			return runStart(cf)
		},
	}
	// Generated from the registry at command-construction time, so a device
	// registered before the CLI is assembled shows up in --help with no change here.
	cmd.SetHelpTemplate(cmd.HelpTemplate() + "\n" + devices.RenderCatalog(devices.Default, ""))
	addCommonFlags(cmd, cf)
	return cmd
}

func newDevicesCmd() *cobra.Command {
	var operation string
	cmd := &cobra.Command{
		Use:   "devices",
		Short: "List the operations, the devices each one can run, and their -o options",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if operation != "" {
				if _, ok := devices.LookupOperation(devices.Operation(operation)); !ok {
					return fmt.Errorf("unknown operation %q; valid operations: %s",
						operation, strings.Join(devices.OperationNames(), ", "))
				}
			}
			fmt.Fprint(cmd.OutOrStdout(),
				devices.RenderCatalog(devices.Default, devices.Operation(operation)))
			return nil
		},
	}
	cmd.Flags().StringVar(&operation, "operation", "",
		"Show only this operation's devices, instead of all of them")
	return cmd
}

func newCatalogCmd() *cobra.Command {
	var asText bool
	cmd := &cobra.Command{
		Use:   "catalog",
		Short: "Print the whole device catalog for another program to read",
		Long: "Print the whole device catalog for another program to read.\n\n" +
			"The JSON shape is a contract — QPI-UI checks its own catalog against it, and\n" +
			"the Python and TypeScript SDKs produce the same document — and is documented\n" +
			"in full on devices.Catalog (RFC 0003 §9).",
		RunE: func(cmd *cobra.Command, _ []string) error {
			if asText {
				fmt.Fprint(cmd.OutOrStdout(), devices.RenderCatalog(devices.Default, ""))
				return nil
			}
			encoded, err := json.MarshalIndent(devices.CatalogOf(devices.Default), "", "  ")
			if err != nil {
				return err
			}
			fmt.Fprintln(cmd.OutOrStdout(), string(encoded))
			return nil
		},
	}
	cmd.Flags().BoolVar(&asText, "text", false,
		"Print the same text `devices` shows, instead of JSON")
	cmd.Flags().Bool("json", true, "Print the catalog as JSON (the default)")
	return cmd
}

func addCommonFlags(cmd *cobra.Command, cf *commonFlags) {
	f := cmd.Flags()
	// No short form for --operation: -o is --option, and -O beside it would be a
	// hazard in a command usually written once into a unit file (RFC 0003 §13.7).
	f.StringVar(&cf.operation, "operation", os.Getenv("QPI_OPERATION"),
		"What this driver does: "+strings.Join(devices.OperationNames(), " | "))
	f.StringVarP(&cf.qpiAddr, "qpi-addr", "a", envOr("QPI_ADDR", "http://127.0.0.1:8090"),
		"Full URL of the QPI server")
	f.StringVarP(&cf.token, "token", "t", os.Getenv("QPI_ACCESS_TOKEN"),
		"Access token identifying this driver to the QPI server")
	f.StringVarP(&cf.device, "device", "d", os.Getenv("QPI_DEVICE"),
		"Which backend to run within the operation; defaults to the operation's own")
	f.StringVar(&cf.caFile, "ca-file", envOr("QPI_CA_FILE", "./bin/qpi.ca.pem"),
		"Where the downloaded server root CA certificate is written")
	f.StringVar(&cf.caFingerprint, "ca-fingerprint", os.Getenv("QPI_CA_FINGERPRINT"),
		"SHA-256 fingerprint pinning the downloaded root CA of the QPI server")
	f.StringArrayVarP(&cf.options, "option", "o", nil,
		"A setting of the chosen device as key=value, repeatable (e.g. -o channels=mapper.bf.tmc:K)")
	f.IntVar(&cf.recvTimeoutMs, "recv-timeout-ms",
		envIntOr("QPI_RECV_TIMEOUT_MS", int(qpidriver.DefaultRecvTimeout/time.Millisecond)),
		"How long the receive loop blocks per attempt before checking for shutdown, in ms")
}

// runStart resolves the device within the chosen operation, lets that device's own
// schema check the -o options, and runs the driver its builder returns — the same
// sequence as the Python CLI's shared handler.
func runStart(cf *commonFlags) error {
	if cf.operation == "" {
		return fmt.Errorf("--operation is required; one of %s (or set QPI_OPERATION)",
			strings.Join(devices.OperationNames(), ", "))
	}
	operation := devices.Operation(cf.operation)
	spec, ok := devices.LookupOperation(operation)
	if !ok {
		return fmt.Errorf("unknown operation %q; valid operations: %s",
			cf.operation, strings.Join(devices.OperationNames(), ", "))
	}
	if cf.token == "" {
		return fmt.Errorf("access token is required; set --token/-t or the QPI_ACCESS_TOKEN environment variable")
	}

	if cf.device == "" {
		// The operation's default device is a fact about the operation, not about
		// this build: a binary that registered its own devices may not have it. Ask
		// for a choice rather than failing over a device the operator never typed.
		if !devices.Default.Has(operation, spec.DefaultDevice) {
			if names := devices.DeviceNames(devices.Devices(operation)); len(names) > 0 {
				return fmt.Errorf("--device is required: this build's %s devices are %s",
					operation, strings.Join(names, ", "))
			}
		}
		cf.device = spec.DefaultDevice
	}
	device, err := devices.Resolve(operation, cf.device)
	if err != nil {
		return err
	}
	raw, err := parseOptions(cf.options)
	if err != nil {
		return err
	}
	opts, err := device.ParseOptions(raw)
	if err != nil {
		return err
	}

	driver, err := device.Build(configOf(cf), opts)
	if err != nil {
		return err
	}
	return qpidriver.Run(driver, configOf(cf))
}

// configOf is the universal transport config every driver is built with.
func configOf(cf *commonFlags) qpidriver.Config {
	return qpidriver.Config{
		QpiAddr:       cf.qpiAddr,
		Token:         cf.token,
		CaFingerprint: cf.caFingerprint,
		CaFilePath:    cf.caFile,
		RecvTimeout:   time.Duration(cf.recvTimeoutMs) * time.Millisecond,
	}
}

func newVersionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Show the version of the QPI driver CLI",
		RunE: func(cmd *cobra.Command, _ []string) error {
			fmt.Fprintln(cmd.OutOrStdout(), Version)
			return nil
		},
	}
}

// parseOptions turns repeatable `-o key=value` flags into raw strings. Only the
// syntax is the CLI's business; which keys exist and what type each value has
// belong to the chosen device's schema, which runs on the result.
func parseOptions(pairs []string) (map[string]string, error) {
	opts := make(map[string]string, len(pairs))
	for _, pair := range pairs {
		key, value, ok := strings.Cut(pair, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" {
			return nil, fmt.Errorf("invalid option %q; expected key=value", pair)
		}
		opts[key] = strings.TrimSpace(value)
	}
	return opts, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envIntOr(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}
