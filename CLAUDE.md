# Notes for agents

## A long-running simulation campaign may be in progress

A batch of JoinMarket simulations may be running on the Kubernetes cluster (namespace
`rajnoha-ns`). Before running, cleaning up, restarting or redeploying anything there, or pushing
simulation images, find out whether it is still running and what to watch out for:

- `docs/campaign_2026/RUNBOOK.md` — what is running, how to check its progress, what not to do
  while it runs, and how to read its results.
- `docs/campaign_2026/CHECKLIST.md` — status of each configuration and run.
