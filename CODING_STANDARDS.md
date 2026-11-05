# Coding standards

These rules guide reviewer judgement.

- Trace response fields that assert external effects (`revoked`, `deleted`, `persisted`, `verified`) to code that observed the effect. When a provider reuses another provider's response shape, re-check every field against its own backend.
- Keep test doubles for external binaries and services to behavior pinned by a contract test or a cited source fact.

## Frontend architecture review

When reviewing frontend changes, check the architecture from [#118](https://github.com/nguyencanhtrung/asr-studio/issues/118).
Use [ADR 0010](docs/adr/0010-frontend-reducer-core.md) and [Frontend design §11](docs/frontend-design.md#11-kiến-trúc-mã-frontend-reducer-effect-và-store) for the rules and exceptions.

- Check pure reducers, state ownership, data effects, and view boundaries.
- Check stale responses, cancellation, communication between features, and shared provider logic.
- Check tests, transition documentation, module loading, asset protection, and runtime constraints.

The reviewer checks code and tests. Passing scans do not replace review.
The coder does not need a checklist or compliance report before review.
Request coder changes only for identified violations. Cite the rule and code. State the required correction.
Report verification limits separately. Block approval for unresolved violations. Check corrections during the next review.
