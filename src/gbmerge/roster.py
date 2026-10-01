"""Reading things off disk: the LMS roster, Gradescope exports, the config file.

Everything here is I/O and shape-detection; nothing here knows about late
penalties or matching. If it reads a file it lives in this module, including
the config loader, which started as five lines in merge.py and grew.

The two formats:

* The **LMS roster export** (the Export button on the gradebook page, not the
  "grades" export -- different file, different column names). One row per
  student, plus a "Points Possible" row and a "Test Student" row that are not
  students and have to survive into the upload anyway.
* The **Gradescope assignment export** (Download Grades -> CSV). One row per
  submission, keyed by ``SID``, one column per question.

-- ana
"""

from __future__ import annotations

import csv
import difflib
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


# --- Errors ---


class GbmergeError(Exception):
    """Base class for every error this package raises on purpose.

    The CLI catches this and prints ``gbmerge: <message>`` without a traceback.
    Anything that escapes as a different exception type is a bug, or a code
    path nobody has gotten around to handling.
    """


class ConfigError(GbmergeError):
    """The config file could not be parsed, or holds a value we can't use."""


class RosterError(GbmergeError):
    """The roster file is missing, unreadable, or not shaped like a roster."""


class ExportError(GbmergeError):
    """A Gradescope export is missing, unreadable, or not shaped like one."""


class EncodingError(GbmergeError):
    """A file could not be decoded with the encoding we were told to use."""


class ColumnNotFoundError(GbmergeError):
    """A column we need is not in the file.

    Carries the name we looked for and the closest spellings we did find, so
    the CLI can show them. ``candidates`` is ordered best-first.
    """

    def __init__(self, column: str, candidates: Sequence[str] = (), where: str = "file"):
        self.column = column
        self.candidates = list(candidates)
        self.where = where
        message = f"no column named {column!r} in {where}"
        if self.candidates:
            shown = ", ".join(repr(c) for c in self.candidates[:5])
            message += f"\n  closest matches: {shown}"
        super().__init__(message)


# --- Column names we expect to see ---

# Canvas/Moodle roster exports. These are the literal header strings; they are
# not normalized anywhere, because the upload has to go back with the same
# header text it came with.
ROSTER_ID_COLUMN = "SIS User ID"
ROSTER_LOGIN_COLUMN = "SIS Login ID"
ROSTER_NAME_COLUMNS = ("Student", "Student Name", "Name")

# Gradescope. Note SID, not "Student ID" -- people type "Student ID" and then
# file a bug about the roster.
GRADESCOPE_ID_COLUMN = "SID"
GRADESCOPE_EMAIL_COLUMN = "Email"
GRADESCOPE_NAME_COLUMNS = ("Name", "Student Name")
GRADESCOPE_TOTAL_COLUMN = "Total Score"
GRADESCOPE_MAX_COLUMN = "Max Points"
GRADESCOPE_LATENESS_COLUMN = "Lateness (H:M:S)"
GRADESCOPE_STATUS_COLUMN = "Status"
GRADESCOPE_SUBMITTED_COLUMN = "Submission Time"

# Columns in a roster export that are metadata, not assignments. Anything not
# in here and not one of the ID columns is treated as an assignment column.
ROSTER_META_COLUMNS = {
    "Student", "Student Name", "Name", "ID", "SIS User ID", "SIS Login ID",
    "Integration ID", "Root Account", "Section", "Sections", "Login ID",
    "Current Score", "Current Points", "Final Score", "Final Points",
    "Current Grade", "Final Grade", "Unposted Current Score",
    "Unposted Final Score", "Unposted Current Grade", "Unposted Final Grade",
}

# Gradescope columns that are not questions.
EXPORT_META_COLUMNS = {
    "Name", "First Name", "Last Name", "SID", "Email", "Sections",
    "Total Score", "Max Points", "Status", "Submission ID", "Submission Time",
    "Lateness (H:M:S)", "View Count", "Submission Count",
}

# Rows in the roster that are not people. The LMS puts a "Points Possible" row
# directly under the header and a "Test Student" row somewhere in the middle,
# and it wants both of them back when you upload. Scoring them, or dropping
# them, is how you get an import that fails with no useful message.
SENTINEL_NAMES = {"test student", "student, test", "points possible"}

# A roster assignment column looks like "Homework 4 (884213)". The number is
# the LMS's internal assignment id.
ASSIGNMENT_ID_RE = re.compile(r"\((\d{3,})\)\s*$")

# A Gradescope question column looks like "1: Proof (5.0)".
QUESTION_POINTS_RE = re.compile(r"\(([0-9]+(?:\.[0-9]+)?)\)\s*$")

DEFAULT_ENCODING = "utf-8-sig"
FALLBACK_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


# --- Low-level reading ---


