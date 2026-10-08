"""sec fetcher modules, one per provider model.

None exist yet: no upstream sec row is expressible as a declaration, so
:mod:`opendata.data.providers.sec.specs` records all 24 as ``NOT_DECLARABLE``. A module appears
here when a ``ModelSpec`` is declared and ``make_http_json_fetcher`` generates its fetcher.
"""
