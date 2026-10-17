- Add YAML-only `decompose_after_timeouts_by_difficulty` overrides for the fresh-build
  timeout threshold. Use `{medium: 1, large: 1}` to park those cards `too-wide` and request
  decomposition after one timeout that reaches the model. Unconfigured difficulties keep
  the global default of two; zero disables parking for that difficulty. Fix-round and
  pre-model timeouts remain excluded. Refs #549.
