# Reference data for the realism report (A8)

`make realism RUN=<run_id>` (or `python -m harness.realism --run <run_id>`) compares the pretend child's
lines with real children's speech. It uses two outside sources whose raw files can't be shared, so they
aren't in the repo. Only the per-age summary built from them is.

| File | What | In the repo? |
|---|---|---|
| `childes_reference.json` | Per-age numbers (means, SDs, counts, citations) built from the two below. No transcript text. | **Yes** |
| `raw/kuperman_aoa.xlsx` (or `.csv`) | Kuperman et al. (2012) age-of-acquisition ratings | No: download it (step 2) |
| `raw/childes/OCSC/...*.cha` | CHILDES transcripts (CHAT format) | No: download only to rebuild the summary (step 1) |

`data/raw/` is gitignored, so nothing you put there gets committed.

## What works after a fresh clone

| You have | What the report shows |
|---|---|
| Nothing extra | Script fidelity, the simulated child's measures and the comparison with real children (sentence length, questions, vocabulary variety), using the committed `childes_reference.json`. The pretend child's late-word share and its comparison show "–". |
| + the word list (step 2) | Everything above plus the late-word share and its comparison. **This is all most people need.** |
| + the CHILDES transcripts (step 1) | Lets you rebuild `childes_reference.json`, e.g. to add a corpus or change the task filter (step 3). |

The report never fails for missing data. It prints a note and fills the missing parts with "–".
`make test` and `make mock` don't need either source.

## 1. CHILDES

- **What:** transcripts of real children talking, mostly with parents at home. Use English, typically
  developing corpora (e.g. from Eng-NA). The builder keeps only the target child's lines (`*CHI`) and
  skips files whose `@ID` group marks a clinical group (anything other than empty or TD).
