#!/usr/bin/env bash
#
# Compile the Go and TypeScript snippets in the documentation.
#
# Run through `make test-docs`. A snippet in a compiled language is checked by the
# compiler and by nothing else, so a block nobody compiles is a block that stops
# building the moment an API moves — the TypeScript `registerDevice` example did not
# type-check at all, and there was no way to find that out short of pasting it.
#
# Each self-contained block is extracted to a scratch directory wired to the SDK in
# *this* checkout, then built. Nothing is checked in beside the README, so there is no
# second copy to keep in step: the document is the source.
#
# A block is opted out by preceding it in the document with:
#
#     <!-- docs-check: skip -->
#
# and one is opted *in* by naming it:
#
#     <!-- docs-check: compile=<name> -->
#
# Only named blocks are compiled. A `go` block showing three lines of a struct
# literal is illustration, not a program, and a harness that tried to build every
# fence would spend its life being told so.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAILURES=0
CHECKED=0

# extract <document> <name>
#
# Prints the fenced block that the marker `<!-- docs-check: compile=<name> -->`
# precedes. Exits 1 if there is no such block, because a name in this script that no
# longer matches the document is the same kind of rot as an uncompiled block.
extract() {
    local document="$1" name="$2"
    awk -v want="$name" '
        $0 ~ "<!-- docs-check: compile=" want " -->" { armed = 1; next }
        armed && /^```/ { armed = 0; inside = 1; next }
        inside && /^```/ { inside = 0; found = 1; exit }
        inside { print }
        END { if (!found) exit 1 }
    ' "$document"
}

report() {
    local label="$1" status="$2" output="$3"
    CHECKED=$((CHECKED + 1))
    if [ "$status" -eq 0 ]; then
        echo "[check-snippets] ✓ $label"
    else
        FAILURES=$((FAILURES + 1))
        echo "[check-snippets] ✗ $label"
        echo "$output" | sed 's/^/    /'
    fi
}

# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------
# Built in a throwaway module whose `replace` directives point at this checkout, so
# the snippet is compiled against the code in the working tree rather than whatever
# is published.

check_go() {
    local label="$1" document="$2" name="$3" module="$4" path="$5"
    local dir="$WORK/go/$name"
    mkdir -p "$dir"

    if ! extract "$document" "$name" > "$dir/main.go"; then
        report "$label" 1 "no block marked 'docs-check: compile=$name' in $document"
        return
    fi

    cat > "$dir/go.mod" <<EOF
module docsnippet/$name

go 1.25

require $module v0.0.0

replace $module => $ROOT/$path
EOF

    local output status
    output=$(cd "$dir" && go mod tidy 2>&1 && go build -o /dev/null ./... 2>&1) && status=0 || status=$?
    report "$label" "$status" "$output"
}

# ---------------------------------------------------------------------------
# TypeScript
# ---------------------------------------------------------------------------
# Type-checked in a scratch package with the built SDK symlinked into node_modules,
# which is how a reader consumes it: `qpi-driver` and `qpi-driver/devices` resolve
# through the real export map, against the real .d.ts files, rather than through a
# path this script invented. Needs `npm run build` in the SDK first, which
# `make test-docs` does.
#
# The scratch package is ESM ("type": "module") because a README block ends in a
# top-level `await new …().run()`, as a reader's entry point would.

check_ts() {
    local label="$1" document="$2" name="$3"
    local sdk="$ROOT/qpi-driver/js"
    local dir="$WORK/ts/$name"
    mkdir -p "$dir/node_modules"

    if ! extract "$document" "$name" > "$dir/snippet.ts"; then
        report "$label" 1 "no block marked 'docs-check: compile=$name' in $document"
        return
    fi

    ln -sfn "$sdk" "$dir/node_modules/qpi-driver"
    ln -sfn "$sdk/node_modules/typescript" "$dir/node_modules/typescript"
    ln -sfn "$sdk/node_modules/@types" "$dir/node_modules/@types"
    printf '{"name":"docsnippet-%s","private":true,"type":"module"}\n' "$name" \
        > "$dir/package.json"
    cat > "$dir/tsconfig.json" <<'EOF'
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["snippet.ts"]
}
EOF

    local output status
    output=$(cd "$dir" && ./node_modules/typescript/bin/tsc --project . 2>&1) \
        && status=0 || status=$?
    report "$label" "$status" "$output"
}

echo "[check-snippets] compiling the documentation's Go and TypeScript blocks…"

check_go "go SDK: adding a device of your own" \
    "$ROOT/qpi-driver/go/README.md" "go-custom-device" \
    "github.com/sopherapps/qpi/qpi-driver/go" "qpi-driver/go"

check_go "go SDK: the bluefors_gen1 monitor" \
    "$ROOT/qpi-driver/go/README.md" "go-bluefors" \
    "github.com/sopherapps/qpi/qpi-driver/go" "qpi-driver/go"

check_go "go client: quick start" \
    "$ROOT/qpi-client/go/README.md" "go-client-quickstart" \
    "github.com/sopherapps/qpi/qpi-client/go" "qpi-client/go"

check_ts "typescript SDK: the bluefors_gen1 monitor" \
    "$ROOT/qpi-driver/js/README.md" "ts-bluefors"

check_ts "typescript SDK: registering a device" \
    "$ROOT/qpi-driver/js/README.md" "ts-register-device"

if [ "$FAILURES" -gt 0 ]; then
    echo "[check-snippets] ✗ $FAILURES of $CHECKED blocks do not build"
    exit 1
fi
echo "[check-snippets] ✓ $CHECKED blocks build"
