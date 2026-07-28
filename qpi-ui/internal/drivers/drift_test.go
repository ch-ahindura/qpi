package drivers

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The catalog is split, and the split is not arbitrary (RFC 0003 §9):
//
//   - Operations belong to QPI-UI. An operation is the set of event types the
//     server has handlers for, so the SDKs mirror this package's enum the way they
//     already mirror EventType.
//   - Devices and their options belong to the driver. Which backends exist, what
//     `-o` keys each takes, which extra installs it — the SDK knows and the server
//     does not. QPI-UI's copy exists only to render setup snippets.
//
// So this file checks the server's device catalog against the SDK's, and the SDK's
// operations against the server's. Until the server can fetch a catalog from a
// connected driver, the mechanism is this test over a checked-in fixture:
// regenerating it is then the deliberate act of recording a catalog change.

// regenerate is the one command that fixes any failure here. Named in every
// message, so nobody has to work out how the fixture was made.
const regenerate = "make sync-driver-catalog"

// sdkCatalog is the `qpi-driver catalog --json` document, as the SDKs produce it.
// Only the fields this test compares are read; the rest of the document is the
// SDKs' business.
type sdkCatalog struct {
	SchemaVersion int `json:"schema_version"`
	Operations    []struct {
		Name    string   `json:"name"`
		Events  []string `json:"events"`
		Devices []struct {
			Name    string `json:"name"`
			Extra   string `json:"extra"`
			Options []struct {
				Key      string  `json:"key"`
				Help     string  `json:"help"`
				Required bool    `json:"required"`
				Default  *string `json:"default"`
			} `json:"options"`
		} `json:"devices"`
	} `json:"operations"`
}

func loadSdkCatalog(t *testing.T) sdkCatalog {
	t.Helper()
	path := filepath.Join("testdata", "catalog.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v; regenerate it with `%s`", path, err, regenerate)
	}
	var catalog sdkCatalog
	if err := json.Unmarshal(raw, &catalog); err != nil {
		t.Fatalf("parsing %s: %v; regenerate it with `%s`", path, err, regenerate)
	}
	if catalog.SchemaVersion == 0 || len(catalog.Operations) == 0 {
		t.Fatalf("%s looks hand-written or truncated; regenerate it with `%s`", path, regenerate)
	}
	return catalog
}

// TestCatalogHasEveryDeviceTheSdkShips is the check that matters: a device added to
// the SDK and not to catalog.go registers no snippets, so an operator who selects it
// in the dashboard gets nothing to copy.
func TestCatalogHasEveryDeviceTheSdkShips(t *testing.T) {
	catalog := loadSdkCatalog(t)

	for _, operation := range catalog.Operations {
		for _, device := range operation.Devices {
			spec, ok := Default.Lookup(Kind(device.Name))
			if !ok {
				t.Errorf("the SDK ships a %s device %q that this catalog does not know; "+
					"add a Spec for it in catalog.go (then `%s`)",
					operation.Name, device.Name, regenerate)
				continue
			}
			if string(spec.Operation) != operation.Name {
				t.Errorf("device %q is %s in the SDK and %s here",
					device.Name, operation.Name, spec.Operation)
			}
			if spec.Extra != device.Extra {
				t.Errorf("device %q installs from %q in the SDK and %q here",
					device.Name, device.Extra, spec.Extra)
			}
		}
	}
}

// TestCatalogShipsNoDeviceTheSdkDoesNot is the other direction: a Spec for a device
// no SDK has means a dashboard offering a driver that cannot be run.
func TestCatalogShipsNoDeviceTheSdkDoesNot(t *testing.T) {
	catalog := loadSdkCatalog(t)

	known := map[string]bool{}
	for _, operation := range catalog.Operations {
		for _, device := range operation.Devices {
			known[device.Name] = true
		}
	}

	for _, kind := range Default.Kinds() {
		// Custom is deliberately not a device: it is the catch-all for a driver
		// QPI-UI has no built-in knowledge of.
		if kind == Custom || known[string(kind)] {
			continue
		}
		t.Errorf("this catalog offers %q, which no SDK device provides; remove its "+
			"Spec or add the device to the SDK (then `%s`)", kind, regenerate)
	}
}

