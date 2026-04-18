# Pool Operator Obligations (AGPLv3 §13)

If you run the CELLULE pool as a public network service — meaning users other than yourself interact with it over a network — **you have obligations under AGPLv3 section 13**. This document summarizes them in plain English.

## TL;DR

1. You may run, modify, and deploy the pool freely.
2. If you modify it and your modified version serves users over a network, you must offer **the corresponding source code** to those users, under AGPLv3, at no additional charge.
3. You may not add restrictions that further restrict the recipients' rights granted by the AGPLv3.
4. The CELLULE and cellule.ai trademarks are NOT licensed — don't use them to imply endorsement.

## What counts as "public network service"?

**Yes, §13 applies:**
- You run a pool accessible from the public internet, anyone can join or query.
- Your pool is open to your company employees over the internet (they are "remote users interacting" in AGPL terms).
- You host an inference API based on CELLULE code, even gated behind an API key the public can obtain.

**No, §13 does not apply:**
- You run the pool on your laptop for personal use.
- You run it inside a fully isolated lab network with no external access.
- You run it only for yourself, accessible over localhost or VPN to yourself.

## If you only deploy unmodified images

If you pull `celluleai/pool:1.0.0` from Docker Hub, run it as-is, and don't modify the code, **you don't have a publication obligation**: the source is already public at https://github.com/CELLULEAI/POOL and you simply point your users there.

You still must **not remove** license notices (LICENSE, NOTICE, MAINTAINERS files are embedded in `/opt/cellule/` inside the image — keep them).

## If you modify the code

At minimum, you must:

1. **Publish your modified source code** somewhere your users can access. A public GitHub fork is the easiest path. A private URL offered to your users also works.
2. **Include a notice in your deployment** pointing users to the modified source — for example, add a `/source` endpoint or a banner in your UI.
3. **License your modifications under AGPL-3.0-or-later** — you cannot relicense AGPLv3 code under more restrictive terms.

## What you CANNOT do

- Ship a proprietary fork that you keep secret from your users.
- Strip the LICENSE, NOTICE, or MAINTAINERS files from the image.
- Use the CELLULE or cellule.ai trademark to imply your fork is official.
- Refuse source access to your users and hide behind paywall/NDA while running a public service.

## Enterprise option

If you want to run a fork WITHOUT the AGPLv3 source-sharing obligation (for example: a closed-source enterprise deployment), you must obtain a **commercial license** from CELLULE.

Contact: david.mourgues@gmail.com

The commercial license grants you permissive distribution rights in exchange for an annual fee. This dual-tier model is documented in [NEED_TO_KNOW_ARCHITECTURE.md](./NEED_TO_KNOW_ARCHITECTURE.md).

## Patent reservation

The AGPLv3 grants you a patent license for the specific contributions made by each contributor. It does NOT waive CELLULE's right to file patents on future innovations, nor does it grant you a license to patents CELLULE may obtain later.

If you operate a fork and believe your modifications may infringe CELLULE patents, contact david.mourgues@gmail.com to discuss licensing.

## Trademark

`CELLULE`, `cellule.ai`, and the CELLULE logo are reserved. You may:
- Say your service is "based on CELLULE" or "a CELLULE-compatible pool" — factual descriptive use.

You may not:
- Name your fork "CELLULE X" or "cellule-<something>" — implies endorsement.
- Use the CELLULE logo on your product.
- Advertise as if you were the official project.

## Questions

Good faith questions are welcome: david.mourgues@gmail.com. When in doubt, ask — we prefer clarifying to enforcing.

---

This document is informative only. The LICENSE file is authoritative.
Last updated: 2026-04-18
