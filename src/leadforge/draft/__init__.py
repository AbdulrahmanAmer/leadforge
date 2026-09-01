"""Agent drafting (v0.3, ADR-012) — evidence packets out, gated drafts in.

Built by Wave-2 unit F. In-harness by default: `leadforge draft export` writes one compact packet per
enrolled target (evidenced facts only), the agent writes the two model slots (subject + one observation
citing exactly one packet fact), and `leadforge draft apply` runs the mechanical no-fabrication gate before
anything is stored. No LLM API key is required anywhere in this package.
"""
