"""The four model touchpoints, and the provider-agnostic client behind them.

Router, planner, judge and chart selector. Each is bounded by deterministic code: the
router's verdict decides a response type, the planner's output must pass the plan
validator, the judge is advisory with one re-plan, and the selector may only pick within
the legal chart set. None of them ever sees a trial record or authors a number.
"""
