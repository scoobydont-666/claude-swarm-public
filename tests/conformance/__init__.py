"""Routing Protocol v1 Conformance Test Suite.

Tests validate that the protocol correctly handles dispatch decisions,
state persistence, and enforcement hooks across distinct agent kinds:
  - ProjectA (multi-repo cross-edit, long thinking)
  - TaxPrep (interview pipeline, rapid dispatch bursts)
  - Reference (minimal, text-only, single-file edits)

Shares temp directory + fixture setup via conftest.py.
"""
