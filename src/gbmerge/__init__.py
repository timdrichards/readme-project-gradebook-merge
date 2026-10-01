"""gbmerge -- merge Gradescope exports into an LMS gradebook upload.

The short version, from Python::

    from gbmerge import MergeJob, load_config

    job = MergeJob(
        roster="roster.csv",
        exports="exports/hw4_scores.csv",
        assignment="Homework 4 (884213)",
        config=load_config(),
    )
    plan = job.plan()
    print(plan.format_preview())     # nothing has been written yet
    job.write(plan, "upload.csv")

Underneath: :func:`load_roster` and :func:`load_export` read the files,
:func:`match_students` decides who is who, :func:`apply_policy` turns
submissions into scores, and :func:`build_upload_plan` with
:func:`write_upload_csv` produces the file. :func:`register_policy` adds a
scoring rule of your own.

This writes real grades into a real gradebook and there is no undo. Run
``gbmerge check`` first, and ``gbmerge merge --dry-run`` after that.
"""

from __future__ import annotations

__version__ = "0.6.2"

from .matching import (
    Ambiguity,
    AmbiguousMatchError,
    Duplicate,
    Match,
    MatchError,
    MatchReport,
    explain_unmatched,
    match_students,
    parse_match_on,
)
from .policy import (
    Policy,
    PolicyError,
    PolicyOutcome,
    ScoreContext,
    ScoredStudent,
    ScoreSheet,
    apply_policy,
    apply_rounding,
    available_policies,
    drop_lowest_questions,
    get_policy,
    late_days,
    register_policy,
)
from .roster import (
    DEFAULT_CONFIG,
    ColumnNotFoundError,
    Config,
    ConfigError,
    EncodingError,
    ExportError,
    GbmergeError,
    GradescopeExport,
    Roster,
    RosterError,
    RosterRow,
    Submission,
    detect_excel_damage,
    find_config,
    load_config,
    load_export,
    load_exports,
    load_roster,
    normalize_email,
    normalize_sis_id,
)
from .writer import (
    Change,
    Gradebook,
    MergeJob,
    UploadPlan,
    WriteError,
    build_upload_plan,
    write_audit_log,
    write_upload_csv,
)

__all__ = [
    "__version__",
    # jobs
    "Gradebook", "MergeJob",
    # loading
    "load_roster", "load_export", "load_exports", "load_config", "find_config",
    "Roster", "RosterRow", "GradescopeExport", "Submission", "Config",
    "DEFAULT_CONFIG", "detect_excel_damage", "normalize_email", "normalize_sis_id",
    # matching
    "match_students", "explain_unmatched", "parse_match_on", "MatchReport",
    "Match", "Ambiguity", "Duplicate",
    # policy
    "apply_policy", "register_policy", "get_policy", "available_policies",
    "late_days", "apply_rounding", "drop_lowest_questions",
    "Policy", "PolicyOutcome", "ScoreContext", "ScoreSheet", "ScoredStudent",
    # output
    "build_upload_plan", "write_upload_csv", "write_audit_log", "UploadPlan",
    "Change",
    # errors
    "GbmergeError", "ConfigError", "RosterError", "ExportError", "EncodingError",
    "ColumnNotFoundError", "MatchError", "AmbiguousMatchError", "PolicyError",
    "WriteError",
]
