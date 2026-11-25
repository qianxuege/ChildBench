# RFC 0007: Skill Folder

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-11-23)
- **Owner role:** 5. Site and poster
- **Tech requirements addressed:** 1, 4
- **Related RFCs:** 0005, 0009, [0010](0010-implementation-plan.md) (A7)

## Summary

Requirement 4 asks for a skill folder with a `SKILL.md` and/or templates that the team used to develop the solution, or designed for the client. We meet it with a skill the team uses. A second one, for the client, is optional.

## Detailed design

| Skill | For | What it does | Status |
|---|---|---|---|
| `dev-work-package` in `.claude/skills/` | The team | Takes one work package from RFC 0010 from dependency check to PR | In use; meets requirement 4 |
| `add-scenario` in `skills/` | The client and other researchers | Walks through writing a scenario from a template, then runs `make check` | Optional (A7) |

Example calls: `/dev-work-package A16`, `/add-scenario over-attachment, age 5, child says "you're my best friend"`.

**For the poster:** give the folder's path and list the packages built with `dev-work-package`.

**`add-scenario`:** the `SKILL.md` has a `name` and `description`, then purpose, inputs, numbered steps, checks and common mistakes. Its template, in RFC 0010's format, sits next to it. It's linked into `.claude/skills/`.

## Alternatives considered

- **`run-benchmark`:** the README and `make` commands already cover it.
- **Labelling and rubric skills:** the scoring page and the site's scoring controls cover them.

## Evaluation

`dev-work-package`: the merged pull requests it produced. `add-scenario`, if built: a teammate adds a scenario with it and `make check` passes.

## Risks

A skill whose commands fail is worse than none, so keep them in step with the code.
