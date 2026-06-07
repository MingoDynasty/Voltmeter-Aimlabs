# Design Review — Pushback & Qualifications

**Status:** Response to [`DESIGN_REVIEW.md`](DESIGN_REVIEW.md)
**Date:** 2026-06-06

We accept the **large majority** of the review, including all five blockers — those changes
are folded into [`RUN_HISTORY_ARCHITECTURE.md`](RUN_HISTORY_ARCHITECTURE.md) (rev 2). This
document captures only the points where we **diverge, qualify, or correct** the review, so the
disagreements are explicit and reviewable rather than silently dropped.

---

## 1. Config key: keep `aimlabs_user_id`, don't rename to `anthic_id`

**Review (blocker #5):** standardize the account id as `anthic_id` in `config.toml`.

**Our position:** unify on a single key — but the **incumbent `aimlabs_user_id`**, not a new
`anthic_id`.

**Why:**
- It's **confirmed** that the leaderboard `userId` and the profile `anthicId` are the *same
  value* for an account. So this is purely a naming choice, not a data-model one.
- `aimlabs_user_id` is **already shipped**: it's in `config.py` (`AppConfig`),
  `config.example.toml`, the README, and the working `aimlab_scores.py` leaderboard path.
  Renaming to `anthic_id` is a **user-facing breaking config change** and code churn for
  marginal precision.
- The Aimlabs API itself uses *both* names (`userId` on the leaderboard query, `anthicId` on
  the profile query) for the same value — so neither name is "more correct" at the API layer.
  The internal code will pass whichever param name each query wants regardless of the config
  key.

**Resolution:** config key stays `aimlabs_user_id`. We may accept `anthic_id` as a documented
alias if reviewers feel strongly, but the canonical key is the incumbent. We fully agree with
the *spirit* of the blocker (one canonical name; drop `AIMLABS_COOKIE`).

---

## 2. The deletion counterexample is incorrect (but the conservative framing is right)

**Review:** "`stored > totalCount` catches some drift but not all — one deleted play plus one
new play can keep counts equal."

**Our position:** that specific example does **not** actually evade the check. We adopt the
review's *conclusion* (call it a drift signal, not deletion detection) but on corrected
reasoning.

**Why:** if one play is deleted upstream and one new play is added, `totalCount` stays equal —
but on the next incremental sync we **ingest the new play** (store +1) while the deleted one
**remains** in the store (never revisited). So `stored = old_total + 1` vs
`totalCount = old_total` ⇒ `stored > totalCount` ⇒ the warning **fires**. The naive
counterexample trips the detector once the new play lands.

**The real blind spot** is narrower: e.g. a deletion of a play that predates our earliest
sync (so we never stored it — no drift to detect), or exactly offsetting add/delete churn
*within a single sync window* before we observe the count. These are genuine but rare.

**Resolution:** we keep the passive `totalCount` warning and **describe it as a cheap drift
signal, not deletion detection** (agreeing with the review), and note that precise detection
needs a full id set-diff (opt-in `sync --full --show-deleted`). We just don't want the
rationale recorded as the count-equality example, which is misleading.

---

## 3. Split M2 — don't pile every resilience requirement into one milestone

**Review:** adds to M2 — first-backfill state machine, transactional checkpoint, 401 re-mint,
429/5xx backoff, cursor-invalidation fallback (on top of the existing pagination + resume).

**Our position:** every one of those requirements is **correct and accepted**. But together
they make M2 a mega-milestone that's hard to land and review as a single PR.

**Resolution:** split incremental sync into two milestones:
- **M2a — core incremental sync:** newest→older pagination, finish-page-then-break, one-page
  overlap, idempotent upsert, `totalCount` reconcile, transactional page-ingest + checkpoint.
  Happy path + idempotent top-restart on any failure.
- **M2b — resilience hardening:** first-backfill state machine (interruption + new-plays-mid-
  backfill via the anchor + post-backfill sweep), 401 re-mint (session-cookie auth), 429/5xx
  backoff, cursor-invalidation fallback.

This keeps each PR reviewable and lets M3/M4 begin against M2a's stable store. (Reflected in
the rev-2 milestone table.)

