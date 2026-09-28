"""Agent Memory Leaderboard (AML) HTTP adapter for total-agent-memory.

Exposes exactly three endpoints — GET /health, POST /add, POST /search —
implementing the AML cycle-2 Add/Search contract on top of TAM's own save
and recall pipeline. Every AML user_id lives in its own TAM store served by
its own worker process; see docs/benchmarks/aml-cycle2/README.md.
"""
