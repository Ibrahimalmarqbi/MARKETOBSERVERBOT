from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


class GroundedLLM:
    def __init__(self, api_key: str = "", api_base: str = "https://api.openai.com/v1", model: str = "gpt-5-mini"):
        self.api_key = api_key
        self.api_base = api_base
        self.model = model
        self.client = None
        if api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=api_key, base_url=api_base)
            except Exception as exc:
                logger.warning("LLM client unavailable: %s", exc)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def translate_headline(self, headline: str, target_language: str) -> str:
        if not self.client:
            raise LLMUnavailable("LLM is not configured")
        target = "Arabic" if target_language == "ar" else "English"
        system = (
            f"You are a faithful financial-news translator. Translate the supplied headline into {target}. "
            "Return only the translation, with no commentary. Do not add facts, predictions, sentiment, or advice. "
            "Preserve tickers, company names, currencies, percentages, dates, and numbers exactly. Keep it under 220 characters."
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": headline},
            ],
            max_completion_tokens=220,
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMUnavailable("LLM returned empty translation")
        return content.strip().strip('"')

    def explain(self, language: str, user_question: str, facts: dict) -> str:
        if not self.client:
            raise LLMUnavailable("LLM is not configured")
        system = (
            "You are a careful market-analysis explainer. Use only the supplied facts. "
            "Never invent a price, date, indicator, catalyst, confidence, or trade. "
            "Do not change numeric values. Explain uncertainty and say when a view is conditional. "
            "This is research and analysis only, not personalized financial advice. "
            f"Reply in {'Arabic' if language == 'ar' else 'English'}."
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": "Question:\n" + user_question + "\nVerified facts (JSON):\n" + json.dumps(facts, ensure_ascii=False)},
            ],
            max_completion_tokens=700,
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMUnavailable("LLM returned empty content")
        return content.strip()