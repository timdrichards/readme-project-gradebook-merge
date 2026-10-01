# Issues (this repo has 3 stars and 13 open issues, all from TAs)

---

**#4 -- `gbmerge: no column named 'SIS User ID' in roster.csv`**
opened by tavi-ta

Ran `gbmerge check` on my course's roster export and it dies immediately.

> **ana** commented:
> Your roster is probably the "grades" export, not the gradebook export. They
> have different column names and the grades one has no `SIS User ID` at all.
> Use the one from the Export button, and don't open it in Excel first --
> Excel rewrites the header row and turns the ids into numbers. `check` will
> usually tell you when that has happened, but not always.

---

**#7 -- Scores are off by one day of late penalty**
opened by rmoreno

A student submitted 20 minutes late and lost ten points.

> **ana** commented:
> Working as intended: the penalty is per *started* day. Twenty minutes late
> is day one. It comes off the assignment's max points, not off what they
> earned, so on a 100-point assignment one day is always ten points. Whether
> that's the right policy is up to your course; it's what the script does.
> `grace_minutes` exists in the config if you want a window.

---

**#9 -- Can I use this for a course with no Gradescope?**
opened by bright-ta-2

We use handwritten scores in a spreadsheet.

> **ana** commented:
> Should work if you make your sheet look like a Gradescope export: columns
> `SID`, `Name`, `Email`, `Total Score`, `Max Points`, then one column per
> question. If you leave out `Lateness (H:M:S)` everyone is treated as on
> time and the late penalty quietly does nothing -- `check` says so, `merge`
> only mentions it with `--verbose`. Honestly nobody has tried it.

---

**#11 -- Students who never submitted got a zero, and I passed `--on-missing skip`**
opened by rmoreno

I thought skip meant skip.

> **ana** commented:
> `--on-missing` is about students with no *row* in the export. Gradescope
> writes a row for everyone once the assignment closes, with Status `Missing`
> and a blank score, so those students match fine and score 0. The dry run
> tags them `[row in Gradescope, no submission]`. If you don't want them
> written at all, take them out of the export first. I know.

---

**#13 -- My totals don't match what Gradescope shows**
opened by tavi-ta

Every student is a few points lower than the Gradescope total.

> **ana** commented:
> You copied my config, which has `drop_lowest: 1` -- that's our course
> policy, not a default. With it set, gbmerge re-totals the question columns
> itself instead of using `Total Score`, which also throws away any manual
> adjustment a grader made in Gradescope. Set it to 0 unless your syllabus
> says otherwise.
