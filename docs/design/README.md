# Design record

These documents were written while Haddon was built. They are kept because they explain why the code is the way
it is, including options that were tried and dropped. They describe the past; for current behaviour, the
documents one level up and the code are authoritative.

- [decisions.md](decisions.md) is the design log, in chronological order. Early sections are named after build
  steps ("Chunk C1", "Chunk F2"); later ones after the design issues below ("Issue 0004"). Code comments cite
  these section names, so a comment saying "see chunk C2a" or "issue 0005" points here. A later section can
  supersede an earlier one.
- [simplification-plan.md](simplification-plan.md) and [issues/](issues/README.md) are the plan that reshaped
  an earlier, larger design into the current one: a continuous journal, direct audited commands instead of an
  approval chain, a smaller reconciliation model, and a named-command API for agents. The work they describe is
  done.

When a change alters accounting meaning or reverses a decision recorded here, add a section to the end of
`decisions.md` saying what changed and why.