def read_text(path: str | os.PathLike[str], encoding: str | None = None) -> tuple[str, str]:
    """Read a text file and return ``(text, encoding_used)``.

    With no ``encoding``, tries utf-8 (BOM-tolerant) then cp1252, which is what
    you get when someone saves the export from Excel on Windows. With an explicit
    ``encoding``, a decode failure is an error rather than something we paper
    over -- if you asked for an encoding you had a reason.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        raise GbmergeError(f"no such file: {p}") from None
    except IsADirectoryError:
        raise GbmergeError(f"{p} is a directory, not a file") from None
    except PermissionError:
        raise GbmergeError(f"cannot read {p}: permission denied") from None

    if encoding:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError) as exc:
            raise EncodingError(
                f"cannot read {p} as {encoding}: {exc}\n"
                f"  try --encoding cp1252 if the file has been through Excel"
            ) from None

    for candidate in FALLBACK_ENCODINGS:
        try:
            return raw.decode(candidate), candidate
        except UnicodeDecodeError:
            continue
    raise EncodingError(
        f"cannot decode {p} with any of {', '.join(FALLBACK_ENCODINGS)}; "
        f"pass --encoding explicitly"
    )


def read_rows(
    path: str | os.PathLike[str],
    encoding: str | None = None,
) -> tuple[list[str], list[dict[str, str]], str]:
    """Read a CSV into ``(fieldnames, rows, encoding_used)``.

    Rows are plain ``str -> str`` dicts with no type coercion. Short rows are
    padded; extra cells are kept under ``__extra__``, which usually means an
    unescaped comma in a name someone typed into the LMS by hand.
    """
    text, used = read_text(path, encoding)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = [f for f in (reader.fieldnames or [])]
    if not fieldnames:
        raise GbmergeError(f"{path} is empty (no header row)")

    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {}
        for name in fieldnames:
            value = raw.get(name)
            row[name] = "" if value is None else value
        extras = raw.get(None)
        if extras:
            # Keep them; check() reports them. Usually means an unescaped comma
            # inside a student name that someone typed into the LMS by hand.
            row["__extra__"] = ",".join(str(x) for x in extras)
        rows.append(row)
    return fieldnames, rows, used


def strip_bom(value: str) -> str:
    """Remove a UTF-8 BOM from the front of a string, if present."""
    return value.lstrip("﻿")


# --- Value normalization ---


def normalize_sis_id(value: Any) -> str:
    """Return an LMS/Gradescope id in the one form we compare against.

    Ids arrive as ``"31402118"``, as ``"31402118.0"`` or ``"3.14021e+07"`` once a
    spreadsheet has decided they are numbers, as ``"'31402118"`` once someone has
    forced them back to text, and with stray spaces. The scientific-notation case
    has already lost digits by the time we see it and is not recoverable, so it
    comes back unchanged and :func:`detect_excel_damage` complains about it.
    """
    if value is None:
        return ""
    text = str(value).strip().strip("'").strip()
    if not text:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def normalize_email(value: Any) -> str:
    """Return the local part of an email address, lowercased.

    Gradescope and the LMS disagree about the domain for the same human:
    ``ttran@cs.umass.edu`` in one, ``ttran@umass.edu`` in the other. Nobody has an
    account on one system and not the other, so the local part is the identity
    and the domain is noise. If two people ever share a local part at different
    domains we call them ambiguous rather than guessing.
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    return text.split("@", 1)[0].strip()


def normalize_name(value: Any) -> str:
    """Collapse a name to something comparable: lowercase, no punctuation.

    "Tran, Tavi" and "Tavi Tran" both become "tavi tran". Middle names are left
    alone, which is why name matching is opt-in.
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    if "," in text:
        last, _, first = text.partition(",")
        text = f"{first.strip()} {last.strip()}"
    text = re.sub(r"[^a-z\s'-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_score(value: Any) -> float | None:
    """Parse a score cell. Returns ``None`` for blanks and non-numbers.

    A blank score in Gradescope means "no submission", which is not a zero and is
    handled by ``--on-missing``. ``-``, ``N/A`` and ``EX`` are also ``None``; we
    do not guess.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "N/A", "n/a", "NA", "EX", "ex"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_lateness(value: Any) -> int:
    """Parse Gradescope's ``Lateness (H:M:S)`` column into whole seconds.

    The hours field is unbounded: four days late reads ``"96:12:04"``. Blank and
    ``"0:00:00"`` both mean on time. An unparseable value raises, because quietly
    calling a weird lateness "on time" hands a student a grade they did not
    earn.
    """
    if value is None:
        return 0
    text = str(value).strip()
    if not text or text in {"-", "N/A"}:
        return 0
    parts = text.split(":")
    if len(parts) == 2:
        parts = ["0", *parts]
    if len(parts) != 3:
        raise ExportError(f"cannot parse lateness value {text!r} (want H:M:S)")
    try:
        hours, minutes, seconds = (int(float(p)) for p in parts)
    except ValueError:
        raise ExportError(f"cannot parse lateness value {text!r} (want H:M:S)") from None
    total = hours * 3600 + minutes * 60 + seconds
    return max(0, total)


