[中文](README.md) | **English**

# star-pulse

Generate a "GitHub star leaderboard" locally: take a snapshot every day, publish a weekly report (weekly star gains, category breakdown, star-inflation flags), and build a **daily board** of what actually moved.

Zero third-party dependencies — Python 3.11+ standard library only. CI needs no `pip install`.

> Live board: https://logic77lhl.github.io/star-pulse/

---

## Approach

One sentence: **trust no third-party delta; take your own snapshot every day and subtract two real dates.**

```
                    ┌─ (1) seed list (config/watchlist.txt)   ← explicit, always included
                    ├─ (2) Trending page parsing             ← what's hot right now
    candidates ─────┤
                    ├─ (3) in-pool renewal (reserved slots)  ← keeps old projects' series unbroken
                    └─ (4) Search API                        ← fills the remaining slots
                                  │
                                  ▼
                    candidate pool (max_candidates, default 800)
                                  │
                                  ▼
                   daily snapshot → data/snapshots/*.json    ← the source of truth, committed
                                  │
                       reload on start ▼
                          SQLite (local query cache, not committed)
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
             docs/ daily board            reports/ weekly report
             (04:00 daily)                (Mondays 10:00)
```

The board's row count isn't fixed — it's recomputed daily and is a **rolling window, not an accumulating list**:

```
pool 800 − archived/forked = rendered rows (currently 799)
```

Only repositories with a snapshot for that day are selected, so the detail table never grows with history; the `repo` table accumulates because deltas need it.

### Four discovery channels, in priority order

Request budget is finite, so whoever comes first gets the slot. The order is by "how irreplaceable this channel is":

| # | Channel | Why here |
|---|---|---|
| 1 | Seed list | You chose these explicitly; they must never drop out |
| 2 | Trending | The only source of genuinely new entrants |
| 3 | In-pool renewal | Guarantees a time series for projects already tracked |
| 4 | Search API | Fills whatever is left |

### Data layers

- `data/snapshots/*.json` — the facts, one file per day, committed
- SQLite — a local query cache rebuilt from snapshots on start, never committed
- `docs/` — the board, regenerated daily
- `reports/` — weekly markdown

### Three principles that don't bend

1. **No cumulative counters.** Only two real snapshots subtracted.
2. **The raw snapshot is the source of truth.** Everything else can be regenerated.
3. **When a fetch fails, record the failure.** Never write 0 to mean "unknown".

## Why daily, not weekly

A weekly diff cannot tell "gained 300 stars steadily" from "one viral day". Daily snapshots keep the shape; the weekly report is a summarisation, not the only data.

## Why no third-party data source (measured)

Third-party "trending" APIs report deltas with unclear definitions and unclear windows. Two numbers taken an unknown interval apart are not a rate. Measured against our own snapshots, the discrepancy was large enough that the ranking order changed — so the project takes its own photos.

## Quick start

```bash
# 1. Token (works without one, but 60 requests/hour won't cover the pool)
#    edit .env, fill GITHUB_TOKEN
#    https://github.com/settings/personal-access-tokens/new
#    Fine-grained, permission: Public Repositories → Metadata: Read

# 2. Health check: config, quota, data sources
python -m star_pulse doctor

# 3. Small trial run, ~1 minute
python -m star_pulse run-daily --limit 50

# 4. Report (no history yet → "new projects" plus a warm-up note)
python -m star_pulse report --period new

# 5. After 8 days of run-daily, weekly gains work
python -m star_pulse report --period last-week
```

## Deploying to GitHub

1. Create a public repository (Pages on a free account requires public).
2. Enable Actions.
3. Enable GitHub Pages (the board URL).
4. Optional: LLM-written Chinese commentary — set the key in Actions secrets. Without it, the board still renders; only the commentary section is skipped.

## What the report contains

- Weekly star gains, ranked, with the raw delta and the observation window
- Category breakdown
- Star-inflation flags (heuristics, not verdicts)
- Three quality gates before anything is published

## Layout

```
star_pulse/          the package (stdlib only)
config/              watchlist and tunables
data/snapshots/      daily facts, committed
docs/                the board
reports/             weekly markdown
tests/               unittest
```

## Tuning

`max_candidates`, request concurrency, and the search query set live in `config/`. The main constraint is the GitHub rate limit: the pool size is chosen so a full day fits in the token's quota.

## Known limits

- A snapshot shows *how much* a project gained, never *why*.
- Star-inflation detection is heuristic, not a judgement.
- 53 consecutive days is still short: seasonality and one-off virality are indistinguishable.
- The pool is a sample, not all of GitHub.

## License

[MIT](LICENSE)
