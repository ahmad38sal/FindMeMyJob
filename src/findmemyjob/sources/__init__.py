"""Pluggable job source adapters.

Each source returns a list of normalized `Job` rows. Sources are responsible
for their own pagination, dedup, and auth.
"""
