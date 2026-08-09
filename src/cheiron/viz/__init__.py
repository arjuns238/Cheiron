"""Chart legality, the response envelope, and citation verification.

`rules` decides which chart types are *legal* for a result shape; the model then picks a
*preference* within that set and can only ever downgrade to the default. `citations`
verifies each excerpt at its offsets against the fetched payload and drops any that does
not match, rather than emitting an unverified one.
"""
