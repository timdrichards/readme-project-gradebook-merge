"""Command line interface.

Four subcommands:

``merge``
    Do the thing. Loads the roster and the exports, matches, scores, writes the
    upload CSV. ``--dry-run`` stops before writing and prints what it would do.

``check``
    Look at the same inputs and complain about everything suspicious without
    touching anything. Run this first. It is the only command that validates
    the assignment name before using it.

``match-report``
    Just the matching, in detail: who matched how, who did not match, and a
    guess at why.

``init-config``
    Write a starter ``gbmerge.yml``.

Exit codes: 0 fine, 1 something we refused to do, 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .matching import explain_unmatched, parse_match_on
from .policy import (
    Policy,
    ROUNDING_MODES,
    available_policies,
    merge_exempt_ids,
)
from .roster import (
    ASSIGNMENT_ID_RE,
    Config,
    GbmergeError,
    detect_excel_damage,
    default_config_text,
    load_config,
)
from .writer import MergeJob, summarize_exports

PROG = "gbmerge"

EPILOG = """\
examples:
  gbmerge check --roster roster.csv --assignment "Homework 4 (884213)"
  gbmerge merge --roster roster.csv --assignment "Homework 4 (884213)" --dry-run
  gbmerge merge --roster roster.csv --assignment "Homework 4 (884213)" \\
      --exports exports/hw4_scores.csv --out upload.csv --audit-log audit.jsonl

the assignment name is the LMS column header, quoted, including the id in
brackets. it is not the name of the gradescope assignment.
"""


# --- Argument types ---


def percent_or_fraction(text: str) -> float:
    """Parse ``--late-penalty``: ``0.10``, ``.1`` and ``10%`` all mean 10%.

    Bare numbers above 1 are rejected rather than silently divided by 100.
    ``--late-penalty 10`` meaning a thousand percent is a typo we can catch,
    and the config validator says the same thing about the config key.
    """
    value = text.strip()
    if value.endswith("%"):
        try:
            return float(value[:-1]) / 100.0
        except ValueError:
            raise argparse.ArgumentTypeError(f"cannot read {text!r} as a percentage") from None
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"cannot read {text!r} as a number; try 0.10 or 10%"
        ) from None
    if number > 1:
        raise argparse.ArgumentTypeError(
            f"{text} is more than 100%; the penalty is a fraction, so ten percent "
            f"is 0.10 (or write 10%)"
        )
    if number < 0:
        raise argparse.ArgumentTypeError("the late penalty cannot be negative")
    return number


def non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text} must be 0 or more")
    return value


def match_on_list(text: str) -> list[str]:
    """``--match-on sis_id,email`` -> ``["sis_id", "email"]``."""
    from .matching import MatchError

    try:
        return parse_match_on(text)
    except MatchError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


# --- Parsers ---


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Flags every subcommand understands."""
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file to use. default: the first gbmerge.yml found here or "
        "in a parent directory",
    )
    parser.add_argument(
        "--encoding",
        metavar="NAME",
        help="force an encoding for every input file, e.g. cp1252 for files "
        "that have been through Excel. default: try utf-8, then cp1252",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print machine-readable output instead of the human version",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show every row rather than the first twenty, and say what is "
        "being ignored",
    )


def _add_inputs(parser: argparse.ArgumentParser, *, assignment_required: bool) -> None:
    """The roster/exports/assignment trio."""
    parser.add_argument(
        "--roster",
        required=True,
        metavar="PATH",
        help="the LMS gradebook export CSV. this is the file from the Export "
        "button, not the 'grades' download -- they have different columns",
    )
    parser.add_argument(
        "--exports",
        metavar="PATH",
        help="a Gradescope export CSV, or a directory of them. default: the "
        "exports_dir config key, which is 'exports'",
    )
    parser.add_argument(
        "--assignment",
        required=assignment_required,
        metavar="NAME",
        help="the roster column to write into, exactly as the LMS spells it, "
        'including the id in brackets: "Homework 4 (884213)". quote it',
    )


