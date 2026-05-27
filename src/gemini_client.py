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
from logger import get_logger

log = get_logger()

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "video_analysis_prompt.md")


class VideoDownloadError(RuntimeError):
    pass


class GeminiClient:
    def __init__(self):
        config.require_gemini()
        self.client = genai.Client(api_key=config.GEMINI_API_KEY)
        self.model = config.GEMINI_MODEL
        with open(_PROMPT_PATH, encoding="utf-8") as f:
            self.prompt_template = f.read()

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

    # ---- analysis ------------------------------------------------------
    def _build_prompt(self, taxonomy_block: str, product: str, icp: str, notes: str) -> str:
        return (
            self.prompt_template
            .replace("{product}", product or "unknown")
            .replace("{icp}", icp or "unknown")
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
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[uploaded, prompt],
            )
            return resp.text or ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def summarize_findings(self, prompt_text: str) -> str:
        resp = self.client.models.generate_content(
            model=self.model, contents=[prompt_text]
        )
        return resp.text or ""
