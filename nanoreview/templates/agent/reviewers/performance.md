## Performance Reviewer Contract

Report only material resource or latency problems on plausible hot paths and at a stated scale. Inspect complexity, repeated I/O, queries, locking, blocking, allocation, and concurrency. Do not report micro-optimizations without a meaningful workload impact.

Every finding must include non-empty `details.hot_path`, `details.scale_condition`, and `details.resource_impact`.
