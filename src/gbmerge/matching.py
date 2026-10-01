"""Deciding which Gradescope submission belongs to which roster student.

Three strategies, tried in the order you ask for them:

``sis_id``
    The LMS id from the roster against Gradescope's ``SID``. Exact after
    normalization. The only reliable one, and the only one on by default.
``email``
    Local part only -- ``ttran`` matches ``ttran``, whether Gradescope says
    ``ttran@cs.umass.edu`` and the roster says ``ttran@umass.edu`` or the
    reverse.
``name``
    Normalized name, exact; with ``fuzzy`` on, near-misses above a threshold,
    which is how "Tavi Tran" finds "Tavi  Tran" and also how it finds the
    wrong Tran if you are unlucky.

Every match records which strategy found it. Read the match report before you
believe any of it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .roster import (
    GbmergeError,
    GradescopeExport,
    Roster,
    RosterRow,
    Submission,
    normalize_email,
    normalize_name,
    normalize_sis_id,
)

STRATEGIES = ("sis_id", "email", "name")
DEFAULT_STRATEGIES = ("sis_id",)
DEFAULT_FUZZY_THRESHOLD = 0.88


class MatchError(GbmergeError):
    """Matching hit something it was told not to continue past."""


class AmbiguousMatchError(MatchError):
    """One submission matched more than one roster student."""


@dataclass
class Match:
    """A submission tied to a roster row, and how we got there."""

    student: RosterRow
    submission: Submission
    strategy: str
    confidence: float = 1.0
    note: str = ""

@dataclass
class Ambiguity:
    """A submission that matched several roster students at once."""

    submission: Submission
    candidates: list[RosterRow]
    strategy: str
    resolution: str = "unresolved"

    def describe(self) -> str:
        names = ", ".join(c.label for c in self.candidates)
        return (
            f"{self.submission.label} matched {len(self.candidates)} students "
            f"by {self.strategy}: {names} [{self.resolution}]"
        )


@dataclass
class Duplicate:
    """Two submissions that both claim the same roster student."""

    student: RosterRow
    kept: Submission
    dropped: Submission
    reason: str = ""

    def describe(self) -> str:
        return (
            f"{self.student.label} has two submissions "
            f"({self.kept.source} and {self.dropped.source}); "
            f"kept {self.kept.source}{(' -- ' + self.reason) if self.reason else ''}"
        )


@dataclass
class MatchReport:
    """Everything matching learned, including what it could not do.

    Read this before a merge. ``unmatched_students`` is the list that decides
    grades: with the default ``--on-missing skip`` those students are not
    written at all, and whatever the LMS already has for them stays.
    """

    matches: list[Match] = field(default_factory=list)
    unmatched_students: list[RosterRow] = field(default_factory=list)
    unmatched_submissions: list[Submission] = field(default_factory=list)
    ambiguous: list[Ambiguity] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    strategies_used: list[str] = field(default_factory=list)
    roster_size: int = 0
    submission_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def by_student_index(self) -> dict[int, Match]:
        """Matches keyed by ``RosterRow.index``, for fast lookup downstream."""
        return {m.student.index: m for m in self.matches}

    @property
    def strategy_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for match in self.matches:
            counts[match.strategy] = counts.get(match.strategy, 0) + 1
        return counts

    @property
    def is_clean(self) -> bool:
        """True when every student and every submission found a partner."""
        return not (self.unmatched_students or self.unmatched_submissions or self.ambiguous)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable view, used by ``--json``."""
        return {
            "roster_size": self.roster_size,
            "submissions": self.submission_count,
            "matched": len(self.matches),
            "strategies": self.strategies_used,
            "strategy_counts": self.strategy_counts,
            "unmatched_students": [
                {"name": r.name, "sis_id": r.sis_id, "login": r.login_id}
                for r in self.unmatched_students
            ],
            "unmatched_submissions": [
                {"name": s.name, "sid": s.sid, "email": s.email,
                 "source": s.source, "row": s.row_number}
                for s in self.unmatched_submissions
            ],
            "ambiguous": [
                {"submission": a.submission.label, "strategy": a.strategy,
                 "resolution": a.resolution,
                 "candidates": [c.label for c in a.candidates]}
                for a in self.ambiguous
            ],
            "duplicates": [
                {"student": d.student.label, "kept": d.kept.source,
                 "dropped": d.dropped.source}
                for d in self.duplicates
            ],
            "warnings": list(self.warnings),
        }

    def format_text(self, *, verbose: bool = False, limit: int = 20) -> str:
        """Render the report for a terminal.

        With ``verbose``, every unmatched row is listed. Without it, the first
        ``limit`` of each kind, which is enough to see the pattern -- if all
        twelve unmatched students have blank SIDs in Gradescope, you will know
        from the first three.
        """
        lines: list[str] = []
        lines.append(
            f"matched {len(self.matches)} of {self.roster_size} students "
            f"from {self.submission_count} submissions "
            f"(strategies: {', '.join(self.strategies_used) or 'none'})"
        )
        counts = self.strategy_counts
        if counts:
            detail = ", ".join(f"{name}: {n}" for name, n in sorted(counts.items()))
            lines.append(f"  by strategy: {detail}")

        def block(title: str, items: Sequence[str]) -> None:
            if not items:
                return
            shown = items if verbose else items[:limit]
            lines.append("")
            lines.append(f"{title} ({len(items)}):")
            lines.extend(f"  {line}" for line in shown)
            if len(items) > len(shown):
                lines.append(f"  ... and {len(items) - len(shown)} more (use --verbose)")

        block(
            "roster students with no submission",
            [
                f"{r.label}"
                + (f" <{r.login_id}>" if r.login_id else " <no login on roster>")
                for r in self.unmatched_students
            ],
        )
        block(
            "submissions with no roster student",
            [
                f"{s.label} <{s.email or 'no email'}> in {s.source} line {s.row_number}"
                for s in self.unmatched_submissions
            ],
        )
        block("ambiguous", [a.describe() for a in self.ambiguous])
        block("duplicate submissions", [d.describe() for d in self.duplicates])

        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  {w}" for w in self.warnings)

        if self.unmatched_submissions and "email" not in self.strategies_used:
            lines.append("")
            lines.append(
                "hint: matching is on SIS id only. If Gradescope rows have blank "
                "SIDs, try --match-on sis_id,email"
            )
        return "\n".join(lines)


