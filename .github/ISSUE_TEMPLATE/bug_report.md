---
name: Bug report
about: Something behaves differently than documented
labels: bug
---

**What happened**

**What you expected**

**Reproduce**

```bash
# the exact commands, starting from a fresh log if possible
```

**Environment**

- OS:
- Python version (`python --version`):
- Running via Docker? (image tag / `docker --version`):

**Evidence**

Output of `python -m sentinel --log <your log> --json health --deep`, plus any
JSON error document (`--json` on the failing command) or stderr log lines. Please
strip anything you consider sensitive — card ids in your own data, key ids.
