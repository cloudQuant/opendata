"""sec fetcher modules, one per provider model.

None exist yet: no upstream sec row is expressible as a declaration, so
:mod:`opendata.data.providers.sec.specs` records all 24 as ``NOT_DECLARABLE``. A module appears
here when a ``ModelSpec`` is declared and ``make_http_json_fetcher`` generates its fetcher.

Re-checked this round for the three rows ``capability-roadmap.json`` lists as already covered
(:data:`opendata.data.providers.sec.specs.RECHECKED_THIS_ROUND`): all three were refused again on
their census record, so this directory gains no generated module -- a fetcher cannot be produced for
a model the declaration format cannot express, and writing one by guesswork would be a claim.
"""