# --- Strategy implementations ---


def _roster_index(roster: Roster, strategy: str) -> dict[str, list[RosterRow]]:
    if strategy == "sis_id":
        return roster.by_sis_id()
    if strategy == "email":
        return roster.by_email_key()
    if strategy == "name":
        return roster.by_name_key()
    raise MatchError(f"unknown match strategy {strategy!r} (known: {', '.join(STRATEGIES)})")


def _submission_key(submission: Submission, strategy: str) -> str:
    if strategy == "sis_id":
        return normalize_sis_id(submission.sid)
    if strategy == "email":
        return submission.email_key or normalize_email(submission.email)
    if strategy == "name":
        return submission.name_key or normalize_name(submission.name)
    raise MatchError(f"unknown match strategy {strategy!r}")


def fuzzy_candidates(
    key: str,
    index: dict[str, list[RosterRow]],
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> list[tuple[float, RosterRow]]:
    """Roster rows whose key is close to ``key``, best first.

    :class:`difflib.SequenceMatcher` on the normalized strings. No nickname
    table, no transliteration, so "Bob" will not find "Robert" and never will.
    """
    scored: list[tuple[float, RosterRow]] = []
    for candidate_key, rows in index.items():
        ratio = difflib.SequenceMatcher(None, key, candidate_key).ratio()
        if ratio >= threshold:
            for row in rows:
                scored.append((ratio, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1].index))
    return scored


def _collect_submissions(
    exports: GradescopeExport | Iterable[GradescopeExport],
) -> tuple[list[Submission], list[str]]:
    """Flatten one or more exports into a single submission list."""
    if isinstance(exports, GradescopeExport):
        exports = [exports]
    submissions: list[Submission] = []
    warnings: list[str] = []
    for export in exports:
        submissions.extend(export.submissions)
        warnings.extend(export.warnings)
    return submissions, warnings


