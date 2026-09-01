"""Outreach layer (v0.3, ADR-011/012) — enrol, draft-gate, approve, send (dry-run by default), sync, status.

Built by Wave-2 unit E. The CLI surface is `leadforge outreach ...` (see outreach/cli.py). Nothing in this
package sends unless `outreach.armed: true` in leadforge.yaml AND `--live` AND `--i-am <approver>` agree.
"""