def _add_matching(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--match-on",
        type=match_on_list,
        metavar="LIST",
        help="comma-separated strategies, in order: sis_id, email, name. "
        "default: sis_id. email compares the local part only",
    )
    parser.add_argument(
        "--fuzzy",
        action="store_true",
        default=None,
        help="allow near-miss name matching. only affects the name strategy, "
        "and every fuzzy match is listed in the report. check them",
    )
    parser.add_argument(
        "--on-ambiguous",
        choices=("report", "skip", "first", "fail"),
        help="when one submission matches two students: report (default, "
        "listed but not merged), skip, first, or fail",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Merge Gradescope exports into an LMS gradebook upload CSV.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- merge ------------------------------------------------------------
    merge = subparsers.add_parser(
        "merge",
        help="write the upload CSV",
        description="Merge exports into an upload CSV. Writes real grades.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(merge)
    _add_inputs(merge, assignment_required=True)
    _add_matching(merge)
    merge.add_argument(
        "--out",
        metavar="PATH",
        help="where to write the upload CSV. default: the out config key, "
        "which is upload.csv",
    )
    merge.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="do everything except write. prints the same summary and the list "
        "of cells that would change",
    )
    merge.add_argument(
        "--policy",
        metavar="NAME",
        help="scoring policy to use. built in: " + ", ".join(available_policies()),
    )
    merge.add_argument(
        "--late-penalty",
        type=percent_or_fraction,
        metavar="FRACTION",
        help="penalty per started late day, as a fraction or a percentage: "
        "0.10 or 10%%. default 0.10",
    )
    merge.add_argument(
        "--max-late-days",
        type=non_negative_int,
        metavar="N",
        help="how many late days are accepted. past this, the submission "
        "scores zero unless beyond_max_late is 'cap'. default 3",
    )
    merge.add_argument(
        "--drop-lowest",
        type=non_negative_int,
        metavar="N",
        help="drop the N lowest question scores before totalling. above 0 the "
        "total will not match Gradescope's Total Score. default 0",
    )
    merge.add_argument(
        "--round",
        dest="rounding",
        choices=ROUNDING_MODES,
        help="how to round the final score. default half_up",
    )
    merge.add_argument(
        "--exempt-id",
        action="append",
        default=None,
        metavar="ID",
        help="LMS id exempt from the late penalty (accommodations). repeatable. "
        "adds to the config's exempt_ids rather than replacing it",
    )
    merge.add_argument(
        "--exempt-file",
        metavar="PATH",
        help="file of exempt LMS ids, one per line, # for comments",
    )
    merge.add_argument(
        "--on-missing",
        choices=("skip", "zero", "fail"),
        help="a roster student with no submission: skip (default, writes "
        "nothing and leaves the LMS value alone), zero, or fail",
    )
    merge.add_argument(
        "--audit-log",
        metavar="PATH",
        help="append a JSONL record of this merge. nothing else records why a "
        "score came out the way it did",
    )
    merge.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="do not ask before writing. required when there is no terminal",
    )

    # -- check ------------------------------------------------------------
    check = subparsers.add_parser(
        "check",
        help="validate the inputs without writing anything",
        description=(
            "Read the roster, the exports and the config, and report anything "
            "that looks wrong. Writes nothing, ever."
        ),
    )
    _add_common(check)
    _add_inputs(check, assignment_required=False)
    _add_matching(check)
    check.add_argument(
        "--drop-lowest",
        type=non_negative_int,
        metavar="N",
        help="check totals as if this many questions were dropped",
    )

    # -- match-report -----------------------------------------------------
    report = subparsers.add_parser(
        "match-report",
        help="show how students matched, and who did not",
        description=(
            "Match submissions to students and print the result. No scoring, no writing."
        ),
    )
    _add_common(report)
    _add_inputs(report, assignment_required=False)
    _add_matching(report)
    report.add_argument(
        "--explain",
        action="store_true",
        help="guess at why each unmatched submission failed to match",
    )

    # -- init-config ------------------------------------------------------
    init = subparsers.add_parser(
        "init-config",
        help="write a starter config file",
        description="Write a gbmerge.yml with every key and its default.",
    )
    init.add_argument(
        "--out",
        default="gbmerge.yml",
        metavar="PATH",
        help="where to write it. default gbmerge.yml",
    )
    init.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="overwrite an existing config file",
    )
    init.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS)
    init.add_argument("--json", action="store_true", dest="as_json", help=argparse.SUPPRESS)

    return parser


