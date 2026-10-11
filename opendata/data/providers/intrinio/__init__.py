"""Intrinio provider package: three census models declared as engine ``ModelSpec`` records.

The forex pair directory, the company search and the security price-adjustment feed are each a
single path-addressed GET whose rows live under one nested key, so all three ride the shipped
declarative engine without a hand-written fetcher.
"""
