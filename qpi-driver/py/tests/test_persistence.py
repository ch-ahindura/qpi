"""Device write-back: the trust boundary (RFC 0004 §10).

A tuner writes ``quantify.device.yml``; the `process` driver reads it for every
job. These assert the three guarantees that make that safe — the schema the
loader reads, no tag ``safe_load`` refuses, and nothing replaced until the
candidate has been verified.
"""

import pytest
import yaml
from qpi_driver.tuners.utils.persistence import (
    BACKUP_SUFFIX,
    ELEMENT_TYPE_PROP,
    PersistenceError,
    _equivalent,
    _verify_loads,
    restore_backup,
    save_device_config,
    serialise_device,
)


class FakeParameter:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class Unreadable:
    def get(self):
        raise RuntimeError("instrument offline")


class FakeSubmodule:
    def __init__(self, **parameters):
        self.parameters = {k: FakeParameter(v) for k, v in parameters.items()}


class FakeElement:
    def __init__(self, name, submodules=None, parameters=None):
        self.name = name
        self.submodules = submodules or {}
        self.parameters = parameters or {}


class FakeEdge(FakeElement):
    def __init__(self, name, parent, child, submodules=None):
        super().__init__(name, submodules)
        self._parent_element_name = parent
        self._child_element_name = child


class FakeDevice:
    def __init__(self, elements, edges=None):
        self._elements = elements
        self._edges = edges or {}

    def elements(self):
        return list(self._elements)

    def edges(self):
        return list(self._edges)

    def get_element(self, name):
        return self._elements[name]

    def get_edge(self, name):
        return self._edges[name]


def _device() -> FakeDevice:
    return FakeDevice(
        elements={
            "q0": FakeElement(
                "q0",
                submodules={
                    "rxy": FakeSubmodule(amp180=0.12, motzoi=0.0),
                    "clock_freqs": FakeSubmodule(f01=5e9, readout=6e9),
                },
            )
        },
        edges={"q0_q1": FakeEdge("q0_q1", "q0", "q1", {"cz": FakeSubmodule(amp=0.3)})},
    )


# --- serialisation ------------------------------------------------------------


def test_an_element_is_written_with_the_class_the_loader_rebuilds_it_from():
    config = serialise_device(_device())
    element_type = config["q0"][ELEMENT_TYPE_PROP]

    assert element_type["path"].endswith("FakeElement")
    assert element_type["args"] == ["q0"]
    assert config["q0"]["rxy"] == {"amp180": 0.12, "motzoi": 0.0}


def test_an_edge_is_written_with_the_two_elements_it_joins():
    """By keyword, not position.

    An edge's own name is not what its class is constructed from — the pair it
    joins is. Named rather than positional because qblox's edges are pydantic
    models and pydantic takes no positional arguments at all, so a file written
    with them parses and then fails to instantiate.
    """
    config = serialise_device(_device())
    element_type = config["q0_q1"][ELEMENT_TYPE_PROP]

    assert element_type["args"] == []
    assert element_type["kwargs"] == {
        "parent_element_name": "q0",
        "child_element_name": "q1",
    }


def test_an_element_is_still_written_with_its_own_name():
    """Only edges move to keywords; a qubit is constructed from its name."""
    config = serialise_device(_device())
    assert config["q0"][ELEMENT_TYPE_PROP]["args"] == ["q0"]
    assert config["q0"][ELEMENT_TYPE_PROP]["kwargs"] == {}


def test_structural_fields_are_not_written_as_calibration():
    """`edge_type` and the endpoints are already in `element_type`.

    qblox's elements are pydantic models carrying both as ordinary fields, so
    they come back looking like calibration. Writing them out makes the loader
    try to *assign* them to a constructed object, which its discriminated fields
    refuse — and the file it just wrote no longer loads.
    """
    config = serialise_device(_device())
    for name, element in config.items():
        for structural in ("edge_type", "parent_element_name", "child_element_name"):
            assert structural not in element, (
                f"{name} wrote {structural} as a calibration parameter"
            )


def test_an_unreadable_parameter_is_skipped_not_stringified():
    """str() of a live object round-trips into a numeric parameter as a string."""
    device = FakeDevice(
        elements={
            "q0": FakeElement(
                "q0", submodules={"rxy": FakeSubmodule(amp180=0.1)}, parameters={}
            )
        }
    )
    device._elements["q0"].submodules["rxy"].parameters["broken"] = Unreadable()

    config = serialise_device(device)
    assert config["q0"]["rxy"] == {"amp180": 0.1}


def test_an_object_valued_parameter_is_skipped():
    device = FakeDevice(
        elements={
            "q0": FakeElement("q0", submodules={"rxy": FakeSubmodule(amp180=0.1)})
        }
    )
    device._elements["q0"].submodules["rxy"].parameters["obj"] = FakeParameter(object())

    assert "obj" not in serialise_device(device)["q0"]["rxy"]


def test_the_identity_parameter_is_not_calibration():
    device = FakeDevice(
        elements={"q0": FakeElement("q0", parameters={"IDN": FakeParameter("acme")})}
    )
    assert "IDN" not in serialise_device(device)["q0"]


