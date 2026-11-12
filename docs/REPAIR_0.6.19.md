# Reliability repair v0.6.19

Baseline: main at 013ad8f55f2e2665e107ee6652368311d83ee622. The previous main.py is retained unchanged as app/legacy.py; its UI, translations, analysis panels and team-data helpers are reused. Existing LINE rich menu settings and Render plan are not changed.

## Fixed
- NBA source errors no longer prevent fallback execution. Whole-season schedule, today's scoreboard and the existing ESPN feed are isolated.
- All-sport queries run independently, not sequentially. Webhooks validate the original signature and acknowledge before querying external feeds.
- Added bounded caching, request coalescing, 403/429 cooldown, stale-data labels and event deduplication.
- MLB upcoming search covers 240 days instead of 90; UTC dates are converted to Taiwan time and home/away labels are preserved.
- Missing fixture data offers retry/navigation and an explicitly labelled simulated matchup, without inventing a dated fixture.
- Both analysis panels and the analyze-other-teams button remain intact.
- Startup probes log real provider results and use LINE's validate/reply API, which validates payloads without sending messages.

## Verification and limits
26 local mocked regression tests passed for parsers, fallback isolation, cache, command dispatch, signature validation, duplicate events and immediate webhook acknowledgement. These are not end-to-end LINE user-delivery tests. Deployment logs must separately confirm real source availability and message validation.

No paid resources or API keys were added. Public access does not establish commercial redistribution rights; verify source terms before commercial release. Existing picks are deterministic demonstrations, not trained AI predictions. Rosters are not verified starting lineups. In-process caches/tasks are ephemeral and are not a durable queue; Render free-tier sleep/cold starts are not eliminated by this patch.
