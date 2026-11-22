- **A requirement-gate re-dispatch now names the rows it could not read.** When a coder's
  `## Requirements` section held lines the parser skipped (`- r1: finished it`), the
  follow-up only listed the open items, so the coder rewrote the same format and the card
  blocked as "requirements unresolved". The follow-up now quotes the skipped lines as not
  understood and restates the accepted forms (`- r1: done`, `- r1: done — <note>`,
  `- r1: declined — <reason>`); the gate's log line and bounce comment show them too.
