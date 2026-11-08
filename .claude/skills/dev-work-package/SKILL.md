---
name: dev-work-package
description: For ChildBench developers only. Implements one backend work package from RFC 0010 (e.g. A1, A11, B5) end to end — checks its dependencies, branches from main, keeps the package's GitHub issue and project-board card up to date, edits only the package's listed files, runs the checks, records findings in docs/findings.md, writes the handoff note, marks the package done and opens the PR. Use when a developer says "implement A11", "do work package B5", or runs /dev-work-package <id>. Not for benchmark users; for running the benchmark or adding scenarios use the user-facing skills in skills/.
argument-hint: <package id, e.g. A11>
---

# Implement a ChildBench work package (developers only)

The plan lives in `rfcs/0010-backend-implementation-plan.md`, section 11. Each package is a block like this, and this skill depends on that format:

```
**A11 · Manual relay** · Owner A · Needs: — (A1 only for the `make run` shortcut) · Status: todo

- ...what to build...
- **Files:** `harness/llm/manual.py` (new), `harness/llm/__init__.py`, ...
- **Done when:** ...
```

The argument is the package id (`A11`, `B5`, `A5-app`, ...). If none was given, ask for it.

## 1. Read and understand the package

1. Read `CLAUDE.md` and RFC 0010 sections 1–11. Rules in `CLAUDE.md` always apply. Never edit or filter the chatbot-under-test's replies. No real child data. Keys only in `.env`.
2. Find the package block by its id. If it isn't there, list the package ids that exist and stop.
3. Write down for yourself: what to build, the **Files** list, the **Done when** check, **Needs** and **Status**.
4. **Stop and tell the developer** if any of these is true:
   - Status is already `done`.
   - The block is marked *Operation*. Operations are real runs that the owner runs by hand. Offer only to help read the results afterwards.
   - It has no **Files** list. Ask the developer what it may change.

5. **Find the package's GitHub issue:** `gh issue list --repo qianxuege/ChildBench --state all --search "[<ID>] in:title"`. Issues are titled `[<ID>] <name>`.
   - If there's none, create one from the package block: title `[<ID>] <short name>`, body = the block plus a link to RFC 0010 section 11, assignee = the package's owner (Cici → `qianxuege`, Tianyou → `Laitty`). Tell the developer you created it.
   - If it's closed, stop and ask. The work may already be done.

## 2. Check dependencies

1. `git fetch origin` and check that the working tree is clean (`git status --porcelain`). If it isn't, stop and ask: don't stash or discard anyone's work.
2. For each package under **Needs**, look up its block **in `origin/main`'s copy of the RFC** (`git show origin/main:rfcs/0010-backend-implementation-plan.md`). Each must be `Status: done` there.
   - Needs written as "— (X only for ...)" or "X for the real data" are soft. Note them, but don't block.
   - Needs that aren't packages (e.g. "Ran's provider decision", "An API key") can't be checked from git. Ask the developer to confirm them.
3. If a hard dependency isn't done on `main`, stop. Say which one is missing and who owns it.

## 3. Branch

Create the branch from the latest `main`:

```bash
git switch -c wp/<id-lowercase>-<short-name> origin/main
```

For example: `wp/a11-manual-relay`. If the branch already exists, ask whether to continue on it or start over.

Then mark the issue as started (see "GitHub issue and board" below): comment `Started on branch wp/<…>.`, and set its board Status to *In Progress*.

## 4. Implement

- **Change only the files in the package's Files list.** Files marked "(new)" may be created. For a shared file with a scope note, such as `harness/schemas.py` (`Transcript` only) or `Makefile` (its own target only), stay inside that scope. Section 11's "Shared files" rules apply.
- If the work needs any other file, **stop and ask the developer first**. Explain why the file is needed.
- Follow the repo's conventions:
  - Agent-facing tool docstrings use the RFC 0004 format (purpose, When to use, When NOT to use, Args, Returns).
  - Tools return `{"ok": false, "error": ...}` instead of raising.
  - The overall rating is computed by `harness/rating.py`, never by the judge.
