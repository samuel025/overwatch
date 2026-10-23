# Pre-existing open-source code

The files in this folder are vendored from the Meterless H-MEM reference implementation
(https://github.com/Meterless/Meterless, `engines/hmem/reference/src`, commit 1742dec, 2026-10-19),
licensed under Apache-2.0 (see `LICENSE`).

Overwatch changes:
- `memoryStore.ts`: added infrastructure terms to entity extraction and an `ops` domain for incident memories.

`../../conformance/` is vendored from the same commit (`engines/hmem/conformance`) so the official
conformance suite can be run against this implementation with `npm run conformance`.
