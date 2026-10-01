"""Turning a matched submission into the number that goes in the gradebook.

This is the part with the course policy baked into it, so it is the part worth
reading twice. In order:

1. Work out the raw score. Normally that is Gradescope's ``Total Score``. If
   ``drop_lowest`` is set we re-total the question columns ourselves, and the
   number we write will not be the number Gradescope shows.
2. Work out how late it is, in **started** days. Anything past the deadline at
   all is day one.
3. Hand both to a policy function, which returns the final score. The built-in
   policies are registered below; :func:`register_policy` adds your own.
4. Round.

Exemptions (accommodations) skip step 3's penalty entirely. They are listed by
LMS id in ``exempt_ids``, because emails are not stable across the two systems
and an accommodation applied to the wrong person is worse than most bugs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Sequence

from .matching import MatchReport
from .roster import (
    GbmergeError,
    Roster,
    RosterRow,
    Submission,
    format_lateness,
    normalize_sis_id,
)

SECONDS_PER_DAY = 86400
ROUNDING_MODES = ("none", "int", "half_up", "half_even", "tenth")
ON_MISSING = ("skip", "zero", "fail")


class PolicyError(GbmergeError):
    """A policy could not be applied, or does not exist."""


# --- Policy settings ---


@dataclass
class Policy:
    """The grading rules for one merge.

    Attributes:
        name: Which registered policy function to run.
        late_penalty_per_day: Fraction of the assignment's max points removed per
            started late day. ``0.10`` is ten percent.
        max_late_days: How many late days are accepted.
        beyond_max_late: ``"zero"`` scores anything later than ``max_late_days``
            as 0; ``"cap"`` stops the penalty growing instead.
        grace_minutes: Lateness below this counts as on time. Defaults to 0, so a
            submission 20 minutes late loses a full day.
        drop_lowest: Drop this many of the lowest question scores before
            totalling. Above 0, the total stops matching Gradescope's.
        rounding: One of :data:`ROUNDING_MODES`. round_places: decimal places.
        exempt_ids: LMS ids exempt from the late penalty.
    """

    name: str = "standard"
    late_penalty_per_day: float = 0.10
    max_late_days: int = 3
    beyond_max_late: str = "zero"
    grace_minutes: float = 0.0
    drop_lowest: int = 0
    rounding: str = "half_up"
    round_places: int = 2
    exempt_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Policy":
        """Build a :class:`Policy` from a config mapping.

        Accepts a :class:`~gbmerge.roster.Config` or any plain dict with the
        same keys. Unknown keys are ignored here; the config loader is the
        thing that rejects them.
        """
        exempt = {
            normalize_sis_id(value)
            for value in config.get("exempt_ids", ()) or ()
            if str(value).strip()
        }
        return cls(
            name=str(config.get("policy", "standard")),
            late_penalty_per_day=float(config.get("late_penalty_per_day", 0.10)),
            max_late_days=int(config.get("max_late_days", 3)),
            beyond_max_late=str(config.get("beyond_max_late", "zero")),
            grace_minutes=float(config.get("grace_minutes", 0)),
            drop_lowest=int(config.get("drop_lowest", 0)),
            rounding=str(config.get("round", "half_up")),
            round_places=int(config.get("round_places", 2)),
            exempt_ids=exempt,
        )

    def is_exempt(self, student: RosterRow) -> bool:
        """True if this student is exempt from the late penalty."""
        return bool(student.sis_id) and student.sis_id in self.exempt_ids

    def describe(self) -> str:
        """One-line summary, printed by ``check`` and the dry run."""
        bits = [f"policy={self.name}"]
        if self.late_penalty_per_day:
            bits.append(
                f"{self.late_penalty_per_day:.0%}/started day, "
                f"max {self.max_late_days} days then {self.beyond_max_late}"
            )
        else:
            bits.append("no late penalty")
        if self.grace_minutes:
            bits.append(f"grace {self.grace_minutes:g} min")
        if self.drop_lowest:
            bits.append(f"drop lowest {self.drop_lowest}")
        bits.append(f"round={self.rounding}")
        if self.exempt_ids:
            bits.append(f"{len(self.exempt_ids)} exempt")
        return ", ".join(bits)


# --- Lateness ---


def late_days(lateness_seconds: int, grace_minutes: float = 0.0) -> int:
    """How many late days a submission is, counting **started** days.

    One second past the deadline is one day late. Twenty minutes past is one day
    late. Twenty-five hours past is two. This is the most surprising thing the
    tool does and it is deliberate: the course policy it was written for counts
    days, not hours, and rounding down hands most of a day's penalty back to
    anyone who submitted at 11:59pm plus a bit.
    """
    effective = lateness_seconds - (grace_minutes * 60.0)
    if effective <= 0:
        return 0
    return int(math.ceil(effective / SECONDS_PER_DAY))


def penalty_points(
    max_points: float,
    days: int,
    policy: Policy,
) -> float:
    """Points removed for ``days`` of lateness, before any floor at zero.

    The penalty is a fraction of the assignment's max points, not of the score
    earned -- a student who scored 40/100 and is one day late loses 10 points,
    not 4.
    """
    if days <= 0 or not policy.late_penalty_per_day:
        return 0.0
    charged = days
    if policy.max_late_days >= 0 and days > policy.max_late_days:
        if policy.beyond_max_late == "cap":
            charged = policy.max_late_days
        else:
            return float(max_points)  # scored as zero; see standard_policy
    return float(max_points) * float(policy.late_penalty_per_day) * charged


# --- Score assembly ---


@dataclass
class ScoreContext:
    """Everything a policy function is given about one student.

    This is the argument passed to anything registered with
    :func:`register_policy`, so it is public API: adding a field is fine,
    renaming one breaks other people's policies.
    """

    student: RosterRow
    submission: Submission | None
    raw_score: float
    max_points: float
    late_days: int
    lateness_seconds: int
    exempt: bool
    policy: Policy
    dropped: list[tuple[str, float]] = field(default_factory=list)

    @property
    def is_late(self) -> bool:
        return self.late_days > 0

    @property
    def fraction(self) -> float:
        """Raw score as a fraction of max points, or 0 when max is 0."""
        if not self.max_points:
            return 0.0
        return self.raw_score / self.max_points


@dataclass
class PolicyOutcome:
    """What a policy function returns: a score, and why."""

    score: float
    notes: list[str] = field(default_factory=list)

    @classmethod
    def coerce(cls, value: "PolicyOutcome | float | int", name: str) -> "PolicyOutcome":
        """Accept a bare number from a policy function, for convenience."""
        if isinstance(value, PolicyOutcome):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return cls(score=float(value))
        raise PolicyError(
            f"policy {name!r} returned {type(value).__name__}; "
            f"expected a number or a PolicyOutcome"
        )


PolicyFn = Callable[[ScoreContext], "PolicyOutcome | float"]

_REGISTRY: dict[str, PolicyFn] = {}


def register_policy(name: str, fn: PolicyFn | None = None) -> Any:
    """Register a scoring policy under ``name``.

    Usable directly or as a decorator::

        from gbmerge import register_policy

        @register_policy("half_credit_late")
        def half_credit_late(ctx):
            if ctx.exempt or not ctx.is_late:
                return ctx.raw_score
            return ctx.raw_score * 0.5

    The function takes a :class:`ScoreContext` and returns a number or a
    :class:`PolicyOutcome`. Rounding, the zero floor and the exemption list are
    applied by :func:`apply_policy` afterwards, so a policy only answers one
    question: given this score and this lateness, what is the grade?

    Re-registering a name replaces it. Pass ``fn`` directly or use this as a
    decorator; either way the function comes back.

    Raises:
        PolicyError: if ``name`` is empty or ``fn`` is not callable.
    """
    if not name or not str(name).strip():
        raise PolicyError("a policy needs a name")

    def _register(func: PolicyFn) -> PolicyFn:
        if not callable(func):
            raise PolicyError(f"policy {name!r} is not callable")
        _REGISTRY[str(name)] = func
        return func

    if fn is None:
        return _register
    return _register(fn)


def get_policy(name: str) -> PolicyFn:
    """Look up a registered policy.

    Raises:
        PolicyError: if nothing is registered under that name, listing what is.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise PolicyError(
            f"unknown policy {name!r}; registered policies are "
            + ", ".join(sorted(_REGISTRY))
        ) from None


