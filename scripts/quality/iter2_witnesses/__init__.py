"""Per-provider offline witness configuration modules for the iter2 claims producer.

One module per provider source keeps witness ownership provider-atomic: a claim pins
its own module, so another provider adding a witness can never stale this provider's
digests. A module exposes ``CONFIG`` in the shape ``iter2_claims_fixtures`` reads.
"""
