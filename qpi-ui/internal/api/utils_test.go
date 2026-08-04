package api

import (
	"strings"
	"testing"

	"qpi/internal/drivers"
)

// TestJoinKindsNamesWhatIsAvailable covers the sentence handleDriverCreate hands an
// operator whose kind×language pairing does not exist. "it ships" followed by
// nothing would be the worst version of this message, so the empty case says so.
func TestJoinKindsNamesWhatIsAvailable(t *testing.T) {
	if got := joinKinds(nil); got != "no built-in devices" {
		t.Errorf("joinKinds(nil) = %q, want a sentence about there being none", got)
	}
	if got := joinKinds(drivers.Default.KindsIn(drivers.Go)); got != `"bluefors_gen1"` {
		t.Errorf("joinKinds(KindsIn(go)) = %q, want just the quoted monitor", got)
	}
	got := joinKinds(drivers.Default.KindsIn(drivers.Python))
	for _, want := range []string{`"mock"`, `"qblox"`, `"bluefors_gen1"`} {
		if !strings.Contains(got, want) {
			t.Errorf("joinKinds(KindsIn(python)) = %q, want it to contain %s", got, want)
		}
	}
}
