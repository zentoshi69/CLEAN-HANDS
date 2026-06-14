# Deploying CLEAN HANDS — the one true way

**This repo (`CLEAN-HANDS`) is the single source of truth.** The server at
`/home/clean/CLEAN-HANDS` is a git checkout of it. Never copy files onto the
server by hand — that is how every outage this project ever had happened.

## Deploy anything

```bash
cd /home/clean/CLEAN-HANDS && sudo -u clean git stash --include-untracked && \
  sudo -u clean git pull --ff-only origin main && sudo bash clean-hands/deploy/redeploy.sh
```

> Use `git stash` (recoverable), **never** `git checkout -- .` — the latter
> silently discards any emergency hotfix made on the box. `git stash` is a
> no-op when the tree is clean, so it's always safe to run.

That's it. The script installs the website + nginx vhost, restarts the app,
and prints a VERIFY block (expect: app 200, 11 live spans, website 200).

## What about `zeno-monitor`?

Historical accident: CLEAN HANDS was first built inside the private
`zeno-monitor` repo (an unrelated dashboard). Everything needed was merged
here. `zeno-monitor` is NOT deployed and should not be used for CLEAN work.
