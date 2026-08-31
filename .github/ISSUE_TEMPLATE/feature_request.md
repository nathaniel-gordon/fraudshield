---
name: Feature request
about: Propose a change or addition
labels: enhancement
---

**The problem it solves**

What can't you do today, or what costs more than it should?

**Proposed shape**

CLI surface, event types, or projection — whatever you have. Rough is fine.

**Fit with the architecture**

The log is append-only, live state must equal replay, and the read side runs
without the ML stack (see `docs/adr/`). If the idea bends one of those, say which
and why it's worth it.
