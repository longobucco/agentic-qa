# OSWorld campaign analysis

Cross-arm analysis of every OSWorld campaign (old Phase-1 and new official-protocol infrastructure; Claude Sonnet 5 and gpt-6-astra), produced 2026-10-14 after the campaigns were interrupted.

- `FINDINGS.md` — results, why runs fail (verified vs suspected), infrastructure defects, old→new flips, time and cost. Start here.
- `ASTRA_ANALYSIS.md` — task-by-task failure analysis of gpt-6-astra (99 tasks: behaviour, cause, certainty); data in `astra_ledger.json`.
- `ASTRA_IMPROVEMENT_RESEARCH.md` — how to improve Astra: failure pools, literature, candidate interventions; section 6 records the experiment decision (what is explored, what is set aside and why).
- `RESEARCH_BRIEF_FOR_LLM.md` and `RESEARCH_REPORT_FROM_LLM.md` — the brief given to an external research LLM and its report, kept verbatim (corrections and verdicts in `ASTRA_IMPROVEMENT_RESEARCH.md` 6.10).
- `SUMMARY.md` — every aggregate table.
- `METHODOLOGY.md` — data, definitions, failure taxonomy, how the labels were verified, limits.
- `tables/` — task × arm pass matrix with hand notes, timing, per-bucket metrics, failures by checker.
- `figures/` (PDF and PNG) and `make_figures.py` — ten figures drawn from the per-run table (pass rate by arm, run composition, categories, failure modes, environment factors, infrastructure flips, time and cost, consistency, checkers, quota friction).
- `campaign_analysis.py`, `manual_notes.json`, `verification.json` — generator and its hand-written inputs.

The documentation files listed above (every `.md` here except this README) are thesis material and stay local: they are ignored by git.

What is **not** in git (size): the raw results and full transcripts, the per-run table (3,770 rows) and one cross-arm page per task (373 pages, with per-run evidence). They live in the external archive `agentic-qa-archive-20261014/` (`analysis_campaigns/` next to the raw trees). Regenerate with
`python campaign_analysis.py <archive_root> <out_dir> manual_notes.json verification.json` (needs Pillow, openpyxl, python-pptx).
