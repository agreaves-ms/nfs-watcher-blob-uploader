# General Instructions

* Conventions and styling from the codebase take precedence for all changes.
* Breaking changes are acceptable.
* Backward-compatibility layers or legacy support are added only when explicitly requested.
* Tests, scripts, and one-off markdown docs are created or modified only when explicitly requested.

Rules for comments:

* Remain brief and factual, describing behavior, intent, invariants, and edge cases.
* Use documentation comments following modern language constructs.
* Thought processes, step-by-step reasoning, and narrative comments do not appear in code.
* Comments that contradict current behavior are removed or updated.
* Temporal markers (phase references, dates, task IDs) are removed from code files during any edit.

Rules for fixing errors:

* Proactively fix any problem encountered while working in the codebase, even when unrelated to the original request.
* Root-cause fixes are preferred over symptom-only patches.
* Further investigation of the codebase or through tools is always allowed.
