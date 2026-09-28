"""Every published result fixture must say, in the file, how it was produced.

``tests/fixtures/results/*.json`` are the numbers ``scripts/render_results.py`` writes into
README.md, docs/MODEL.md and docs/FEES_EXPLAINED.md. They are the repository's published
claims, so each one has to carry a machine-checkable ``_provenance`` block: which script and
arguments produced it, at which commit, over which inputs.

The evaluator already appends a record per run to ``out/eval_log.jsonl`` - but ``out/`` is
git-ignored, so that record never travels with the repository and cannot be used to verify a
fixture anyone else checks out. As of the 2026-09-28 audit **none of the 16 fixtures then
committed could be matched to a logged run** (see docs/AUDIT_2026-09-28.md): their bytes match
no ``results_sha256`` in the log under any serialisation, only two carry even a hand-written
prose note, and the one run that names a fixture path ran at a commit that is not reachable
from this branch.

Those 16 are listed in :data:`UNVERIFIED_PROVENANCE` with the reason. Listing them is not
approval - it records the debt so it is visible and cannot grow silently. **A fixture added or
regenerated from now on must carry the block**, and a name may only leave the list by gaining
one. Nothing here deletes or rewrites a fixture: the numbers may well be right, they are simply
not reproducible from what the repository contains.
"""
from __future__ import annotations

import json
import pathlib
import unittest

RESULTS = pathlib.Path(__file__).resolve().parent / "fixtures" / "results"

#: Required keys of a ``_provenance`` block, and what each must answer.
REQUIRED = {
    "script": "which script produced the file (repo-relative path)",
    "argv": "the exact arguments it was run with",
    "git_head": "the full commit the script ran at",
    "git_dirty": "whether the working tree had uncommitted changes then",
    "generated_utc": "when the run finished",
    "inputs": "what it read (recorded fold/date range, and a fingerprint of each input database)",
}

#: Fixtures committed before the provenance rule, with why each cannot be verified today.
#: Do not add to this list - add a ``_provenance`` block to the fixture instead.
UNVERIFIED_PROVENANCE = {
    "micro_discovery.json": (
        "carries a hand-written prose '_fixture' note and a spec_hash (52cab7ecdc) that matches "
        "eval_log entry #7, whose run wrote to /tmp/claude-501/micro_discovery.new.json; the only "
        "logged run naming this path (#8) ran at b1238ce4d, which is not reachable from "
        "claude/exec-readiness; no logged results_sha256 matches these bytes"),
    "arb_backtest_w2.json": (
        "hand-written prose '_fixture' note only; no run recorded in out/eval_log.jsonl. ALSO STALE: "
        "scripts/arb_backtest.Ledger.available was fixed on 2026-09-28 to stop counting liquidity taken "
        "after the moment being priced, so a re-run no longer reproduces these numbers"),
    "arb_fixture_p09.json": "no provenance of any kind in the file; no logged run",
    "college_experiment_p04.json": "no provenance of any kind in the file; no logged run",
    "eligibility_p11.json": "no provenance of any kind in the file; no logged run",
    "espn_wp_alignment_p04.json": "no provenance of any kind in the file; no logged run",
    "fee_flip_p10.json": "no provenance of any kind in the file; no logged run",
    "feed_parity_w1_p04.json": "no provenance of any kind in the file; no logged run",
    "leadlag_nfl_2026_w2.json": "no provenance of any kind in the file; no logged run",
    "lines_eval_p13.json": "no provenance of any kind in the file; no logged run",
    "micro_synthetic.json": "synthetic fixture, but records neither the script nor the commit that built it",
    "momentum_synthetic_40.json": "synthetic fixture, but records neither the script nor the commit that built it",
    "replay_ncaaf_2026_w2.json": "no provenance of any kind in the file; no logged run",
    "replay_nfl_2026_w1.json": "no provenance of any kind in the file; no logged run",
    "week1_p03.json": "no provenance of any kind in the file; no logged run",
    "week1_p05.json": "no provenance of any kind in the file; no logged run",
}


def _fixtures() -> list[pathlib.Path]:
    return sorted(RESULTS.glob("*.json"))


class ResultsProvenanceTests(unittest.TestCase):
    def test_every_new_results_fixture_carries_a_provenance_block(self):
        """A fixture outside the documented list must say how it was produced."""
        for p in _fixtures():
            if p.name in UNVERIFIED_PROVENANCE:
                continue
            with self.subTest(fixture=p.name):
                doc = json.loads(p.read_text(encoding="utf-8"))
                prov = doc.get("_provenance")
                self.assertIsInstance(
                    prov, dict,
                    f"{p.name} publishes numbers but has no _provenance block. Add one with "
                    f"{sorted(REQUIRED)}, or, if it predates the rule, list it in UNVERIFIED_PROVENANCE "
                    f"with the reason (see docs/AUDIT_2026-09-28.md).")
                for key, what in REQUIRED.items():
                    self.assertIn(key, prov, f"{p.name}: _provenance is missing {key!r} ({what})")
                    self.assertNotIn(prov[key], (None, "", [], {}),
                                     f"{p.name}: _provenance[{key!r}] is empty ({what})")
                head = str(prov["git_head"])
                self.assertRegex(head, r"^[0-9a-f]{40}$", f"{p.name}: git_head {head!r} is not a full commit sha")
                self.assertIs(prov["git_dirty"], False,
                              f"{p.name} was generated from a dirty working tree; regenerate it from a clean commit")

    def test_the_quarantine_list_does_not_rot(self):
        """Every listed name exists, and no listing is left behind after a fixture is removed."""
        on_disk = {p.name for p in _fixtures()}
        stale = sorted(UNVERIFIED_PROVENANCE.keys() - on_disk)
        self.assertEqual(stale, [], f"UNVERIFIED_PROVENANCE lists fixtures that no longer exist: {stale}")

    def test_every_listed_fixture_has_a_stated_reason(self):
        for name, reason in UNVERIFIED_PROVENANCE.items():
            with self.subTest(fixture=name):
                self.assertTrue(str(reason).strip(), f"{name} is quarantined without a reason")


if __name__ == "__main__":
    unittest.main()
