"""Shared plumbing for the TAM benchmark adapters (AMA-Bench, LongMemEval-V2).

- tam_worker: an isolated TAM store in its own spawned process.
- budget_proxy / run_guarded: an OpenAI metering proxy with a hard dollar ceiling.
- stub_openai: a local OpenAI-compatible stub for zero-cost dry runs.
"""