def test_lists_of_scalars_survive():
    device = FakeDevice(
        elements={"q0": FakeElement("q0", submodules={"p": FakeSubmodule(ports=["a"])})}
    )
    assert serialise_device(device)["q0"]["p"]["ports"] == ["a"]


# --- writing ------------------------------------------------------------------


def test_a_write_produces_plain_safe_yaml(tmp_path):
    """yaml.dump of live objects emits tags the loader's safe_load refuses."""
    path = tmp_path / "device.yml"
    save_device_config(_device(), path)

    text = path.read_text()
    assert "!!python" not in text
    assert yaml.safe_load(text)["q0"]["rxy"]["amp180"] == 0.12


def test_the_previous_file_is_kept(tmp_path):
    path = tmp_path / "device.yml"
    path.write_text("q0: {}\n")
    save_device_config(_device(), path)

    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    assert backup.exists()
    assert backup.read_text() == "q0: {}\n"


def test_the_backup_can_be_restored(tmp_path):
    path = tmp_path / "device.yml"
    path.write_text("original\n")
    save_device_config(_device(), path)
    restore_backup(path)

    assert path.read_text() == "original\n"


def test_restoring_without_a_backup_is_an_error(tmp_path):
    with pytest.raises(PersistenceError, match="no backup"):
        restore_backup(tmp_path / "device.yml")


def test_the_backup_can_be_declined(tmp_path):
    path = tmp_path / "device.yml"
    path.write_text("original\n")
    save_device_config(_device(), path, keep_backup=False)

    assert not path.with_suffix(path.suffix + BACKUP_SUFFIX).exists()


def test_an_empty_device_is_refused(tmp_path):
    """Writing an empty config would leave the process driver with no device."""
    path = tmp_path / "device.yml"
    path.write_text("original\n")

    with pytest.raises(PersistenceError, match="empty device config"):
        save_device_config(FakeDevice(elements={}), path)
    assert path.read_text() == "original\n"


def test_a_failed_write_leaves_the_original_untouched_and_no_litter(
    tmp_path, monkeypatch
):
    """Nothing is replaced until the candidate has passed verification."""
    path = tmp_path / "device.yml"
    path.write_text("original\n")

    monkeypatch.setattr(
        "qpi_driver.tuners.utils.persistence._verify_loads",
        _raise_persistence_error,
    )
    with pytest.raises(PersistenceError):
        save_device_config(_device(), path)

    assert path.read_text() == "original\n"
    assert list(tmp_path.glob("*.tmp")) == []


def _raise_persistence_error(*_args, **_kwargs):
    raise PersistenceError("verification failed")


def test_verification_rejects_an_element_with_no_type(tmp_path):
    candidate = tmp_path / "candidate.yml"
    candidate.write_text("q0:\n  rxy:\n    amp180: 0.1\n")

    with pytest.raises(PersistenceError, match=ELEMENT_TYPE_PROP):
        _verify_loads(candidate, {"q0": {"rxy": {"amp180": 0.1}}})


def test_verification_rejects_an_unusable_class_path(tmp_path):
    candidate = tmp_path / "candidate.yml"
    expected = {"q0": {ELEMENT_TYPE_PROP: {"path": "nodots", "args": ["q0"]}}}
    candidate.write_text(yaml.safe_dump(expected))

    with pytest.raises(PersistenceError, match="unusable"):
        _verify_loads(candidate, expected)


def test_verification_rejects_a_file_that_reads_back_differently(tmp_path):
    candidate = tmp_path / "candidate.yml"
    candidate.write_text("q0: {}\n")

    with pytest.raises(PersistenceError, match="does not read back as written"):
        _verify_loads(candidate, {"q0": {"rxy": {"amp180": 0.1}}})


def test_verification_rejects_a_file_that_is_not_safe_yaml(tmp_path):
    """yaml.dump of live objects emits exactly this, and safe_load refuses it."""
    candidate = tmp_path / "candidate.yml"
    candidate.write_text("q0: !!python/object/apply:os.system ['echo pwned']\n")

    with pytest.raises(PersistenceError, match="does not parse as safe YAML"):
        _verify_loads(candidate, {"q0": {}})


def test_nan_is_not_treated_as_corruption():
    """An uncalibrated qcodes parameter reads as NaN, and NaN never equals itself."""
    assert _equivalent({"a": float("nan")}, {"a": float("nan")})
    assert _equivalent([1.0, float("nan")], [1.0, float("nan")])
    assert not _equivalent({"a": 1.0}, {"a": 2.0})
    assert not _equivalent({"a": 1.0}, {"b": 1.0})
    assert not _equivalent([1.0], [1.0, 2.0])


def test_a_device_with_nan_parameters_still_writes(tmp_path):
    device = FakeDevice(
        elements={
            "q0": FakeElement("q0", submodules={"p": FakeSubmodule(x=float("nan"))})
        }
    )
    save_device_config(device, tmp_path / "device.yml")
    assert (tmp_path / "device.yml").exists()