# --- Config assembly ---


def config_from_args(args: argparse.Namespace) -> Config:
    """Load the config file and layer the flags on top.

    Flags win over the file, the file wins over the built-in defaults. A flag
    that was not passed is ``None`` and does not count as a value, so
    ``--late-penalty 0`` can turn the penalty off without being mistaken for
    "not specified".
    """
    config = load_config(getattr(args, "config", None))

    overrides: dict[str, Any] = {
        "encoding": getattr(args, "encoding", None),
        "match_on": getattr(args, "match_on", None),
        "fuzzy": getattr(args, "fuzzy", None),
        "on_ambiguous": getattr(args, "on_ambiguous", None),
        "on_missing": getattr(args, "on_missing", None),
        "policy": getattr(args, "policy", None),
        "late_penalty_per_day": getattr(args, "late_penalty", None),
        "max_late_days": getattr(args, "max_late_days", None),
        "drop_lowest": getattr(args, "drop_lowest", None),
        "round": getattr(args, "rounding", None),
        "out": getattr(args, "out", None),
        "audit_log": getattr(args, "audit_log", None),
        "exports_dir": getattr(args, "exports", None),
    }
    merged = config.with_overrides(overrides)

    # Exemptions add up across sources; they never replace each other.
    exempt_ids = merge_exempt_ids(
        merged.get("exempt_ids", []),
        getattr(args, "exempt_id", None) or (),
        getattr(args, "exempt_file", None),
        encoding=merged.get("encoding"),
    )
    merged["exempt_ids"] = exempt_ids

    problems = merged.validate()
    if problems:
        raise GbmergeError("; ".join(problems))
    return merged


def job_from_args(args: argparse.Namespace, config: Config) -> MergeJob:
    """Build a :class:`~gbmerge.writer.MergeJob` from parsed arguments."""
    return MergeJob(
        roster=args.roster,
        exports=getattr(args, "exports", None) or config.get("exports_dir", "exports"),
        assignment=getattr(args, "assignment", None) or "",
        config=config,
    )


def _confirm(prompt: str) -> bool:
    """Ask before writing. Anything but y/yes is no."""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# --- Commands ---


