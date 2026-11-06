"""Site configuration and routing, read from TOML files in /etc/hyperwheel.

Two files, both site-specific and both kept outside the release:

- ``site.toml``: paths, upload settings, the raw-data rule, disk limits and
  the site's phantom Patient ID.
- ``routing.toml``: scanner login -> GRIP project, with that project's API
  address and token URL. Keys are not in here; they live in the key store
  (see ``keys.py``).

Unknown settings are an error, so a typo can't silently fall back to a default.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .ids import is_login, is_project_name

SCHEMA_VERSION = 1
MAX_CHUNK_BYTES = 64 * 1024 * 1024  # GRIP's limit per raw-file chunk
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UploadSettings:
    chunk_bytes: int = 8 * 1024 * 1024
    timeout_s: float = 60.0
    # Each request gets at least the time to send its body at this rate (16 kB/s,
    # about 128 kbit/s), so a large DICOM file or chunk isn't cut off on a slow link.
    slowest_link_bytes_per_s: float = 16_000.0
    # Raw chunks shrink (halving) to this after a request gets no answer or takes longer
    # than target_chunk_s, and grow back after a run of quick ones. Short chunks mean
    # GRIP answers often, which also tells the Upload Monitor the link is working.
    min_chunk_bytes: int = 256 * 1024
    target_chunk_s: float = 30.0
    # Tries within one call (GRIP's retry rule) before the file is parked
    # in retry_wait and tried again on a later run.
    attempts_per_call: int = 4
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    max_retry_after_s: float = 300.0
    # Back-off between runs for a parked file.
    retry_base_s: float = 60.0
    retry_max_s: float = 3600.0


@dataclass(frozen=True)
class RrdfSettings:
    # Off for a site that saves no raw data: nothing is pulled and nothing is flagged.
    enabled: bool = True
    # A raw file belongs to a study if its scanner folder matches the study's
    # StudyDate+StudyTime and its own timestamp falls between the study start
    # and the last acquisition plus this margin (never past the next study).
    margin_minutes: int = 60
    # How long a study's scanner folder is checked again for late raw files.
    watch_days: int = 7
    # How long after a study is stable its scanner folder may still be missing before that is flagged.
    folder_grace_minutes: int = 10


@dataclass(frozen=True)
class ScannerSettings:
    # Set at install from the scanner network the Pi finds (10.42.0.1 or 10.0.0.41).
    host: str = ""
    port: int = 25125
    username: str = "rrdf"
    rrdf_dir: str = "RRDF"
    # The scanner's SSH host key, recorded at install. Unknown keys are refused.
    known_hosts: str = "/etc/hyperwheel/scanner_known_hosts"
    timeout_s: float = 20.0
    # The Pi's network interface on the scanner cable, to tell "no link" from "no answer".
    interface: str = "eth0"


@dataclass(frozen=True)
class OrthancSettings:
    url: str = "http://127.0.0.1:8042"
    timeout_s: float = 30.0


@dataclass(frozen=True)
class DashboardSettings:
    # The Upload Monitor listens on the hotspot address only.
    bind: str = "192.168.99.1"
    port: int = 5000
    # Once everything from the scanner is on the Pi, operators may switch the scanner
    # off; the Pi uploads next time it is on. Ask them to leave it on only when files
    # have waited this long (or the disk is getting full).
    leave_on_after_hours: float = 48.0


@dataclass(frozen=True)
class DiskSettings:
    warn_free_gb: float = 10.0
    stop_free_gb: float = 4.0


@dataclass(frozen=True)
class SiteConfig:
    site: str
    staging_dir: Path
    state_db: Path
    keys_dir: Path
    phantom_patient_id: str | None = None
    # Allows http:// API addresses on this machine only (a local fake GRIP).
    test_mode: bool = False
    upload: UploadSettings = field(default_factory=UploadSettings)
    rrdf: RrdfSettings = field(default_factory=RrdfSettings)
    disk: DiskSettings = field(default_factory=DiskSettings)
    scanner: ScannerSettings = field(default_factory=ScannerSettings)
    orthanc: OrthancSettings = field(default_factory=OrthancSettings)
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)


@dataclass(frozen=True)
class Project:
    name: str
    logins: tuple[str, ...]
    api_base_url: str
    token_url: str
    # Optional: what this study's Patient IDs look like, e.g. "R2-\\d{4}".
    # A Patient ID that doesn't match is flagged, never changed.
    patient_id_pattern: str | None = None


@dataclass(frozen=True)
class Routing:
    projects: dict[str, Project]

    def project_for_login(self, login: str | None) -> Project | None:
        if not login:
            return None
        for project in self.projects.values():
            if login in project.logins:
                return project
        return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: not valid TOML ({e})") from None
    version = data.pop("schema_version", None)
    if version != SCHEMA_VERSION:
        raise ConfigError(f"{path}: schema_version must be {SCHEMA_VERSION}, found {version!r}")
    return data


def _section(cls, data: dict[str, Any], where: str):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    values = {}
    for name, value in data.items():
        default = known[name].default
        expected = type(default)
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(f"{where}.{name}: expected {expected.__name__}, found {value!r}")
        values[name] = value
    return cls(**values)


def load_site(path: Path) -> SiteConfig:
    data = _read_toml(path)
    where = str(path)
    allowed = {f.name for f in fields(SiteConfig)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    for required in ("site", "staging_dir", "state_db", "keys_dir"):
        if not isinstance(data.get(required), str) or not data[required]:
            raise ConfigError(f"{where}: '{required}' is required")

    upload = _section(UploadSettings, data.get("upload", {}), f"{where} [upload]")
    if not 0 < upload.chunk_bytes <= MAX_CHUNK_BYTES:
        raise ConfigError(f"{where} [upload].chunk_bytes must be between 1 and {MAX_CHUNK_BYTES}")
    if not 0 < upload.min_chunk_bytes <= upload.chunk_bytes:
        raise ConfigError(f"{where} [upload].min_chunk_bytes must be between 1 and chunk_bytes")
    if upload.slowest_link_bytes_per_s <= 0:
        raise ConfigError(f"{where} [upload].slowest_link_bytes_per_s must be more than 0")
    if upload.attempts_per_call < 1:
        raise ConfigError(f"{where} [upload].attempts_per_call must be at least 1")
    rrdf = _section(RrdfSettings, data.get("rrdf", {}), f"{where} [rrdf]")
    disk = _section(DiskSettings, data.get("disk", {}), f"{where} [disk]")
    scanner = _section(ScannerSettings, data.get("scanner", {}), f"{where} [scanner]")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", scanner.interface) or scanner.interface.startswith("."):
        raise ConfigError(f"{where} [scanner].interface is not a network interface name")
    orthanc = _section(OrthancSettings, data.get("orthanc", {}), f"{where} [orthanc]")
    if urlsplit(orthanc.url).hostname not in _LOCAL_HOSTS:
        raise ConfigError(f"{where} [orthanc].url must point at this machine")
    dashboard = _section(DashboardSettings, data.get("dashboard", {}), f"{where} [dashboard]")

    phantom = data.get("phantom_patient_id")
    if phantom is not None and (not isinstance(phantom, str) or not phantom.strip()):
        raise ConfigError(f"{where}: phantom_patient_id must be a non-empty string, or left out")
    test_mode = data.get("test_mode", False)
    if not isinstance(test_mode, bool):
        raise ConfigError(f"{where}: test_mode must be true or false")

    return SiteConfig(
        site=data["site"],
        staging_dir=Path(data["staging_dir"]),
        state_db=Path(data["state_db"]),
        keys_dir=Path(data["keys_dir"]),
        phantom_patient_id=phantom,
        test_mode=test_mode,
        upload=upload,
        rrdf=rrdf,
        disk=disk,
        scanner=scanner,
        orthanc=orthanc,
        dashboard=dashboard,
    )


def check_url(url: object, where: str, *, test_mode: bool) -> str:
    if not isinstance(url, str):
        raise ConfigError(f"{where}: expected a URL")
    parts = urlsplit(url)
    if not parts.hostname:
        raise ConfigError(f"{where}: not a URL: {url!r}")
    if parts.scheme == "https":
        return url.rstrip("/")
    if parts.scheme == "http" and test_mode and parts.hostname in _LOCAL_HOSTS:
        return url.rstrip("/")
    raise ConfigError(f"{where}: must be https:// (plain http:// only to this machine, with test_mode on)")


def load_routing(path: Path, *, test_mode: bool = False) -> Routing:
    data = _read_toml(path)
    where = str(path)
    unknown = set(data) - {"projects"}
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    projects: dict[str, Project] = {}
    owner: dict[str, str] = {}
    for name, entry in data.get("projects", {}).items():
        at = f"{where} [projects.{name}]"
        if not is_project_name(name):
            raise ConfigError(f"{at}: project names use letters, digits, '-' and '_' only")
        if name.lower() in {p.lower() for p in projects}:
            raise ConfigError(f"{at}: project listed twice")
        if not isinstance(entry, dict):
            raise ConfigError(f"{at}: expected a table")
        unknown = set(entry) - {"logins", "api_base_url", "token_url", "patient_id_pattern"}
        if unknown:
            raise ConfigError(f"{at}: unknown setting(s) {', '.join(sorted(unknown))}")
        logins = entry.get("logins")
        if not isinstance(logins, list) or not logins:
            raise ConfigError(f"{at}: 'logins' must list at least one scanner login")
        for login in logins:
            if not is_login(login):
                raise ConfigError(f"{at}: login {login!r} must be lower case letters, digits, '.', '-' or '_'")
            if login in owner:
                raise ConfigError(f"{at}: login {login!r} is already routed to {owner[login]}")
            owner[login] = name
        pattern = entry.get("patient_id_pattern")
        if pattern is not None:
            try:
                re.compile(pattern)
            except (re.error, TypeError):
                raise ConfigError(f"{at}.patient_id_pattern: not a valid regular expression") from None
        projects[name] = Project(
            name=name,
            logins=tuple(logins),
            api_base_url=check_url(entry.get("api_base_url"), f"{at}.api_base_url", test_mode=test_mode),
            token_url=check_url(entry.get("token_url"), f"{at}.token_url", test_mode=test_mode),
            patient_id_pattern=pattern,
        )
    return Routing(projects=projects)
