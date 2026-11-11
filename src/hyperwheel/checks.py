"""Checks for operator mistakes, run when a study becomes stable and after each raw-data pull.

Only unsafe problems hold anything back (mixed logins in one study hold the
study; a bad UID is held on its own). Everything
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
from .dicominfo import InstanceInfo
from .state import Store, Study

# Every code a check can raise, so a re-run clears the ones that no longer apply.
STUDY_CHECK_CODES = (
    "MIXED_LOGINS", "BLANK_PATIENT_ID", "PATIENT_ID_SPACES", "PATIENT_ID_PATTERN", "PATIENT_ID_NAME_DIFFER",
    "PHANTOM_UNDER_PARTICIPANT_ID", "PARTICIPANT_UNDER_PHANTOM_ID", "SAME_ID_SAME_DAY", "DEMOGRAPHICS_DIFFER",
)
# Flags about missing raw data, which the Pi no longer raises: DICOM and raw data go to GRIP separately and
# aren't coupled there, and timestamps between series and raw files don't always line up. Whatever raw data
# is there uploads, and a data manager sees on GRIP if something is missing. Any still open are cleared.
RETIRED_RAW_CODES = ("RAW_MISSING_FOR_SERIES", "RAW_FOLDER_NOT_FOUND")

PHANTOM_SERIES = "temperature vial"


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
                            "MISSING_<n> subject. Please move it to the right subject in GRIP."))
    elif clean != pid:
        issues.append(Issue("PATIENT_ID_SPACES", "The Patient ID starts or ends with a space. It is sent as typed. "
                            "Check the subject in GRIP."))
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


def clear_retired_flags(store: Store) -> int:
    """Clear any raw-data flags raised by an earlier release. Returns how many were cleared."""
    cleared = 0
    for flag in store.open_flags():
        if flag.code in RETIRED_RAW_CODES:
            if flag.study_id is not None:
                store.clear_study_flags(flag.study_id, [flag.code])
            else:
                store.clear_flags(flag.code, file_id=flag.file_id, project=flag.project, ref=flag.ref)
            cleared += 1
    return cleared


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
