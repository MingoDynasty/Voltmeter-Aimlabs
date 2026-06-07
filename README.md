# Voltmeter-Aimlabs
An unofficial progress tracker for Voltaic benchmarks on Aimlabs.

## Status

This project is an unofficial personal tracker for Voltaic VALORANT Aimlabs
benchmark progress. Use it only for accounts and data you are authorized to
access, and respect Aimlabs and Voltaic terms, rate limits, and availability.

## Configuration

Copy `config.example.toml` to `config.toml`, then set:

```toml
[aimlabs]
user_id = "YOUR_AIMLAB_USER_ID"
```

`config.toml` is gitignored. If an auth cookie is ever needed, prefer the
gitignored `session_cookie` config value or the `AIMLABS_COOKIE` environment
variable over passing a cookie through `--header`, because command-line secrets
can be exposed in shell history or process lists.

This project requires **Python 3.14+**. The recommended way to run it is with
[uv](https://docs.astral.sh/uv/), which reads `requires-python` / `.python-version`
and provisions a matching interpreter automatically, so no manual Python install
is needed:

```bash
uv run aimlab_scores.py
```

If you prefer your own interpreter, make sure it is Python 3.14 or newer (older
versions fail at startup with a syntax/import error) and run the CLI from the
repository root with `python aimlab_scores.py`.

Packaged/installed distribution is intentionally not supported until the bundled
benchmark resources are moved into package data.

## Example Output

The CLI prints one table per difficulty, then overall rank and subcategory
energy summaries. Exact values depend on the configured Aimlabs account and PBs.
See the [full example output](docs/example_output.log) for a complete run.

```text
python main.py
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

`resources/aimlabs/valorant_s1.json` is the active Voltaic VALORANT benchmark
resource. `resources/kovaaks/kovaaks_s5.json` is reserved for possible future
KovaaKs support and is not currently wired into the CLI.

## License

AGPL-3.0-only. See `LICENSE`.
