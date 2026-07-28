# autosentry README demo — the whole heal loop in one take.
#
# Regenerate from the repo root:
#     PATH="$(make -s venv-info)/bin:$PATH" vhs docs/demo/demo.tape
#
# The recording runs out of /tmp/autosentry-demo rather than the repo so
# the supervisor's absolute `cwd=` in the log stays short and readable.
# Everything it needs is copied there in the hidden setup below, so the
# demo is a clean room every run — no leftover state from a prior take.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1000
Set Height 720
Set Padding 20
Set Theme "Dracula"
Set TypingSpeed 40ms

# ----- setup (not recorded) --------------------------------------------
Hide
Type "rm -rf /tmp/autosentry-demo && mkdir -p /tmp/autosentry-demo/.autosentry"
Enter
Type "cp docs/demo/train.py /tmp/autosentry-demo/"
Enter
Type "cp docs/demo/autosentry.yaml /tmp/autosentry-demo/.autosentry/"
Enter
# BATCH_SIZE has to be a real env var, not just a config value: the
# `half` keyword resolves against the supervisor's environment.
# DEMO_STEP_DELAY paces train.py so the log is readable at GIF speed.
Type "cd /tmp/autosentry-demo && export BATCH_SIZE=8 DEMO_STEP_DELAY=0.45"
Enter
Type "clear"
Enter
Show

# ----- beat 1: it catches the failure and fixes it ----------------------
Type "autosentry run"
Enter
Sleep 9s

# ----- beat 2: the fix actually worked ----------------------------------
Type `grep -E "batch_size|OutOfMemory|complete" .autosentry/logs/process.log`
Enter
Sleep 4s

# ----- beat 3: the paper trail ------------------------------------------
Type "autosentry incidents list"
Enter
Sleep 5s
