"""Data models for Oxygen Updater API and download state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import Any

_REGION_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")
_BASE_MODEL_RE = re.compile(r"^([A-Z]{2,5}\d{3,5})")


class DownloadStatus(str, Enum):
    QUEUED = "Queued"
    FETCHING_URL = "Fetching URL"
    DOWNLOADING = "Downloading"
    PAUSED = "Paused"
    VERIFYING = "Verifying MD5"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


@dataclass(slots=True)
class Device:
    id: int
    name: str
    product_names: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def brand(self) -> str:
        text = f"{self.name} {' '.join(self.product_names)}".lower()
        if "oppo" in text:
            return "OPPO"
        if "oneplus" in text:
            return "OnePlus"
        if "realme" in text:
            return "realme"
        return "Other"

    @property
    def products_str(self) -> str:
        return ", ".join(self.product_names)

    @property
    def base_models(self) -> list[str]:
        seen: list[str] = []
        for p in self.product_names:
            m = _BASE_MODEL_RE.match(p.strip().upper())
            if m:
                code = m.group(1)
                if code not in seen:
                    seen.append(code)
        return seen if seen else self.product_names

    @property
    def base_name(self) -> str:
        m = _REGION_RE.match(self.name.strip())
        return m.group(1).strip() if m else self.name.strip()

    @property
    def region(self) -> str | None:
        m = _REGION_RE.match(self.name.strip())
        return m.group(2).strip() if m else None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        raw_products = data.get("product_names") or []
        if isinstance(raw_products, str):
            products = [p.strip() for p in raw_products.split(",") if p.strip()]
        else:
            products = [str(p).strip() for p in raw_products if str(p).strip()]
        enabled_raw = data.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.lower() not in ("0", "false", "no")
        else:
            enabled = bool(enabled_raw)
        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"Device {data['id']}"),
            product_names=products,
            enabled=enabled,
        )


@dataclass(slots=True)
class UpdateMethod:
    id: int
    name: str
    recommended_for_rooted: bool = False
    recommended_for_non_rooted: bool = False
    supports_rooted: bool = False

    @property
    def is_full(self) -> bool:
        return "full" in self.name.lower()

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> UpdateMethod:
        def _to_bool(val: Any) -> bool:
            if isinstance(val, str):
                return val.lower() in ("1", "true", "yes")
            return bool(val)

        return cls(
            id=int(data["id"]),
            name=str(data.get("name") or f"Method {data['id']}"),
            recommended_for_rooted=_to_bool(data.get("recommended_for_rooted_device")),
            recommended_for_non_rooted=_to_bool(
                data.get("recommended_for_non_rooted_device")
            ),
            supports_rooted=_to_bool(data.get("supports_rooted_device")),
        )


@dataclass(slots=True)
class UpdateData:
    id: int | None
    version_number: str | None
    ota_version_number: str | None
    changelog: str | None
    description: str | None
    download_url: str | None
    download_size: int
    filename: str | None
    md5sum: str | None
    information: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> UpdateData:
        raw_id = data.get("id")
        return cls(
            id=int(raw_id) if raw_id is not None else None,
            version_number=data.get("version_number"),
            ota_version_number=data.get("ota_version_number"),
            changelog=data.get("changelog"),
            description=data.get("description"),
            download_url=data.get("download_url"),
            download_size=int(data.get("download_size") or 0),
            filename=data.get("filename"),
            md5sum=data.get("md5sum"),
            information=data.get("information"),
        )


@dataclass(slots=True)
class SegmentState:
    index: int
    start: int
    end: int  # inclusive byte index
    downloaded: int = 0

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    @property
    def is_complete(self) -> bool:
        return self.downloaded >= self.size

    @property
    def current_offset(self) -> int:
        return self.start + self.downloaded

    def to_dict(self) -> dict[str, int]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "downloaded": self.downloaded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SegmentState:
        return cls(
            index=int(data["index"]),
            start=int(data["start"]),
            end=int(data["end"]),
            downloaded=int(data.get("downloaded", 0)),
        )


@dataclass
class DownloadJob:
    job_id: str
    device_id: int
    device_name: str
    method_id: int
    method_name: str
    update_data: UpdateData
    output_path: Path
    model_number: str = ""
    connections: int = 8
    verify_md5: bool = True

    # Live state
    status: DownloadStatus = DownloadStatus.QUEUED
    url: str | None = None
    total_bytes: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    active_connections: int = 0
    supports_ranges: bool = False
    error: str | None = None
    segments: list[SegmentState] = field(default_factory=list)

    @property
    def part_path(self) -> Path:
        return self.output_path.with_name(self.output_path.name + ".part")

    @property
    def state_path(self) -> Path:
        return self.output_path.with_name(self.output_path.name + ".ota.state.json")

    @property
    def progress_pct(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, (self.downloaded_bytes * 100.0) / self.total_bytes)


def format_bytes(num_bytes: float | int | None) -> str:
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def format_speed(bps: float | int | None) -> str:
    if not bps or bps <= 0:
        return "0 B/s"
    speed = float(bps)
    if speed < 1024.0:
        return f"{speed:.0f} B/s"
    if speed < 1024.0 * 1024.0:
        return f"{speed / 1024.0:.1f} KB/s"
    if speed < 1024.0 * 1024.0 * 1024.0:
        return f"{speed / (1024.0 * 1024.0):.2f} MB/s"
    return f"{speed / (1024.0 * 1024.0 * 1024.0):.2f} GB/s"


def build_ota_filename(
    model_number: str,
    update_data: UpdateData,
    fallback_prefix: str = "ota",
) -> str:
    """Build a clean '[fwversion].zip' filename."""
    fw = (update_data.version_number or update_data.ota_version_number or "").strip()

    def _clean(s: str) -> str:
        s = re.sub(r"[\\/\s:]+", "_", s)
        s = re.sub(r"[^A-Za-z0-9._()-]", "", s)
        return s.strip("._-")

    clean_fw = _clean(fw)
    if clean_fw:
        return f"{clean_fw}.zip"
    raw_model = model_number.split(",")[0].split("(")[0].strip()
    clean_model = _clean(raw_model)
    if clean_model:
        return f"{clean_model}.zip"
    if update_data.filename:
        return Path(update_data.filename).name
    return f"{fallback_prefix}.zip"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 86400 * 7:
        return "--:--"
    secs = int(seconds)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


_SPEED_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtb]?)(?:b(?:/s)?)?\s*$", re.IGNORECASE)


def parse_speed_limit(value: str | float | int | None) -> int:
    """Parse a speed limit into bytes/sec. Plain numbers are treated as MB/s. 0 = unlimited."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(float(value) * 1024 * 1024))
    text = value.strip().lower()
    if not text or text in ("0", "none", "unlimited", "inf"):
        return 0
    m = _SPEED_RE.match(text)
    if not m:
        raise ValueError(f"Invalid speed limit: {value!r} (use e.g. 0, 10, 5M, 500K)")
    amount = float(m.group(1))
    unit = m.group(2).lower()
    multipliers = {
        "": 1024 * 1024,  # default unit: MB/s
        "b": 1,
        "k": 1024,
        "m": 1024 * 1024,
        "g": 1024 * 1024 * 1024,
        "t": 1024 * 1024 * 1024 * 1024,
    }
    return max(0, int(amount * multipliers[unit]))


def format_speed_limit(bps: int) -> str:
    if bps <= 0:
        return "Unlimited"
    return f"{format_bytes(bps)}/s"
