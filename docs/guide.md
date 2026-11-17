# Using hyra in a project

## The whole thing
```bash
cd my-repo
npx skills add <owner>/<repo>      # add the hyra skills (skills.sh)
hyra init                          # nothing to answer
```
Then tell your coding agent what you want: *"use hyra to find out whether X; it's done when Y."* It handles the rest: it opens a
mission, queues tasks, runs workers, gets results independently reviewed, and keeps notes. (The first time, the agent also puts the
`hyra` command on your PATH.)

`hyra init` creates `.hyra/` in the repo and writes your machine-wide settings (`~/.hyra/config.json`) the first time.
If you have a ledger server it connects the repo to it automatically.

## Optional, once per machine: the hardened ledger
Seals worker claims on a *different* machine and enforces blind review and approvals there; backs itself up to a private git repo.
Skip it for quick or exploratory work.
```bash
hyra server up                       # picks a tailnet device that accepts ssh, or: hyra server up noctezeta@blend
```
It asks for a private git repo to back the ledger up to (every 6 hours from then on). Every `hyra init` after that connects automatically.
`hyra config set --user ledger_hosts '["noctezeta@blend","nitez@APOTHEOSIS"]'` tells it which machines (and usernames) are yours.

## Day to day
```bash
hyra status                          # what's happening, what needs you
hyra msg inbox human                 # what your agents are asking
hyra approval list ; hyra approval grant 3     # big compute is yours to approve
hyra run                             # long unattended work (in tmux): launches the agents itself
hyra doctor                          # something off? it says what
```

## Where things live
| | |
|---|---|
| `<repo>/.hyra/` | that project's queue, notes, evidence, config |
| `~/.hyra/config.json` | your machine-wide settings (`hyra config show`) |
| `~/.hyra/ledger.json` | where your ledger is and its tokens (mode 600; treat like an ssh key) |
| ledger machine `~/.hyra-server/` | the ledger's database (sealed claims, approvals, audit trail) |