def _resolve_ambiguity(
    submission: Submission,
    candidates: list[RosterRow],
    strategy: str,
    on_ambiguous: str,
) -> tuple[RosterRow | None, Ambiguity]:
    """Apply the ``--on-ambiguous`` policy to one contested submission."""
    record = Ambiguity(submission=submission, candidates=list(candidates), strategy=strategy)
    if on_ambiguous == "fail":
        raise AmbiguousMatchError(
            f"{submission.label} matches {len(candidates)} roster students by "
            f"{strategy}: " + ", ".join(c.label for c in candidates)
        )
    if on_ambiguous == "first":
        record.resolution = f"took first ({candidates[0].label})"
        return candidates[0], record
    # "report" and "skip" both leave the submission unassigned. The difference
    # is only whether the CLI prints the list or exits non-zero over it.
    record.resolution = "skipped" if on_ambiguous == "skip" else "reported, not merged"
    return None, record


def _prefer(kept: Submission, other: Submission) -> tuple[Submission, Submission, str]:
    """Pick between two submissions for the same student.

    Later submission time wins, since Gradescope grades the latest attempt. With
    no usable times we keep the one we saw first and say so -- which means file
    order decides a grade, a good reason not to pass two exports of the same
    assignment.
    """
    kept_time = (kept.submitted_at or "").strip()
    other_time = (other.submitted_at or "").strip()
    if kept_time and other_time and other_time != kept_time:
        if other_time > kept_time:
            return other, kept, "later submission time"
        return kept, other, "later submission time"
    if other_time and not kept_time:
        return other, kept, "only one row has a submission time"
    if kept_time and not other_time:
        return kept, other, "only one row has a submission time"
    return kept, other, "no submission times to compare; kept the first one seen"


def match_students(
    roster: Roster,
    exports: GradescopeExport | Iterable[GradescopeExport],
    *,
    match_on: Sequence[str] = DEFAULT_STRATEGIES,
    fuzzy: bool = False,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    on_ambiguous: str = "report",
) -> MatchReport:
    """Match submissions to roster students.

    Args:
        roster: Loaded roster. Sentinel rows are never matched against.
        exports: One :class:`~gbmerge.roster.GradescopeExport` or several. Two
            exports holding the same student produce a duplicate record.
        match_on: Strategies in priority order. Each submission is matched by the
            first strategy that produces exactly one candidate.
        fuzzy: Allow near-miss name matching. Ids and emails stay exact.
        fuzzy_threshold: Similarity in ``[0, 1]`` required for a fuzzy match.
        on_ambiguous: ``report`` (default), ``skip``, ``first`` or ``fail``.

    Returns:
        A :class:`MatchReport`. Students with no submission and submissions with
        no student are both recorded there rather than raised.

    Raises:
        MatchError: on an unknown strategy.
        AmbiguousMatchError: when ``on_ambiguous="fail"`` and one hits.
    """
    strategies = [s for s in match_on if s]
    for strategy in strategies:
        if strategy not in STRATEGIES:
            raise MatchError(
                f"unknown match strategy {strategy!r} (known: {', '.join(STRATEGIES)})"
            )
    if not strategies:
        raise MatchError("no match strategies given; use --match-on sis_id,email")

    submissions, warnings = _collect_submissions(exports)
    report = MatchReport(
        strategies_used=list(strategies),
        roster_size=len(roster.students),
        submission_count=len(submissions),
        warnings=list(warnings),
    )

    indexes = {strategy: _roster_index(roster, strategy) for strategy in strategies}
    claimed: dict[int, Match] = {}
    unmatched: list[Submission] = []

    for submission in submissions:
        matched = False
        for strategy in strategies:
            key = _submission_key(submission, strategy)
            if not key:
                continue
            candidates = list(indexes[strategy].get(key, ()))
            confidence = 1.0

            if not candidates and strategy == "name" and fuzzy:
                scored = fuzzy_candidates(key, indexes[strategy], fuzzy_threshold)
                if scored:
                    best = scored[0][0]
                    # Everything within a whisker of the best score is a
                    # candidate; if two names tie, that is an ambiguity, not a
                    # coin flip.
                    near = [row for ratio, row in scored if ratio >= best - 0.01]
                    candidates = near
                    confidence = best

            if not candidates:
                continue

            if len(candidates) > 1:
                chosen, record = _resolve_ambiguity(
                    submission, candidates, strategy, on_ambiguous
                )
                report.ambiguous.append(record)
                if chosen is None:
                    matched = True  # handled, even if not merged
                    break
                candidates = [chosen]

            student = candidates[0]
            existing = claimed.get(student.index)
            if existing is not None:
                kept, dropped, reason = _prefer(existing.submission, submission)
                report.duplicates.append(
                    Duplicate(student=student, kept=kept, dropped=dropped, reason=reason)
                )
                if kept is submission:
                    claimed[student.index] = Match(
                        student=student,
                        submission=submission,
                        strategy=strategy,
                        confidence=confidence,
                        note=f"replaced a row from {dropped.source}",
                    )
                matched = True
                break

            claimed[student.index] = Match(
                student=student,
                submission=submission,
                strategy=strategy,
                confidence=confidence,
                note="" if confidence >= 1.0 else f"fuzzy name match at {confidence:.2f}",
            )
            matched = True
            break

        if not matched:
            unmatched.append(submission)

    # Preserve roster order in the output: the upload file is written row by
    # row against the roster, and a report that lists people in a different
    # order than the file is harder to check against it.
    report.matches = [claimed[i] for i in sorted(claimed)]
    report.unmatched_students = [r for r in roster.students if r.index not in claimed]
    report.unmatched_submissions = unmatched

    if fuzzy and any(m.confidence < 1.0 for m in report.matches):
        count = sum(1 for m in report.matches if m.confidence < 1.0)
        report.warnings.append(
            f"{count} matches came from fuzzy name comparison; check them by hand"
        )
    return report


