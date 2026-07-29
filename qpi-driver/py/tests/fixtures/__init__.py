"""Inputs the tests feed to the code under test, rather than tests themselves.

Mostly data files (``quantify.device.yml``, ``quantify.hardware.json``), but a
fixture can also be a module — ``half_imported_device`` is one, and it needs this
package to be importable by name.
"""