- Use `make mock` and `make test`, never paid models, unless the developer says otherwise. Local Ollama is fine for a "Done when" check that names it, if the servers are running.
- Add or update tests in `tests/` for what you built.

## 5. Check

1. Run `make test`. Every test must pass.
2. Run `make mock` if the package touches the run pipeline (`harness/run.py`, `harness/agents/`, `harness/llm/`, `harness/tools/`, `configs/`).
3. Run the package's **Done when** check. If it's a command, run it. If it needs a person (e.g. "one real conversation through one app works by hand"), list it as a manual step for the developer and don't claim it passed.
4. **Scope check:** compare `git diff --name-only origin/main...HEAD` plus uncommitted changes with the Files list. `docs/findings.md`, this package's Status line and the RFC's Changelog table are always allowed. Explain any other file outside the list and get the developer's OK, or revert it.
5. If anything fails, fix it and run the checks again. Don't move on with failing checks.
6. **Record findings.** If a check or a run showed something worth knowing beyond "it works" — a model behaving unexpectedly, a wrong assumption in the RFC, a problem in another package's area, a number that matters later — add an entry at the top of `docs/findings.md` using its template (next free `F-NNN`, the run ids, what you saw, why, decision, status). Don't edit old entries.
   - If the finding changes the plan, don't edit other packages' blocks yourself. Add a line to the RFC's **Changelog** table only after the developer agrees to the change, and propose the block edits to them.
   - If it needs new work, propose a follow-up issue; create it once the developer agrees, and link it from the finding.

## 6. Hand off

1. In RFC 0010, change only this package's `Status: todo` (or `in progress`) to `Status: done`. Don't touch other blocks.
2. Write the handoff note:

   ```markdown
   ## <ID> · <package name>

   **What changed:** <files and behaviour, briefly>
   **What the next packages can rely on:** <interfaces, commands, file formats, config keys>
   **Checks:** make test (<n> passed) · make mock · Done when: <passed / manual step left: ...>
   **Not done / follow-ups:** <anything left, or "none">
   ```

3. Show the developer the diff summary and the note, and **ask before committing and pushing**.
4. Once they say yes: commit, push the branch, and open a PR into `main` with the handoff note as its description, ending with `Closes #<issue>` so merging closes the issue.
5. Update the issue: comment with the PR link and any manual step left, and set its board Status to *In Review* if the board has that option (otherwise leave it at *In Progress*; merging closes the issue).
6. Tell the developer to merge the PR before anyone starts a package that needs it.

## GitHub issue and board

The board is GitHub Project **4** owned by `qianxuege` ("ChildBench"). Use only what the package needs: its own issue and its own card. Never change other people's issues or cards, project settings, fields or views.

```bash
# project id, Status field id and its options (cache these for the session)
gh project view 4 --owner qianxuege --format json --jq .id
gh project field-list 4 --owner qianxuege --format json \
  --jq '.fields[] | select(.name=="Status") | {id, options: [.options[] | {id, name}]}'
# the card for issue #N (add it if it's missing)
gh project item-list 4 --owner qianxuege --format json --limit 200 \
  --jq '.items[] | select(.content.number==N) | .id'
gh project item-add 4 --owner qianxuege --url https://github.com/qianxuege/ChildBench/issues/N
# set Status
gh project item-edit --id <item id> --project-id <project id> --field-id <status field id> --single-select-option-id <option id>
```

- Match the Status option by name, ignoring case (`in progress`, `in review`, `done`). If an option doesn't exist, skip that step and say so.
- If `gh` reports a missing `project` scope, skip the board steps, finish everything else, and tell the developer to run `gh auth refresh -s project`.
- Issue comments are public. Keep them to facts: branch, PR link, checks, manual steps left. No keys, run data or child lines.

## Don't

- Run *Operation* packages, or anything that spends money.
- Commit `results/` mock runs, the text files of guardrails-off runs, `.env`, or any `blind_key.csv`.
- Edit another package's Status, or start a second package in the same session.
- Rewrite or delete existing entries in `docs/findings.md`.
- Touch GitHub issues or board cards other than this package's own, except to create a follow-up issue the developer agreed to.