def cmd_merge(args: argparse.Namespace) -> int:
    """Run a merge, or a dry run of one."""
    config = config_from_args(args)
    job = job_from_args(args, config)

    gb = job.load()
    if args.verbose:
        print(f"roster:  {gb.roster.path} ({len(gb.roster.students)} students, "
              f"{len(gb.roster.sentinels)} non-student rows, {gb.roster.encoding})")
        print(summarize_exports(gb.exports))
        if config.source:
            print(f"config:  {config.source}")
        print(f"policy:  {Policy.from_config(config).describe()}")

    if not gb.exports:
        raise GbmergeError(
            f"no Gradescope exports found in {job.exports!r} "
            f"(looking for {config.get('exports_glob')})"
        )

    # No validation of the assignment name here on purpose -- see MergeJob.plan.
    # Run 'gbmerge check' if you want the name checked before it is used.
    plan = job.plan()

    if args.as_json:
        payload = plan.summary()
        payload["changes"] = [
            {
                "sis_id": c.student.sis_id,
                "name": c.student.name,
                "before": c.before,
                "after": c.after,
            }
            for c in plan.changes
        ]
        payload["warnings"] = list(plan.warnings)
        payload["dry_run"] = bool(args.dry_run)
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(plan.format_preview(verbose=args.verbose))

    if args.dry_run:
        if not args.as_json:
            print()
            print("dry run: nothing written.")
        return 0

    destination = Path(args.out or config.get("out") or "upload.csv")
    if not args.yes:
        if not sys.stdin.isatty():
            raise GbmergeError(
                "refusing to write without --yes when there is no terminal to ask at"
            )
        overwriting = sum(1 for c in plan.changes if not c.is_new)
        question = f"Write {len(plan.changes)} changed grades to {destination}?"
        if overwriting:
            question = (
                f"Write {len(plan.changes)} changed grades to {destination}, "
                f"replacing {overwriting} existing grades?"
            )
        if not _confirm(question):
            print("nothing written.")
            return 1

    meta = {
        "command": " ".join(sys.argv),
        "config_file": str(config.source) if config.source else None,
        "dry_run": False,
    }
    written = job.write(plan, destination, audit_log=args.audit_log, meta=meta)
    log = args.audit_log or config.get("audit_log")
    print(f"wrote {written} ({len(plan.changes)} changed, {len(plan.rows)} rows)")
    if log:
        print(f"audit log appended to {log}")
    else:
        print("no audit log written (use --audit-log to keep a record)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate the inputs. Returns 1 if anything is wrong enough to stop for."""
    config = config_from_args(args)
    job = job_from_args(args, config)

    problems: list[str] = []
    notes: list[str] = []

    gb = job.load()
    roster = gb.roster
    notes.append(
        f"roster {roster.path}: {len(roster.students)} students, "
        f"{len(roster.sentinels)} non-student rows kept, decoded as {roster.encoding}"
    )
    for warning in roster.warnings:
        problems.append(f"roster: {warning}")

    if not gb.exports:
        problems.append(
            f"no exports found in {job.exports!r} matching " f"{config.get('exports_glob')!r}"
        )
    for export in gb.exports:
        notes.append(
            f"export {export.path}: {len(export)} submissions, "
            f"{len(export.questions)} questions, decoded as {export.encoding}"
        )
        for warning in export.warnings:
            problems.append(f"export: {warning}")
        for warning in detect_excel_damage(
            export.fieldnames,
            [{q.header: "" for q in export.questions}],
            export.id_column,
        ):
            if "no assignment column" not in warning:
                problems.append(f"export {export.path.name}: {warning}")

        # A question total that does not match the stated Total Score usually
        # means a manual adjustment in Gradescope, which drop_lowest throws
        # away without saying so.
        if config.get("drop_lowest"):
            mismatched = 0
            for sub in export.submissions:
                if sub.total_score is None:
                    continue
                total = sum(v for v in sub.question_scores.values() if v is not None)
                if abs(total - sub.total_score) > 0.001:
                    mismatched += 1
            if mismatched:
                problems.append(
                    f"export {export.path.name}: {mismatched} submissions where "
                    f"the question columns do not add up to Total Score; with "
                    f"drop_lowest set, those adjustments will be lost"
                )

    # The assignment column: the one thing merge will not check for you.
    assignment = getattr(args, "assignment", None)
    if assignment:
        if assignment in roster.fieldnames:
            notes.append(f"assignment column {assignment!r}: found")
            if not ASSIGNMENT_ID_RE.search(assignment):
                problems.append(
                    f"assignment column {assignment!r} has no '(123456)' id on "
                    f"the end; the roster column usually does"
                )
        else:
            suggestions = roster.suggest_columns(assignment)
            message = f"no column named {assignment!r} in {roster.path}"
            if suggestions:
                message += "\n    closest matches:\n" + "\n".join(
                    f"      {s!r}" for s in suggestions[:5]
                )
                if any(
                    ASSIGNMENT_ID_RE.sub("", s).strip() == assignment.strip()
                    for s in suggestions
                ):
                    message += (
                        "\n    (the roster's column name carries an id in "
                        "brackets, and the match has to be exact)"
                    )
            else:
                message += "\n    the roster's assignment columns are:\n" + "\n".join(
                    f"      {c!r}" for c in roster.assignment_columns()[:15]
                )
            problems.append(message)
    else:
        notes.append(
            "no --assignment given; skipping the assignment column check "
            "(this is the check worth running)"
        )

    report = job.match()
    notes.append(
        f"matching: {len(report.matches)} of {report.roster_size} students "
        f"matched from {report.submission_count} submissions"
    )
    if report.unmatched_submissions:
        problems.append(
            f"{len(report.unmatched_submissions)} submissions did not match any "
            f"student; run 'gbmerge match-report --explain' to see why"
        )
    if report.ambiguous:
        problems.append(f"{len(report.ambiguous)} submissions matched more than one student")
    if report.unmatched_students:
        level = problems if config.get("on_missing") == "fail" else notes
        level.append(
            f"{len(report.unmatched_students)} students have no submission; "
            f"with --on-missing {config.get('on_missing')} "
            + (
                "they will be written as 0"
                if config.get("on_missing") == "zero"
                else "they will be left as they are in the LMS"
            )
        )

    policy = Policy.from_config(config)
    notes.append(f"policy: {policy.describe()}")
    if policy.exempt_ids:
        roster_ids = set(roster.by_sis_id())
        missing = sorted(policy.exempt_ids - roster_ids)
        if missing:
            problems.append(
                "exempt_ids not on the roster: "
                + ", ".join(missing)
                + " (these are LMS ids, not emails or Gradescope SIDs)"
            )
    if config.source is None:
        notes.append(
            "no config file in use; built-in defaults apply. "
            "'gbmerge init-config' writes one"
        )
    else:
        notes.append(f"config: {config.source}")

    if args.as_json:
        print(
            json.dumps(
                {"problems": problems, "notes": notes, "ok": not problems},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for note in notes:
            print(f"ok    {note}")
        for problem in problems:
            print(f"PROBLEM  {problem}")
        print()
        print(
            f"{len(problems)} problems, {len(notes)} checks passed"
            if problems
            else "no problems found"
        )
    return 1 if problems else 0


def cmd_match_report(args: argparse.Namespace) -> int:
    """Print the match report and nothing else."""
    config = config_from_args(args)
    job = job_from_args(args, config)
    gb = job.load()
    report = job.match()

    if args.as_json:
        payload = report.to_dict()
        if args.explain:
            payload["explanations"] = explain_unmatched(report, gb.roster)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(report.format_text(verbose=args.verbose))
    if args.explain:
        explanations = explain_unmatched(report, gb.roster)
        if explanations:
            print()
            print("why they did not match:")
            for line in explanations:
                print(f"  {line}")
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    """Write a starter config file."""
    destination = Path(args.out)
    if destination.exists() and not args.yes:
        raise GbmergeError(f"{destination} already exists (pass --yes to overwrite)")
    try:
        destination.write_text(default_config_text(), encoding="utf-8")
    except OSError as exc:
        raise GbmergeError(f"cannot write {destination}: {exc}") from None
    print(f"wrote {destination}")
    print("every key in it is a default; delete the ones you do not need.")
    return 0


COMMANDS = {
    "merge": cmd_merge,
    "check": cmd_check,
    "match-report": cmd_match_report,
    "init-config": cmd_init_config,
}


# --- Entry point ---


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code.

    Note:
        Unknown arguments are ignored rather than rejected, because the course
        wrapper scripts pass flags this tool has never heard of; ``--verbose``
        lists what was dropped. This is also why a mis-quoted assignment name --
        ``--assignment Homework 4`` -- loses the ``4`` here and fails much later
        with ``KeyError: 'Homework'``.
    """
    parser = build_parser()
    args, extras = parser.parse_known_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    if extras and getattr(args, "verbose", False):
        print(f"ignoring extra arguments: {' '.join(extras)}", file=sys.stderr)

    handler = COMMANDS[args.command]
    try:
        return handler(args)
    except GbmergeError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; nothing written.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