- **Get OCSC (the corpus we use):**
  1. Open <https://talkbank.org/childes/access/Eng-NA/OCSC.html>. If asked, log in with a free TalkBank
     account (button at the top right of talkbank.org).
  2. Click **Download transcripts** (<https://talkbank.org/data/childes/Eng-NA/OCSC?f=zip>).
  3. Unzip it so the `.cha` files sit under `data/raw/childes/OCSC/` (any depth inside is fine):
     ```bash
     mkdir -p data/raw/childes && unzip ~/Downloads/OCSC.zip -d data/raw/childes/
     ```
  Other corpora go the same way, one folder per corpus under `data/raw/childes/`.
- **Terms:** TalkBank's ground rules: cite MacWhinney (2000) and each corpus's own reference (on its
  page), and don't redistribute the transcripts. So the `.cha` files never go in this repo.
- **Cite:** MacWhinney, B. (2000). *The CHILDES Project: Tools for analyzing talk* (3rd ed.). Lawrence
  Erlbaum. Plus each corpus used (listed under `corpora` in `childes_reference.json`).

### Corpora checked so far

| Corpus | Ages | Use? | Why |
|---|---|---|---|
| **OCSC** (Ohio Child Speech Corpus), Eng-NA | 4–9 (one child just under 4) | **Yes** | Typically developing, 302 children at ages 4–9. The builder leaves out its drilled tasks (Alphabet, Numbers, Wug, Reading aloud), about 45% of the child's lines, and keeps HowTo, the picture tasks, the robot intro and end-of-session chat. |
| **Bates**, Eng-NA | 1;08 and 2;04 | No | Toddlers only; our youngest scenario child is 3. It adds nothing to the comparison. |

**OCSC limits, on top of the ones below:** the children describe pictures and explain tasks to an adult in a
museum lab, with a social robot present for about half of them. That shortens turns and holds down
questions, so a simulated child who asks more questions isn't necessarily wrong. Mostly White, highly
educated families in Ohio. It covers none of our ages 3, 10 or 12.

**Cite:** Wagner, L., Alghowinhem, S., Alwan, A., Bowdrie, K., Breazeal, C., Clopper, C. G., Fosler-Lussier,
E., Jamsek, I. A., Lander, D., Ramnath, R., & Ross, J. (2025). The Ohio Child Speech Corpus. *Speech
Communication, 170*, 103206. doi:10.21415/60TV-D669

## 2. Kuperman age-of-acquisition norms

- **What:** adults' estimates of the age at which they learned about 30,000 English words, applied to
  their inflected forms (about 52,000 words).
- **Get it:** Marc Brysbaert's OSF project <https://osf.io/d7x6q/overview> ("English age of acquisition
  (AoA) ratings (Kuperman et al., 2012)"). Download **`AoA_51715_words.xlsx`** (direct link
  <https://osf.io/download/bx7vm/>, 4 MB) and save it as `data/raw/kuperman_aoa.xlsx`:
  ```bash
  mkdir -p data/raw && curl -L -o data/raw/kuperman_aoa.xlsx https://osf.io/download/bx7vm/
  ```
  No conversion needed: the report reads the `.xlsx` directly (column `AoA_Kup_lem`) with the standard
  library. A CSV at `data/raw/kuperman_aoa.csv` works too; any sheet with a `Word` column and an AoA
  column (`Rating.Mean`, `AoA_Kup_lem`, `AoA_Kup` or `AoA`) does. The project has three files:
  - **`AoA_51715_words.xlsx` (used):** all English AoA ratings combined, with ratings applied to
    inflected forms (51,715 words). Its `AoA_Kup_lem` column gives "playing" the rating of "play" and
    "teeth" that of "tooth". Children use such forms all the time, so this matters for the comparison.
  - `AoA_ratings_Kuperman_et_al_BRM_with_PoS.xlsx`: the ratings as published (31,124 words, base forms
    only). With it, the share of rated words falls from 98% to 91% for the A5 pilot and from 88% to 78%
    for OCSC at age 4, because real children use more inflected forms; the conclusion is the same.
  - The Biemiller master file: test-based ages from Brysbaert & Biemiller (2017), *Behavior Research
    Methods, 49*, 1520–1523, for 43,991 word meanings. Not used (one row per meaning, not per word).
- **Not rated, so left out of the share:** contractions ("don't", "it's"), fillers ("yeah", "mm") and most
  names. They count in neither the numerator nor the denominator; the "AoA coverage" column shows how
  many words were rated.
- **Read with care:** adults rate everyday words late ("and" 4.6, "it" 4.4, "the" 4.0), so real
  4-year-olds in OCSC already score 52% "learned after age". The share means something only next to
  the CHILDES row, never on its own.
- **Terms:** free for research; the OSF project sets no licence, so the file isn't redistributed here. Cite the paper.
- **Cite:** Kuperman, V., Stadthagen-Gonzalez, H., & Brysbaert, M. (2012). Age-of-acquisition ratings for
  30,000 English words. *Behavior Research Methods, 44*(4), 978–990. https://doi.org/10.3758/s13428-012-0210-4

## 3. Build and run

```bash
make realism RUN=<run_id>     # or: python -m harness.realism --run <run_id>; RUN defaults to the latest run
```

That writes `realism.json` and `realism.md` in the run folder. To rebuild the committed summary after
changing corpora (needs step 1, and step 2 for the late-word numbers):

```bash
python -m harness.realism build-childes --cha-dir data/raw/childes   # writes data/childes_reference.json
```

`--norms PATH` points either command at another word-list file; `--skip-tasks` changes which OCSC tasks
are left out (default: alphabet, numbers, reading, wug).

**Before committing a rebuilt `childes_reference.json`:** check that it still holds only numbers, no
transcript text, and that `citations` lists every corpus used. TalkBank's rules allow sharing such
aggregates with citation; check any new corpus's own page for extra conditions.

## Limits

See RFC 0002, "Realism report": CHILDES is spoken and children type to chatbots; it shows *how* a child
speaks, not *what* they disclose; the norms are adults' memories; the speech check already shapes the
lines; no one rates how childlike the lines sound.
