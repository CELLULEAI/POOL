# Contributing to CELLULE / POOL

Thank you for your interest in contributing.

## TL;DR

- The project is dual-licensed: AGPLv3 for this public repo, proprietary for the POOL-PRIVATE repo (enterprise and pre-patent modules).
- External contributions require signing the CLA so CELLULE can relicense if needed.
- POOL-PRIVATE contributions are invitation-only, under NDA.

## How to contribute

### Small fixes (typos, docs, obvious bugs)

1. Open a PR on main.
2. Sign off in the PR body: "I agree to the CELLULE CLA v1.0 — <name>, <date>, <github username>"
3. Maintainer reviews and merges.

### Features or substantial changes

1. Open an issue first to discuss design.
2. After alignment, open a PR.
3. Include tests for non-trivial changes.
4. Sign the CLA.

### What will be rejected

- Code overlapping POOL-PRIVATE roadmap (ask in an issue if unsure). Flags: intent-based smart routing algorithms, split-brain detection heuristics, settlement/slashing activation logic, production-tuned thresholds.
- Secrets, tokens, keys (even test values).
- Third-party code without a compatible license. AGPLv3-compatible: MIT, Apache, BSD, GPL. Not compatible: proprietary.

## Invariants

Before architectural changes, read the ARCHITECTURE and GOVERNANCE docs. Load-bearing invariants:

- Worker-first (usability beats abstraction purity)
- No master pool (federation is peer-to-peer)
- Zero-knowledge user memory (never log plaintext)
- Ed25519 peer identity

## Licensing

By opening a PR you confirm:
1. You have read and agreed to the CLA.
2. Your contribution is your original work OR compatible prior work.
3. You grant CELLULE the right to relicense under alternative commercial terms.

## Contact

- Website: https://cellule.ai
- Issues: https://github.com/CELLULEAI/POOL/issues
- Commercial: david.mourgues@gmail.com
