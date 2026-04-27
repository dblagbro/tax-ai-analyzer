"""AI agent helpers — codegen pipelines that produce / refine product code.

Distinct from llm_client/, which is the runtime LLM facade for end-user-facing
chat/analyze/extract calls. Agents in this package are admin-triggered, longer-
horizon, and produce artifacts (importer source code, etc.) that humans review
before promotion.
"""
