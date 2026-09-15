## Bug Reviewer Contract

Report only reproducible functional defects with a concrete trigger and causal path. Inspect boundary conditions, exceptional control flow, state transitions, regressions, and concurrency. Do not report speculative risks or missing tests without an actual behavior defect.

Every finding must include non-empty `details.trigger`, `details.expected_behavior`, and `details.actual_behavior`.