def format_lateness(seconds: int) -> str:
    """Inverse of :func:`parse_lateness`, for reports and the audit log."""
    if seconds <= 0:
        return "0:00:00"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def assignment_id(column: str) -> str | None:
    """Return the trailing ``(123456)`` id of a roster assignment column.

    ``assignment_id("Homework 4 (884213)")`` -> ``"884213"``.
    ``assignment_id("Homework 4")`` -> ``None``.
    """
    match = ASSIGNMENT_ID_RE.search(column or "")
    return match.group(1) if match else None


def question_points(column: str) -> float | None:
    """Return the max points encoded in a Gradescope question column header."""
    match = QUESTION_POINTS_RE.search(column or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


# --- Roster ---


@dataclass
class RosterRow:
    """One line of the LMS roster export, with the raw cells kept intact.

    ``data`` is the source row exactly as read. Everything we write back out
    comes from ``data``, so any column we do not understand still round-trips.
    """

    index: int
    data: dict[str, str]
    sis_id: str = ""
    login_id: str = ""
    email_key: str = ""
    name: str = ""
    name_key: str = ""
    is_sentinel: bool = False

    @property
    def label(self) -> str:
        """Something printable for reports: name plus id if we have one."""
        if self.name and self.sis_id:
            return f"{self.name} ({self.sis_id})"
        return self.name or self.sis_id or f"row {self.index + 2}"

@dataclass
class Roster:
    """The LMS roster export, parsed but not interpreted.

    ``fieldnames`` is the header in file order and ``rows`` is every row in file
    order, students and sentinels alike; the upload is written against both, so
    do not sort either. ``id_column``, ``login_column`` and ``name_column`` are
    the headers we identified for the id, login/email and display name.
    """

    path: Path
    encoding: str
    fieldnames: list[str]
    rows: list[RosterRow]
    id_column: str
    login_column: str = ""
    name_column: str = ""
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[RosterRow]:
        return iter(self.rows)

    @property
    def students(self) -> list[RosterRow]:
        """Rows that are actual people -- no Test Student, no Points Possible."""
        return [r for r in self.rows if not r.is_sentinel]

    @property
    def sentinels(self) -> list[RosterRow]:
        """The rows that are not people but have to stay in the file anyway."""
        return [r for r in self.rows if r.is_sentinel]

    def assignment_columns(self) -> list[str]:
        """Every column that looks like a gradeable assignment, in file order."""
        return [
            name
            for name in self.fieldnames
            if name not in ROSTER_META_COLUMNS and name != self.id_column
        ]

    def find_assignment_column(self, name: str) -> str:
        """Return the roster column matching ``name`` exactly.

        The match is exact, including the trailing ``(123456)`` assignment id,
        because that is what the LMS keys the import on. Two columns can legitimately
        differ only by that id -- a re-created assignment keeps its title -- so fuzzy
        matching here would write the right numbers into the wrong column and the LMS
        would accept it without complaint.

        Raises:
            ColumnNotFoundError: with the nearest spellings attached.
        """
        if name in self.fieldnames:
            return name
        raise ColumnNotFoundError(name, self.suggest_columns(name), where=str(self.path))

    def suggest_columns(self, name: str) -> list[str]:
        """Assignment columns whose names are close to ``name``, best first."""
        candidates = self.assignment_columns()
        target = (name or "").strip().lower()
        scored: list[tuple[float, str]] = []
        for column in candidates:
            base = column.lower()
            stripped = ASSIGNMENT_ID_RE.sub("", base).strip()
            ratio = max(
                difflib.SequenceMatcher(None, target, base).ratio(),
                difflib.SequenceMatcher(None, target, stripped).ratio(),
            )
            # A prefix match matters more than raw similarity: someone who
            # typed "Homework 4" wants "Homework 4 (884213)" first, not
            # "Homework 14 (884987)".
            if stripped == target or base.startswith(target):
                ratio += 0.5
            scored.append((ratio, column))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [column for ratio, column in scored if ratio > 0.45]

    def index_by(self, attribute: str) -> dict[str, list[RosterRow]]:
        """Students grouped by one of ``sis_id``, ``email_key``, ``name_key``.

        The values are lists: ids should be unique and usually are, but
        cross-listed sections produce duplicate rows, and two people can share
        an email local part across domains.
        """
        index: dict[str, list[RosterRow]] = {}
        for row in self.students:
            key = getattr(row, attribute, "")
            if key:
                index.setdefault(key, []).append(row)
        return index

    def by_sis_id(self) -> dict[str, list[RosterRow]]:
        """Students grouped by normalized LMS id."""
        return self.index_by("sis_id")

    def by_email_key(self) -> dict[str, list[RosterRow]]:
        """Students grouped by email local part."""
        return self.index_by("email_key")

    def by_name_key(self) -> dict[str, list[RosterRow]]:
        """Students grouped by normalized name."""
        return self.index_by("name_key")


def _pick_column(fieldnames: Sequence[str], candidates: Iterable[str]) -> str:
    """First of ``candidates`` present in ``fieldnames``, else ""."""
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    return ""


def _is_sentinel_row(name: str, sis_id: str) -> bool:
    """True for the rows the LMS puts in the export that are not students."""
    key = (name or "").strip().lower()
    if key in SENTINEL_NAMES:
        return True
    # The Points Possible row has no id and no login, and its name cell is
    # sometimes blank rather than "Points Possible" depending on LMS version.
    if not sis_id and key.replace(" ", "") in {"", "pointspossible"}:
        return True
    return False


def load_roster(
    path: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    id_column: str = ROSTER_ID_COLUMN,
    login_column: str | None = None,
    name_column: str | None = None,
) -> Roster:
    """Load an LMS roster export.

    Args:
        path: The gradebook **export**, not the "grades" download -- they have
            different columns and the grades download has no ``SIS User ID``.
        encoding: Force an encoding. Default: try utf-8, then cp1252.
        id_column: Student id column header. Default ``"SIS User ID"``.
        login_column: Login/email column header. Auto-detected if omitted.
        name_column: Display-name column header. Auto-detected if omitted.

    Returns:
        A :class:`Roster` with every row in file order, sentinels included.

    Raises:
        ColumnNotFoundError: if ``id_column`` is not in the header.
        RosterError: if the file has no data rows.
    """
    p = Path(path)
    fieldnames, raw_rows, used_encoding = read_rows(p, encoding)
    fieldnames = [strip_bom(f) for f in fieldnames]
    raw_rows = [{strip_bom(k): v for k, v in row.items()} for row in raw_rows]

    if id_column not in fieldnames:
        close = difflib.get_close_matches(id_column, fieldnames, n=5, cutoff=0.4)
        raise ColumnNotFoundError(id_column, close, where=str(p))
    if not raw_rows:
        raise RosterError(f"{p} has a header but no rows")

    login = login_column or _pick_column(fieldnames, (ROSTER_LOGIN_COLUMN, "Login ID", "Email"))
    display = name_column or _pick_column(fieldnames, ROSTER_NAME_COLUMNS)

    rows: list[RosterRow] = []
    for index, data in enumerate(raw_rows):
        sis_id = normalize_sis_id(data.get(id_column, ""))
        name = (data.get(display, "") if display else "").strip()
        login_value = (data.get(login, "") if login else "").strip()
        rows.append(
            RosterRow(
                index=index,
                data=data,
                sis_id=sis_id,
                login_id=login_value,
                email_key=normalize_email(login_value),
                name=name,
                name_key=normalize_name(name),
                is_sentinel=_is_sentinel_row(name, sis_id),
            )
        )

    roster = Roster(
        path=p,
        encoding=used_encoding,
        fieldnames=fieldnames,
        rows=rows,
        id_column=id_column,
        login_column=login,
        name_column=display,
    )
    roster.warnings.extend(detect_excel_damage(fieldnames, raw_rows, id_column))
    if not roster.students:
        raise RosterError(
            f"{p} has {len(rows)} rows but none of them look like students; "
            f"is this the grades export rather than the roster export?"
        )
    return roster


# --- Gradescope exports ---


@dataclass
class QuestionColumn:
    """One question column from a Gradescope export."""

    header: str
    max_points: float | None = None


@dataclass
class Submission:
    """One row of a Gradescope export: one student's work on one assignment."""

    row_number: int
    sid: str
    email: str
    email_key: str
    name: str
    name_key: str
    total_score: float | None
    max_points: float | None
    question_scores: dict[str, float | None]
    lateness_seconds: int
    submitted_at: str
    status: str
    source: str

    @property
    def is_missing(self) -> bool:
        """True when Gradescope has a row but no actual submission."""
        if self.total_score is not None:
            return False
        return all(v is None for v in self.question_scores.values())

    @property
    def label(self) -> str:
        if self.name and self.sid:
            return f"{self.name} ({self.sid})"
        return self.name or self.sid or self.email or f"row {self.row_number}"


@dataclass
class GradescopeExport:
    """A parsed Gradescope assignment export."""

    path: Path
    encoding: str
    fieldnames: list[str]
    submissions: list[Submission]
    questions: list[QuestionColumn]
    id_column: str = GRADESCOPE_ID_COLUMN
    email_column: str = GRADESCOPE_EMAIL_COLUMN
    has_lateness: bool = True
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.submissions)

    def __iter__(self) -> Iterator[Submission]:
        return iter(self.submissions)

    @property
    def name(self) -> str:
        """A human label for the export, taken from the filename.

        ``exports/hw4_scores.csv`` -> ``hw4``. This is only used in reports; it
        has nothing to do with the roster column name, and hoping the two line
        up is how people end up uploading Homework 3 into Homework 4.
        """
        stem = self.path.stem
        if stem.endswith("_scores"):
            stem = stem[: -len("_scores")]
        return stem

    @property
    def max_points(self) -> float | None:
        """Max points for the assignment, if the export states one."""
        for submission in self.submissions:
            if submission.max_points is not None:
                return submission.max_points
        totals = [q.max_points for q in self.questions if q.max_points is not None]
        return sum(totals) if totals else None

