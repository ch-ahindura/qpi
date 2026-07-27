"""Rendering the device catalog — for ``--help``, for people, and for programs.

The catalog itself is :mod:`qpi_driver.builtins.registry`; this module only turns
it into text. Nothing here imports a device or runs one: discovering what exists
must never require having installed all of it (RFC 0003 §5), so an install target
that is not fully installed is reported from package metadata alone.

Mirrors ``qpi-ui/internal/drivers/catalog.go``, whose half of the catalog is data
in exactly the same way. :func:`catalog_dict` is the shape those two compare
against each other (RFC 0003 §9).
"""

import importlib.metadata
import re
from typing import Any

from qpi_driver.builtins.registry import (
    DeviceSpec,
    Operation,
    OptionSpec,
    devices,
    operations,
)

# The version of the `catalog --json` shape, not of the package. Bump it only for
# a change a consumer must notice — Go, JS and qpi-ui all read this document.
SCHEMA_VERSION = 1

# The distribution whose extras ship the built-in devices.
_DISTRIBUTION = "qpi-driver"

# The leading name of a requirement string, e.g. `qblox-scheduler` in
# `qblox-scheduler>=1.0; extra == "qblox"`.
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9._-]+")


def catalog_dict() -> dict[str, Any]:
    """The whole catalog as plain data, in the shape ``catalog --json`` prints.

    **This shape is a contract.** Go, JS and QPI-UI all consume it, so add fields
    rather than renaming or removing them, and bump :data:`SCHEMA_VERSION` when a
    consumer must notice the change (RFC 0003 §9)::

        {
          "schema_version": 1,
          "operations": [
            {
              "name": "process",                  # the --operation / subcommand
              "summary": "Run quantum jobs …",
              "default_device": "mock",           # used when --device is omitted
              "events": ["JobDispatch", "JobResult"],
              "devices": [
                {
                  "name": "qblox",                # the --device value
                  "summary": "Qblox scheduler …",
                  "extra": "qpi-driver[cli,qblox]",   # "" when base [cli] is enough
                  "options": [
                    {
                      "key": "data_dir",          # the -o key
                      "help": "Directory the executor …",
                      "type": "safe_dir",         # kind of value expected
                      "default": "./bin/data",    # as typed; null when there is none
                      "required": false,
                      "example": "./bin/data"
                    }
                  ]
                }
              ]
            }
          ]
        }

    Key order is the declaration order of the operations, the options, and the
    sorted device names, so two runs of the same version produce byte-identical
    output and a drift check can diff it.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "operations": [
            {
                "name": spec.name.value,
                "summary": spec.summary,
                "default_device": spec.default_device,
                "events": [event.value for event in spec.events],
                "devices": [_device_dict(device) for device in devices(spec.name)],
            }
            for spec in operations()
        ],
    }


def _device_dict(spec: DeviceSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "summary": spec.summary,
        "extra": spec.extra,
        "options": [
            {
                "key": option.key,
                "help": option.help,
                "type": option.type_name,
                "default": option.default,
                "required": option.required,
                "example": option.example,
            }
            for option in spec.options
        ],
    }


def device_lines(operation: Operation) -> tuple[str, ...]:
    """One line per device and per ``-o`` option of *operation*, for generated help.

    Logical lines, not display lines: the caller decides how to join them, since
    ``--help`` needs a blank line between each and a terminal listing does not.
    Devices sharing an option schema — every ``process`` device does — share one
    options block rather than repeating it.

    A device that cannot be described takes its own line saying so, so one broken
    or half-installed device can never take down ``--help`` (RFC 0003 §5).
    """
    specs = devices(operation)
    lines = [
        f"Devices for {operation.value} (-d/--device, "
        f"default {_default_device(operation)}):"
    ]

    schemas: dict[tuple[OptionSpec, ...], list[str]] = {}
    for spec in specs:
        try:
            lines.append(_device_line(spec))
            schemas.setdefault(spec.options, []).append(spec.name)
        except Exception as exc:  # a third-party spec must not break --help
            name = getattr(spec, "name", "?")
            lines.append(f"• {name} — cannot be described: {type(exc).__name__}")

    for options, names in schemas.items():
        if not options:
            continue
        lines.append(f"Options (-o key=value) for {_device_list(names, specs)}:")
        lines.extend(_option_line(option) for option in options)

    return tuple(lines)


def render_catalog(operation: Operation | None = None) -> str:
    """The catalog as readable text: operations, their devices, their options.

    Covers every operation unless *operation* narrows it to one.
    """
    wanted = operations() if operation is None else [_operation_spec(operation)]
    blocks = []
    for spec in wanted:
        lines = [f"{spec.name.value} — {spec.summary}"]
        lines.extend(f"  {line}" for line in device_lines(spec.name))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def missing_requirements(extra: str) -> tuple[str, ...]:
    """Distributions the install target *extra* needs that are not installed.

    *extra* is a :attr:`DeviceSpec.extra` as written, e.g. ``qpi-driver[cli,qblox]``.
    Answered from this package's own metadata, so no device module is imported to
    find out. Anything undeterminable — running from a source tree with no
    installed distribution, a requirement that does not parse — counts as
    installed: reporting a working device as missing is the worse error.
    """
    wanted = _extra_names(extra)
    if not wanted:
        return ()

    try:
        requirements = importlib.metadata.requires(_DISTRIBUTION) or []
    except importlib.metadata.PackageNotFoundError:
        return ()

    return missing_for_extras(wanted, requirements)


def missing_for_extras(
    wanted: tuple[str, ...], requirements: list[str]
) -> tuple[str, ...]:
    """The not-installed distributions *requirements* pull in for *wanted* extras.

    Split out from :func:`missing_requirements` so the resolution can be tested
    against a requirement list rather than against whatever happens to be
    installed. An extra of this same distribution — ``qpi-driver[aer]``, how the
    ``qiskit_aer`` extra is written — is followed through, once each.
    """
    missing: list[str] = []
    seen: set[str] = set()
    pending = list(wanted)

    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)

        for requirement in requirements:
            specifier, _, marker = requirement.partition(";")
            if (
                f'extra == "{name}"' not in marker
                and f"extra == '{name}'" not in marker
            ):
                continue

            match = _REQUIREMENT_NAME.match(specifier.strip())
            if match is None:
                continue
            distribution = match.group(0)

            if _canonical(distribution) == _canonical(_DISTRIBUTION):
                pending.extend(_extra_names(specifier.strip()))
            elif not _is_installed(distribution):
                missing.append(distribution)

    return tuple(dict.fromkeys(missing))


def _device_line(spec: DeviceSpec) -> str:
    """``• qblox — Qblox scheduler (legacy). unavailable: pip install "…"``"""
    parts = [f"• {spec.name}"]
    if spec.summary:
        parts.append(f"— {spec.summary}")
    if missing_requirements(spec.extra):
        parts.append(f'unavailable: pip install "{spec.extra}"')
    return " ".join(parts)


def _option_line(option: OptionSpec) -> str:
    """``• data_dir=<safe_dir> (default: ./bin/data) — Directory the executor …``"""
    if option.required:
        note = f"required, e.g. {option.example}" if option.example else "required"
    elif option.default:
        note = f"default: {option.default}"
    else:
        note = f"optional, e.g. {option.example}" if option.example else "optional"
    return f"• {option.key}=<{option.type_name}> ({note}) — {option.help}"


def _device_list(names: list[str], specs: tuple[DeviceSpec, ...]) -> str:
    """Name the devices an options block applies to, or say "every" if it is all."""
    if len(names) > 1 and len(names) == len(specs):
        return f"every {specs[0].operation.value} device"
    return ", ".join(names)


def _operation_spec(operation: Operation):
    for spec in operations():
        if spec.name is operation:
            return spec
    raise ValueError(f"unknown operation {operation!r}")


def _default_device(operation: Operation) -> str:
    return _operation_spec(operation).default_device


def _extra_names(extra: str) -> tuple[str, ...]:
    """The extras an install target names: ``qpi-driver[cli,qblox]`` → cli, qblox."""
    _, bracket, rest = extra.partition("[")
    if not bracket:
        return ()
    return tuple(name.strip() for name in rest.rstrip("]").split(",") if name.strip())


def _is_installed(distribution: str) -> bool:
    try:
        importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _canonical(distribution: str) -> str:
    return distribution.lower().replace("_", "-")