def available_policies() -> list[str]:
    """Names of every registered policy, sorted."""
    return sorted(_REGISTRY)


@register_policy("standard")
def standard_policy(ctx: ScoreContext) -> PolicyOutcome:
    """The default: a flat penalty per started late day.

    Past ``max_late_days`` the submission scores zero rather than keeping a
    reduced score, unless ``beyond_max_late`` is ``"cap"``. Exempt students
    take no penalty at all.
    """
    if ctx.exempt:
        if ctx.is_late:
            return PolicyOutcome(
                ctx.raw_score,
                [f"exempt from late penalty ({ctx.late_days}d late)"],
            )
        return PolicyOutcome(ctx.raw_score)

    if not ctx.is_late:
        return PolicyOutcome(ctx.raw_score)

    if ctx.policy.max_late_days >= 0 and ctx.late_days > ctx.policy.max_late_days:
        if ctx.policy.beyond_max_late == "zero":
            return PolicyOutcome(
                0.0,
                [
                    f"{ctx.late_days}d late, past max_late_days="
                    f"{ctx.policy.max_late_days}: scored 0"
                ],
            )

    removed = penalty_points(ctx.max_points, ctx.late_days, ctx.policy)
    score = max(0.0, ctx.raw_score - removed)
    return PolicyOutcome(
        score,
        [f"{ctx.late_days}d late: -{removed:g} of {ctx.max_points:g}"],
    )