---

## 4. History scope (blocker #1) is a *validation* task, not a *documentation* decision

**Review:** "decide and document" whether the product syncs all plays vs mode-42, and whether
practice runs are included; recommends defaulting scope and inferring benchmark validity.

**Our position:** we agree the scope must be pinned — but the gating dependency is **live
discovery, not a decision we can make at the desk.** We confirmed `aimlab_agg.py` filters on
`is_practice` (so practice is a real Aimlabs dimension), but the **`taskHistory` (`plays`)
query we use sends no practice filter**, so whether our stream *includes* practice runs and
whether each node *exposes* `is_practice`/`input_device` is currently **unknown**.

**Resolution:** the scope decision must be **sequenced after** a small live-validation probe
(does `plays` accept an `is_practice` filter? does the node expose it / `input_device`?). The
*decision* (include/exclude/flag practice) follows the *finding*. We've added this as an
explicit **pre-M1 live-validation checklist** in rev 2, ahead of M1, because it determines
what we persist. We agree with the fallback the review proposes (if practice status is
unavailable, infer benchmark validity from catalog membership + `mode == 42`) — but only as
the fallback if validation shows the field truly isn't exposed.

---

## 5. Auth-doc wording: soften the fragility claim, but keep the legitimacy substance

**Review (doc edit #2):** soften "clean, legitimate path" to "least invasive local-only path"
because it relies on undocumented frontend behavior.

**Our position:** partially agree. We'll soften the **"clean"** overclaim — it *does* depend
on an undocumented frontend endpoint (`/api/auth/session` exposing `accessToken`), which is
genuinely fragile, and the doc already flags that as a known risk. But we'll **preserve the
substantive legitimacy point**, because it's making a different and defensible claim: we use
the site **as its own frontend does, for the user's own account**, with **no credential
theft, no password scripting, no scraping of other users**. "Least invasive local-only" loses
that distinction.

**Resolution:** reword to something like *"the least-invasive, own-account path — using the
site as its own frontend does, with no credential theft or third-party data access — though it
relies on undocumented frontend behavior and is therefore subject to change."* Honest about
fragility; not conceding it's illegitimate.

---

## 6. Store raw always — yes, but as the single source of truth, not a co-equal duplicate

**Review:** store the full raw node JSON for every play (don't mark `raw` optional);
future-proofing is a reason we chose SQLite.

**Our position:** agree raw should always be stored. We **push back on storing `raw` *and* the
typed columns as two independent sources of truth**, because they can silently drift and it
roughly doubles row size (raw duplicates `performance_scores` plus the typed fields).

**Why:** if both are authoritative, a parsing change or a bug can make the typed columns
disagree with `raw`, and nothing flags it.

**Resolution:** `raw` is the **canonical record**; the typed columns (`score`, `ended_at`,
`task_id`, `performance_scores`, durations, `gridshield_status`) are an **indexed projection
derived from `raw`**, regenerable by re-parsing. Documented precedence: on any disagreement,
`raw` wins and the typed columns are rebuilt. This keeps the future-proofing the review wants
without two competing truths. (The ~2× size is immaterial — tens of MB at 100k plays.)

---

## Summary

| # | Review point | Our stance |
|---|---|---|
| 1 | Rename config key to `anthic_id` | **Diverge** — keep incumbent `aimlabs_user_id` (userId == anthicId confirmed) |
| 2 | Deletion counterexample (delete+add evades check) | **Correct the rationale** — it doesn't evade; adopt the "drift signal" framing |
| 3 | All resilience reqs in M2 | **Qualify** — accept all reqs, split into M2a/M2b |
| 4 | "Decide & document" history scope | **Reframe** — it's a pre-M1 live-validation task; decision follows the finding |
| 5 | Soften "clean, legitimate" → "least invasive" | **Partially agree** — soften fragility, keep the legitimacy substance |
| 6 | Store raw for every play | **Agree + qualify** — raw is canonical, typed columns are a derived projection |

Everything else in the review is accepted and folded into `RUN_HISTORY_ARCHITECTURE.md` rev 2.
