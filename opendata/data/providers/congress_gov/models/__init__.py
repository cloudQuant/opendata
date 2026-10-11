"""congress_gov fetcher modules, one per provider model.

None exist yet: no upstream congress_gov row is expressible as a declaration, so
:mod:`opendata.data.providers.congress_gov.specs` records all 8 as ``NOT_DECLARABLE`` with the
capability id that blocks each. A module appears here when a ``ModelSpec`` is declared in that
record and ``make_http_json_fetcher`` generates its fetcher -- which is also the moment
``provider.py`` gains a binding for it and ``catalog.engine_declared_models()`` starts to see it.

No hand-written fetcher is kept here in the meantime. A fetcher is what the engine would have to
execute, and for these 8 rows the mechanism it would execute (a branch that picks the path, a
fan-out over URLs a response published, a join across two documents, one row assembled from several)
is not something the declaration format can carry; writing it as imperative code would be an
unreviewed claim about those endpoints rather than a declaration of them.
"""
