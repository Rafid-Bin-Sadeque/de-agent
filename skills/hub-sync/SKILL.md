---
name: hub-sync
description: End-of-session sync. Builds one proposed diff for the hub from dea diff, plans applied, and git changes in the scanned repos since the last sync (status.md rows, profile body edits, integration pages) and shows it for the human to accept. Use at the end of a work session or when the Stop hook suggests it.
---

# hub-sync

## When to use
After real work happened: plans applied, code merged, a decision made. The Stop hook suggests it when scanned repos changed.

## Inputs
- Hub path (current hub). "Since the last sync" = since the newest date in `status.md` Done rows or the last
  commit that touched the hub, whichever is later; ask the human if unclear.

## Steps
1. Spawn `de-agent:runner` to collect, in one pass: `dea diff`, the list of `.dea/plans/*.applied.json` since the
   last sync (id, title, status), and `git log --since=<date> --name-only` for each scanned repo path.
2. Run `dea scan` so generated blocks are current (that is the only write to generated content).
3. Draft a single proposal:
   - `status.md`: new Done rows for finished work; In-progress rows for open work; Dated checks for follow-ups
     (`Due` as YYYY-MM-DD, `State` one of todo/doing/blocked/waiting/done).
   - Profile body edits: only facts that changed behaviour or gotchas, in the human body section.
   - Integration pages for new edges that have none (see `onboard-project` step 6).
4. Show everything as ONE unified diff. Ask the human to accept, edit or drop each hunk.
5. Apply only accepted hunks. `dea lint` must exit 0.

## Output
- The unified diff, then (after acceptance) the files written and the `dea lint` result.

## Never
- Never edit generated blocks (`<!-- dea:generated -->`), `catalog/` or `platform/`.
- Never write a claim you did not see evidence for in the diff, plan results or git log.
- Never add rows for work that did not happen. Never write secrets. Never run `dea apply` or `dea approve`.
