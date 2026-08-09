"""Everything that touches ClinicalTrials.gov: compile, fetch, cache, flatten.

`compiler` turns a Plan into per-leg API requests and projects the narrowest `fields=` set
that answers it — which is why anything new that reads a raw record must extend the
projection, or it will silently see nothing. `normalizer` flattens records to scalars and
flat lists, counting every exclusion by reason.
"""
