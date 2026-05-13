"""Centralized configuration pulled from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_pass: str
    redis_url: str

    video_root: str
    music_root: str

    video_phash_frames: int
    video_phash_threshold: int
    video_duration_tolerance: int
    video_use_gpu: bool
    chromaprint_threshold: float

    thumbs_dir: str

    @property
    def db_dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_pass}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


def load_config() -> Config:
    return Config(
        db_host=_env("DB_HOST", "postgres"),
        db_port=_env_int("DB_PORT", 5432),
        db_name=_env("DB_NAME", "mediadedup"),
        db_user=_env("DB_USER", "mediadedup"),
        db_pass=_env("DB_PASS", "mediadedup"),
        redis_url=_env("REDIS_URL", "redis://redis:6379/0"),
        video_root=_env("VIDEO_ROOT", "/media/video"),
        music_root=_env("MUSIC_ROOT", "/media/music"),
        video_phash_frames=_env_int("VIDEO_PHASH_FRAMES", 16),
        video_phash_threshold=_env_int("VIDEO_PHASH_THRESHOLD", 12),
        video_duration_tolerance=_env_int("VIDEO_DURATION_TOLERANCE", 3),
        video_use_gpu=_env_bool("VIDEO_USE_GPU", True),
        chromaprint_threshold=_env_float("CHROMAPRINT_THRESHOLD", 0.85),
        thumbs_dir=_env("THUMBS_DIR", "/thumbs"),
    )


CFG = load_config()


VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpeg", ".mpg", ".ts", ".m2ts", ".vob", ".3gp",
    ".asf", ".rm", ".rmvb", ".divx", ".f4v", ".ogv",
}

AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".alac", ".ape", ".aiff", ".aif", ".dsf",
    ".dff", ".mka", ".wv",
}
