# Voltmeter-Aimlabs
An unofficial progress tracker for Voltaic benchmarks on Aimlabs.

## Status

This project is an unofficial personal tracker for Voltaic VALORANT Aimlabs
benchmark progress. Use it only for accounts and data you are authorized to
access, and respect Aimlabs and Voltaic terms, rate limits, and availability.

It ships two command-line tools that share the same authentication:

- **`voltmeter`** — the run-history pipeline: sync your play history into a local
  store and render an offline runs table with per-scenario stats.
- **`aimlab_scores.py`** — the original snapshot tool: fetch your current PB for
  each benchmark scenario and print energy/rank tables (see [Score snapshots](#score-snapshots)).

## Requirements

This project requires **Python 3.14+**. The recommended way to run it is with
[uv](https://docs.astral.sh/uv/), which reads `requires-python` / `.python-version`
and provisions a matching interpreter automatically, so no manual Python install
is needed. Run the tools from a checkout of this repository.

A pip-installed distribution is not yet fully supported: the `voltmeter` entry
point exists, but the bundled benchmark resources are not packaged as package
data, so run from the repository root (e.g. with `uv`) for now.

## Configuration

Copy `config.example.toml` to `config.toml` (gitignored) and set at least your
Aimlabs user id:

```toml
[aimlabs]
user_id = "YOUR_AIMLAB_USER_ID"
```

`config.example.toml` documents the full schema — `[storage]`, `[sync]`, and
`[report]` are all optional and default to the values shown there. The session
credential is **not** stored in `config.toml`; see [Authentication](#authentication).

## Authentication

Both tools authenticate with an Aim Lab session cookie, stored locally as the
`AIMLAB_SESSION` environment variable (or in a gitignored `.env`). Provide it in
one of two ways:

- **`voltmeter login`** opens an embedded browser; log in normally (MFA/captcha
  are handled natively) and it writes `AIMLAB_SESSION` to a gitignored `.env`.
  This needs the optional `pywebview` dependency — with uv:
  `uv run --with pywebview voltmeter login`.
- **Manually:** log in at aimlabs.com, open DevTools → Application → Cookies →
  aimlabs.com, and copy `__Secure-next-auth.session-token` into `.env` as
  `AIMLAB_SESSION="…"`.

`voltmeter` never accepts the session cookie as a command-line argument: `voltmeter
sync`'s only override is `--session-file PATH` — a path to a file whose first line
holds the cookie, never the secret itself. `sync` never opens a login window; if the
credential is missing or expired it exits asking you to run `voltmeter login`.
`voltmeter report` is fully offline and never touches authentication or the network.

`aimlab_scores.py` reads `AIMLAB_SESSION` automatically too, but additionally accepts
a general `--header "Key: Value"` passthrough for debugging. Avoid passing a `Cookie:`
or `Authorization:` secret through it — command-line values can leak into shell history
and process lists, and an explicit `--header` cookie overrides the resolved
`AIMLAB_SESSION`.

## Run-history pipeline (`voltmeter`)

```bash
uv run voltmeter login   # capture the session cookie into .env (once)
uv run voltmeter sync    # fetch new plays into the local store
uv run voltmeter report  # offline: runs table + per-scenario stats
```

- `sync` pulls your play history into the SQLite store at `data/aimlabs.db`
  (override with `[storage].db_path`). `--full` re-derives every projection and
  reconciles totals; `--report` prints the report afterward.
- `report` reads only from the store — reverse-chronological runs plus per-scenario
  PB, median, and rolling stats, scoped by `[report].family`. Non-APPROVED runs
  are excluded by default (`--include-all-statuses` to include them).
- Global options: `--config PATH`, `--verbose`. Run `voltmeter <command> --help`
  for the full surface.

The scenario catalog (task_id → scenario metadata) is built automatically from the
bundled `resources/aimlabs/` files, so the flow above needs no setup step for it.
`uv run voltmeter refresh-catalog` is a maintainer-only diagnostic: run it after
editing those resource files to validate the rebuild and report any duplicate
`task_id` collisions. Normal use never requires it.

## Score snapshots

`aimlab_scores.py` prints one table per difficulty, then overall rank and
subcategory energy summaries for your current PBs. Exact values depend on the
configured Aimlabs account and PBs. It uses the same `AIMLAB_SESSION` credential
as `voltmeter` (see [Authentication](#authentication)).

```bash
uv run aimlab_scores.py
```

See the [full example output](docs/example_output.log) for a complete run.

```text
Fetched novice scores in 10.46s (21/21 returned).
Fetched intermediate scores in 10.50s (21/21 returned).
Fetched advanced scores in 10.56s (21/21 returned).

=== OVERALL RANK ===
Difficulty    Overall Rank  Energy  Next Rank                      Subcats
------------  ------------  ------  -----------------------------  -------
NOVICE        Gold          488     Max                            8
INTERMEDIATE  Immortal      843     Max                            8
ADVANCED      Unranked      853     94.8% to Radiant (energy 900)  8

=== SUBCATEGORY ENERGY BY DIFFICULTY ===
--- INTERMEDIATE ---
Category/Subcategory  Energy  Source Scenario    Rank
--------------------  ------  -----------------  --------
Flick-tech/Dynamic    826     Angleshot          Immortal
Flick-tech/Core       825     Fourshot Adaptive  Immortal
Flick-tech/Reflex     888     Widereflex         Immortal
Micros/Evasive        829     Angleshot Micro    Immortal
Micros/Core           802     Micro 2 Sphere     Immortal
Micros/Reflex         872     Micropace          Immortal
Stability/Strafe      819     Controlstrafes     Immortal
Stability/Precise     899     Angle Track        Immortal
```

## Ranking Methodology

The tracker follows Voltaic's official benchmark energy model: scenario energy
uses the tier's thresholds plus the prior-tier ghost rank where applicable, each
subcategory uses the best scenario energy capped at the next tier's first rank
minus one, and overall benchmark energy is the floored harmonic mean of the
subcategory energies.

The main scenario table labels threshold rank as `Score Rank`. This can be
higher than the capped `Energy` contribution for a lower difficulty: for
example, a strong multi-tier Novice scenario score can reach an Immortal score
rank while still contributing at most 499 energy to Novice.

Sources:
- https://app.voltaic.gg/leaderboards/about
- https://github.com/VoltaicHQ/energy-calculation/blob/main/energy_tutorial.md

## Resources

The `voltmeter` scenario catalog loads **every** `*.json` file under
`resources/aimlabs/` — currently `valorant_s1.json`, `aimlabs_s2.json`, and
`aimlabs_s3.json` — so all three are active for run-history matching.

The `aimlab_scores.py` snapshot tool is narrower: it reads only
`resources/aimlabs/valorant_s1.json` (the Voltaic VALORANT benchmark).

The `resources/kovaaks/` files (`kovaaks_s3.json`, `kovaaks_s4.json`,
`kovaaks_s5.json`) are reserved for possible future KovaaKs support and are not
currently wired into either tool.

## License

AGPL-3.0-only. See `LICENSE`.