def load_export(
    path: str | os.PathLike[str],
    *,
    encoding: str | None = None,
    id_column: str = GRADESCOPE_ID_COLUMN,
    email_column: str = GRADESCOPE_EMAIL_COLUMN,
) -> GradescopeExport:
    """Load one Gradescope CSV export.

    Args:
        path: Path to the export.
        encoding: Force an encoding; default is to sniff.
        id_column: Student id column. Gradescope calls it ``SID``, not
            ``Student ID``, whatever the web UI shows.
        email_column: Email column header.

    Raises:
        ExportError: if the file has no usable identity column at all.

    Note:
        An export with no ``Lateness (H:M:S)`` column -- an assignment set up
        without a due date, or a file rebuilt by hand -- loads as if everyone was
        on time, and the late penalty silently does nothing. ``check`` says so;
        merge only mentions it under ``--verbose``.
    """
    p = Path(path)
    fieldnames, raw_rows, used_encoding = read_rows(p, encoding)
    fieldnames = [strip_bom(f) for f in fieldnames]
    raw_rows = [{strip_bom(k): v for k, v in row.items()} for row in raw_rows]

    has_id = id_column in fieldnames
    has_email = email_column in fieldnames
    if not has_id and not has_email:
        close = difflib.get_close_matches(id_column, fieldnames, n=5, cutoff=0.4)
        raise ExportError(
            f"{p} has neither {id_column!r} nor {email_column!r}; "
            f"this does not look like a Gradescope export"
            + (f"\n  did you mean: {', '.join(repr(c) for c in close)}" if close else "")
        )

    name_column = _pick_column(fieldnames, GRADESCOPE_NAME_COLUMNS)
    first_name = "First Name" if "First Name" in fieldnames else ""
    last_name = "Last Name" if "Last Name" in fieldnames else ""
    has_lateness = GRADESCOPE_LATENESS_COLUMN in fieldnames

    questions = [
        QuestionColumn(header=header, max_points=question_points(header))
        for header in fieldnames
        if header not in EXPORT_META_COLUMNS and not header.startswith("__")
    ]

    submissions: list[Submission] = []
    for index, data in enumerate(raw_rows):
        if name_column:
            name = (data.get(name_column) or "").strip()
        elif first_name or last_name:
            name = f"{data.get(first_name, '')} {data.get(last_name, '')}".strip()
        else:
            name = ""
        email = (data.get(email_column, "") if has_email else "").strip()
        submissions.append(
            Submission(
                row_number=index + 2,  # +2: one for the header, one for 1-based
                sid=normalize_sis_id(data.get(id_column, "")) if has_id else "",
                email=email,
                email_key=normalize_email(email),
                name=name,
                name_key=normalize_name(name),
                total_score=parse_score(data.get(GRADESCOPE_TOTAL_COLUMN)),
                max_points=parse_score(data.get(GRADESCOPE_MAX_COLUMN)),
                question_scores={
                    q.header: parse_score(data.get(q.header)) for q in questions
                },
                lateness_seconds=(
                    parse_lateness(data.get(GRADESCOPE_LATENESS_COLUMN))
                    if has_lateness
                    else 0
                ),
                submitted_at=(data.get(GRADESCOPE_SUBMITTED_COLUMN, "") or "").strip(),
                status=(data.get(GRADESCOPE_STATUS_COLUMN, "") or "").strip(),
                source=p.name,
            )
        )

    export = GradescopeExport(
        path=p,
        encoding=used_encoding,
        fieldnames=fieldnames,
        submissions=submissions,
        questions=questions,
        id_column=id_column,
        email_column=email_column,
        has_lateness=has_lateness,
    )
    if not has_lateness:
        export.warnings.append(
            f"{p.name} has no {GRADESCOPE_LATENESS_COLUMN!r} column; "
            f"every submission will be treated as on time"
        )
    if not has_id:
        export.warnings.append(
            f"{p.name} has no {id_column!r} column; matching will have to fall "
            f"back to email or name"
        )
    blank_ids = sum(1 for s in submissions if not s.sid)
    if has_id and blank_ids:
        export.warnings.append(
            f"{p.name}: {blank_ids} of {len(submissions)} rows have an empty "
            f"{id_column!r}; those students can only be matched by email or name"
        )
    return export


