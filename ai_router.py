import os
import time
import requests


class UnifiedAIRouter:
    """
    Router AI Terpadu dengan hierarki prioritas tertinggi:
    Tier 1: Claude (claude-opus-4-6 -> claude-sonnet-4-6)
    Tier 2: Gemini Flagship (gemini-4-hydrogen -> gemini-3.5-pro)
    Tier 3: Gemini High-Speed Fallback (gemini-3.8-flash -> gemini-flash-latest)
    Dipakai seragam oleh Scheduled Report maupun Co-Pilot Live Chat.
    """

    def __init__(self):
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.cooldown_until = {}

        self.priority_chain = [
            {"provider": "anthropic", "model": "claude-opus-4-6", "tier": "Tier-1 Primary"},
            {"provider": "anthropic", "model": "claude-sonnet-4-6", "tier": "Tier-1 Secondary"},
            {"provider": "gemini", "model": "gemini-4-hydrogen", "tier": "Tier-2 Flagship"},
            {"provider": "gemini", "model": "gemini-3.5-pro", "tier": "Tier-2 Backup"},
            {"provider": "gemini", "model": "gemini-3.8-flash", "tier": "Tier-3 Fallback"},
            {"provider": "gemini", "model": "gemini-flash-latest", "tier": "Tier-3 Emergency"},
        ]

    def _is_available(self, model_name: str) -> bool:
        now = time.time()
        return now >= self.cooldown_until.get(model_name, 0)

    def _set_cooldown(self, model_name: str, seconds: int = 1800):
        self.cooldown_until[model_name] = time.time() + seconds

    def _call_anthropic(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 1800) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.anthropic_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.15,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        if resp.status_code in (402, 429, 529):
            self._set_cooldown(model, 3600)
            raise RuntimeError(f"Quota/RateLimit Anthropic ({resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()

    def _call_gemini(self, model: str, system_prompt: str, user_prompt: str, max_tokens: int = 1800) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.gemini_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.15,
                "maxOutputTokens": max_tokens,
            },
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        if resp.status_code in (403, 404, 429, 503):
            self._set_cooldown(model, 1800)
            raise RuntimeError(f"Quota/Unavailable Gemini {model} ({resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int = 1800) -> tuple[str, str]:
        """
        Mengembalikan tuple: (hasil_teks_ai, label_model_aktif)
        """
        errors = []
        for entry in self.priority_chain:
            provider = entry["provider"]
            model = entry["model"]
            tier = entry["tier"]

            if not self._is_available(model):
                continue

            if provider == "anthropic":
                if not self.anthropic_key:
                    continue
                try:
                    text = self._call_anthropic(model, system_prompt, user_prompt, max_tokens)
                    return text, f"{model} ({tier})"
                except Exception as exc:
                    errors.append(f"{model}: {exc}")
                    continue

            if provider == "gemini":
                if not self.gemini_key:
                    continue
                try:
                    text = self._call_gemini(model, system_prompt, user_prompt, max_tokens)
                    return text, f"{model} ({tier})"
                except Exception as exc:
                    errors.append(f"{model}: {exc}")
                    continue

        fallback_msg = (
            "Semua engine AI utama sedang mencapai batas kuota sementara. "
            "Sistem beralih ke kalkulasi kuantitatif deterministik lokal."
        )
        return fallback_msg, "Deterministic-Quant-Fallback"
