# Agent Skill Evals

This directory contains a small, reviewable behavioral contract for the public `tcia-query-skill`. It complements deterministic script and MCP/REST tests; it does not replace them.

`prompts.csv` includes positive and negative routing cases. For positive cases, inspect the execution trace and final answer for the required and forbidden behaviors. For negative cases, confirm that the TCIA query skill is not selected merely because the prompt mentions DICOM, SQLite, cancer imaging, literature, or repository maintenance.

Evaluate against a pinned local bundle or fixtures when answer content depends on dataset records. Do not make live-network freshness, publication, or deployment a prerequisite for routing evaluation. Record the skill version, model, harness version, bundle fingerprint when used, trace, final answer, and grader result.

Use deterministic trace checks for observable events such as selected tools, commands, writes, and forbidden transfers. Use a separate read-only grading pass with `answer-rubric.schema.json` for source authority, provenance, access boundaries, evidence, freshness state, and output size. Treat model grading as review support; retain the trace and deterministic evidence for diagnosis.

Start with these cases and add a regression row only when real use exposes a distinct failure mode. Compare completion quality, routing accuracy, input tokens, and latency before changing the skill description, instructions, or MCP loading strategy.
