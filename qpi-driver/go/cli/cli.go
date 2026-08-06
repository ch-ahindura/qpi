// Package cli is the `qpi-driver` command line, built over the [devices] registry
// (RFC 0003 §4, §6).
//
// It is a package rather than a `main` so that a build of your own can be the CLI:
// register your device with [devices.Register] and call [Execute], and your binary
// has the same `start`/`devices`/`version` commands as the officially maintained
// one. Go resolves imports at compile time, so this — not a device named in a
// string — is how a device from outside the SDK reaches the CLI. See the module
// README.
//
// The command that ships with the SDK is exactly that: qpi-driver/main.go
// registers the built-in devices and calls Execute.
package cli

import (
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
var Version = "0.4.0"

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
// same commands as the stock one.
func NewRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:           "qpi-driver",
		Short:         "Quantum Processing Interface (QPI) Driver CLI",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.AddCommand(newStartCmd(), newDevicesCmd(), newVersionCmd())
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
		Long: "Run a driver: one --operation, on one --device within it (RFC 0001 §4).\n\n" +
			"QPI-UI is where a driver is registered and where the command to run it —\n" +
			"this operation, this device, these -o options — is generated. Run\n" +
			"`qpi-driver devices` to see which devices this build has.",
		RunE: func(_ *cobra.Command, _ []string) error {
			return runStart(cf)
		},
	}
	addCommonFlags(cmd, cf)
	return cmd
}

// newDevicesCmd lists what this build can run — names only. What a device does and
// which -o keys it takes is documented where a driver is registered, in QPI-UI.
func newDevicesCmd() *cobra.Command {
	var operation string
	cmd := &cobra.Command{
		Use:   "devices",
		Short: "List the devices this build can run, by operation",
		RunE: func(cmd *cobra.Command, _ []string) error {
			wanted := devices.Operations()
			if operation != "" {
				if !devices.KnownOperation(devices.Operation(operation)) {
					return fmt.Errorf("unknown operation %q; valid operations: %s",
						operation, strings.Join(devices.OperationNames(), ", "))
				}
				wanted = []devices.Operation{devices.Operation(operation)}
			}
			for _, op := range wanted {
				names := devices.DeviceNames(devices.Devices(op))
				listed := "none"
				if len(names) > 0 {
					listed = strings.Join(names, ", ")
				}
				fmt.Fprintf(cmd.OutOrStdout(), "%s: %s\n", op, listed)
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&operation, "operation", "",
		"Show only this operation's devices, instead of all of them")
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
		"Which backend to run within the operation; `qpi-driver devices` lists them")
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

// runStart resolves the device within the chosen operation, hands it its -o
// options, and runs the driver its builder returns — the same sequence as the
// Python CLI's shared handler.
//
// An option no device read is reported after the build rather than checked against a
// declared list beforehand: the device's own code is the only description of what it
// accepts (RFC 0003 §9).
func runStart(cf *commonFlags) error {
	if cf.operation == "" {
		return fmt.Errorf("--operation is required; one of %s (or set QPI_OPERATION)",
			strings.Join(devices.OperationNames(), ", "))
	}
	operation := devices.Operation(cf.operation)
	if !devices.KnownOperation(operation) {
		return fmt.Errorf("unknown operation %q; valid operations: %s",
			cf.operation, strings.Join(devices.OperationNames(), ", "))
	}
	if cf.token == "" {
		return fmt.Errorf("access token is required; set --token/-t or the QPI_ACCESS_TOKEN environment variable")
	}
	// There is no default device: QPI-UI generates the command that launches a driver
	// and it always names one. Say what this build has instead of guessing — unless it
	// has none, in which case Resolve's own message is the one worth printing.
	if names := devices.DeviceNames(devices.Devices(operation)); cf.device == "" && len(names) > 0 {
		return fmt.Errorf("--device is required: this build's %s devices are %s",
			operation, strings.Join(names, ", "))
	}

	device, err := devices.Resolve(operation, cf.device)
	if err != nil {
		return err
	}
	raw, err := parseOptions(cf.options)
	if err != nil {
		return err
	}
	opts := devices.NewOptions(raw)

	driver, err := device.Build(configOf(cf), opts)
	if err != nil {
		return err
	}
	if unread := opts.Unread(); len(unread) > 0 {
		label := "option"
		if len(unread) > 1 {
			label = "options"
		}
		return fmt.Errorf("unknown %s %q for %s device %q", label,
			strings.Join(unread, "\", \""), operation, cf.device)
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
// syntax is the CLI's business; which keys mean anything and what type each value
// has belong to the device that reads them.
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
