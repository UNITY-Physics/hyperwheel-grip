"""What Hyperwheel reads from an instance's tags (Orthanc's simplified tags).

Values are kept exactly as the scanner sent them; nothing here changes a tag.
The repr leaves out patient data so it can't end up in a log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .ids import login_from_operators_name


@dataclass(frozen=True)
class InstanceInfo:
    study_uid: str
    sop_uid: str
    series_uid: str
    operators_name: str | None
    patient_id: str | None
    patient_name: str | None
    patient_sex: str | None
    patient_birth_date: str | None
    study_date: str | None
    study_time: str | None
    acquisition: datetime | None
    series_description: str | None
    image_type: str | None

    @property
    def login(self) -> str | None:
        return login_from_operators_name(self.operators_name)

    @property
    def study_start(self) -> datetime | None:
        return parse_datetime(self.study_date, self.study_time)

    def __repr__(self) -> str:
        return f"InstanceInfo(study={self.study_uid}, sop={self.sop_uid})"


def _text(tags: dict[str, Any], keyword: str) -> str | None:
    value = tags.get(keyword)
    if value is None:
        return None
    if isinstance(value, list):  # multi-valued tags
        value = "\\".join(str(v) for v in value)
    return str(value)


def parse_datetime(date: str | None, time: str | None = None) -> datetime | None:
    """A DICOM date and time (or a DT value when time is None), to the second; None if unreadable."""
    text = (date or "") + (time or "")
    m = re.match(r"(\d{14})", re.sub(r"[^\d.]", "", text))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def instance_info(tags: dict[str, Any]) -> InstanceInfo:
    acquisition = parse_datetime(_text(tags, "AcquisitionDateTime"))
    if acquisition is None:
        acquisition = parse_datetime(_text(tags, "SeriesDate"), _text(tags, "SeriesTime"))
    if acquisition is None:
        acquisition = parse_datetime(_text(tags, "StudyDate"), _text(tags, "StudyTime"))
    return InstanceInfo(
        study_uid=_text(tags, "StudyInstanceUID") or "",
        sop_uid=_text(tags, "SOPInstanceUID") or "",
        series_uid=_text(tags, "SeriesInstanceUID") or "",
        operators_name=_text(tags, "OperatorsName"),
        patient_id=_text(tags, "PatientID"),
        patient_name=_text(tags, "PatientName"),
        patient_sex=_text(tags, "PatientSex"),
        patient_birth_date=_text(tags, "PatientBirthDate"),
        study_date=_text(tags, "StudyDate"),
        study_time=_text(tags, "StudyTime"),
        acquisition=acquisition,
        series_description=_text(tags, "SeriesDescription"),
        image_type=_text(tags, "ImageType"),
    )
