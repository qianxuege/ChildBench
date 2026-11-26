# Invented tool labels (fixture)

Short invented lines, hand-labelled, so `make evaluate` can show the tool-accuracy measure working before T2's real
sets exist. Not T2's labels: the numbers they give mean nothing. T2 puts the real sets in `private/tool_labels/`,
same format, and `make evaluate` uses those instead.

One CSV per tool, named after it. Columns: `text`, `expected` (`flag` = the tool should catch it, `ok` = it shouldn't),
`note`, and the tool's own inputs:

| File | Extra columns |
|---|---|
| `lint_child_utterance.csv` | `age` |
| `check_content_ceiling.csv` | `level` (`mild` or `moderate`) |
| `key_facts.csv` | `must_say`: facts separated by `;`, words for one fact by `|` |
| `scan_realistic_pii.csv`, `detect_pii_request.csv`, `detect_referral.csv` | none |

No real names, numbers or addresses; nothing above "moderate". These lines never go to a chatbot.
