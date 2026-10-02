"""
The original (v1) CRDT, kept verbatim for before/after benchmarks only.

One op per character, O(n) linked-list scans for every position lookup, and a
set of every applied op id in each snapshot. Not used by the application.
"""
