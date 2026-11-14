# Performance targets

The thresholds of the latency recipe. They come from the measurements of 11 and 14 November 2026
on the UAT host (12 cores): the bare CLIs and Plugboard in front of them, one short prompt
("reply with one word"). Plugboard adds almost nothing to a CLI (the gateway's own answer is 0.01 s);
what a call costs is the CLI itself: it is started for each request (3 to 4 s before its first
event), then the model answers. The targets keep a margin of about 1.5 to 2 times the measures
and are checked by [`scripts/bench_latence.py --check`](../../scripts/bench_latence.py) and
[`scripts/recette_services.py`](../../scripts/recette_services.py).

## Short call, one word, no stream (median of 3, one call at a time)

| Provider (CLI) | Measured | Target |
| --- | --- | --- |
| agy, `gemini-3.8-flash-low` | 5.2 to 7.7 s | at most 12 s |
| agy, `gemini-3.8-flash-medium` | 5.6 to 13.7 s | at most 18 s |
| claude, `claude-haiku-4-5` | 7.3 s (bare CLI) | at most 12 s |
| claude, `claude-sonnet-5-5` | not measured | at most 15 s |
| codex, `gpt-5.6-luna` | 5.8 s | at most 12 s |

Plugboard's own share, gateway against bare CLI on the same call: at most 0.5 s.

## First word of a stream

The first `data:` event with text, from the request sent.

| Provider | Measured | Target |
| --- | --- | --- |
| claude (live stream) | 5.8 s | at most 8 s |
| agy (live stream) | 6.1 s | at most 8.5 s |
| codex (whole message, replayed) | the end of the call, 5.8 s | at most 12 s |

On a one-word answer the first word comes a few tenths of a second before the end: the gain of the
live stream is the length of the answer, which grows with it. The CLI's start (3.2 to 3.8 s
before its first event) is a fixed cost that no stream removes.

## Calls at the same time

A CLI provider runs one chat at a time (`PLUGBOARD_CLI_PROVIDER_MAX_INFLIGHT=1`); the others wait
up to `PLUGBOARD_PROVIDER_WAIT_S` (10 s). With 3 calls at once, each is either served
(`200`) or refused at once with `503 providers_busy` within 10.5 s; none hangs longer. The second
served call takes about the length of the first one more.

## Endpoints without inference

`/health`, `/health/ready`, `/v1/models`, `/v1/me`: median at most 0.1 s and no call over 1 s
(measured: 0.01 s).

## Background jobs

After a start, within 120 s (`--wait 120`) each of the five jobs of the Jobs page is `success` or
`running`: price refresh, model catalogue refresh, provider watchdog, availability snapshots, data
retention. `disabled`, `not_started`, `stopped`, `error`, `stale` or a missing job fails the
recipe. The backup timer of the prod host is not a gateway job: check it with `systemctl
list-timers`.

These figures are the first thresholds, not guarantees; they are revised from the real runs of the
bench and kept here.
