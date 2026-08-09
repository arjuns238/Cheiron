"""The data contracts: the field registry, the Plan, and the HTTP envelope.

`fields.py` is the registry the rest of the system derives from — the planner's legal-field
list, the plan validator, the viz rules and the automatic warnings all read it, so a new
field is added here rather than in four places.
"""
