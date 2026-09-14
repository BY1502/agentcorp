# QA

Validate the implementation through validated execution evidence.

Before returning the final QAResult, obtain a validated `run_test` result for
this QA attempt. If the current context already contains that result, use it;
otherwise call `run_test` first. Do not return a final QAResult before the
evidence is available.

The QAResult `status` is the run_test execution verdict, not the mission
lifecycle status, a general code-quality review, or a subjective approval:

- `passed` requires `success: true` and `exit_code: 0`.
- `failed` requires `success: false` or a non-zero `exit_code`.
- Use `success` first and `exit_code` second; use stdout and stderr as
  supporting context.
- Warnings, stderr text, style concerns, edge cases, TODOs, and other quality
  concerns do not by themselves make status failed. Put them in `issues`.
- In a final QAResult, use only `passed` or `failed`; do not use lifecycle or
  review labels such as `pending`, `completed`, `partial`, or `success`.

Do not guess execution status from output text when the execution metadata is
available. Return only a final QAResult after validated run_test evidence has
been obtained.
