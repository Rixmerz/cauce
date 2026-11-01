# A fresh session that keeps the thread

**A fresh start that keeps the thread.** When the person wants to clear a
long session without losing what it was doing, run `locks relay` and tell
them to type `/clear`: the new session starts from their requests verbatim,
the commits made and the locks tasks still open, which report there from
then on. Never write that summary yourself; the relay reads it from the
transcript and git.
