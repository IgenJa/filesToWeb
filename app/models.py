"""Settings shared by the API and the encoders."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ImageFormat = Literal["webp", "avif", "jpeg"]
VideoFormat = Literal["mp4", "webm"]
VideoQuality = Literal["high", "balanced", "small"]
EncoderPreset = Literal[
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]
MaxResolution = Literal["original", "2160", "1440", "1080", "720"]


class Settings(BaseModel):
    """Optimization settings for one batch."""

    model_config = ConfigDict(extra="ignore")

    image_format: ImageFormat = "webp"
    image_quality: int = Field(default=82, ge=1, le=100)
    max_width: int = Field(default=2560, ge=1, le=20000)
    max_height: int = Field(default=2560, ge=1, le=20000)
    keep_image_dimensions: bool = False
    preserve_metadata: bool = False

    video_format: VideoFormat = "mp4"
    video_quality: VideoQuality = "balanced"
    encoder_preset: EncoderPreset = "medium"
    max_resolution: MaxResolution = "1080"
    remove_audio: bool = False
    skip_suitable: bool = True

    def image_extension(self) -> str:
        return {"webp": ".webp", "avif": ".avif", "jpeg": ".jpg"}[self.image_format]

    def video_extension(self) -> str:
        return ".mp4" if self.video_format == "mp4" else ".webm"


class JobRequest(BaseModel):
    """Body of POST /api/jobs."""

    model_config = ConfigDict(extra="ignore")

    file_ids: list[str] = Field(min_length=1)
    output_dir: str | None = None
    settings: Settings = Field(default_factory=Settings)


class PathsRequest(BaseModel):
    """Body of POST /api/paths. Paths may be files or folders."""

    paths: list[str] = Field(min_length=1)


class DialogRequest(BaseModel):
    mode: Literal["files", "folder", "output"]


class OpenRequest(BaseModel):
    path: str | None = None
    job_id: str | None = None
