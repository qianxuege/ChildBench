# Reference data for the realism report (A8)

`python -m harness.realism` compares the pretend child's lines with real children's speech. It needs two
outside sources. Neither is committed: put them in `data/raw/` (gitignored), then build the summary file.

| File | What | Committed? |
|---|---|---|
| `raw/childes/<Corpus>/...*.cha` | CHILDES transcripts (CHAT format) | No |
| `raw/kuperman_aoa.csv` | Kuperman et al. (2012) age-of-acquisition ratings | No |
| `childes_reference.json` | Per-age summary numbers built from the two above (means, SDs, counts) | Only after the licence check below |

## 1. CHILDES

- **What:** transcripts of real children talking, mostly with parents at home. Use English, typically
  developing corpora (e.g. from Eng-NA). The builder keeps only the target child's lines (`*CHI`) and
  skips files whose `@ID` group marks a clinical group (anything other than empty or TD).
- **Where:** <https://talkbank.org/childes/>. Downloads need a free TalkBank login (button at the top
  right). Download the corpus zips by hand and unzip them under `data/raw/childes/`, one folder per corpus.
- **Terms:** TalkBank's ground rules: cite MacWhinney (2000) and each corpus's own reference (on its
  page), and don't redistribute the transcripts. So the `.cha` files never go in this repo.
- **Cite:** MacWhinney, B. (2000). *The CHILDES Project: Tools for analyzing talk* (3rd ed.). Lawrence
  Erlbaum. Plus each corpus used (listed under `corpora` in `childes_reference.json`).

## 2. Kuperman age-of-acquisition norms

- **What:** adults' estimates of the age at which they learned about 30,000 English words.
- **Where:** the supplementary material of the paper (doi below), or the authors' page at Ghent
  University's Center for Reading Research. Save the sheet as CSV at `data/raw/kuperman_aoa.csv`. It
  needs a `Word` column and an AoA column (`Rating.Mean`, `AoA_Kup_lem`, `AoA_Kup` or `AoA`).
- **Terms:** free for research; no open licence is stated, so the file isn't redistributed here.
- **Cite:** Kuperman, V., Stadthagen-Gonzalez, H., & Brysbaert, M. (2012). Age-of-acquisition ratings for
  30,000 English words. *Behavior Research Methods, 44*(4), 978–990. https://doi.org/10.3758/s13428-012-0210-4

## 3. Build and run

```bash
python -m harness.realism build-childes --cha-dir data/raw/childes
make realism RUN=<run_id>     # or: python -m harness.realism --run <run_id>
```

The first writes `data/childes_reference.json`; the second writes `realism.json` and `realism.md` in the
run folder. Without the reference or the norms, the report still has the simulated side and the
script-fidelity counts, and says what is missing.

**Before committing `childes_reference.json`:** it holds only aggregate numbers per age, not any
transcript text, which TalkBank's rules normally allow with citation. Check the corpora's own pages for
extra conditions, and keep the citations in the file.

## Limits

See RFC 0002, "Realism report": CHILDES is spoken and children type to chatbots; it shows *how* a child
speaks, not *what* they disclose; the norms are adults' memories; the speech check already shapes the
lines; no one rates how childlike the lines sound.
