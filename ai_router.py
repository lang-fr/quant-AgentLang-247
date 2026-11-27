import os
import requests


class MultiTierAIRouter:
    def __init__(self):
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.primary_gemini = os.getenv("GEMINI_PRO_MODEL", "gemini-4-hydrogen")
        self.primary_claude = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
        self.fallback_gemini = os.getenv("GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
        self.active_setup_engine = "Quant Deterministic Engine"
        self.active_analysis_engine = "Quant Deterministic Engine"

    def _call_claude(self, prompt: str, max_tokens: int = 950) -> str:
        if not self.anthropic_key:
            return ""
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.primary_claude,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=25,
            )
            if resp.status_code == 200:
                return resp.json()["content"][0]["text"].strip()
        except Exception:
            pass
        return ""

    def _call_gemini(self, model_name: str, prompt: str, max_tokens: int = 950) -> str:
        if not self.gemini_key:
            return ""
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={self.gemini_key}"
            )
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
                },
                timeout=25,
            )
            if resp.status_code == 200:
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass
        return ""

    def route_high_priority(self, prompt: str, role: str = "ANALISA", max_tokens: int = 950) -> str:
        out = self._call_gemini(self.primary_gemini, prompt, max_tokens)
        if out:
            engine_label = f"{self.primary_gemini} (Tier-1 Gemini)"
        else:
            out = self._call_claude(prompt, max_tokens)
            if out:
                engine_label = f"{self.primary_claude} (Tier-1 Claude)"
            else:
                out = self._call_gemini(self.fallback_gemini, prompt, max_tokens)
                engine_label = f"{self.fallback_gemini} (Fallback)" if out else "Quant Deterministic Engine"

        if role == "SETUP":
            self.active_setup_engine = engine_label
        else:
            self.active_analysis_engine = engine_label
        return out

    def generate_analysis(self, prompt: str) -> str:
        return self.route_high_priority(prompt, role="ANALISA")

    def refine_setup(self, prompt: str) -> str:
        return self.route_high_priority(prompt, role="SETUP")

    def generate(self, sys_prompt: str, usr_prompt: str, max_tokens: int = 950, role: str = "ANALISA"):
        combined = f"{sys_prompt}\n\n{usr_prompt}"
        text = self.route_high_priority(combined, role=role, max_tokens=max_tokens)
        if not text:
            text = (
                "1. Makro & Likuiditas: Mode deterministik kuantitatif aktif (API LLM tidak merespons).\n"
                "2. Konfluensi Intermarket & Orderflow: Sinyal divalidasi berdasarkan struktur MTF & likuiditas.\n"
                "3. Skeptic Veto: Patuhi batas risiko 1% dan SL ketat sesuai aturan mesin."
            )
        engine = self.active_setup_engine if role == "SETUP" else self.active_analysis_engine
        return text, engine


UnifiedAIRouter = MultiTierAIRouter
