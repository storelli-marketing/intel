"""Gemini analysis engine.

Gemini cannot fetch an Instagram URL directly, so the flow is:
  1. download the reel to a temp .mp4 with yt-dlp
  2. upload it through the Gemini Files API
  3. ask the model to analyze the video + metadata and return JSON

If a video cannot be downloaded, raise so the caller can mark the row failed.
"""
import os
import tempfile
import time

from google import genai

import config
import taxonomy
from logger import get_logger

log = get_logger()

_PROMPTS = os.path.join(os.path.dirname(__file__), "..", "prompts")
_PROMPT_PATH = os.path.join(_PROMPTS, "video_analysis_prompt.md")
_QA_PROMPT_PATH = os.path.join(_PROMPTS, "qa_compiler_prompt.md")

# Backoff for transient Gemini 503 UNAVAILABLE: up to 3 retries, waiting
# 10s / 30s / 60s before each. After that we give up and the caller marks the
# row failed.
RETRY_DELAYS = (10, 30, 60)


class VideoDownloadError(RuntimeError):
    pass


class QuotaExhaustedError(RuntimeError):
    """Gemini 429 RESOURCE_EXHAUSTED — quota/rate limit hit. Distinct from a
    real per-video failure: the run should stop rather than burn through the
    remaining rows marking them all failed."""


def _is_unavailable(exc: Exception) -> bool:
    """True for a transient 503 / UNAVAILABLE error from the Gemini API."""
    code = getattr(exc, "code", None)
    if code == 503:
        return True
    status = str(getattr(exc, "status", "") or "").upper()
    if status == "UNAVAILABLE":
        return True
    text = str(exc).upper()
    return "503" in text or "UNAVAILABLE" in text


def _is_quota(exc: Exception) -> bool:
    """True for a 429 RESOURCE_EXHAUSTED quota error."""
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    status = str(getattr(exc, "status", "") or "").upper()
    if status == "RESOURCE_EXHAUSTED":
        return True
    text = str(exc).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text


class GeminiClient:
    def __init__(self):
        config.require_gemini()
        self.client = genai.Client(api_key=config.GEMINI_API_KEY)
        self.model = config.GEMINI_MODEL
        with open(_PROMPT_PATH, encoding="utf-8") as f:
            self.prompt_template = f.read()
        with open(_QA_PROMPT_PATH, encoding="utf-8") as f:
            self.qa_template = f.read()

    # ---- video acquisition --------------------------------------------
    def _download(self, ig_link: str) -> str:
        """Download an IG reel to a temp mp4. Returns the file path."""
        try:
            import yt_dlp
        except ImportError as e:
            raise VideoDownloadError("yt-dlp not installed") from e

        tmp_dir = tempfile.mkdtemp(prefix="storelli_")
        out_tmpl = os.path.join(tmp_dir, "video.%(ext)s")
        opts = {
            "outtmpl": out_tmpl,
            "format": "mp4/best",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([ig_link])
        except Exception as e:  # noqa: BLE001 - yt-dlp raises many types
            raise VideoDownloadError(f"download failed for {ig_link}: {e}") from e

        for fn in os.listdir(tmp_dir):
            if fn.startswith("video."):
                return os.path.join(tmp_dir, fn)
        raise VideoDownloadError(f"no file produced for {ig_link}")

    def _upload_and_wait(self, path: str, timeout: int = 120):
        myfile = self.client.files.upload(file=path)
        deadline = time.time() + timeout
        while getattr(myfile.state, "name", str(myfile.state)) == "PROCESSING":
            if time.time() > deadline:
                raise RuntimeError("Gemini file processing timed out")
            time.sleep(2)
            myfile = self.client.files.get(name=myfile.name)
        state = getattr(myfile.state, "name", str(myfile.state))
        if state == "FAILED":
            raise RuntimeError("Gemini file processing failed")
        return myfile

    # ---- model call with 503 retry/backoff -----------------------------
    def _generate(self, contents) -> str:
        """Call generate_content, retrying transient 503 UNAVAILABLE with
        10s/30s/60s backoff. Non-503 errors propagate immediately."""
        attempt = 0
        while True:
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=contents)
                return resp.text or ""
            except Exception as e:  # noqa: BLE001
                if _is_quota(e):
                    # Daily/rate quota — retrying within the run won't help.
                    raise QuotaExhaustedError(str(e)) from e
                if _is_unavailable(e) and attempt < len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempt]
                    attempt += 1
                    log.warning("Gemini 503 UNAVAILABLE; retry %d/%d in %ds",
                                attempt, len(RETRY_DELAYS), delay)
                    time.sleep(delay)
                    continue
                raise

    # ---- analysis ------------------------------------------------------
    def _build_prompt(self, taxonomy_block: str, product: str, icp: str, notes: str) -> str:
        return (
            self.prompt_template
            .replace("{product_context}", taxonomy.PRODUCT_CONTEXT)
            .replace("{product}", product or "(blank)")
            .replace("{icp}", icp or "(blank)")
            .replace("{notes}", notes or "(none)")
            .replace("{taxonomy}", taxonomy_block)
        )

    def analyze(self, ig_link: str, taxonomy_block: str, product: str, icp: str,
                notes: str) -> str:
        """Download -> upload -> generate. Returns raw model text (expected JSON)."""
        path = self._download(ig_link)
        try:
            uploaded = self._upload_and_wait(path)
            prompt = self._build_prompt(taxonomy_block, product, icp, notes)
            return self._generate([uploaded, prompt])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def qa_review(self, initial_json: str, taxonomy_block: str, product: str,
                  icp: str, notes: str) -> str:
        """Text-only QA/compiler pass over the first-pass tags. Returns JSON text."""
        prompt = (
            self.qa_template
            .replace("{product_context}", taxonomy.PRODUCT_CONTEXT)
            .replace("{initial_json}", initial_json)
            .replace("{product}", product or "(blank)")
            .replace("{icp}", icp or "(blank)")
            .replace("{notes}", notes or "(none)")
            .replace("{taxonomy}", taxonomy_block)
        )
        return self._generate([prompt])

    def summarize_findings(self, prompt_text: str) -> str:
        return self._generate([prompt_text])
