import os
import requests
UnifiedAIRouter = MultiTierAIRouter

class MultiTierAIRouter:
    def __init__(self):
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.primary_claude = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
        self.primary_gemini = os.getenv("GEMINI_PRO_MODEL", "gemini-4-hydrogen")
        self.fallback_gemini = os.getenv("GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
        self.active_setup_engine = "Rule-Based Quant Engine"
        self.active_analysis_engine = "Rule-Based Quant Engine"

    def _call_claude(self, prompt: str, max_tokens: int = 900) -> str:
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
                data = resp.json()
                return data["content"][0]["text"].strip()
        except Exception:
            pass
        return ""

    def _call_gemini(self, model_name: str, prompt: str) -> str:
        if not self.gemini_key:
            return ""
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.gemini_key}"
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 950},
                },
                timeout=25,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            pass
        return ""

    def route_high_priority(self, prompt: str, role: str = "ANALISA") -> str:
        # Urutan prioritas: Gemini 4 Hydrogen -> Claude -> Gemini Fallback
        out = self._call_gemini(self.primary_gemini, prompt)
        if out:
            engine_label = f"{self.primary_gemini} (Tier-1 Priority)"
        else:
            out = self._call_claude(prompt)
            if out:
                engine_label = f"{self.primary_claude} (Tier-1 Claude)"
            else:
                out = self._call_gemini(self.fallback_gemini, prompt)
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
