"""Routing label of this provider package.

The label equals the package directory name (design §7.1: one
package per source). It is derived from the filesystem instead of
being written as a string literal: it is a routing name, not a
module reference, and the zero-dependency scanner (AC-16) keeps
runtime ``*.py`` free of upstream-root string constants.
"""

from pathlib import Path

#: Routing label of the akshare source (equals the package name).
SOURCE = Path(__file__).resolve().parent.name
