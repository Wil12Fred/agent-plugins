---
name: verifying-a-claim
description: >
  Use before writing "verified", "blocked", "done" or "ready" about anything — a
  test run, a deploy, a fix, an absence. Fourteen ways a check passes while
  proving nothing, each one drawn from a real incident, and the questions that
  separate a check that measured the thing from one that measured nothing. Also
  use when a result looks too clean, when a search returns no hits, or when a
  gate has never once failed. Do not use for deciding *which* evidence a change
  needs, or for reading a debt register — that is `measuring-technical-debt`.
---

# Verifying a claim

Every entry below is a check that came back green, was believed, and was wrong.
None was a bug in the tool. Each was a case of measuring something adjacent to
the question and reading the answer as if it were the question.

The pattern underneath all fourteen: **a passing check proves the check passed.**
Whether it proves your claim is a separate question, and it is the one nobody
asks when the result is the one they wanted.

---

## The fourteen

**1. A green test that measured zero.** A suite reported success with every case
skipped — a missing fixture made them all skip, and skipped renders as "not
failed". Ask what the number of *executed* assertions was, not the exit code.

**2. A blocker inferred instead of run.** "This can't work because X" written
without running it. X was true and irrelevant; the thing worked. If you have not
executed it, the word is "expected", not "blocked".

**3. `--is-ancestor` lying about a squashed merge.** Checking whether a commit
reached the default branch returns false when the work was squashed on the way
in: the change is there, the commit is not. Cross-check by searching the log for
the identifier, and when the two disagree, believe the diff.

**4. An assumed default branch.** Half a comparison ran against `master` in a
repository whose default is `main`. Everything downstream was arithmetic on the
wrong baseline. Read the default branch; never assume which word it is.

**5. A hypothesis mistaken for a reproduction.** "I reproduced it" describing a
situation constructed to match the theory, not the report. A reproduction starts
from what the reporter did, not from what you think went wrong.

**6. A platform limit reported as somebody's oversight.** "They forgot to expose
this" when the platform cannot expose it. Before attributing a gap to a person,
check whether the thing is possible.

**7. A change whose diff did not contain what its tests measured.** A proposal
described behaviour its own diff did not implement: the commits went to an
integration branch and the feature branch was never pushed. The cited test run
was real — it measured a different artefact. **Prove the branch holds the
evidence**: for every commit you cite, confirm it is on the branch you are
proposing.

**8. A rule invented out of dictation noise.** A garbled phrase in a transcript
was read as a requirement and implemented. If a requirement appears only once,
in text that could be a mishearing, confirm it before building on it.

**9. A guard wired to a signal nobody sends.** A check that fires on a flag no
producer ever sets. It never triggered, so it never failed, so it looked
healthy. **A gate that has never failed is not proven; it is unmeasured.** Break
it on purpose once and watch it go red.

**10. A coverage number hiding a live defect.** High coverage over a function
whose branches were all exercised with inputs that could not distinguish right
from wrong. Coverage says a line ran, never that an assertion could have failed.

**11. A mutation that changed two things at once.** A "revert it and watch the
test fail" proof where the revert also removed a second change. The test went
red for the other reason. Mutate exactly one thing, and confirm the file's hash
moved and then moved back.

**12. An empty API field read as a negative answer.** A response field came back
empty and was reported as "the feature is off". The field is only populated
under conditions unrelated to the question. Absent is not false; find the
positive probe.

**13. A config file trusted over the running environment.** The repository's
configuration said one thing, the deployed process had a different value from a
secret. Read the environment that is running, not the file that describes it.

**14. A search that could not have matched, reported as proof of absence.**
Grepping for a term that the codebase spells differently, or in an encoding the
tool rewrites, then concluding the thing does not exist. **Prove the search can
find a positive** before trusting a negative: run it against a case you know is
there.

---

## Before you write "verified", "blocked" or "ready"

1. What exactly did I execute? Name the command and where it ran.
2. How many assertions actually *ran* — not how many passed?
3. Could this check have failed? When did it last fail, or has it never?
4. Does the artefact I tested contain the change I am claiming?
5. If I am claiming an absence, can my search find a known positive?
6. Am I reading a description of the system, or the system?
7. Did I change exactly one thing between the passing and failing runs?
8. Is "empty" being read as "false"?
9. Is this a reproduction, or a construction that matches my theory?
10. If I inferred a blocker, what happens when I actually run it?
11. Does the branch I am proposing carry the commits I cited?
12. Is the identifier I matched on the one the system uses, or the one I expect?
13. What did I *not* check, and would a reader assume I had?

The last one is the one that keeps mattering. **An audit that claims full
coverage and skipped something is worse than one that says what it skipped**,
because the reader has no way to know which they are holding.

---

## The general rule

When a result is the one you were hoping for, that is the moment to ask what
else would produce the same result. Most of the fourteen above were caught by
someone asking exactly that, late.
