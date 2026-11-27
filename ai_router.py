import os
import logging
from typing import Optional, Dict, Any
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("FlagshipAIRouter")


class FlagshipAIRouter:
    """
    Router AI untuk quant-AgentLang-247:
    - Default / Scan awal: Gemini 4 Hydrogen (effort: high)
    - Audit / Refine setup probabilitas tinggi: Claude Sonnet 5.5 (effort: max) dengan fallback otomatis
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        timeout: int = 45,
    ):
        self.gemini_api_key = (gemini_api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.anthropic_api_key = (anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
        self.anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
        self.gemini_effort = os.getenv("GEMINI_REASONING_EFFORT", "high").strip().lower()
        self.anthropic_effort = os.getenv("ANTHROPIC_REASONING_EFFORT", "max").strip().lower()
        self.timeout = timeout

    def _call_gemini(self, prompt: str, system_instruction: str = "") -> Optional[str]:
        if not self.gemini_api_key:
            return None
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        )
        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "thinkingConfig": {"thinkingLevel": self.gemini_effort}
            },
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                texts = [p.get("text", "") for p in parts if p.get("text")]
                if texts:
                    return "\n".join(texts).strip()
        except Exception as e:
            logger.warning(f"Gemini ({self.gemini_model}, effort={self.gemini_effort}) gagal: {e}")
        return None

    def _call_anthropic(self, prompt: str, system_instruction: str = "") -> Optional[str]:
        if not self.anthropic_api_key:
            return None
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self.anthropic_model,
            "max_tokens": 4096,
            "output_config": {"effort": self.anthropic_effort},
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_instruction:
            payload["system"] = system_instruction

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content") or []
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if texts:
                return "\n".join(texts).strip()
        except Exception as e:
            logger.warning(f"Anthropic ({self.anthropic_model}, effort={self.anthropic_effort}) gagal: {e}")
        return None

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        prefer_deep_reasoning: bool = False,
    ) -> str:
        if prefer_deep_reasoning:
            res = self._call_anthropic(prompt, system_instruction) or self._call_gemini(prompt, system_instruction)
        else:
            res = self._call_gemini(prompt, system_instruction) or self._call_anthropic(prompt, system_instruction)

        if res:
            return res

        return "[Router Info] Kedua provider LLM belum mengembalikan respons; cek API key & nama model di environment."

    def route(self, prompt: str, system_instruction: str = "", prefer_deep_reasoning: bool = False) -> str:
        return self.generate(prompt, system_instruction, prefer_deep_reasoning=prefer_deep_reasoning)

    def complete(self, prompt: str, system_instruction: str = "", prefer_deep_reasoning: bool = False) -> str:
        return self.generate(prompt, system_instruction, prefer_deep_reasoning=prefer_deep_reasoning)

    def refine_setup(self, prompt: str, system_instruction: str = "", confluence_score: int = 0) -> str:
        sys_msg = (
            system_instruction
            or "Anda adalah analis kuantitatif institusional (SMC, ICT, Order Flow, OHLC, Makro). "
               "Evaluasi hanya setup high-probability; jika konfluensi belum matang atau di luar Kill Zone, "
               "nyatakan WAIT/NO SNIPER dan jelaskan syarat validasi entry, SL, TP1->BEP+1, TP2, dan TP3."
        )
        return self.generate(
            prompt=prompt,
            system_instruction=sys_msg,
            prefer_deep_reasoning=(confluence_score >= 75),
        )
