# Dupaco Credit Union Knowledge System

A **version-aware, source-grounded Python knowledge engine** built for the Knowledge Hackathon assessment. It answers credit-union questions using the **five supplied website snapshots (May–September 2026)**, identifies changes and contradictions, and avoids presenting outdated or unsupported information as fact.

## Project objective

A website can publish multiple versions of the same information. An old loan calculator may show a different rate from the current loan-rate page; a promotional offer may expire; or two account tiers may have legitimately different APYs.

The goal of this project is to provide **the correct answer for the requested date**, with the appropriate product, term, tier, and supporting source. When the snapshots do not establish an answer, the system should say so rather than guess.

## Key features

- **Historical, date-aware answers:** `answer(question, as_of=None)` uses the latest available snapshot unless an earlier date is requested.
- **Precise financial data extraction:** Parses published Markdown tables for rates, loan terms, model years, savings tiers, certificates, APR, and APY.
- **Source selection:** Prefers authoritative product and rate pages over potentially stale calculators, promotions, or older articles.
- **Conflict detection:** Compares like-for-like facts across snapshots and explains which published value is preferred and why.
- **Evidence and uncertainty:** Returns source URLs, snapshot identifiers, confidence, and an explanation when information cannot be confirmed.
- **Offline and deterministic:** Uses Python's standard library; no external API, paid model, or API key is required.

## How it works

```text
Five website snapshots
         |
         v
 Ingest pages and dates
         |
         v
 Select snapshot by as_of date
         |
         v
   Analyze user question
         |
      +--+--------------------+
      |                       |
      v                       v
Structured rate tables   Source-grounded
and tier matching         text retrieval
      |                       |
      +-----------+-----------+
                  |
                  v
     Answer + sources + confidence

Separate analysis: compare equivalent facts across snapshots
                       -> conflicts + chosen resolution
```

The submission exposes the interface required by the assessment:

| Method | Purpose |
| --- | --- |
| `build()` | Creates the knowledge-system instance. |
| `ingest(snapshot)` | Reads and stores the provided website snapshot. |
| `answer(question, as_of=None)` | Returns an evidence-backed answer for the selected date. |
| `conflicts()` | Returns identified contradictions, supporting sources, and the preferred value. |

## Repository layout

```text
dupaco-knowledge-hackathon/
├── README.md
├── submission/
│   ├── solution.py          # Required implementation
│   ├── requirements.txt     # Dependency declaration
│   └── NOTES.md             # Design decisions and API-key information
└── tests/                   # Optional local regression tests
```

The organizer's `harness/`, `eval/`, and `snapshots_zip/` folders are supplied separately in the **original participant kit** and are **not redistributed in this repository**.

## Requirements

- Python **3.11 recommended** (assessment supports Python 3.9+).
- Original Knowledge Hackathon participant kit, including its evaluation harness and supplied website snapshots.
- No third-party Python packages or API keys.

## Run the local evaluation

1. Download and extract the **original participant kit** provided by the organizer.
2. Copy this repository's `submission/` folder into the extracted kit's root directory, next to `harness/` and `eval/`.
3. Open a terminal in the **original kit root**, then run:

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl --out results/submission
```

The evaluation harness will extract the supplied snapshot archives on the first run if necessary. Inspect the generated report and question-level results in `results/submission/`.

If the optional test folder is included, run:

```bash
python -m unittest discover -s tests -v
```

## Public practice evaluation

In the earlier local run against the **40 supplied practice questions**, the implementation reported:

| Metric | Recorded result |
| --- | ---: |
| Public practice score | **0.9690 (96.9%)** |
| Stale-value trap hits | **0** |
| Execution errors | **0** |
| Repeat-answer consistency | **100%** |

**These are public practice results, not a hidden-test score or a guarantee of hiring-assessment performance.** The public run did not include the hidden conflict gold labels, so its displayed overall score should not be interpreted as a fully evaluated score for all grading categories. Results may vary if the source code or evaluation setup changes.

## Design choices

**Why structured parsing?** Exact financial figures must not be mixed across account tiers, vehicle years, or loan terms. Reading the published tables directly is more predictable than free-form number extraction.

**Why keep history?** “Current rate” and “rate as of May 2026” can require different answers. The system preserves each snapshot instead of overwriting older facts.

**How are conflicts handled?** Two values are not automatically contradictory: a jumbo certificate and a standard certificate, or a 60-month and an 84-month auto loan, may correctly have different rates. The system compares equivalent facts and uses source authority and publication chronology to decide which value should be returned.

**What if the answer is missing?** The engine indicates that the published snapshots do not verify the requested fact and recommends confirming it with the credit union.

## Limitations and future improvements

The supplied snapshots are the only knowledge source. The system cannot confirm changes made after the newest supplied snapshot, and some unstructured pages may be harder to search than official rate tables. Future work would expand extraction from unstructured content, improve detection of same-snapshot contradictions, and add more paraphrase and source-reliability regression tests.

## Submission notes

The required files are `submission/solution.py`, `submission/requirements.txt`, and `submission/NOTES.md`. Provide the hiring team with the **GitHub repository URL** and the requested commit hash, and grant repository read access if private.

**Data handling:** The snapshot archives were supplied for this assessment. Do **not** commit, redistribute, or publicly upload those original website snapshots or the participant kit's data files. The grader can evaluate the source using its own copy of the kit.
