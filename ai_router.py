import os
import logging
from typing import Optional, Dict, Any
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("FlagshipAIRouter")


class FlagshipAIRouter:
    """
    Router AI utama untuk quant-AgentLang-247.
    Mengutamakan Gemini API (dengan fallback ke Anthropic API jika tersedia),
    serta menyediakan fallback aman agar proses tidak langsung crash jika API key belum diset.
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        gemini_model: str = "gemini-2.5-flash",
        anthropic_model: str = "claude-sonnet-4-20250514",
        timeout: int = 30,
    ):
        self.gemini_api_key = gemini_api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.anthropic_api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.gemini_model = os.getenv("GEMINI_MODEL", gemini_model).strip()
        self.anthropic_model = os.getenv("ANTHROPIC_MODEL", anthropic_model).strip()
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
            logger.warning(f"Gemini API gagal dipanggil: {e}")
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
            "max_tokens": 1500,
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
            logger.warning(f"Anthropic API gagal dipanggil: {e}")
        return None

    def generate(self, prompt: str, system_instruction: str = "") -> str:
        """Menghasilkan respons teks dari LLM yang tersedia."""
        result = self._call_gemini(prompt, system_instruction)
        if result:
            return result

        result = self._call_anthropic(prompt, system_instruction)
        if result:
            return result

        logger.warning("Tidak ada respons LLM aktif (cek GEMINI_API_KEY / ANTHROPIC_API_KEY).")
        return "[Router Info] LLM tidak merespons atau API key belum dikonfigurasi; laporan berjalan dalam mode data/fallback."

    def route(self, prompt: str, system_instruction: str = "") -> str:
        """Alias untuk kompatibilitas pemanggilan."""
        return self.generate(prompt, system_instruction)

    def complete(self, prompt: str, system_instruction: str = "") -> str:
        """Alias untuk kompatibilitas pemanggilan."""
        return self.generate(prompt, system_instruction)
