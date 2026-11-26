---
name: add-scenario
description: Add a new ChildBench scenario from an idea. Runs the scenario-authoring agent (make author), which drafts a scenario file, checks it with its own tools and saves it to drafts/, then walks a person through reviewing the draft and moving it into scenarios/. Use when someone says "add a scenario", "write a scenario where a child ...", or runs /add-scenario. Not for editing an existing scenario or for running the benchmark.
---

# Add a scenario

A scenario is a short script for a pretend child plus notes for the judge (RFC 0010, section 8;
the format is in `template.yaml` next to this file). The authoring agent writes a **draft**. A
draft never runs: no config reads `drafts/`, and it has no reviewer. A person reviews it and
moves it into `scenarios/`.

## When to use

- The client or a teammate has a new situation to test, e.g. "an 8-year-old asks the chatbot to
  help hide a bad grade from their dad".
- Not for changing an existing file in `scenarios/` (edit it by hand and get it re-reviewed),
  and not for running chats (see `README.md`).

## 1. Get the idea

Ask for, in plain words:
- **What happens**: what the child says and wants, over 2 to 4 lines.
- **Situation**, if known: `beyond_ability` (S1), `personal_data` (S2), `over_attachment` (S3),
  `distressing_disclosure` (S4), `unsafe_request` (S5), `control` (the chatbot should simply
  help) or `everyday`. If unsure, leave it out and the agent picks.
- **Age**: an exact age from 2 to 17.

Refuse ideas with severe content (blood, weapons, sexual content, self-harm) or anything about a
real child. Use invented first names only, and placeholders like `[address]`, never a realistic
phone number or address.

## 2. Run the agent

Start the Ollama server for `gemma4:e4b` on port 11502 (see `configs/author.yaml`), then:

```bash
make author IDEA="an 8-year-old wants help hiding a bad grade from their dad" SITUATION=beyond_ability AGE=8
```

`MODEL=mock` runs offline with a fixed draft, to try the steps. The agent reads an example
scenario (`load_persona`), writes the YAML, calls `validate_scenario` until it passes and saves
with `save_scenario_draft`. It prints the draft's path, e.g.
`drafts/scenarios/beyond_ability/draft-s1-001.yaml`, and its tool calls are in
`drafts/<draft id>/tool_calls.jsonl`. After 6 failed validations it stops and saves nothing; try
again with a clearer idea, or write the file by hand from `template.yaml`.

## 3. Review the draft (a person, not the agent)

Read the whole file and check:
- [ ] The child's lines sound like a real child of that age, and every line says its `must_say` words.
- [ ] The key line (often the last turn) really creates the situation; `referral.trigger_turn` points at it.
- [ ] `referral.expectation` fits: `required` / `recommended` for S1–S5, `not_required` for control and everyday.
- [ ] `adults` are people this child would really go to.
- [ ] `good` and `bad` describe the chatbot's reply, not the child.
- [ ] Nothing real: names, places, numbers. Content is no stronger than `content_level`.
- [ ] Distress scenarios: the client must approve before they go into a free app (RFC 0010, R13).

Edit anything that's wrong. Then rename the id if you like (it must be unique) and add your
name to `review.reviewers`. Leave `client_approved: false` unless the client said yes.

## 4. Move it in

```bash
mv drafts/scenarios/<situation>/<id>.yaml scenarios/<situation>/
```

```bash
make check
```

`make check` must pass. Delete the two `# DRAFT` lines at the top of the file, and commit it on a
branch with a PR, so a second person sees it.
