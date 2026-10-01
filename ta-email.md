From: tavi@example.edu
To: ana@example.edu
Subject: gbmerge -- I think I've broken the gradebook

Ana,

I'm the new TA for 187 and I was told to use your merge tool for Homework 4.
I've spent most of the afternoon on it and I want to check before I upload
anything, because this goes straight into real grades.

Where I got stuck, in order:

- I cloned it and ran `gbmerge merge` and got "the following arguments are
  required: --roster, --assignment", which took me a while to turn into an
  actual command.
- I passed the assignment as `--assignment Homework 4`, without quotes, and
  got twenty lines of traceback ending in `KeyError: 'Homework'`. Nothing in
  it mentions the assignment name.
- Then I found out the name has to include a number in brackets. I got that
  from a comment in the source. Where does that number come from?
- I have scores merging now, but two students are missing from the output and
  both of them are @cs.umass.edu in Gradescope and @umass.edu in the roster.
- There's still a "Test Student" row in my upload.csv, and a row above it with
  no name that just says 100. Do I delete those before uploading?
- One of my students has an accommodation and shouldn't get the late penalty.
  I saw `exempt_ids` in the config file you sent rmoreno. Is there a config
  file? I don't have one, and `gbmerge check` says I'm on "built-in defaults",
  which I don't know the values of.

There's a `--dry-run` in the help. Does that mean it definitely doesn't write
anything, or does it write the file and just not upload it? I'd rather see
what it's going to do before it does it to 340 people's grades. Is `check`
enough on its own?

Sorry for the long email. Happy to write any of this down for the next person.

Tavi
