# OSWorld campaign analysis

Cross-arm analysis of every OSWorld campaign (old Phase-1 and new official-protocol infrastructure; Claude Sonnet 4.6, Claude Sonnet 5, gpt-6-astra), produced 2026-10-14 after the campaigns were interrupted.

- `FINDINGS.md` — results, why runs fail (verified vs suspected), infrastructure defects, old→new flips, time and cost. Start here.
- `SUMMARY.md` — every aggregate table.
- `METHODOLOGY.md` — data, definitions, failure taxonomy, how the labels were verified, limits.
- `tables/` — task × arm pass matrix with hand notes, timing, per-bucket metrics, failures by checker.
- `campaign_analysis.py`, `manual_notes.json`, `verification.json` — generator and its hand-written inputs.

What is **not** in git (size): the raw results and full transcripts, the per-run table (4,844 rows) and one cross-arm page per task (375 pages, with per-run evidence). They live in the external archive `agentic-qa-archive-20261014/` (`analysis_campaigns/` next to the raw trees). Regenerate with
`python campaign_analysis.py <archive_root> <out_dir> manual_notes.json verification.json` (needs Pillow, openpyxl, python-pptx).