def load_exports(
    directory: str | os.PathLike[str] = "exports",
    *,
    pattern: str = "*_scores.csv",
    encoding: str | None = None,
    id_column: str = GRADESCOPE_ID_COLUMN,
    email_column: str = GRADESCOPE_EMAIL_COLUMN,
) -> list[GradescopeExport]:
    """Load every export in a directory, sorted by filename.

    The default ``pattern`` is the convention from the original course repo:
    ``hw4_scores.csv``, ``quiz2_scores.csv``. Not recursive.

    Raises:
        ExportError: if the directory does not exist.
    """
    d = Path(directory)
    if not d.exists():
        raise ExportError(f"exports directory {d} does not exist")
    if not d.is_dir():
        raise ExportError(f"{d} is not a directory")
    paths = sorted(d.glob(pattern))
    return [
        load_export(p, encoding=encoding, id_column=id_column, email_column=email_column)
        for p in paths
    ]


def resolve_exports(
    target: str | os.PathLike[str],
    *,
    pattern: str = "*_scores.csv",
    encoding: str | None = None,
    id_column: str = GRADESCOPE_ID_COLUMN,
    email_column: str = GRADESCOPE_EMAIL_COLUMN,
) -> list[GradescopeExport]:
    """Load exports from a file or a directory, whichever ``target`` is."""
    p = Path(target)
    if p.is_dir():
        return load_exports(
            p, pattern=pattern, encoding=encoding, id_column=id_column, email_column=email_column
        )
    return [load_export(p, encoding=encoding, id_column=id_column, email_column=email_column)]


