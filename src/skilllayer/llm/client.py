from __future__ import annotations


class LLMClient:
    """Tiny extracted LLM interface used by the A3 mock paths."""

    def __init__(self, mode: str = "mock") -> None:
        self.mode = mode
        self.provider = mode
        self.model = None
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_input_tokens = 0
        self.reasoning_tokens = 0

    @staticmethod
    def estimate_tokens(text: str) -> int:
        return max(1, len(text.split()))

    def generate(self, prompt: str) -> str:
        self.call_count += 1
        self.prompt_tokens += self.estimate_tokens(prompt)
        response = "Use the existing workflow and verify with tests."
        self.completion_tokens += self.estimate_tokens(response)
        return response
