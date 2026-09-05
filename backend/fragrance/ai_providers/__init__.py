"""
fragrance/ai_providers

Strategy + Adapter package for the LLM pipeline. `base.py` defines the port
(`StructuredLLMProvider`) every family adapts to; `registry.py` resolves the
active `AIModelConfig` row to a concrete provider instance. Callers in
`llm.py`/`search.py`/`tasks.py` depend only on `StructuredLLMProvider` and
`registry.get_active_provider` — never on a concrete adapter class.
"""