# --- Damage detection ---


def detect_excel_damage(
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, str]],
    id_column: str = ROSTER_ID_COLUMN,
) -> list[str]:
    """Look for signs that a CSV has been opened and saved in a spreadsheet.

    Excel is the most common cause of a merge that runs cleanly and produces a
    file the LMS rejects: it reformats ids as numbers, drops zero padding, and
    rewrites the header row. None of that raises anything anywhere -- the file is
    still valid CSV, it just no longer means what it meant.

    Returns:
        Human-readable warnings. Empty means nothing looked wrong, not that
        nothing is wrong.
    """
    warnings: list[str] = []

    trailing = [f for f in fieldnames if f != f.strip()]
    if trailing:
        warnings.append(
            "header row has columns with leading/trailing spaces "
            f"({', '.join(repr(f) for f in trailing[:3])}); a spreadsheet has "
            "probably rewritten it"
        )

    scientific = 0
    floaty = 0
    for row in rows[:2000]:
        value = (row.get(id_column) or "").strip()
        if not value:
            continue
        if re.fullmatch(r"\d(\.\d+)?[eE][+-]?\d+", value):
            scientific += 1
        elif value.endswith(".0") and value[:-2].isdigit():
            floaty += 1
    if scientific:
        warnings.append(
            f"{scientific} values in {id_column!r} are in scientific notation "
            f"(e.g. 3.14021E+07); those ids have lost digits and cannot be "
            f"recovered from this file"
        )
    if floaty:
        warnings.append(
            f"{floaty} values in {id_column!r} end in '.0'; they are being "
            f"compared with the '.0' stripped, but the file has been through a " f"spreadsheet"
        )

    assignment_like = [
        f
        for f in fieldnames
        if f not in ROSTER_META_COLUMNS and f != id_column and f.strip()
    ]
    without_id = [f for f in assignment_like if not ASSIGNMENT_ID_RE.search(f)]
    if assignment_like and len(without_id) == len(assignment_like):
        warnings.append(
            "no assignment column has a trailing '(123456)' id; either this is "
            "not a gradebook export, or the header row has been rewritten"
        )

    if any("__extra__" in row for row in rows):
        broken = sum(1 for row in rows if "__extra__" in row)
        warnings.append(
            f"{broken} rows have more cells than the header has columns; there "
            f"is probably an unescaped comma or quote in the file"
        )

    return warnings


# --- Config ---

# Built-in defaults. Every key here can be set in the config file and most can
# be overridden by a flag. Note that drop_lowest defaults to 0: the original
# script hardcoded 1 because that was our course's policy, and copying someone
# else's config is a quiet way to inherit their grading rules.
DEFAULT_CONFIG: dict[str, Any] = {
    "roster_id_column": ROSTER_ID_COLUMN,
    "roster_login_column": ROSTER_LOGIN_COLUMN,
    "gradescope_id_column": GRADESCOPE_ID_COLUMN,
    "gradescope_email_column": GRADESCOPE_EMAIL_COLUMN,
    "exports_dir": "exports",
    "exports_glob": "*_scores.csv",
    "out": "upload.csv",
    "encoding": DEFAULT_ENCODING,
    "match_on": ["sis_id"],
    "fuzzy": False,
    "fuzzy_threshold": 0.88,
    "on_missing": "skip",
    "on_ambiguous": "report",
    "policy": "standard",
    "late_penalty_per_day": 0.10,
    "max_late_days": 3,
    "beyond_max_late": "zero",
    "grace_minutes": 0,
    "drop_lowest": 0,
    "round": "half_up",
    "round_places": 2,
    "exempt_ids": [],
    "audit_log": "",
    "keep_test_student": True,
}

CONFIG_FILENAMES = ("gbmerge.yml", "gbmerge.yaml", "config.yml")

_BOOLS = {
    "true": True,
    "yes": True,
    "on": True,
    "false": False,
    "no": False,
    "off": False,
}