@register_policy("no_penalty")
def no_penalty_policy(ctx: ScoreContext) -> PolicyOutcome:
    """Take the score as-is. Lateness is recorded but costs nothing.

    Useful for a first merge you intend to check by hand, and for courses that
    handle lateness somewhere else entirely.
    """
    if ctx.is_late:
        return PolicyOutcome(
            ctx.raw_score, [f"{ctx.late_days}d late, no penalty applied"]
        )
    return PolicyOutcome(ctx.raw_score)


@register_policy("strict")
def strict_policy(ctx: ScoreContext) -> PolicyOutcome:
    """Anything late at all scores zero, unless the student is exempt."""
    if ctx.exempt or not ctx.is_late:
        return PolicyOutcome(ctx.raw_score)
    return PolicyOutcome(0.0, [f"{ctx.late_days}d late: strict policy, scored 0"])


# --- Rounding and drops ---


def apply_rounding(value: float, mode: str = "half_up", places: int = 2) -> float:
    """Round a score.

    ``half_up`` is hand-rolled through :mod:`decimal` because Python's built-in
    :func:`round` is banker's rounding -- ``round(8.5)`` is 8 -- and a student
    looking at an 8 where they expected a 9 will, correctly, ask why.

    ``places`` applies to the half_up and half_even modes.

    Raises:
        PolicyError: on an unknown mode.
    """
    if mode == "none":
        return float(value)
    try:
        dec = Decimal(str(float(value)))
    except (InvalidOperation, ValueError):
        raise PolicyError(f"cannot round non-numeric score {value!r}") from None

    if mode == "int":
        return float(dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if mode == "tenth":
        return float(dec.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    if mode == "half_up":
        quantum = Decimal(1).scaleb(-max(0, int(places)))
        return float(dec.quantize(quantum, rounding=ROUND_HALF_UP))
    if mode == "half_even":
        quantum = Decimal(1).scaleb(-max(0, int(places)))
        return float(dec.quantize(quantum, rounding=ROUND_HALF_EVEN))
    raise PolicyError(f"unknown rounding mode {mode!r} (known: {', '.join(ROUNDING_MODES)})")


def drop_lowest_questions(
    question_scores: Mapping[str, float | None],
    count: int,
) -> tuple[float, list[tuple[str, float]]]:
    """Total the question scores, dropping the ``count`` lowest.

    Unattempted questions (``None``) are dropped first, since a blank counts as a
    zero for totalling and dropping it is what a student would want. Returns
    ``(total, dropped)``, where ``dropped`` is ``(question header, score)`` pairs.

    Note:
        This totals the *question* columns, so it ignores any manual adjustment
        Gradescope shows only in ``Total Score``: a grader's bonus or override is
        thrown away. ``gbmerge check`` reports exports where the questions do not
        add up to the stated total.
    """
    values = [(header, 0.0 if score is None else float(score)) for header, score in question_scores.items()]
    if not values:
        return 0.0, []
    if count <= 0:
        return sum(score for _, score in values), []

    order = sorted(values, key=lambda pair: (pair[1], pair[0]))
    dropped = order[: min(count, len(order))]
    dropped_headers = {header for header, _ in dropped}
    total = sum(score for header, score in values if header not in dropped_headers)
    return total, dropped


def raw_score_for(submission: Submission, policy: Policy) -> tuple[float, list[tuple[str, float]]]:
    """The pre-penalty score for a submission, and anything dropped.

    With ``drop_lowest`` at 0 this is Gradescope's ``Total Score`` exactly.
    Above 0 the questions are re-totalled here.
    """
    if policy.drop_lowest > 0 and submission.question_scores:
        return drop_lowest_questions(submission.question_scores, policy.drop_lowest)
    if submission.total_score is not None:
        return float(submission.total_score), []
    if submission.question_scores:
        total, _ = drop_lowest_questions(submission.question_scores, 0)
        return total, []
    return 0.0, []


def max_points_for(submission: Submission | None, policy: Policy, fallback: float) -> float:
    """Max points for the assignment, after any drops.

    Dropping a question lowers the denominator too; otherwise dropping your
    worst question would still cost you its points.
    """
    if submission is None:
        return fallback
    stated = submission.max_points if submission.max_points is not None else fallback
    if policy.drop_lowest <= 0 or not submission.question_scores:
        return float(stated or 0.0)

    # We do not always know a question's max from the header, so fall back to
    # an even split of the stated total across the questions. It is a guess,
    # and it is why drop_lowest and a hand-built export are a bad combination.
    per_question_max: list[float] = []
    for header in submission.question_scores:
        from .roster import question_points  # local import: display helper only

        points = question_points(header)
        per_question_max.append(points if points is not None else 0.0)
    known = [p for p in per_question_max if p > 0]
    if len(known) == len(per_question_max) and known:
        removed = sum(sorted(known)[: policy.drop_lowest])
        return max(0.0, float(stated or 0.0) - removed)
    if stated and per_question_max:
        share = float(stated) / len(per_question_max)
        return max(0.0, float(stated) - share * policy.drop_lowest)
    return float(stated or 0.0)


# --- Results ---


@dataclass
class ScoredStudent:
    """One student's outcome: what we will write, and how we got there."""

    student: RosterRow
    submission: Submission | None
    action: str  # "write", "skip"
    raw_score: float | None
    final_score: float | None
    max_points: float
    late_days: int = 0
    lateness_seconds: int = 0
    penalty: float = 0.0
    exempt: bool = False
    policy_name: str = ""
    dropped: list[tuple[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when this row will put a value in the upload file."""
        return self.action == "write" and self.final_score is not None

    def to_dict(self) -> dict[str, Any]:
        """A flat JSON-serializable record. This is the audit log line."""
        return {
            "sis_id": self.student.sis_id,
            "name": self.student.name,
            "action": self.action,
            "raw_score": self.raw_score,
            "final_score": self.final_score,
            "max_points": self.max_points,
            "late_days": self.late_days,
            "lateness": format_lateness(self.lateness_seconds),
            "penalty": round(self.penalty, 4),
            "exempt": self.exempt,
            "policy": self.policy_name,
            "dropped": [{"question": q, "score": s} for q, s in self.dropped],
            "source": self.submission.source if self.submission else None,
            "notes": list(self.notes),
        }


@dataclass
class ScoreSheet:
    """The scored roster: one :class:`ScoredStudent` per student, in file order."""

    rows: list[ScoredStudent] = field(default_factory=list)
    policy: Policy = field(default_factory=Policy)
    warnings: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.rows)

    @property
    def written(self) -> list[ScoredStudent]:
        return [r for r in self.rows if r.changed]

    @property
    def skipped(self) -> list[ScoredStudent]:
        return [r for r in self.rows if r.action == "skip"]

    @property
    def late(self) -> list[ScoredStudent]:
        return [r for r in self.rows if r.late_days > 0]

    @property
    def penalized(self) -> list[ScoredStudent]:
        return [r for r in self.rows if r.penalty > 0]

    @property
    def exempted(self) -> list[ScoredStudent]:
        return [r for r in self.rows if r.exempt]

    @property
    def zeros(self) -> list[ScoredStudent]:
        return [r for r in self.rows if r.changed and (r.final_score or 0) == 0]

    def summary(self) -> dict[str, Any]:
        """Counts for the dry-run banner and ``--json``."""
        return {
            "students": len(self.rows),
            "writing": len(self.written),
            "skipping": len(self.skipped),
            "late": len(self.late),
            "penalized": len(self.penalized),
            "exempt": len(self.exempted),
            "zeros": len(self.zeros),
            "policy": self.policy.describe(),
        }


def apply_policy(
    roster: Roster,
    report: MatchReport,
    policy: Policy | None = None,
    *,
    on_missing: str = "skip",
    max_points: float | None = None,
) -> ScoreSheet:
    """Score every student on the roster against a match report.

    Args:
        roster: The loaded roster. Sentinel rows are not scored.
        report: Output of :func:`~gbmerge.matching.match_students`.
        policy: Settings and policy name. Defaults to 10% per started day, three
            days, then zero.
        on_missing: A roster student with no submission -- ``"skip"`` writes
            nothing and leaves the LMS value alone, ``"zero"`` writes a 0,
            ``"fail"`` raises.
        max_points: Max points, if the exports do not say.

    Returns:
        A :class:`ScoreSheet` in roster order.

    Raises:
        PolicyError: on an unknown policy or ``on_missing`` value, or on a
            missing submission when ``on_missing="fail"``.
    """
    policy = policy or Policy()
    if on_missing not in ON_MISSING:
        raise PolicyError(
            f"unknown --on-missing value {on_missing!r} (known: {', '.join(ON_MISSING)})"
        )
    fn = get_policy(policy.name)
    matches = report.by_student_index
    sheet = ScoreSheet(policy=policy)

    fallback_max = float(max_points or 0.0)
    if not fallback_max:
        for match in report.matches:
            if match.submission.max_points:
                fallback_max = float(match.submission.max_points)
                break

    for student in roster.students:
        match = matches.get(student.index)
        exempt = policy.is_exempt(student)

        if match is None:
            if on_missing == "fail":
                raise PolicyError(
                    f"no submission for {student.label}; pass --on-missing skip "
                    f"or --on-missing zero to continue"
                )
            zeroed = on_missing == "zero"
            sheet.rows.append(
                ScoredStudent(
                    student=student,
                    submission=None,
                    action="write" if zeroed else "skip",
                    raw_score=None,
                    final_score=(
                        apply_rounding(0.0, policy.rounding, policy.round_places)
                        if zeroed
                        else None
                    ),
                    max_points=fallback_max,
                    exempt=exempt,
                    policy_name=policy.name,
                    notes=[
                        "no submission: scored 0 (--on-missing zero)"
                        if zeroed
                        else "no submission: left as-is in the LMS (--on-missing skip)"
                    ],
                )
            )
            continue

        submission = match.submission
        raw, dropped = raw_score_for(submission, policy)
        points = max_points_for(submission, policy, fallback_max)
        days = late_days(submission.lateness_seconds, policy.grace_minutes)

        ctx = ScoreContext(
            student=student,
            submission=submission,
            raw_score=raw,
            max_points=points,
            late_days=days,
            lateness_seconds=submission.lateness_seconds,
            exempt=exempt,
            policy=policy,
            dropped=dropped,
        )
        try:
            outcome = PolicyOutcome.coerce(fn(ctx), policy.name)
        except PolicyError:
            raise
        except Exception as exc:  # a third-party policy blew up
            raise PolicyError(
                f"policy {policy.name!r} failed on {student.label}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        final = max(0.0, float(outcome.score))
        final = apply_rounding(final, policy.rounding, policy.round_places)

        notes = list(outcome.notes)
        if match.confidence < 1.0:
            notes.append(f"matched by {match.strategy} at {match.confidence:.2f} confidence")
        if dropped:
            notes.append(
                "dropped " + ", ".join(f"{q} ({s:g})" for q, s in dropped)
            )
        if submission.is_missing:
            notes.append("Gradescope has a row but no submission")

        sheet.rows.append(
            ScoredStudent(
                student=student,
                submission=submission,
                action="write",
                raw_score=raw,
                final_score=final,
                max_points=points,
                late_days=days,
                lateness_seconds=submission.lateness_seconds,
                penalty=max(0.0, raw - float(outcome.score)),
                exempt=exempt,
                policy_name=policy.name,
                dropped=dropped,
                notes=notes,
            )
        )

    if policy.drop_lowest and sheet.rows:
        sheet.warnings.append(
            f"drop_lowest={policy.drop_lowest}: totals are re-computed from the "
            f"question columns and will not match Gradescope's Total Score"
        )
    missing_exempt = policy.exempt_ids - {r.student.sis_id for r in sheet.rows}
    if missing_exempt:
        # Almost always a typo'd id, or an id copied from the email column.
        sheet.warnings.append(
            "exempt_ids not found on the roster: " + ", ".join(sorted(missing_exempt))
        )
    return sheet


def load_exempt_file(path: str, *, encoding: str | None = None) -> list[str]:
    """Read a file of exempt LMS ids, one per line.

    Blank lines and ``#`` comments are ignored, as is anything after a comma, so
    a pasted two-column list of id and name works.

    Raises:
        PolicyError: if the file cannot be read.
    """
    from .roster import read_text  # local import to keep the module graph flat

    try:
        text, _ = read_text(path, encoding)
    except GbmergeError as exc:
        raise PolicyError(f"cannot read exempt file: {exc}") from None

    ids: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        first = line.split(",", 1)[0].strip()
        if first:
            ids.append(normalize_sis_id(first))
    return ids


def merge_exempt_ids(
    config_ids: Iterable[Any],
    flag_ids: Sequence[str] = (),
    exempt_file: str | None = None,
    *,
    encoding: str | None = None,
) -> list[str]:
    """Combine exempt ids from the config, ``--exempt-id`` and ``--exempt-file``.

    The three sources add together rather than overriding each other, because an
    accommodation that disappears when you pass an unrelated flag is a bug with a
    person's name on it.
    """
    combined: list[str] = [normalize_sis_id(v) for v in config_ids or () if str(v).strip()]
    combined.extend(normalize_sis_id(v) for v in flag_ids if str(v).strip())
    if exempt_file:
        combined.extend(load_exempt_file(exempt_file, encoding=encoding))
    seen: set[str] = set()
    ordered: list[str] = []
    for value in combined:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
