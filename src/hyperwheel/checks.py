"""Checks for operator mistakes, run when a study becomes stable and after each raw-data pull.

Only unsafe problems hold anything back (mixed logins in one study hold the
study; an unassigned raw file and a bad UID are held on their own). Everything
else is a flag for the data manager: the data still goes to GRIP unchanged,
because data managers fix subjects and sessions in GRIP.

Flag texts are written for data managers and carry no patient data; the
dashboard shows the Patient ID next to them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .config import Project, SiteConfig
from .dicominfo import InstanceInfo, parse_datetime
from .ids import rrdf_timestamp
from .state import Store, Study

# Every code a check can raise, so a re-run clears the ones that no longer apply.
STUDY_CHECK_CODES = (
    "MIXED_LOGINS", "BLANK_PATIENT_ID", "PATIENT_ID_SPACES", "PATIENT_ID_PATTERN", "PATIENT_ID_NAME_DIFFER",
    "PHANTOM_UNDER_PARTICIPANT_ID", "PARTICIPANT_UNDER_PHANTOM_ID", "SAME_ID_SAME_DAY", "DEMOGRAPHICS_DIFFER",
)
RAW_CHECK_CODES = ("RAW_MISSING_FOR_SERIES", "RAW_FOLDER_NOT_FOUND")

PHANTOM_SERIES = "temperature vial"
NO_RAW_SERIES = ("localizer", "localiser")


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str
    level: str = "flag"


def logins(instances: Iterable[InstanceInfo]) -> set[str | None]:
    return {i.login for i in instances}


def mixed_logins(instances: list[InstanceInfo]) -> Issue | None:
    found = logins(instances)
    if len(found) > 1:
        names = ", ".join(sorted(x or "(none)" for x in found))
        return Issue("MIXED_LOGINS", f"This study has images from more than one scanner login ({names}). It is "
                     "held: nothing is uploaded or deleted until a data manager decides where it belongs.", "hold")
    return None


def study_issues(store: Store, study: Study, instances: list[InstanceInfo], site: SiteConfig,
                 project: Project) -> list[Issue]:
    issues: list[Issue] = []
    first = instances[0]
    pid = first.patient_id or ""
    clean = pid.strip()

    if clean == "0":
        # Some scanner software sends 0 when the field is left empty. It's sent unchanged.
        issues.append(Issue("BLANK_PATIENT_ID", "The Patient ID is blank (the scanner sent 0). Please move this "
                            "session to the right subject in GRIP."))
    elif clean == "":
        issues.append(Issue("BLANK_PATIENT_ID", "The Patient ID is blank. GRIP files the session under a "
                            "MISSING_<n> subject; please move it to the right subject in GRIP."))
    elif clean != pid:
        issues.append(Issue("PATIENT_ID_SPACES", "The Patient ID starts or ends with a space. It is sent as typed; "
                            "check the subject in GRIP."))
    if clean and project.patient_id_pattern:
        pattern = re.compile(project.patient_id_pattern)
        if not pattern.fullmatch(clean):
            issues.append(Issue("PATIENT_ID_PATTERN", "The Patient ID doesn't look like this study's IDs. "
                                "Check it for a typo and fix the subject in GRIP if needed."))
        in_name = {m.group(0) for m in pattern.finditer(first.patient_name or "")}
        if in_name and clean not in in_name:
            issues.append(Issue("PATIENT_ID_NAME_DIFFER", "The patient name contains a different ID from the "
                                "Patient ID, which suggests a typo in one of them."))

    if site.phantom_patient_id:
        phantom = any(PHANTOM_SERIES in (i.series_description or "").lower() for i in instances)
        if phantom and clean != site.phantom_patient_id:
            issues.append(Issue("PHANTOM_UNDER_PARTICIPANT_ID", "This looks like a phantom scan (it has a "
                                "temperature vial series) but isn't under the site's phantom ID."))
        if not phantom and clean == site.phantom_patient_id:
            issues.append(Issue("PARTICIPANT_UNDER_PHANTOM_ID", "This is under the site's phantom ID but has no "
                                "temperature vial series, so it may be a participant."))

    if clean and clean != site.phantom_patient_id:
        for other in store.studies():
            if other.id == study.id or other.project != study.project or (other.patient_id or "").strip() != clean:
                continue
            label = f"{other.study_date}_{(other.study_time or '')[:6]}"
            if other.study_date and other.study_date == study.study_date:
                issues.append(Issue("SAME_ID_SAME_DAY", f"The same Patient ID was scanned twice on the same day (also in "
                                    f"session {label}). If these are two children, fix one in GRIP."))
            if ((first.patient_sex and other.patient_sex and first.patient_sex != other.patient_sex) or
                    (first.patient_birth_date and other.patient_birth_date
                     and first.patient_birth_date != other.patient_birth_date)):
                issues.append(Issue("DEMOGRAPHICS_DIFFER", f"Another session with this Patient ID ({label}) has a "
                                    "different sex or birth date, so they may be different people."))
    return _dedupe(issues)


def raw_issues(store: Store, study: Study, folder_found: bool) -> list[Issue]:
    """Series that should have raw data but don't. Flags only; run after a raw-data pull."""
    if not folder_found:
        return [Issue("RAW_FOLDER_NOT_FOUND", "No raw-data folder for this study was found on the scanner. "
                      "Check that raw data saving is on for this protocol.")]
    raw_times = {rrdf_timestamp(f.name) for f in store.files(study.id, kind="rrdf") if f.state != "seen"}
    missing = []
    for f in store.files(study.id, kind="dicom"):
        description = (f.series_description or "").lower()
        if "ORIGINAL" not in (f.image_type or "") or any(s in description for s in NO_RAW_SERIES):
            continue
        when = parse_datetime(f.acquisition_time)
        if when is not None and when not in raw_times:
            missing.append(f.series_description or f.series_uid or "?")
    if missing:
        names = ", ".join(sorted(set(missing)))
        return [Issue("RAW_MISSING_FOR_SERIES", f"No raw data found for: {names}.")]
    return []


def apply(store: Store, study: Study, issues: list[Issue], codes: Iterable[str]) -> None:
    """Raise these issues and clear the earlier ones of the given codes that no longer apply."""
    raised = {i.code for i in issues}
    store.clear_study_flags(study.id, [c for c in codes if c not in raised])
    for issue in issues:
        store.raise_flag(issue.code, issue.detail, study_id=study.id, level=issue.level)


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen, out = set(), []
    for i in issues:
        if i.code not in seen:
            seen.add(i.code)
            out.append(i)
    return out
