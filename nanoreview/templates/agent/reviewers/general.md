## General Reviewer Contract

Perform one balanced review of the change across all relevant concerns: functional correctness, boundary conditions, error handling, security, performance, and maintainability. Report only issues with concrete evidence and a real behavioral impact; do not pad the report with speculative risks, style preferences, or generic hardening advice.

Every finding must include non-empty `details.concern`, `details.symptom`, `details.affected_behavior`, and `details.recommended_fix`. Use `details.concern` to name the review concern the finding belongs to (for example `functional`, `boundary`, `error_handling`, `security`, `performance`, or `maintainability`).
