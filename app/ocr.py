from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(slots=True)
class OCRPageResult:
    text: str
    confidence_avg: float | None = None
    line_count: int = 0


class PaddleOCRBackend:
    def __init__(self, lang: str = "de") -> None:
        self.lang = _normalize_paddle_lang(lang)
        self._engine = None

    def extract_image(self, image_path: str | Path) -> OCRPageResult:
        result = self._engine_instance().predict(str(image_path))
        if not result:
            return OCRPageResult(text="", confidence_avg=None, line_count=0)
        page_result = result[0]
        rec_texts = getattr(page_result, "rec_texts", None) or []
        rec_scores = getattr(page_result, "rec_scores", None) or []
        texts: list[str] = []
        scores: list[float] = []
        for index, raw_text in enumerate(rec_texts):
            text = str(raw_text or "").strip()
            if not text:
                continue
            texts.append(text)
            if index < len(rec_scores):
                try:
                    scores.append(float(rec_scores[index]))
                except (TypeError, ValueError):
                    pass
        confidence_avg = sum(scores) / len(scores) if scores else None
        return OCRPageResult(
            text="\n".join(texts),
            confidence_avg=confidence_avg,
            line_count=len(texts),
        )

    def _engine_instance(self):
        if self._engine is None:
            from paddleocr import PaddleOCR  # type: ignore

            self._engine = PaddleOCR(use_angle_cls=True, lang=self.lang)
        return self._engine


def ocr_backend_available(engine: str) -> bool:
    if engine.strip().lower() != "paddleocr":
        return False
    try:
        import paddleocr  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=4)
def get_ocr_backend(engine: str, lang: str) -> PaddleOCRBackend:
    normalized = engine.strip().lower()
    if normalized != "paddleocr":
        raise RuntimeError(f"Unsupported OCR engine: {engine}")
    return PaddleOCRBackend(lang=lang or "de")


def _normalize_paddle_lang(lang: str) -> str:
    normalized = (lang or "").strip().lower()
    aliases = {
        "de": "german",
        "deu": "german",
        "german": "german",
        "en": "en",
        "eng": "en",
        "english": "en",
    }
    return aliases.get(normalized, normalized or "german")
