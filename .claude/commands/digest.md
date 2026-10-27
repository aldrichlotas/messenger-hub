---
description: Analyse an org's new Messenger messages and update its tracker, roadmap and dashboard
argument-hint: <org key>   (see `py -3.10 hub.py status`)
---

Update the documents for the organisation `$ARGUMENTS`.

1. Run `py -3.10 hub.py analyze $ARGUMENTS --prepare`. If it says there's nothing new, tell me and stop.
   (It doesn't sync. If I asked for fresh messages, run `py -3.10 hub.py sync $ARGUMENTS` first;
   it opens a browser window.)
2. Read `data/orgs/$ARGUMENTS/_prompt.md` and follow it exactly. It names the inbox and the folder to update.
3. When the documents are updated, run `py -3.10 hub.py analyze $ARGUMENTS --mark-done`.
4. Tell me the 3 most important things for me, and link `data/orgs/$ARGUMENTS/README.md`.