// TestCatalogOptionsMatchTheSdk checks the option keys, since those are what an
// operator types and what the snippets render. An option in a snippet that the
// driver does not read is a command that exits 1 the first time it is pasted.
func TestCatalogOptionsMatchTheSdk(t *testing.T) {
	catalog := loadSdkCatalog(t)

	for _, operation := range catalog.Operations {
		for _, device := range operation.Devices {
			spec, ok := Default.Lookup(Kind(device.Name))
			if !ok {
				continue // reported by TestCatalogHasEveryDeviceTheSdkShips
			}

			fromSdk := map[string]struct {
				required bool
				fallback *string
			}{}
			for _, option := range device.Options {
				fromSdk[option.Key] = struct {
					required bool
					fallback *string
				}{option.Required, option.Default}
			}

			for _, option := range spec.Options {
				sdk, ok := fromSdk[option.Key]
				if !ok {
					t.Errorf("device %q: this catalog has an option %q the driver does "+
						"not read; a snippet using it would fail (then `%s`)",
						device.Name, option.Key, regenerate)
					continue
				}
				if sdk.required != option.Required {
					t.Errorf("device %q option %q: required is %v in the SDK and %v here",
						device.Name, option.Key, sdk.required, option.Required)
				}
				fallback := ""
				if sdk.fallback != nil {
					fallback = *sdk.fallback
				}
				if fallback != option.Default {
					t.Errorf("device %q option %q: default is %q in the SDK and %q here",
						device.Name, option.Key, fallback, option.Default)
				}
			}

			if missing := missingKeys(fromSdk, spec.Options); len(missing) > 0 {
				t.Errorf("device %q: the driver reads %s, which this catalog does not "+
					"list; add them in catalog.go (then `%s`)",
					device.Name, strings.Join(missing, ", "), regenerate)
			}
		}
	}
}

// TestOperationsMatchTheSdk checks the other direction of truth: the server owns the
// operations, so an SDK that knows one this package does not is an SDK talking about
// events the server has no handler for.
func TestOperationsMatchTheSdk(t *testing.T) {
	catalog := loadSdkCatalog(t)

	fromSdk := make([]string, 0, len(catalog.Operations))
	for _, operation := range catalog.Operations {
		fromSdk = append(fromSdk, operation.Name)
	}
	ours := []string{string(Process), string(Monitor)}

	sort.Strings(fromSdk)
	sorted := append([]string(nil), ours...)
	sort.Strings(sorted)
	if strings.Join(fromSdk, ",") != strings.Join(sorted, ",") {
		t.Errorf("the SDK knows operations %v; this server has handlers for %v. "+
			"Operations are the server's to define (RFC 0003 §9): either add a "+
			"handler here or drop it from the SDKs", fromSdk, ours)
	}

	// The event types each operation involves are the server's definition of it, so
	// an SDK reporting different ones is reporting a different operation.
	for _, operation := range catalog.Operations {
		var want []string
		switch operation.Name {
		case string(Process):
			want = []string{eventJobDispatch, eventJobResult}
		case string(Monitor):
			want = []string{eventCryostatReading}
		default:
			continue
		}
		if strings.Join(operation.Events, ",") != strings.Join(want, ",") {
			t.Errorf("operation %q involves %v in the SDK and %v here",
				operation.Name, operation.Events, want)
		}
	}
}

func missingKeys(
	fromSdk map[string]struct {
		required bool
		fallback *string
	},
	ours []Option,
) []string {
	have := map[string]bool{}
	for _, option := range ours {
		have[option.Key] = true
	}
	missing := make([]string, 0, len(fromSdk))
	for key := range fromSdk {
		if !have[key] {
			missing = append(missing, fmt.Sprintf("%q", key))
		}
	}
	sort.Strings(missing)
	return missing
}
