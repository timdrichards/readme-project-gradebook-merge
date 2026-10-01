# Start here — gradebook-merge

## What this project is

**gradebook-merge** is a Python tool that takes score exports from Gradescope,
matches them against a course roster exported from the LMS, applies a late
penalty, and writes a CSV that can be uploaded back into the gradebook. It
installs a command called `gbmerge`, and it can also be imported and driven
from Python.

It was written by one TA for one course. Four other TAs now use it. It has no
documentation of any kind, and it writes real grades for real students.

**Your job:** write the README.

## Who you are writing for

A new teaching assistant who has been handed this tool and told to use it this
week. They are not a strong programmer, they are working against a deadline,
and if they get it wrong, 340 students get the wrong grade.

There is also a second reader, further down the page: another TA who wants to
call this from their own script instead of from the terminal, and who needs to
know what the package exports and how to extend it.

This is the project with consequences. That should change how you write.

## What's in this folder

| File | What it is |
|---|---|
| `src/gbmerge/cli.py` | The command line: the subcommands, every flag, and the help text |
| `src/gbmerge/roster.py` | Reading files: the roster, the Gradescope exports, encodings, column detection, and the config file format |
| `src/gbmerge/matching.py` | Deciding which submission belongs to which student, and reporting the ones it could not place |
| `src/gbmerge/policy.py` | The grading rules — lateness, drops, rounding, exemptions — and the hook for registering your own |
| `src/gbmerge/writer.py` | Building the upload CSV and the audit log; the `Gradebook` and `MergeJob` objects |
| `src/gbmerge/__init__.py` | The public API: what the package exports, and a short usage note |
| `pyproject.toml` | The manifest. Also where the command's name comes from |
| `config-example.yml` | An example config file, with the author's comments |
| `issues.md` | Five issue threads, all filed by TAs, with the author's replies |
| `ta-email.md` | An email from Tavi, a new TA, listing everywhere they got stuck |

There is no README. That is the assignment.

## A reading order that works

1. **`ta-email.md` first.** Tavi walks through their whole afternoon in order.
   It is the single most useful document here, and close to an outline of the
   README you need to write.
2. **`src/gbmerge/cli.py`.** The argument parser is the user interface. Read
   the module docstring, then `build_parser`, then the `cmd_*` functions to see
   what each subcommand actually does.
3. **`config-example.yml`.** Note what is configurable, and ask yourself how a
   new user would ever have known this file could exist.
4. **`issues.md`.** Five different TAs, five different failure modes. Each one
   is either a section of your README or a line in a troubleshooting list.
5. **`src/gbmerge/__init__.py`, then `policy.py`.** These two are where the
   library half of the README comes from: what to import, and how someone adds
   a grading rule of their own.

Read the docstrings and comments in the source. The author left notes in them
that appear nowhere else, and some of them are warnings.

## Before you start writing

- What exactly does someone type to run this, with every argument in place?
- What must be true about their files before they run it?
- What will go wrong first, and what will the error message look like when it
  does?
- What does this tool do that a user would want to know about *before* it does
  it to their gradebook?
- Which behaviour is configurable, where do the defaults live, and what are
  they?
- If someone wants to use it from Python instead of the shell, what do they
  import and what do they call?
- How would they add a grading rule the tool does not already have?

## What the README has to do

- **Follow the [Standard Readme spec](https://github.com/RichardLitt/standard-readme).**
  That means its required sections, in its order: Title, Short Description, an
  optional Long Description, Table of Contents, Install, Usage, API (this
  project has one, so it is not optional here), Contributing, License.
- **Show worked examples of both halves of the tool.**
  - *The CLI:* several real invocations with their output. More than the happy
    path — include at least one thing going wrong, and include a dry run.
  - *The library:* how to import it and use it from Python, and one example of
    extending it through the policy hook.
- **Be usable by Tavi.** Someone reading only your README, with the files in
  front of them, should be able to run this correctly the first time.

## Three rules

**Use only what is in this folder.** Some things you would want to know are not
here. That is real — the material is as incomplete as the situation.

**Where the material does not say, write that down.** Mark it as unknown, or as
a question for the maintainer. Do not invent an answer that sounds plausible;
in this project, a plausible wrong answer has a cost.

**Be honest about the limits.** There is no undo here. A README that hides that
is worse than no README at all.

## Where your README goes

This repository is your own copy of the project, made from the course template. Write your README as a file named `README.md` at the top level of this repository, next to this `START.md`, not inside `src/`.

Commit it to the `main` branch. GitHub shows `README.md` on the repository's front page, so open your repository in a browser after you commit and check that it reads the way you meant it to. Leave every other file as it is: your README describes this code, it does not change it.

Your instructor will tell you when drafts are due, and who to add as collaborators so they can read your work.