def parse_match_on(value: str | Sequence[str]) -> list[str]:
    """Parse ``--match-on sis_id,email`` into a validated list.

    Raises:
        MatchError: on an unknown or duplicated strategy name.
    """
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    else:
        parts = [str(part).strip() for part in value]
    parts = [p for p in parts if p]
    if not parts:
        raise MatchError("--match-on needs at least one strategy")
    seen: list[str] = []
    for part in parts:
        if part not in STRATEGIES:
            raise MatchError(
                f"unknown match strategy {part!r}; known strategies are "
                + ", ".join(STRATEGIES)
            )
        if part in seen:
            raise MatchError(f"--match-on lists {part!r} twice")
        seen.append(part)
    return seen


def explain_unmatched(report: MatchReport, roster: Roster) -> list[str]:
    """Guess at *why* each unmatched submission failed, for the match report.

    These are guesses. "12 unmatched" with no explanation sends people to
    re-export files that were fine, and the true reason is usually one of four
    boring things.
    """
    notes: list[str] = []
    emails, names, ids = roster.by_email_key(), roster.by_name_key(), roster.by_sis_id()

    for sub in report.unmatched_submissions:
        reasons: list[str] = []
        if not sub.sid:
            reasons.append("no SID in the export")
        elif sub.sid not in ids:
            reasons.append(f"SID {sub.sid} is not on the roster")
        if not sub.email_key:
            reasons.append("no email in the export")
        elif sub.email_key in emails:
            reasons.append(
                f"email local part matches {emails[sub.email_key][0].label} "
                f"-- add 'email' to --match-on"
            )
        else:
            reasons.append(f"email local part {sub.email_key!r} is not on the roster")
        if sub.name_key and sub.name_key in names:
            reasons.append(
                f"name matches {names[sub.name_key][0].label} exactly -- try --match-on name"
            )
        notes.append(f"{sub.label}: " + "; ".join(reasons))

    for student in report.unmatched_students:
        if not student.sis_id:
            notes.append(f"{student.label}: roster row has no {roster.id_column}")
    return notes
