# Design Review — Pushback & Qualifications, Round 2

**Status:** Companion to [`DESIGN_REVIEW_v2.md`](DESIGN_REVIEW_v2.md) and the §5.1 live-validation probe
**Date:** 2026-06-06

Round-2 review ([`DESIGN_REVIEW_v2.md`](DESIGN_REVIEW_v2.md)) was **accepted nearly wholesale** —
all six items are folded into [`RUN_HISTORY_ARCHITECTURE.md`](RUN_HISTORY_ARCHITECTURE.md) rev 3.

Unlike [`DESIGN_PUSHBACK.md`](DESIGN_PUSHBACK.md) (which pushed back on the external reviewer),
**most of this document pushes back on *our own* live-validation findings.** When the §5.1 probe
came back clean, the right move wasn't to rubber-stamp it — empirical results from **one account
at one moment** deserve the same skepticism we apply to a reviewer. These are the hedges we placed
on our own data before letting it harden into design claims, plus one place we refined a round-2
reviewer recommendation.

---

## Part A — Pushbacks on our own §5.1 / auth probe findings

### 1. "Mode 42 ⇒ no practice" is one account at one time — not a structural guarantee

**Finding:** the live probe showed `task_mode=42 & is_practice=true` = **0** and `… = false`
= **919**; account-wide there are only 4 practice plays, all outside mode 42. Tempting conclusion:
"practice can't contaminate the stream — no action needed."

**Our pushback:** that proves *this* account has *currently* no mode-42 practice plays. It does
**not** prove the platform makes mode-42-practice impossible for every account or future state.
And we separately confirmed the history stream exposes **no** `is_practice` field/filter — so if
contamination ever did occur, we'd have **no per-row way to detect or exclude it**, and it would
silently inflate PBs/stats.

**Why it matters:** a progress/rank tracker that silently counts practice runs is misleading, and
the failure would be invisible.

**Resolution (rev 3, §5.1 / §5.2 / §8.3):** keep the conclusion (drop the `is_practice` column —
it's unpopulatable *and* unneeded), but add a **cheap per-sync contamination check** via the
aggregate endpoint: `count(user_id, task_mode=42, is_practice=true)`; if **> 0**, warn. Same
drift-signal philosophy as the `totalCount` check. Converts a silent, one-account-specific
assumption into a visible warning for ~one extra call.

### 2. The auth-failure *trigger* is a known-unknown — handle it, don't explain it

**Finding:** mid-probe the session route returned `accessTokenError: RefreshAccessTokenError`
(refresh token dead) while the cookie's `expires` was still a month out; re-`login` fixed it.

**Our pushback:** we can name the *remedy* (re-login, empirically verified) but **not the
trigger.** The refresh token dying ~2 minutes after a successful mint is unexplained — last cached
token? rapid session-route calls? coincidence? We don't control or observe Aimlabs' server-side
token lifecycle, so any story we tell is a guess.

**Why it matters:** designing around a lifecycle we *think* we understand invites a brittle
heuristic. Designing around what we *observe* is robust.

**Resolution (rev 3, §4 / §8.4; auth doc §9.1, §9.3):** treat **any** `accessTokenError` as a
terminal "re-login required" state (distinct from cookie-expiry and from a transient 5xx); the
re-mint path **stops and surfaces it**, never retry-loops. The doc explicitly records the trigger
as **not understood**, which is itself the argument for the defensive handling.

### 3. We found a second endpoint the design hadn't acknowledged

**Finding:** the practice cross-check only worked because of `aimlab.plays_agg`
(`AimlabPlayWhere`), a server-side aggregate that filters by `user_id` / `task_id` / `task_mode` /
`is_practice` and returns `count` / `avg` / `max`.

**Our pushback (on the design's completeness):** this isn't just probe scaffolding — it's a real,
useful capability the architecture had ignored. Leaving it undocumented means the next person
re-discovers it.

**Why it matters:** it's what makes the contamination check (and future count reconciliation)
cheap, and it's a distinct tool from the `aimlabProfile.plays` sync stream.

**Resolution (rev 3, §5.2):** recorded as a complementary cross-check endpoint (not the sync
source), including the field-name divergence (`task_mode` here vs `mode` on the history filter) and
the gotcha that its `max{}` aggregate **500s on an empty result set** (use `count`-only).

### 4. Honesty note: cursor-expiry is still unvalidated

**Not a pushback — a gap we refused to paper over.** The §5.1 checklist had a "does a stored Relay
cursor survive between runs?" item we never actually tested. We could have quietly called §5.1
"100% closed"; instead it stays **open**.

**Resolution (rev 3, §5.1 / §16):** marked unvalidated but **non-blocking** — the M2b
cursor-rejection → top-restart fallback already makes a dead cursor safe — and deferred to M2b
testing rather than claimed as done.

---

## Part B — One refinement to a round-2 reviewer recommendation

### 5. raw→projection repair: re-derive the *whole* projection, don't selectively compare

**Review v2 (blocker #1)** correctly caught that "raw is canonical" conflicted with a
"mutable-only `--full`," and recommended: on `--full`, parse incoming `raw`, **compare** derived
immutable fields to the stored projection, and conditionally rebuild/warn.

**Our refinement:** accept the problem, simplify the fix. Rather than a compare-and-conditionally-
rebuild path, make the typed columns a **pure function of `raw`, re-derived in full on every `raw`
write.** This dissolves the mutable/immutable column split entirely (`gridshield_status` is just
another derived column), makes "raw wins" **structural rather than documentary**, and subsumes the
reviewer's concern with less machinery. A change to a field we *expected* to be stable still emits
a drift warning — but as an observation, not a storage branch.

**Resolution (rev 3, §7 / §7.1):** typed columns are a re-derived projection of `raw`; incremental
= insert-only no-op; `--full` = re-derive + warn on unexpected drift. Same guarantee the reviewer
wanted, fewer moving parts.

---

## Summary

| # | Finding / recommendation | Our stance | Landed in |
|---|---|---|---|
| 1 | "Mode 42 ⇒ no practice, no action needed" | **Qualify** — one-account/point-in-time; add cheap contamination check | §5.1, §5.2, §8.3 |
| 2 | Auth failure cause | **Hedge** — trigger not understood; handle *any* `accessTokenError` defensively | §4, §8.4; auth §9.1/§9.3 |
| 3 | `plays_agg` endpoint | **Record** — complementary cross-check tool the design had ignored | §5.2 |
| 4 | "§5.1 fully closed" | **Honest gap** — cursor-expiry unvalidated, non-blocking | §5.1, §16 |
| 5 | v2 blocker #1 repair (compare-then-rebuild) | **Refine** — re-derive whole projection; simpler, structural | §7, §7.1 |

Everything else from round-2 review is accepted as-stated and folded into rev 3.