class Config(dict):
    """The merged configuration: defaults, then file, then flags.

    A plain dict subclass on purpose -- it gets dumped into the audit log and
    printed by ``--json``. ``source`` is the file it came from, if any.
    """

    def __init__(self, values: dict[str, Any] | None = None, source: Path | None = None):
        super().__init__(DEFAULT_CONFIG)
        if values:
            self.update(values)
        self.source = source

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        """Return a copy with ``overrides`` applied, ignoring ``None`` values.

        ``None`` means "the flag was not passed", which is different from a
        flag passed with a falsy value: ``--late-penalty 0`` has to be able to
        turn the penalty off.
        """
        merged = Config(dict(self), source=self.source)
        for key, value in overrides.items():
            if value is None:
                continue
            merged[key] = value
        return merged

    def validate(self) -> list[str]:
        """Check value types and ranges. Returns a list of problems."""
        problems: list[str] = []

        penalty = self.get("late_penalty_per_day")
        if not isinstance(penalty, (int, float)):
            problems.append("late_penalty_per_day must be a number")
        elif not 0 <= float(penalty) <= 1:
            problems.append(
                f"late_penalty_per_day is {penalty}; it is a fraction, so 10% "
                f"is 0.10 and not 10"
            )

        max_days = self.get("max_late_days")
        if not isinstance(max_days, int) or isinstance(max_days, bool) or max_days < 0:
            problems.append("max_late_days must be a whole number of days, 0 or more")

        drop = self.get("drop_lowest")
        if not isinstance(drop, int) or isinstance(drop, bool) or drop < 0:
            problems.append("drop_lowest must be a whole number, 0 or more")

        grace = self.get("grace_minutes")
        if not isinstance(grace, (int, float)) or grace < 0:
            problems.append("grace_minutes must be a number of minutes, 0 or more")

        if self.get("beyond_max_late") not in {"zero", "cap"}:
            problems.append("beyond_max_late must be 'zero' or 'cap'")

        if self.get("on_missing") not in {"skip", "zero", "fail"}:
            problems.append("on_missing must be 'skip', 'zero' or 'fail'")

        if self.get("on_ambiguous") not in {"report", "skip", "first", "fail"}:
            problems.append("on_ambiguous must be 'report', 'skip', 'first' or 'fail'")

        if self.get("round") not in {"none", "int", "half_up", "half_even", "tenth"}:
            problems.append("round must be one of: none, int, half_up, half_even, tenth")

        match_on = self.get("match_on")
        if not isinstance(match_on, list) or not match_on:
            problems.append("match_on must be a non-empty list of strategies")
        else:
            known = {"sis_id", "email", "name"}
            unknown = [m for m in match_on if m not in known]
            if unknown:
                problems.append(
                    f"match_on has unknown strategies: {', '.join(map(str, unknown))} "
                    f"(known: sis_id, email, name)"
                )

        exempt = self.get("exempt_ids")
        if not isinstance(exempt, list):
            problems.append("exempt_ids must be a list")

        return problems

    @property
    def exempt_id_set(self) -> set[str]:
        """``exempt_ids`` normalized the same way roster ids are."""
        return {normalize_sis_id(v) for v in self.get("exempt_ids", []) if str(v).strip()}


def parse_mini_yaml(text: str) -> dict[str, Any]:
    """Parse the small subset of YAML the config file is allowed to use.

    This is not YAML. It is the part of YAML we use, hand-parsed so the tool has
    no dependencies -- installing PyYAML on the department machines needs a
    ticket. Supported: flat ``key: value`` pairs, ``#`` comments, quoted and bare
    strings, numbers, booleans, ``null``, inline lists (``[sis_id, email]``) and
    block lists (``- 31402118``).

    Anything else -- nesting, multi-line strings, anchors -- raises
    :class:`ConfigError` with a line number rather than being ignored, because a
    config key that is silently dropped is a grading policy that silently did not
    apply.
    """
    values: dict[str, Any] = {}
    current_list_key: str | None = None

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue

        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped.startswith("- "):
            if current_list_key is None:
                raise ConfigError(f"line {lineno}: list item outside of a key")
            values[current_list_key].append(_parse_scalar(stripped[2:], lineno))
            continue
        if stripped == "-":
            raise ConfigError(f"line {lineno}: empty list item")

        if indent:
            raise ConfigError(
                f"line {lineno}: indented key {stripped.split(':')[0]!r}; "
                f"this config format is flat, with no nested sections"
            )

        if ":" not in stripped:
            raise ConfigError(f"line {lineno}: expected 'key: value', got {stripped!r}")

        key, _, rest = stripped.partition(":")
        key = key.strip()
        if not key:
            raise ConfigError(f"line {lineno}: empty key")
        rest = _strip_comment(rest.strip())

        if rest == "":
            values[key] = []
            current_list_key = key
            continue

        current_list_key = None
        values[key] = _parse_value(rest, lineno)

    return values


