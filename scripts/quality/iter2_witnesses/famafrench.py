"""Offline witness configuration for the ``famafrench`` provider.

Fama/French is not a JSON or HTML source: each of the six frozen identities downloads one official
ZIP archive from ``mba.tuck.dartmouth.edu`` and parses the single member inside it. The fetchers
call :func:`opendata.data.providers.famafrench._source.fetch_archive`, which reaches the governed
client directly through the module attribute ``get_shared_http_client`` -- there is no
``http_transport`` for these models to honour, which is exactly the precondition that kept all six
rows ``UNREVIEWED``. The witness therefore declares the ``client_seam`` kind
``shared_http_client`` on that one attribute, so the runner substitutes the repository's fake
governed client and the shipped ``zipfile`` / member-selection / parser code stays under test.

All six entries live in ``_parts/famafrench/official_archive_rows.py`` because every one of them
is a verbatim slice of a retained live capture rather than a body reconstructed by hand: this
module carries only the provider-level configuration, and ``witness_module_rels`` pins both files
for every claim here.

The refusals are French's own stable codes. ``FrenchProviderError`` is this provider's
attributable refusal type -- it carries ``code`` and ``status`` beside the message, so a witness
names a code instead of matching on prose. All six are deliberately code-only: a breakpoint row
printed with nineteen percentile cells, a factor block whose column header is gone, a portfolio
row missing a cell, a cell printed as prose where the archive publishes a decimal, a ``.Dat``
section whose value header is gone, and a section retagged to the requirement anchor the caller
did not select are each refused before any contract column is addressed, so no cell exists for
the refusal to attribute.

What this configuration does not cover: the ``_TXT.zip`` mirror branch, the country ratios table,
and the without-dividends international archives are not witnessed here -- the shard's docstring
records the same boundary beside the slices.
"""

from __future__ import annotations

from typing import Any, Final

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.famafrench.provider",
    "allowed_hosts": ["mba.tuck.dartmouth.edu"],
    "models": {},
}
