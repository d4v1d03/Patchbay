"""USD per million tokens: (input, output, cached input), matched by substring
on the model id.

DeepSeek charges about 2x in peak hours (01–04 and 06–10 UTC, weekdays). These
are off-peak prices, so runs compare regardless of when they ran.
"""

PRICES = {
    "deepseek-v4-pro": (0.66, 1.98, 0.022),
    "deepseek-flash": (0.15, 0.60, 0.003),
    "deepseek-chat": (0.15, 0.60, 0.003),  # legacy alias; responses report deepseek-flash
    "qwen3-coder": (1.00, 5.00, 0.20),
    "kimi-k2": (0.60, 2.50, 0.15),
    "glm-4.6": (0.60, 2.20, 0.11),
    "gemini-3.6-flash": (0.30, 2.50, 0.075),
    "gemini-3.8-flash": (0.30, 2.50, 0.075),
    "gemini": (0.30, 2.50, 0.075),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
}


def cost_usd(model: str, prompt: int, completion: int, cached: int = 0) -> float | None:
    for key, (pin, pout, pcache) in PRICES.items():
        if key in model:
            return ((prompt - cached) * pin + cached * pcache + completion * pout) / 1e6
    return None
