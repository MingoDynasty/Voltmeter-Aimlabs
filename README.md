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

For now this is source-checkout tooling. Run the CLI from the repository root
with `python aimlab_scores.py`; packaged/installed distribution is intentionally
not supported until the bundled benchmark resources are moved into package data.

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
