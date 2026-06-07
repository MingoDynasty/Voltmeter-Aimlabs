# Design Review — Pushback & Qualifications, Round 3

**Status:** Companion to [`DESIGN_REVIEW_v3.md`](DESIGN_REVIEW_v3.md)
**Date:** 2026-06-06

Round-3 review ([`DESIGN_REVIEW_v3.md`](DESIGN_REVIEW_v3.md)) confirmed the design is
implementation-ready with no remaining blockers, and raised five cleanup items. **Four are
accepted as-stated** and folded into [`RUN_HISTORY_ARCHITECTURE.md`](RUN_HISTORY_ARCHITECTURE.md)
rev 4. This document captures the **one item we redirect** (item 2 — and following the review's
recommendation literally would *introduce* bugs) plus one small refinement (item 3).

---

## 1. Item 2 — the README/config "drift" resolution is backwards

**Review (item 2, P2):** rev 3 says the canonical key is `aimlabs_user_id`, `AIMLAB_SESSION` is
the session path, and `AIMLABS_COOKIE` should be dropped — but `README.md` / `config.example.toml`
still show `user_id`, `session_cookie`, `AIMLABS_COOKIE`, and `valorant_s1` as active. Recommends
rewriting README/config to the rev-3 naming **before implementation**.

**Our position:** there *is* an inconsistency, but it's **the design doc's, not the README's**,
and the recommended direction would break working documentation. Verified against shipped code:

- **The TOML key really is `[aimlabs].user_id`.** `config.py:33` reads
  `aimlabs_config.get("user_id")` (under the `[aimlabs]` table) and flattens it to the AppConfig
  *attribute* `aimlabs_user_id` (`config.py:50`). So `user_id` under `[aimlabs]` is **correct** —
  you would never write `aimlabs_user_id` as a literal TOML key inside an `[aimlabs]` section. The
  real error was **our design doc's loose phrasing** ("`aimlabs_user_id` in config.toml"). The fix
  is the *opposite direction* from the review's: correct the design doc, leave the config alone.
- **`AIMLABS_COOKIE` and `session_cookie` are live, not dead.** `aimlab_scores.py:265`:
  `session_cookie = os.environ.get("AIMLABS_COOKIE") or app_config.aimlabs_session_cookie`. The
  **currently shipped** tool reads both. Stripping them from the README now would delete docs for
  *working* functionality. (`DESIGN_PUSHBACK.md` #1's "drop `AIMLABS_COOKIE`" was scoped to the
  *pipeline's* auth — which uses `AIMLAB_SESSION` — not a directive to remove it from the current
  tool.)
- **`valorant_s1` / `report_family` are current-tool-correct vs pipeline-future.** The shipped
  scores tool only loads `valorant_s1`; `report_family = "all"`, `sync`, and `report` belong to a
  pipeline that **does not exist yet**. Rewriting README to "default `all`" now documents
  vaporware and risks misleading users of the *current* tool.

**Root cause:** `README.md` / `config.example.toml` describe the **currently shipped
`aimlab_scores` tool**, which is a *different* program from the unbuilt run-history pipeline. They
are not "contradicting rev 3" — they're describing something else, correctly.

**Resolution (rev 4):**
- Fix the design doc's phrasing now: `[aimlabs].user_id` (TOML key) vs `aimlabs_user_id` (AppConfig
  attr), everywhere it appeared (§2, §5.1, §11, §12, decision 7).
- **Defer** the README/`config.example.toml` reconciliation to **M6** (config unification), when the
  pipeline actually ships `AIMLAB_SESSION` / `report_family` and replaces/extends the scores tool.
  Retire `AIMLABS_COOKIE`/`session_cookie` from user docs **only once the shipped tool no longer
  needs them** — added explicitly to M6's acceptance.

Net: item 2 drops from "P2 rewrite README to rev-3 naming now" to "P3 design-doc wording fix now +
an M6 reconciliation task." The implementer follows the (authoritative) design doc; current users'
setup docs stay accurate.

---

## 2. Item 3 — agree, with a scope refinement

**Review (item 3, P3):** projected JSON/text columns (esp. `performance_scores`) need deterministic
serialization or byte-identical idempotency tests get flaky and `--full` raises false drift.

**Our position:** correct — adopt a **canonical serializer** (sorted keys, compact separators).
Refinement on *where* it bites: the **incremental** path is `INSERT … DO NOTHING`, so it never
re-serializes an existing row and is byte-identical *by construction* — determinism is irrelevant
there. It matters specifically on the **`--full` re-derive/compare** path. An equally valid fix is
to detect drift by comparing **parsed structures** rather than serialized strings. (Both recorded;
canonical serialization chosen as the rule, structural compare noted as the alternative.)

**Resolution (rev 4, §7.1):** canonical serialization rule + a fixture where `performance_scores`
key order/whitespace varies but the value is equal ⇒ **no drift**.

---

## 3. Items 1, 4, 5 — accepted as-stated

- **Item 1 (empty first-page, P2):** a real bug — the pseudocode indexed `page[0]` unguarded.
  Adopted exactly: empty stream ⇒ no rows, `newest_id = NULL`, `api_total_count = 0`, backfill
  complete, report renders "no runs," `page_size ≥ 1` validation, tests added (§8.1, M1/M2a, §13).
- **Item 4 (header omits `DESIGN_PUSHBACK_v2.md`, P3):** fixed the review-status note to reference
  all three reviews and all three pushback docs.
- **Item 5 (decision-log numbering, P3):** renumbered the log to be sequential (and added
  decisions 16–17 for the empty-stream and canonical-serialization rules).

---

## Summary

| # | Review item | Severity (review) | Our stance | Landed in |
|---|---|---|---|---|
| 1 | Empty first-page undefined | P2 | **Accept** — real bug | §8.1, M1/M2a, §13 |
| 2 | README/config drift | P2 | **Redirect** — docs describe the shipped tool; fix design-doc phrasing now, reconcile README at M6 | §2/§5.1/§11/§12/dec 7, M6 |
| 3 | Deterministic serialization | P3 | **Accept + refine** — bites `--full` only; structural compare is an alt | §7.1, M1 |
| 4 | Header omits PUSHBACK_v2 | P3 | **Accept** | header note |
| 5 | Decision-log numbering | P3 | **Accept** | §15 |

No design reopens. Rev 4 is the implementation baseline.
