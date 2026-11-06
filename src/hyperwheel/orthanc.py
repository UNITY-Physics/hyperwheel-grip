"""The parts of Orthanc's REST API Hyperwheel uses."""

from __future__ import annotations

from typing import Any, Protocol

import requests

from .ids import quote_path


class OrthancError(Exception):
    pass


class OrthancNotFound(OrthancError):
    """Orthanc no longer has the resource (e.g. it was deleted)."""


class Orthanc(Protocol):
    def changes(self, since: int, limit: int = 100) -> dict[str, Any]: ...
    def instance_tags(self, instance_id: str) -> dict[str, Any]: ...
    def instance_file(self, instance_id: str) -> bytes: ...
    def study_instances(self, study_id: str) -> list[str]: ...
    def delete_instance(self, instance_id: str) -> None: ...
    def delete_study(self, study_id: str) -> None: ...


class OrthancClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8042", *, auth: tuple[str, str] | None = None,
                 timeout_s: float = 30.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = auth
        self.timeout_s = timeout_s

    def _get(self, path: str, **params: Any) -> requests.Response:
        try:
            r = self.session.get(self.base_url + path, params=params or None, timeout=self.timeout_s)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise OrthancError(f"Orthanc did not answer ({type(e).__name__})") from None
        if r.status_code == 404:
            raise OrthancNotFound(f"Orthanc GET {path}: not found")
        if r.status_code != 200:
            raise OrthancError(f"Orthanc GET {path}: HTTP {r.status_code}")
        return r

    def _delete(self, path: str) -> None:
        try:
            r = self.session.delete(self.base_url + path, timeout=self.timeout_s)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise OrthancError(f"Orthanc did not answer ({type(e).__name__})") from None
        if r.status_code not in (200, 404):  # already gone counts as deleted
            raise OrthancError(f"Orthanc DELETE {path}: HTTP {r.status_code}")

    def changes(self, since: int, limit: int = 100) -> dict[str, Any]:
        return self._get("/changes", since=since, limit=limit).json()

    def instance_tags(self, instance_id: str) -> dict[str, Any]:
        return self._get(f"/instances/{quote_path(instance_id)}/simplified-tags").json()

    def instance_file(self, instance_id: str) -> bytes:
        return self._get(f"/instances/{quote_path(instance_id)}/file").content

    def study_instances(self, study_id: str) -> list[str]:
        body = self._get(f"/studies/{quote_path(study_id)}/instances").json()
        return [i["ID"] for i in body if isinstance(i, dict) and isinstance(i.get("ID"), str)]

    def delete_instance(self, instance_id: str) -> None:
        self._delete(f"/instances/{quote_path(instance_id)}")

    def delete_study(self, study_id: str) -> None:
        self._delete(f"/studies/{quote_path(study_id)}")
