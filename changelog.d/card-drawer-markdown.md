The card drawer now renders a card's spec and acceptance criteria as markdown instead of
raw esc()'d source (projectBoard#849cl). Headings, bold, inline/fenced code and ordered/
unordered lists in the Card tab (and in a task's detail drawer) render as HTML rather than
showing literal `##`, `**` and backticks.

The spec/AC prose reuses the exact markdown path the coder's "saying" section already had
(bd-p87t): one shared `prose()` helper emits the `.md-saying` hook with a `data-md`
attribute, `enhanceSaying()` upgrades the fresh nodes on every drawer repaint (card, task
and monitor), and the esc()'d plain-text body stays as the fallback when the marked CDN
load fails. The XSS guard is unchanged — source angle brackets are escaped before parsing,
`javascript:`/`data:`/`vbscript:` hrefs are stripped, and the renderer stays pinned by
subresource integrity — because card text is untrusted, exactly like coder output.
