"""Curve fits over acquisition data, backend-agnostic (RFC 0004 §6.2).

Every fit here raises :class:`FitError` when it cannot produce a usable answer,
and every fit that yields a device parameter checks the answer against the sweep
that produced it. Both rules exist for the same reason: a fit that returns zeros
on failure gets written to the device as though it were a measurement, and a
Lorentzian centre outside its own scan range is a failed fit, not a new qubit
frequency (RFC 0004 §10).
"""

from .chevron import fit_chevron, fit_conditional_phase
from .core import FitError, align, require_in_range, require_positive, signal_of
from .cosine import (
    decaying_cosine,
    fit_drag,
    fit_fine_amplitude,
    fit_rabi,
    fit_ramsey,
)
from .discrimination import fit_readout_discrimination
from .exponential import (
    exponential_decay,
    fit_rb_decay,
    fit_t1,
    fit_t2,
)
from .lorentzian import (
    fit_punchout,
    fit_qubit_spectroscopy,
    fit_resonator_spectroscopy,
    fit_spectroscopy_power,
    lorentzian,
)
from .trace import fit_readout_timing

__all__ = [
    "FitError",
    "align",
    "require_in_range",
    "require_positive",
    "signal_of",
    "lorentzian",
    "fit_resonator_spectroscopy",
    "fit_qubit_spectroscopy",
    "fit_spectroscopy_power",
    "fit_punchout",
    "fit_readout_timing",
    "fit_readout_discrimination",
    "decaying_cosine",
    "fit_rabi",
    "fit_ramsey",
    "fit_drag",
    "fit_fine_amplitude",
    "exponential_decay",
    "fit_t1",
    "fit_t2",
    "fit_rb_decay",
    "fit_chevron",
    "fit_conditional_phase",
]
