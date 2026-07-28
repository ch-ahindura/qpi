package devices

import (
	"fmt"
	"strings"
)

// SchemaVersion is the version of the `catalog --json` shape, not of the module.
// It is shared with the Python and TypeScript SDKs: the same document, produced by
// three implementations, so a change a consumer must notice is a change in all
// three (RFC 0003 §9).
const SchemaVersion = 1

// Catalog is `catalog --json`, and the shape is a contract. It is field-for-field
// the document the Python SDK's qpi_driver.builtins.catalog.catalog_dict produces —
// QPI-UI checks its own catalog against it, and a test compares the two SDKs — so
// add fields rather than renaming or removing them, and bump [SchemaVersion] when a
// consumer must notice.
type Catalog struct {
	SchemaVersion int                `json:"schema_version"`
	Operations    []CatalogOperation `json:"operations"`
}

// CatalogOperation is one operation and the devices this build can run for it.
type CatalogOperation struct {
	Name          string          `json:"name"`
	Summary       string          `json:"summary"`
	DefaultDevice string          `json:"default_device"`
	Events        []string        `json:"events"`
	Devices       []CatalogDevice `json:"devices"`
}

// CatalogDevice is one device as the catalog reports it.
type CatalogDevice struct {
	Name    string          `json:"name"`
	Summary string          `json:"summary"`
	Extra   string          `json:"extra"`
	Options []CatalogOption `json:"options"`
}

// CatalogOption is one `-o` key as the catalog reports it. Default is a pointer so
// that "no default" marshals to null, as it does in Python, rather than to "".
type CatalogOption struct {
	Key      string  `json:"key"`
	Help     string  `json:"help"`
	Type     string  `json:"type"`
	Default  *string `json:"default"`
	Required bool    `json:"required"`
	Example  string  `json:"example"`
}

// CatalogOf builds the catalog document for a registry.
func CatalogOf(r *Registry) Catalog {
	catalog := Catalog{SchemaVersion: SchemaVersion}
	for _, operation := range Operations() {
		entry := CatalogOperation{
			Name:          string(operation.Name),
			Summary:       operation.Summary,
			DefaultDevice: operation.DefaultDevice,
			Events:        operation.Events,
			Devices:       []CatalogDevice{},
		}
		for _, spec := range r.Devices(operation.Name) {
			entry.Devices = append(entry.Devices, catalogDevice(spec))
		}
		catalog.Operations = append(catalog.Operations, entry)
	}
	return catalog
}

func catalogDevice(spec DeviceSpec) CatalogDevice {
	device := CatalogDevice{
		Name:    spec.Name,
		Summary: spec.Summary,
		Extra:   spec.Extra,
		Options: []CatalogOption{},
	}
	for _, option := range spec.Options {
		entry := CatalogOption{
			Key:      option.Key,
			Help:     option.Help,
			Type:     option.Type,
			Required: option.Required,
			Example:  option.Example,
		}
		if option.Default != "" {
			value := option.Default
			entry.Default = &value
		}
		if entry.Type == "" {
			entry.Type = "str"
		}
		device.Options = append(device.Options, entry)
	}
	return device
}

// RenderCatalog is the catalog as readable text: operations, their devices, their
// options. It is what `devices` prints and what `start --help` appends, generated
// from the registry so a new device appears with no change to any help string.
//
// With operation empty, every operation is covered.
func RenderCatalog(r *Registry, operation Operation) string {
	var b strings.Builder
	for _, spec := range Operations() {
		if operation != "" && spec.Name != operation {
			continue
		}
		fmt.Fprintf(&b, "--operation %s — %s\n", spec.Name, spec.Summary)
		for _, line := range DeviceLines(r, spec.Name) {
			fmt.Fprintf(&b, "  %s\n", line)
		}
	}
	return b.String()
}

// DeviceLines is one line per device and per `-o` option of operation. Devices
// sharing an option schema are not made to repeat it.
func DeviceLines(r *Registry, operation Operation) []string {
	specs := r.Devices(operation)
	spec, _ := operationOf(operation)

	header := fmt.Sprintf("Devices for %s (-d/--device)", operation)
	// Only advertise the default if this build actually has it: a binary that
	// registered devices of its own may well not.
	if r.Has(operation, spec.DefaultDevice) {
		header += fmt.Sprintf(", default %s", spec.DefaultDevice)
	}
	lines := []string{header + ":"}

	if len(specs) == 0 {
		return append(lines, fmt.Sprintf(
			"• (this build ships no %s devices; see the Python SDK, "+
				"pip install \"qpi-driver[cli]\")", operation))
	}

	for _, device := range specs {
		lines = append(lines, deviceLine(device))
	}
	for _, device := range specs {
		if len(device.Options) == 0 {
			continue
		}
		lines = append(lines, fmt.Sprintf("Options (-o key=value) for %s:", device.Name))
		for _, option := range device.Options {
			lines = append(lines, optionLine(option))
		}
	}
	return lines
}

func deviceLine(spec DeviceSpec) string {
	line := "• " + spec.Name
	if spec.Summary != "" {
		line += " — " + spec.Summary
	}
	return line
}

func optionLine(option OptionSpec) string {
	kind := option.Type
	if kind == "" {
		kind = "str"
	}

	var note string
	switch {
	case option.Required && option.Example != "":
		note = "required, e.g. " + option.Example
	case option.Required:
		note = "required"
	case option.Default != "":
		note = "default: " + option.Default
	case option.Example != "":
		note = "optional, e.g. " + option.Example
	default:
		note = "optional"
	}
	return fmt.Sprintf("• %s=<%s> (%s) — %s", option.Key, kind, note, option.Help)
}
