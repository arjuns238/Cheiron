"""The HTTP surface: five endpoints plus the demo frontend at `GET /ui`.

`/capabilities` and `/schema` are generated from the Pydantic models and the field
registry, so they cannot drift from the code they document.
"""
