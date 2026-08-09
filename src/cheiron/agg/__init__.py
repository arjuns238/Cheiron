"""The aggregator: where every charted value is born.

Buckets, folds, co-occurrence and the invariant check. This is the only place a number
that reaches the output is computed, and it computes them by folding over lists of source
records. Citations are born here too, attached to the datum they evidence.
"""
