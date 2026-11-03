# RFC 0007: Skill Folder

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-10-27)
- **Owner role:** 5. Site, report, deck
- **Tech requirements addressed:** 1, 4
- **Related RFCs:** 0001, 0005, 0009

## Summary

A `skills/` folder with one `SKILL.md` per task, plus templates, that lets a developer (human or coding agent) add scenarios, plug in a new agent under test, run the benchmark, label transcripts, and validate the judge. The repository layout and public site are in RFC 0009.

## Detailed design

### What each skill does and how to call it

A skill is a folder with a `SKILL.md`: YAML front matter (`name`, `description`) and then step-by-step instructions. A coding agent such as Claude Code loads the file when it is invoked or when the request matches the `description`; a person can read the same file as a checklist.

**How to call a skill in Claude Code.** Claude Code discovers project skills in `.claude/skills/`. Keep the canonical copy in `skills/` and link it: `ln -s ../skills .claude/skills`. Then either type the slash command (for example `/add-scenario`) or just describe the task ("add a scenario where a 5-year-old says the chatbot is their best friend") and the matching skill is used. Without Claude Code, open `skills/<name>/SKILL.md` and follow the steps.

| Skill | What it does | Inputs | Output | Example call |
|---|---|---|---|---|
| `add-scenario` | Walks through writing a persona YAML: choose situation, age, disclosure beats, content level, paired good/bad responses, citation. Runs schema validation and the content-ceiling and lint tools on the beats. | Situation, idea in a sentence | `scenarios/<situation>/<id>.yaml` ready for PR | `/add-scenario over-attachment, age 5, child says "you're my best friend"` |
| `add-agent-adapter` | Creates an adapter so a new agent under test (another model, or a real product like Curio or Buddy.ai) can be driven by the orchestrator. Defines `send(message) -> reply`, handles auth via env vars, adds a smoke test. | Agent name, how it is called (API, web, SDK) | `harness/agents/adapters/<name>.py` + test | `/add-agent-adapter gemini-2.5` |
| `run-benchmark` | Runs the pipeline for chosen scenarios, agents, and seeds; checks cost estimate first; writes transcripts and scores to `results/`; produces a summary table. | Scenario filter, agents, seeds | `results/<run_id>/` + summary | `/run-benchmark pilot --agents claude,gemini --seeds 2` |
| `label-transcripts` | Prepares a blind labeling sheet (hides model names and judge output), explains the label fields, and later merges labelers' sheets and flags disagreements. | Run id, labeler ids | label CSVs, disagreement list | `/label-transcripts run_2026-11-10 labelers=A,B` |
| `validate-judge` | Calls `compute_agreement` for human-vs-human, judge-vs-human, and judge test-retest; writes a kappa table with confidence intervals and warnings. | Run id with labels | `results/<run_id>/judge_validation.md` | `/validate-judge run_2026-11-10` |
| `extend-rubric` | Adds or edits a rule: wording, applicable situations, anchor examples; bumps the rubric version and reminds you to revalidate the judge. | Rule text | new `rubrics/vN.yaml` | `/extend-rubric add rule about repeated referral` |

Each `SKILL.md` has: purpose, when to use, inputs, numbered steps, commands, validation checks (running tools from RFC 0004), and common mistakes. Example front matter:

```markdown
---
name: add-scenario
description: Create and validate a new ChildBench scenario persona file. Use when asked to add, write, or draft a test scenario for a referral situation.
---
```

The team uses these skills while building, so they are tested by real use before handoff to Ran. Templates live next to each skill (`scenario.template.yaml`, `agent_adapter.template.py`, `rubric.template.yaml`).

## Alternatives considered

Only a README: less usable by coding agents and less aligned with requirement 4.

## Evaluation / how we know it works

A teammate who did not write a skill uses it to add a scenario and an adapter; time and errors recorded.

## Risks and open questions

- Skills that call paid APIs must show an estimated cost before running.
- Skills must stay in sync with the code; a skill whose commands fail is worse than none. CI runs each skill's validation commands.

## Milestones

| Date | Milestone |
|---|---|
| Nov 1 | `add-scenario` and `run-benchmark` drafts |
| Nov 8 | `add-agent-adapter` drafted while writing the first two adapters |
| Nov 11 | `label-transcripts` and `validate-judge` used in the pilot |
| Nov 22 | `extend-rubric` used for rubric v1; all skills tested by a teammate who did not write them |
| Dec 3 | Handoff |
