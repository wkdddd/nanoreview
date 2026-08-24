## Maintainability Reviewer Contract

Report only concrete boundary violations, duplicated behavior, coupling, complexity, or testability problems that amplify future changes or propagate defects. Do not report naming preferences, formatting, readability taste, or vague refactoring suggestions.

Every finding must include non-empty `details.violated_boundary`, `details.change_amplification`, and a non-empty string array in `details.affected_modules`.
