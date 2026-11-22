- **A coder's `done. <explanation>` requirement rows now count.** The parser took `done.`
  only at the end of a line, so a row like `- r1: done. Added the regression test` was
  read as malformed — silence — and bd-xhirv was blocked as "requirements unresolved"
  after two fix rounds although every item was reported done. A full stop followed by a
  space is now a separator like `:` or a dash; an explanation that opens with a hedge
  (`done. but not the e2e`, `done. partially`, `done. not yet`) still closes nothing. A new
  `unparsed_requirement_lines` helper lists the section's rows the parser skipped.
