# Need-to-know Architecture

CELLULE operates a dual-tier codebase to balance open-source values with IP protection and enterprise needs.

## Two repositories

| Repository | License | Scope | Audience |
|---|---|---|---|
| [CELLULEAI/POOL](https://github.com/CELLULEAI/POOL) | AGPL-3.0-or-later | Core pool, federation, worker protocol, compaction, RAG, zero-knowledge memory | Public / community |
| CELLULEAI/POOL-PRIVATE | Proprietary | Pre-patent modules, enterprise features, production tuning, R&D | Invitation only |

## Why this structure

- **AGPLv3** on the public repo protects the commons: anyone running this pool as a public network service must publish their source.
- **POOL-PRIVATE** preserves innovations that require patent protection (US grace period deadline 2027-04-17) or are commercialized as enterprise offerings.

## Legal boundary

**AGPLv3 section 13 triggers the moment any code serves a public network service.**

Enforcement rules:

1. The public cellule.ai pool runs exclusively on AGPLv3 code (POOL repo).
2. POOL-PRIVATE modules never deploy on cellule.ai public. They run:
   - On R&D infrastructure (no public users)
   - On enterprise client pools under NDA + commercial license
   - During controlled experiments with authorized access only
3. When a POOL-PRIVATE module matures into something suitable for the public repo, it is **moved to POOL first** (published under AGPLv3), then the private copy is deleted.

## Runtime enforcement

The public pool loads plugins opt-in via environment variable:

```python
# iamine/plugins/__init__.py
if os.getenv("CELLULE_ENTERPRISE") == "1":
    load_enterprise_plugins(pool)
```

Production cellule.ai never sets `CELLULE_ENTERPRISE=1`. Enterprise deployments do, under separate commercial terms.

## Contributor impact

- Public contributions: welcome, AGPLv3, CLA required (see [CLA.md](../CLA.md) and [CONTRIBUTING.md](../CONTRIBUTING.md))
- POOL-PRIVATE interest: email david.mourgues@gmail.com

## Transparency

This document exists to be clear. The open-source part is genuinely open; the private part is genuinely private. Neither masquerades as the other.

---

Last updated: 2026-04-18
