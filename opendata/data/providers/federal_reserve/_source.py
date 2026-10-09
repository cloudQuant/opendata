"""Routing label of this provider package.

The label equals the package directory name (design §7.1: one package per
source), derived from the filesystem instead of a string literal.
"""

from pathlib import Path

#: Routing label of the Federal Reserve source (equals the package name).
SOURCE = Path(__file__).resolve().parent.name
