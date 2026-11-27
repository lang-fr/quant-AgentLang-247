import os
import time
import requests


class UnifiedAIRouter:
    """
    Router AI Terpadu (Sniper Setup, Analisa 8 Pilar, & Co-Pilot Live Chat):
    Tier 1: Claude Frontier (claude-fable-5-2 -> claude-opus-5-5 -> claude-sonnet-5-5 -> claude-opus-4-6)
    Tier 2: Gemini 4 Flagship (gemini-4-carbon -> gemini-4-neon -> gemini-4-argon -> gemini-4-hydrogen)
    Tier 3: Gemini High-Speed Fallback (gemini-3.8-flash -> gemini-3.5-flash-lite)
    """

    def __init__(self):
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.cooldown_until = {}

        self.priority_chain = [
            {"provider": "anthropic", "model": "claude-fable-5-2", "tier": "Tier-1 Flagship"},
            {"provider": "anthropic", "model": "claude-opus-5-5", "tier": "Tier-1 Opus"},
            {"provider": "anthropic", "model": "claude-sonnet-5-5", "tier": "Tier-1 Sonnet"},
            {"provider": "anthropic", "model": "claude-opus-4-6", "tier": "Tier-1 Backup"},
            {"provider": "gemini", "model": "gemini-4-carbon", "tier": "Tier-2 Flagship"},
            {"provider": "gemini", "model": "gemini-4-neon", "tier": "Tier-2 Neon"},
            {"provider": "gemini", "model": "gemini-4-argon", "tier": "Tier-2 Argon"},
            {"provider": "gemini", "model": "gemini-4-hydrogen", "tier": "Tier-2 Hydrogen"},
            {"provider": "gemini", "model": "gemini-3.8-flash", "tier": "Tier-3 Fallback"},
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "tier": "Tier-3 Lite"},
        ]

    def _is_available(self, model_name: str) -> bool:
        return time.time() >= self.cooldown_until.get(model_name, 0)

    def _set_cooldown(self, model_name: str, seconds: int = 1800):
        self.cooldown_until[model_name] = time.time() + seconds

    def _call_anthropic(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 2200) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.1,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=55)
        if resp.status_code in (400, 402, 404, 429, 529):
            self._set_cooldown(model, 3600)
            raise RuntimeError(f"Anthropic {model} unavailable ({resp.status_code})")
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    def _call_gemini(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 2200) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_tokens,
            },
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=55)
        if resp.status_code in (400, 403, 404, 429, 503):
            self._set_cooldown(model, 1800)
            raise RuntimeError(f"Gemini {model} unavailable ({resp.status_code})")
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 2200) -> tuple[str, str]:
        for entry in self.priority_chain:
            provider = entry["provider"]
            model = entry["model"]
            tier = entry["tier"]

            if not self._is_available(model):
                continue

            if provider == "anthropic" and self.anthropic_key:
                try:
                    text = self._call_anthropic(model, system_prompt, user_prompt, max_tokens)
                    return text, f"{model} ({tier})"
                except Exception:
                    continue

            if provider == "gemini" and self.gemini_key:
                try:
                    text = self._call_gemini(model, system_prompt, user_prompt, max_tokens)
                    return text, f"{model} ({tier})"
                except Exception:
                    continue

        return "", "Deterministic-Quant-Fallback"
