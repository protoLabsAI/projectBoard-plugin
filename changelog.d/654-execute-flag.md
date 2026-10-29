Task creation now accepts `execute=true` to add the `execute` label through the
existing store label path, including HTTP creation with `issue_type="task"`.
Tool and board guidance directs execution tasks to the coder ladder with an empty
assignee; the board agent must never assign execution tasks to itself. Writing-only
tasks retain self-assignment, and execution tasks still deliver reports for `board_verify`.
