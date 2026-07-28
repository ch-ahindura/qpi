/**
 * Rendering the device catalog — for `--help`, for people, and for programs.
 *
 * The catalog itself is {@link "./devices.js" | ./devices}; this only turns it into
 * text and JSON. Nothing here imports a device or runs one.
 *
 * @packageDocumentation
 */

import {
  type DeviceSpec,
  hasDevice,
  type Operation,
  type OptionSpec,
  devices,
  lookupOperation,
  operations,
} from "./devices.js";

/**
 * The version of the `catalog --json` shape, not of the package. It is shared with
 * the Python and Go SDKs: the same document, produced by three implementations, so
 * a change a consumer must notice is a change in all three (RFC 0003 §9).
 */
export const SCHEMA_VERSION = 1;

/** One `-o` key as the catalog reports it. */
export interface CatalogOption {
  key: string;
  help: string;
  type: string;
  /** As typed; `null` when there is no default. */
  default: string | null;
  required: boolean;
  example: string;
}

/** One device as the catalog reports it. */
export interface CatalogDevice {
  name: string;
  summary: string;
  extra: string;
  options: CatalogOption[];
}

/** One operation and the devices this build can run for it. */
export interface CatalogOperation {
  name: string;
  summary: string;
  default_device: string;
  events: string[];
  devices: CatalogDevice[];
}

/**
 * `catalog --json`, and the shape is a contract. It is field-for-field the document
 * the Python SDK's `catalog_dict` and the Go SDK's `devices.Catalog` produce —
 * QPI-UI checks its own catalog against it, and a test compares the SDKs — so add
 * fields rather than renaming or removing them, and bump {@link SCHEMA_VERSION} when
 * a consumer must notice.
 */
export interface Catalog {
  schema_version: number;
  operations: CatalogOperation[];
}

/** Build the catalog document from what is registered. */
export function catalog(): Catalog {
  return {
    schema_version: SCHEMA_VERSION,
    operations: operations().map((operation) => ({
      name: operation.name,
      summary: operation.summary,
      default_device: operation.defaultDevice,
      events: operation.events.map(String),
      devices: devices(operation.name).map(catalogDevice),
    })),
  };
}

function catalogDevice(spec: DeviceSpec): CatalogDevice {
  return {
    name: spec.name,
    summary: spec.summary ?? "",
    extra: spec.extra ?? "",
    options: (spec.options ?? []).map((option) => ({
      key: option.key,
      help: option.help,
      type: option.type ?? "str",
      default: option.default === undefined ? null : option.default,
      required: option.required === true,
      example: option.example ?? "",
    })),
  };
}

/**
 * The catalog as readable text: operations, their devices, their options. What
 * `devices` prints and what `start --help` appends, generated from the specs so a
 * new device appears with no change to any help string.
 *
 * With no operation given, every operation is covered.
 */
export function renderCatalog(operation?: Operation): string {
  const lines: string[] = [];
  for (const spec of operations()) {
    if (operation !== undefined && spec.name !== operation) {
      continue;
    }
    lines.push(`--operation ${spec.name} — ${spec.summary}`);
    lines.push(...deviceLines(spec.name).map((line) => `  ${line}`));
  }
  return lines.join("\n");
}

/**
 * One line per device and per `-o` option of an operation. Devices sharing an option
 * schema are not made to repeat it.
 */
export function deviceLines(operation: Operation): string[] {
  const specs = devices(operation);
  const spec = lookupOperation(operation);

  // Only advertise the default if this build actually has it: a bundle that
  // registered devices of its own may well not.
  const defaultDevice = spec?.defaultDevice ?? "";
  const header =
    `Devices for ${operation} (-d/--device)` +
    (hasDevice(operation, defaultDevice) ? `, default ${defaultDevice}` : "") +
    ":";
  const lines = [header];

  if (specs.length === 0) {
    lines.push(
      `• (this build ships no ${operation} devices; see the Python SDK, ` +
        `pip install "qpi-driver[cli]")`,
    );
  }

  for (const device of specs) {
    lines.push(
      `• ${device.name}` + (device.summary ? ` — ${device.summary}` : ""),
    );
  }
  for (const device of specs) {
    if (!device.options?.length) {
      continue;
    }
    lines.push(`Options (-o key=value) for ${device.name}:`);
    lines.push(...device.options.map(optionLine));
  }

  lines.push(
    `Any other ${operation} device can be named by import path — ` +
      `-d ./my-device.js#MyExport, which must export a device builder or a ` +
      `DeviceSpec. Having no declared schema, its -o options are passed to it as ` +
      `typed, unchecked.`,
  );
  return lines;
}

function optionLine(option: OptionSpec): string {
  let note: string;
  if (option.required && option.example) {
    note = `required, e.g. ${option.example}`;
  } else if (option.required) {
    note = "required";
  } else if (option.default) {
    note = `default: ${option.default}`;
  } else if (option.example) {
    note = `optional, e.g. ${option.example}`;
  } else {
    note = "optional";
  }
  return `• ${option.key}=<${option.type ?? "str"}> (${note}) — ${option.help}`;
}
