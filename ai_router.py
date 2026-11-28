import os
import re
import logging
from typing import Optional, Dict, Any, List, Tuple
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("FlagshipAIRouter")


def clean_plain_text(text: str) -> str:
    """Membersihkan simbol markdown/HTML (*, #, <, >) agar output Telegram rapi."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", "", str(text))
    cleaned = cleaned.replace("<>", "").replace("<", "(").replace(">", ")")
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*\*\s+", "- ", cleaned)
    cleaned = cleaned.replace("*", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class FlagshipAIRouter:
    """
    Router AI Multi-Provider & Multi-Tier untuk quant-AgentLang-247 (Additive v2.9):
    1. Gemini 4 Hydrogen via Interactions API (/v1beta/interactions) + fallback generateContent
    2. Anthropic Claude (env ANTHROPIC_MODEL -> claude-sonnet-5-5 -> claude-sonnet-4-6)
    3. Groq Backup (llama-3.3-70b-versatile via GROQ_API_KEY)
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        groq_api_key: Optional[str] = None,
        timeout: int = 45,
    ):
        self.gemini_api_key = (gemini_api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.anthropic_api_key = (anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
        self.groq_api_key = (groq_api_key or os.getenv("GROQ_API_KEY", "")).strip()

        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip() or "gemini-4-hydrogen"
        self.anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5"
        self.gemini_effort = os.getenv("GEMINI_REASONING_EFFORT", "high").strip().lower()
        self.anthropic_effort = os.getenv("ANTHROPIC_REASONING_EFFORT", "max").strip().lower()

        self.timeout = timeout
        self.setup_engine = "Quant Deterministic Engine"
        self.analysis_engine = "Quant Deterministic Engine"
        self.last_status = "IDLE"

    def _post_gemini_interactions(
        self, model_name: str, prompt: str, system_instruction: str = "", use_thinking: bool = True
    ) -> Optional[str]:
        """Jalur resmi Interactions API (/v1beta/interactions) untuk seri gemini-4-hydrogen."""
        if not self.gemini_api_key or not model_name:
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={self.gemini_api_key}"
        payload: Dict[str, Any] = {
            "model": model_name,
            "input": prompt,
            "store": False,
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction

        effort = self.gemini_effort if self.gemini_effort in ["low", "medium", "high", "xhigh"] else "high"
        if use_thinking:
            payload["generation_config"] = {"thinking_level": effort, "temperature": 0.25}
        else:
            payload["generation_config"] = {"temperature": 0.25}

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            if resp.status_code >= 400:
                logger.warning(f"Gemini Interactions [{model_name}] HTTP {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            if data.get("output_text"):
                self.last_status = f"OK:{model_name}:interactions"
                return clean_plain_text(data["output_text"])

            texts: List[str] = []
            for block in (data.get("outputs") or data.get("steps") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "thought":
                    continue
                txt = block.get("text") or block.get("content")
                if isinstance(txt, str) and txt.strip():
                    texts.append(txt.strip())

            if texts:
                self.last_status = f"OK:{model_name}:interactions"
                return clean_plain_text("\n".join(texts))
        except Exception as e:
            logger.warning(f"Gemini Interactions [{model_name}] error: {e}")
        return None

    def _post_gemini(
        self, model_name: str, prompt: str, system_instruction: str = "", use_thinking: bool = True
    ) -> Optional[str]:
        if not self.gemini_api_key or not model_name:
            return None

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_name}:generateContent?key={self.gemini_api_key}"
        )
        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.25},
        }
        if use_thinking and self.gemini_effort in ["minimal", "low", "medium", "high"]:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self.gemini_effort}

        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            if resp.status_code >= 400:
                logger.warning(f"Gemini [{model_name}] HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            candidates = data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                texts = [p.get("text", "") for p in parts if p.get("text") and not p.get("thought")]
                if not texts:
                    texts = [p.get("text", "") for p in parts if p.get("text")]
                if texts:
                    self.last_status = f"OK:{model_name}"
                    return clean_plain_text("\n".join(texts))
        except Exception as e:
            logger.warning(f"Gemini [{model_name}] error: {e}")
        return None

    def _call_gemini_cascade(self, prompt: str, system_instruction: str = "") -> Tuple[Optional[str], str]:
        if not self.gemini_api_key:
            return None, ""

        # 1. Prioritaskan Interactions API untuk gemini-4-hydrogen & model utama dari env
        primary_models = []
        for m in [self.gemini_model, "gemini-4-hydrogen"]:
            if m and m not in primary_models:
                primary_models.append(m)

        for model_name in primary_models:
            for think_on in (True, False):
                res = self._post_gemini_interactions(model_name, prompt, system_instruction, use_thinking=think_on)
                if res:
                    label = f"{model_name} ({self.gemini_effort})" if think_on else model_name
                    return res, label

        # 2. Fallback ke generateContent untuk model utama & seri Flash
        tried: List[Tuple[str, bool]] = []
        for m in primary_models + ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]:
            if (m, True) not in tried:
                tried.append((m, True))
                tried.append((m, False))

        for model_name, think_on in tried:
            res = self._post_gemini(model_name, prompt, system_instruction, use_thinking=think_on)
            if res:
                label = f"{model_name} ({self.gemini_effort})" if think_on else model_name
                return res, label
        return None, ""

    def _post_anthropic(
        self, model_name: str, prompt: str, system_instruction: str = "", use_effort: bool = False
    ) -> Optional[str]:
        if not self.anthropic_api_key or not model_name:
            return None

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_effort and self.anthropic_effort in ["low", "medium", "high", "max"]:
            payload["output_config"] = {"effort": self.anthropic_effort}
        if system_instruction:
            payload["system"] = system_instruction

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code >= 400:
                logger.warning(f"Anthropic [{model_name}] HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            content = data.get("content") or []
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if texts:
                self.last_status = f"OK:{model_name}"
                return clean_plain_text("\n".join(texts))
        except Exception as e:
            logger.warning(f"Anthropic [{model_name}] error: {e}")
        return None

    def _call_anthropic_cascade(self, prompt: str, system_instruction: str = "") -> Tuple[Optional[str], str]:
        if not self.anthropic_api_key:
            return None, ""

        tried: List[Tuple[str, bool]] = []
        if self.anthropic_model:
            tried.append((self.anthropic_model, False))
            tried.append((self.anthropic_model, True))
        for m in ["claude-sonnet-5-5", "claude-sonnet-4-6"]:
            if m != self.anthropic_model:
                tried.append((m, False))

        for model_name, eff_on in tried:
            res = self._post_anthropic(model_name, prompt, system_instruction, use_effort=eff_on)
            if res:
                label = f"{model_name} ({self.anthropic_effort})" if eff_on else model_name
                return res, label
        return None, ""

    def _call_groq_backup(self, prompt: str, system_instruction: str = "") -> Tuple[Optional[str], str]:
        if not self.groq_api_key:
            return None, ""
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "temperature": 0.25,
            "max_tokens": 1800,
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code >= 400:
                return None, ""
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                text = choices[0].get("message", {}).get("content", "").strip()
                if text:
                    self.last_status = "OK:groq"
                    return clean_plain_text(text), "llama-3.3-70b-versatile (Groq)"
        except Exception:
            pass
        return None, ""

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        prefer_deep_reasoning: bool = False,
        role: str = "analysis",
    ) -> str:
        # Gemini 4 Hydrogen diprioritaskan pertama, diikuti Claude dan Groq
        order = [self._call_gemini_cascade, self._call_anthropic_cascade, self._call_groq_backup]

        for fn in order:
            res, engine_label = fn(prompt, system_instruction)
            if res:
                if role == "setup":
                    self.setup_engine = engine_label
                else:
                    self.analysis_engine = engine_label
                return clean_plain_text(res)

        return (
            "Setup divalidasi oleh konfluensi MTF, zona OTE Fibonacci, dan struktur SMC/Orderflow."
        )

    def route(self, prompt: str, system_instruction: str = "", prefer_deep_reasoning: bool = False) -> str:
        return self.generate(prompt, system_instruction, prefer_deep_reasoning=prefer_deep_reasoning)

    def complete(self, prompt: str, system_instruction: str = "", prefer_deep_reasoning: bool = False) -> str:
        return self.generate(prompt, system_instruction, prefer_deep_reasoning=prefer_deep_reasoning)

    def analyze(self, prompt: str, system_instruction: str = "", prefer_deep_reasoning: bool = False) -> str:
        return self.generate(prompt, system_instruction, prefer_deep_reasoning=prefer_deep_reasoning)

    def generate_institutional_analysis(self, prompt: str, system_instruction: str = "") -> Tuple[str, str]:
        text = self.generate(prompt, system_instruction, prefer_deep_reasoning=True, role="analysis")
        return text, self.analysis_engine

    def refine_setup(self, prompt: str, system_instruction: str = "", confluence_score: int = 0) -> str:
        sys_msg = (
            system_instruction
            or "Anda adalah Institutional Quant Strategist (SMC, ICT, Order Flow, OHLC, Makro). "
               "Tulis alasan presisi singkat (maksimal 180 karakter) mengenai validitas POI, "
               "konfluensi intermarket, dan syarat invalidasi. Jangan gunakan simbol *, #, <, >."
        )
        return self.generate(
            prompt=prompt,
            system_instruction=sys_msg,
            prefer_deep_reasoning=(confluence_score >= 75),
            role="setup",
        )

    def chat_live(self, user_question: str, market_summary: str) -> Tuple[str, str]:
        sys_msg = (
            "Anda adalah Kepala Quant Strategist & Eksekutor Institusional (SMC/ICT, Derivatif, Statistik). "
            "Jawab padat, objektif, dan jangan gunakan karakter markdown/HTML seperti *, #, <, >."
        )
        prompt = f"KONTEKS PASAR TERKINI:\n{market_summary}\n\nPERTANYAAN:\n{user_question}"
        ans = self.generate(prompt, system_instruction=sys_msg, prefer_deep_reasoning=False, role="analysis")
        return ans, self.analysis_engine