def _strip_comment(value: str) -> str:
    """Drop a trailing ``#`` comment, respecting quotes."""
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or value[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_value(text: str, lineno: int) -> Any:
    """Parse one value: an inline list, or a scalar."""
    if text.startswith("{"):
        raise ConfigError(f"line {lineno}: inline maps are not supported by this config parser")
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        # csv.reader gets quoted commas right, which is the only hard part of
        # splitting an inline list.
        parts = next(csv.reader([inner], skipinitialspace=True))
        return [_parse_scalar(part, lineno) for part in parts if part.strip()]
    return _parse_scalar(text, lineno)


def _parse_scalar(text: str, lineno: int) -> Any:
    text = _strip_comment(text.strip())
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in _BOOLS:
        return _BOOLS[lowered]
    if lowered in {"null", "~", "none"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def find_config(start: str | os.PathLike[str] = ".") -> Path | None:
    """Look for a config file in ``start``, then in each parent directory.

    Returns the first match, or ``None``. There is no user-level or
    system-level config; this tool is run from inside a course directory.
    """
    here = Path(start).resolve()
    for directory in [here, *here.parents]:
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    search_from: str | os.PathLike[str] | None = ".",
    required: bool = False,
) -> Config:
    """Load configuration, falling back to :data:`DEFAULT_CONFIG`.

    Args:
        path: An explicit config file; missing is an error.
        search_from: Directory to search upward from when ``path`` is None.
            ``None`` skips the search and returns pure defaults.
        required: Raise if the search finds nothing.

    Returns:
        A :class:`Config` whose ``source`` is the file it came from, or None.

    Raises:
        ConfigError: on a missing file, a parse failure, or an unknown key.
    """
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
    elif search_from is not None:
        found = find_config(search_from)
        if found is None:
            if required:
                raise ConfigError(
                    "no config file found (looked for "
                    + ", ".join(CONFIG_FILENAMES)
                    + " here and in parent directories); run 'gbmerge init-config'"
                )
            return Config()
        p = found
    else:
        return Config()

    text, _ = read_text(p)
    try:
        values = parse_mini_yaml(text)
    except ConfigError as exc:
        raise ConfigError(f"{p}: {exc}") from None

    unknown = [k for k in values if k not in DEFAULT_CONFIG]
    if unknown:
        # Refuse rather than ignore. A typo'd key in a grading config is a
        # policy that did not apply, and you find out about it in a regrade
        # request six weeks later.
        suggestions = []
        for key in unknown:
            close = difflib.get_close_matches(key, list(DEFAULT_CONFIG), n=1, cutoff=0.6)
            suggestions.append(f"{key!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
        raise ConfigError(f"{p}: unknown config keys: " + ", ".join(suggestions))

    if "match_on" in values and isinstance(values["match_on"], str):
        values["match_on"] = [s.strip() for s in values["match_on"].split(",") if s.strip()]
    if "exempt_ids" in values and not isinstance(values["exempt_ids"], list):
        values["exempt_ids"] = [values["exempt_ids"]]

    config = Config(values, source=p)
    problems = config.validate()
    if problems:
        raise ConfigError(f"{p}: " + "; ".join(problems))
    return config


def default_config_text() -> str:
    """The starter config written by ``gbmerge init-config``.

    Every key, at its built-in default. ``config-example.yml`` in the repo is
    our course's version of the same file, with our values in it.
    """
    return '''\
# gbmerge.yml -- optional. Without it the built-in defaults apply.
# Every key here has a command-line equivalent; flags win over this file.

exports_dir: "exports"
exports_glob: "*_scores.csv"
out: "upload.csv"
encoding: "utf-8-sig"

# Column headers. Literally these strings, spaces and all. Gradescope's id
# column is SID; it is not "Student ID".
roster_id_column: "SIS User ID"
roster_login_column: "SIS Login ID"
gradescope_id_column: "SID"
gradescope_email_column: "Email"

# Matching, tried in order. Add "email" if your students' SIDs are blank in
# Gradescope; email matching compares the local part only, because Gradescope
# has some people as @cs.umass.edu and the roster has them as @umass.edu.
match_on: [sis_id]
fuzzy: false
fuzzy_threshold: 0.88
on_missing: "skip"          # skip / zero / fail
on_ambiguous: "report"      # report / skip / first / fail

# Grading. The penalty is applied per STARTED day: 20 minutes late is one day.
policy: "standard"
late_penalty_per_day: 0.10
max_late_days: 3
beyond_max_late: "zero"     # past max_late_days: "zero" or "cap"
grace_minutes: 0
# Above 0, totals are recomputed from the question columns and will not equal
# Gradescope's Total Score.
drop_lowest: 0
round: "half_up"            # none / int / half_up / half_even / tenth
round_places: 2

# Exempt from the late penalty (accommodations). LMS ids, not emails.
exempt_ids: []

# The LMS import wants the Test Student row back. Leave this alone.
keep_test_student: true
audit_log: ""
'''
