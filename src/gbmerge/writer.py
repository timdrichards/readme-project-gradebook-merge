"""Building the upload file, and the job object that ties everything together.

The output is a CSV the LMS gradebook import will accept: the roster's identity
columns, then exactly one assignment column. Every roster row is written,
including the two rows that are not students.

Two rules this module exists to enforce. **Never invent a cell**: a student we
did not score keeps whatever the roster already had, so the upload says
"nothing changed for this person" rather than "this person scored nothing".
**Never rename or reorder anything**: the header text goes back out byte for
byte, trailing assignment id and all.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .matching import MatchReport, match_students
from .policy import Policy, ScoreSheet, ScoredStudent, apply_policy
from .roster import (
    DEFAULT_CONFIG,
    GbmergeError,
    GradescopeExport,
    Roster,
    RosterRow,
    assignment_id,
    load_roster,
    resolve_exports,
)


class WriteError(GbmergeError):
    """The upload file could not be written."""


# Identity columns kept in the upload, in this order, when the roster has them.
# The LMS needs enough of these to match rows; more is harmless, fewer is not.
IDENTITY_COLUMNS = (
    "Student", "Student Name", "Name", "ID", "SIS User ID", "SIS Login ID",
    "Integration ID", "Section", "Sections",
)


@dataclass
class Change:
    """One cell that will differ from the roster we read."""

    student: RosterRow
    before: str
    after: str
    scored: ScoredStudent | None = None

    @property
    def is_new(self) -> bool:
        """True when the LMS had nothing here before."""
        return not str(self.before).strip()

    def describe(self) -> str:
        before = self.before if str(self.before).strip() else "(blank)"
        line = f"{self.student.label}: {before} -> {self.after}"
        if self.scored and self.scored.submission and self.scored.submission.is_missing:
            # Gradescope had a row for them with nothing in it. They are being
            # scored 0, not skipped, because they did match a submission.
            line += "  [row in Gradescope, no submission]"
        if self.scored and self.scored.late_days:
            tail = f"{self.scored.late_days}d late"
            if self.scored.exempt:
                tail += ", exempt"
            elif self.scored.penalty:
                tail += f", -{self.scored.penalty:g}"
            line += f"  [{tail}]"
        return line


@dataclass
class UploadPlan:
    """Everything needed to write the upload file, and to explain it first.

    ``rows`` is the finished file as a list of dicts; ``changes`` is only the
    cells that differ from what the roster already held. A dry run prints the
    changes and throws the rows away.
    """

    roster: Roster
    assignment_column: str
    fieldnames: list[str]
    rows: list[dict[str, str]]
    changes: list[Change] = field(default_factory=list)
    sheet: ScoreSheet | None = None
    report: MatchReport | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def assignment_id(self) -> str | None:
        """The trailing ``(123456)`` id of the target column, if it has one."""
        return assignment_id(self.assignment_column)

    def summary(self) -> dict[str, Any]:
        """Counts for the banner, the confirmation prompt and ``--json``."""
        data: dict[str, Any] = {
            "assignment": self.assignment_column,
            "assignment_id": self.assignment_id,
            "rows": len(self.rows),
            "changes": len(self.changes),
            "new_values": sum(1 for c in self.changes if c.is_new),
            "overwrites": sum(1 for c in self.changes if not c.is_new),
        }
        if self.sheet is not None:
            data.update(self.sheet.summary())
        return data

    def format_preview(self, *, limit: int = 20, verbose: bool = False) -> str:
        """Human-readable description of what writing this file would do."""
        summary = self.summary()
        lines = [
            f"assignment column: {self.assignment_column!r}",
            f"rows in file:      {summary['rows']} " f"({len(self.roster.students)} students + "
            f"{len(self.roster.sentinels)} kept non-student rows)",
            f"cells changed:     {summary['changes']} "
            f"({summary['new_values']} new, {summary['overwrites']} overwriting "
            f"an existing grade)",
        ]
        if self.sheet is not None:
            sheet = self.sheet
            lines += [
                f"skipped:           {len(sheet.skipped)} students with no submission",
                f"late:              {len(sheet.late)} ({len(sheet.penalized)} "
                f"penalized, {len(sheet.exempted)} exempt)",
                f"scored zero:       {len(sheet.zeros)}",
                f"policy:            {sheet.policy.describe()}",
            ]

        if self.changes:
            shown = self.changes if verbose else self.changes[:limit]
            lines.append("")
            lines.append("changes:")
            lines.extend(f"  {c.describe()}" for c in shown)
            if len(self.changes) > len(shown):
                lines.append(f"  ... and {len(self.changes) - len(shown)} more (use --verbose)")

        overwrites = [c for c in self.changes if not c.is_new]
        if overwrites:
            lines.append("")
            lines.append(
                f"note: {len(overwrites)} of these replace a grade the LMS already "
                f"has. There is no undo."
            )

        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  {w}" for w in self.warnings)
        return "\n".join(lines)

    def to_csv_text(self) -> str:
        """Render the upload file to a string. Used by the writer and by tests."""
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            buffer,
            fieldnames=self.fieldnames,
            extrasaction="ignore",
            lineterminator="\r\n",  # what the LMS import expects; Excel agrees
        )
        writer.writeheader()
        for row in self.rows:
            writer.writerow(row)
        return buffer.getvalue()


def _output_fieldnames(roster: Roster, assignment_column: str) -> list[str]:
    """Identity columns the roster actually has, then the assignment column.

    Other assignment columns are left out on purpose. Uploading the whole
    gradebook back would re-write every assignment in the course from a file
    that is only correct about one of them.
    """
    keep = [name for name in IDENTITY_COLUMNS if name in roster.fieldnames]
    if roster.id_column not in keep and roster.id_column in roster.fieldnames:
        keep.append(roster.id_column)
    if assignment_column in keep:
        return keep
    return [*keep, assignment_column]


def _format_score(value: float | None) -> str:
    """Format a score for the CSV: no trailing ``.0`` on whole numbers."""
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):g}"


def _current_values(roster: Roster, assignment_column: str) -> list[str]:
    """The column as the roster currently has it, one entry per row.

    A plain dict lookup. If ``assignment_column`` is not a real column this
    raises ``KeyError`` from somewhere three calls below wherever you typed the
    name, which is not a good error message and is the one place the original
    script's manners survive intact. ``gbmerge check`` exists to catch this
    first; merge does not run those checks.
    """
    return [row.data[assignment_column] for row in roster.rows]


def build_upload_plan(
    roster: Roster,
    sheet: ScoreSheet,
    assignment_column: str,
    *,
    report: MatchReport | None = None,
    keep_test_student: bool = True,
) -> UploadPlan:
    """Assemble the upload file in memory. Nothing is written.

    Args:
        roster: The loaded roster. Its row order is the output's row order.
        sheet: Output of :func:`~gbmerge.policy.apply_policy`.
        assignment_column: The exact roster column to write into, including its
            trailing ``(123456)`` id.
        report: The match report, carried along for the audit log.
        keep_test_student: Keep the LMS's non-student rows. Off produces a file
            the import will probably reject; it exists because one course's LMS
            instance wanted it that way.

    Raises:
        KeyError: if ``assignment_column`` is not in the roster. Validate with
            :meth:`~gbmerge.roster.Roster.find_assignment_column` first.
    """
    before = _current_values(roster, assignment_column)
    fieldnames = _output_fieldnames(roster, assignment_column)
    scored_by_index = {s.student.index: s for s in sheet.rows}

    rows: list[dict[str, str]] = []
    changes: list[Change] = []

    for position, row in enumerate(roster.rows):
        if row.is_sentinel and not keep_test_student:
            continue

        out = {name: row.data.get(name, "") for name in fieldnames}
        scored = scored_by_index.get(row.index)

        if scored is not None and scored.changed:
            new_value = _format_score(scored.final_score)
            out[assignment_column] = new_value
            if new_value != (before[position] or ""):
                changes.append(
                    Change(
                        student=row,
                        before=before[position],
                        after=new_value,
                        scored=scored,
                    )
                )
        else:
            # Skipped students and the sentinel rows keep what they had. For
            # Points Possible that is the assignment's max points, which the
            # import wants back unchanged.
            out[assignment_column] = row.data.get(assignment_column, "")

        rows.append(out)

    plan = UploadPlan(
        roster=roster,
        assignment_column=assignment_column,
        fieldnames=fieldnames,
        rows=rows,
        changes=changes,
        sheet=sheet,
        report=report,
    )
    plan.warnings.extend(roster.warnings)
    plan.warnings.extend(sheet.warnings)
    if report is not None:
        plan.warnings.extend(report.warnings)
    if not keep_test_student and roster.sentinels:
        plan.warnings.append(
            f"dropped {len(roster.sentinels)} non-student rows "
            f"(keep_test_student is off); the LMS import may reject this file"
        )
    if not changes:
        plan.warnings.append(
            "nothing would change: every scored student already has this value in the roster"
        )
    return plan


def write_upload_csv(
    path: str | os.PathLike[str],
    plan: UploadPlan,
    *,
    encoding: str = "utf-8-sig",
    overwrite: bool = True,
) -> Path:
    """Write the upload CSV and return the path.

    ``encoding`` defaults to ``utf-8-sig``: the BOM is deliberate, because
    without it Excel mangles non-ASCII names when a TA opens the file to check
    it, and someone always opens the file to check it. ``overwrite=False``
    refuses rather than replacing an existing file.

    Raises:
        WriteError: if the file exists and ``overwrite`` is False, or the write
            fails.

    Warning:
        Opening this file in Excel and saving it rewrites the header row and can
        turn the ids into numbers. Look without saving.
    """
    p = Path(path)
    if p.exists() and not overwrite:
        raise WriteError(f"{p} already exists (pass --yes to overwrite)")
    try:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", newline="", encoding=encoding) as handle:
            handle.write(plan.to_csv_text())
    except OSError as exc:
        raise WriteError(f"cannot write {p}: {exc}") from None
    return p


def write_audit_log(
    path: str | os.PathLike[str],
    plan: UploadPlan,
    *,
    meta: dict[str, Any] | None = None,
    append: bool = True,
) -> Path:
    """Append a JSON-lines audit log of the merge, and return the path.

    One ``{"record": "run", ...}`` line with the settings, then one line per
    student with the raw score, the lateness, the penalty and the final value,
    then one for the match report. ``meta`` adds fields to the run record;
    ``append=False`` truncates first.

    This is the only record of *why* a number came out the way it did. The LMS
    keeps a history of the values it ends up with and nothing else.

    Raises:
        WriteError: if the file cannot be written.
    """
    p = Path(path)
    run: dict[str, Any] = {
        "record": "run",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "assignment": plan.assignment_column,
        "assignment_id": plan.assignment_id,
        "roster": str(plan.roster.path),
        "roster_encoding": plan.roster.encoding,
        "summary": plan.summary(),
        "warnings": list(plan.warnings),
    }
    if meta:
        run.update(meta)

    lines = [json.dumps(run, sort_keys=True)]
    if plan.sheet is not None:
        for scored in plan.sheet.rows:
            record = {"record": "student", **scored.to_dict()}
            lines.append(json.dumps(record, sort_keys=True))
    if plan.report is not None:
        lines.append(
            json.dumps({"record": "match", **plan.report.to_dict()}, sort_keys=True)
        )

    try:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise WriteError(f"cannot write audit log {p}: {exc}") from None
    return p


# --- The objects most library callers want ---


@dataclass
class Gradebook:
    """A roster plus the exports being merged into it.

    The stateful wrapper around the four free functions; each step caches its
    result::

        gb = Gradebook.load("roster.csv", "exports/hw4_scores.csv")
        print(gb.match(match_on=["sis_id", "email"]).format_text())
        sheet = gb.score(Policy(drop_lowest=1))
        plan = gb.plan("Homework 4 (884213)")

    Nothing here writes a file. :meth:`MergeJob.run` does that.
    """

    roster: Roster
    exports: list[GradescopeExport] = field(default_factory=list)
    report: MatchReport | None = None
    sheet: ScoreSheet | None = None

    @classmethod
    def load(
        cls,
        roster_path: str | os.PathLike[str],
        exports: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] = "exports",
        *,
        encoding: str | None = None,
        roster_id_column: str = DEFAULT_CONFIG["roster_id_column"],
        gradescope_id_column: str = DEFAULT_CONFIG["gradescope_id_column"],
        exports_glob: str = DEFAULT_CONFIG["exports_glob"],
    ) -> "Gradebook":
        """Load a roster and one or more exports.

        Args:
            roster_path: The LMS gradebook export.
            exports: A file, a directory, or an iterable of either.
            encoding: Force an encoding on every file.
            roster_id_column: Roster id column header.
            gradescope_id_column: Gradescope id column header.
            exports_glob: Glob used when ``exports`` names a directory.
        """
        roster = load_roster(
            roster_path, encoding=encoding, id_column=roster_id_column
        )
        targets: list[str | os.PathLike[str]]
        if isinstance(exports, (str, os.PathLike)):
            targets = [exports]
        else:
            targets = list(exports)

        loaded: list[GradescopeExport] = []
        for target in targets:
            loaded.extend(
                resolve_exports(
                    target,
                    pattern=exports_glob,
                    encoding=encoding,
                    id_column=gradescope_id_column,
                )
            )
        return cls(roster=roster, exports=loaded)

    def match(self, **kwargs: Any) -> MatchReport:
        """Match submissions to students. Arguments go to :func:`match_students`."""
        self.report = match_students(self.roster, self.exports, **kwargs)
        return self.report

    def score(self, policy: Policy | None = None, *, on_missing: str = "skip") -> ScoreSheet:
        """Apply a policy. Matches first with the defaults if you have not."""
        if self.report is None:
            self.match()
        self.sheet = apply_policy(
            self.roster, self.report, policy, on_missing=on_missing
        )
        return self.sheet

    def plan(self, assignment: str, *, keep_test_student: bool = True) -> UploadPlan:
        """Build the upload plan for one assignment column.

        Validates the column name first, so a typo here is an error that says
        what is wrong instead of a ``KeyError``.
        """
        column = self.roster.find_assignment_column(assignment)
        if self.sheet is None:
            self.score()
        return build_upload_plan(
            self.roster,
            self.sheet,
            column,
            report=self.report,
            keep_test_student=keep_test_student,
        )


@dataclass
class MergeJob:
    """One merge, start to finish, driven by a config mapping.

    This is what the CLI runs, and the easiest entry point from Python::

        from gbmerge import MergeJob, load_config

        job = MergeJob(
            roster="roster.csv",
            exports="exports/hw4_scores.csv",
            assignment="Homework 4 (884213)",
            config=load_config(),
        )
        plan = job.plan()
        print(plan.format_preview())
        job.write(plan, "upload.csv")

    ``config`` is a :class:`~gbmerge.roster.Config` or any dict with the same
    keys; defaults fill in whatever is absent.
    """

    roster: str | os.PathLike[str]
    exports: Any = "exports"
    assignment: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    gradebook: Gradebook | None = None

    def _setting(self, key: str) -> Any:
        return self.config.get(key, DEFAULT_CONFIG.get(key))

    def load(self) -> Gradebook:
        """Load the roster and exports. Cached."""
        if self.gradebook is None:
            self.gradebook = Gradebook.load(
                self.roster,
                self.exports,
                encoding=self._setting("encoding"),
                roster_id_column=self._setting("roster_id_column"),
                gradescope_id_column=self._setting("gradescope_id_column"),
                exports_glob=self._setting("exports_glob"),
            )
        return self.gradebook

    def match(self) -> MatchReport:
        """Run matching with this job's config."""
        gb = self.load()
        return gb.match(
            match_on=self._setting("match_on"),
            fuzzy=bool(self._setting("fuzzy")),
            fuzzy_threshold=float(self._setting("fuzzy_threshold")),
            on_ambiguous=str(self._setting("on_ambiguous")),
        )

    def score(self) -> ScoreSheet:
        """Score every student with this job's policy."""
        gb = self.load()
        if gb.report is None:
            self.match()
        return gb.score(
            Policy.from_config(self.config or DEFAULT_CONFIG),
            on_missing=str(self._setting("on_missing")),
        )

    def plan(self) -> UploadPlan:
        """Build the upload plan.

        Note:
            Unlike :meth:`Gradebook.plan`, this does **not** validate the
            assignment column first -- it is the path the CLI's ``merge`` takes,
            and it reproduces the original script's behaviour of failing deep
            inside with a ``KeyError``. Run ``gbmerge check`` first.
        """
        gb = self.load()
        if gb.sheet is None:
            self.score()
        return build_upload_plan(
            gb.roster,
            gb.sheet,
            self.assignment,
            report=gb.report,
            keep_test_student=bool(self._setting("keep_test_student")),
        )

    def write(
        self,
        plan: UploadPlan | None = None,
        out: str | os.PathLike[str] | None = None,
        *,
        audit_log: str | os.PathLike[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        """Write the upload file, and the audit log if one is configured.

        ``plan`` is built on demand, ``out`` and ``audit_log`` fall back to the
        ``out`` and ``audit_log`` config keys, and ``meta`` adds fields to the audit
        log's run record. Returns the path of the upload file.
        """
        plan = plan or self.plan()
        destination = Path(out or self._setting("out") or "upload.csv")
        write_upload_csv(destination, plan, encoding=self._setting("encoding") or "utf-8-sig")
        log = audit_log or self._setting("audit_log")
        if log:
            write_audit_log(log, plan, meta={**(meta or {}), "out": str(destination)})
        return destination

    def run(
        self,
        *,
        out: str | os.PathLike[str] | None = None,
        dry_run: bool = False,
        audit_log: str | os.PathLike[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> UploadPlan:
        """Do the whole thing: load, match, score, plan, and write.

        With ``dry_run``, the plan comes back without anything being written -- no
        upload file and no audit log either. Returns the :class:`UploadPlan` in both
        cases.
        """
        plan = self.plan()
        if dry_run:
            return plan
        self.write(plan, out, audit_log=audit_log, meta=meta)
        return plan


def summarize_exports(exports: Sequence[GradescopeExport]) -> str:
    """One line per export, for ``check`` and ``--verbose`` merges."""
    lines: list[str] = []
    for export in exports:
        max_points = export.max_points
        points = f"max {max_points:g}" if max_points is not None else "max points not stated"
        lateness = "" if export.has_lateness else ", no lateness column"
        lines.append(
            f"{export.path}: {len(export)} submissions, "
            f"{len(export.questions)} question columns, {points}{lateness}"
        )
    return "\n".join(lines)
