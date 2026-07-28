package drivers

import "text/template"

// snippetData is the flattened view of a registration passed to the setup
// templates. Values that could contain shell metacharacters (Name, Extra) are
// already single-quoted; OptionsEnv and Options are pre-rendered and empty when
// the operation has no per-kind options.
type snippetData struct {
	ScriptURL     string
	Token         string
	QpiAddr       string
	CaFingerprint string
	Name          string
	Device        string
	Operation     string
	Extra         string
	OptionsEnv    string
	Options       string
}

// systemdTmpl is the production one-liner that pipes install-systemd.sh into a
// privileged shell with the registration passed as environment variables.
var systemdTmpl = template.Must(template.New("systemd").Parse(
	`curl -fsSL {{.ScriptURL}} | \
  QPI_TOKEN={{.Token}} QPI_ADDR={{.QpiAddr}} CA_FINGERPRINT={{.CaFingerprint}} QPU_NAME={{.Name}} OPERATION={{.Operation}} DEVICE={{.Device}}{{.OptionsEnv}} sudo -E bash`))

// manualPyCLITmpl installs the driver as a uv tool and runs it directly, for
// operators who would rather not use the systemd installer.
var manualPyCLITmpl = template.Must(template.New("manual-py-cli").Parse(
	`uv tool install --python 3.12 {{.Extra}} && \
  qpi-driver start --operation {{.Operation}} --device {{.Device}} \
    --token {{.Token}} --qpi-addr {{.QpiAddr}} --ca-fingerprint {{.CaFingerprint}} --name {{.Name}}{{.Options}}`))

// manualGoCLITmpl installs the driver CLI with `go install` and runs it
// directly, for operators who would rather not use the systemd installer.
var manualGoCLITmpl = template.Must(template.New("manual-go-cli").Parse(
	`go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver@latest && \
  qpi-driver start --operation {{.Operation}} --device {{.Device}} \
    --token {{.Token}} --qpi-addr {{.QpiAddr}} --ca-fingerprint {{.CaFingerprint}} --name {{.Name}}{{.Options}}`))

// manualTsCLITmpl installs the driver CLI from npm and runs it directly, for
// operators who would rather not use the systemd installer.
var manualTsCLITmpl = template.Must(template.New("manual-ts-cli").Parse(
	`npm install -g qpi-driver && \
  qpi-driver start --operation {{.Operation}} --device {{.Device}} \
    --token {{.Token}} --qpi-addr {{.QpiAddr}} --ca-fingerprint {{.CaFingerprint}} --name {{.Name}}{{.Options}}`))
