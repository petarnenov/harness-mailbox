# Vendored browser libraries

The files in this directory are copied byte for byte from the published npm packages. They are not built,
bundled or modified here, and the dashboard loads them only from its own origin. This file and
`LICENSE-pixi.txt` are not served.

Recorded on 2026-11-28 (`latest` dist-tag of each package on that day).

| File | Package | Version | Path inside the tarball | SHA-256 of the file | Licence |
|---|---|---|---|---|---|
| `pixi-8.23.0.min.js` | `pixi.js` | 8.23.0 | `package/dist/pixi.min.js` | `a2c8ff6d25df3c712e87c7e4a2ddd9e2e2387e2fba01ee9f26d935ed183c0a3b` | MIT, see `LICENSE-pixi.txt` |
| `pixi-unsafe-eval-8.23.0.min.js` | `pixi.js` | 8.23.0 | `package/dist/packages/unsafe-eval.min.js` | `810d09c20fa7f3c2e09c45519a40d36ca90f96ba14cd5707b0e98c4427f664ad` | MIT, see `LICENSE-pixi.txt` |
| `gsap-3.15.1.min.js` | `gsap` | 3.15.1 | `package/dist/gsap.min.js` | `3d1bea374ab1b703cc5c4105358eec63c4465efdc573931dc116e34a0a190c1b` | GSAP Standard "no charge" licence, https://gsap.com/standard-license |

## Sources

| Package | Tarball | Integrity published by the registry (SHA-512) |
|---|---|---|
| `pixi.js` 8.23.0 | https://registry.npmjs.org/pixi.js/-/pixi.js-8.23.0.tgz | `sha512-RkWSwo+mAR0d7MKnJ20IjKkWGPdFh1C0adBRYYLa+SGdxTfbrr6RnQyZZRwjBaG/bDAIQqgBcTBbiNsN6iKFVQ==` |
| `gsap` 3.15.1 | https://registry.npmjs.org/gsap/-/gsap-3.15.1.tgz | `sha512-63yjWRFWj3QRP1UOuD8kcidCcy3nxb1mMRpwHr/xJRvPfm9GtWOXFAYUiL0ro1fgPq2nY53LR+98pAgy9BumBg==` |

## Licences

- **PixiJS** (both `pixi-*` files) is under the MIT License. Its text is in `LICENSE-pixi.txt`, copied from
  `package/LICENSE` of the same tarball.
- **GSAP** (`gsap-3.15.1.min.js`) is **not** under the MIT License and is not covered by this repository's
  `LICENSE`. It is proprietary software of Webflow, used under the GSAP Standard "no charge" licence at
  https://gsap.com/standard-license (read on 2026-11-28; effective 2025-04-30). The copyright and licence header
  at the top of the file must stay unchanged. The licence forbids using GSAP in a tool that lets users build
  visual animations without code in competition with Webflow.

## Why the unsafe-eval module

PixiJS generates code with `new Function`, which the dashboard's Content-Security-Policy (`script-src 'self'`)
forbids. `pixi-unsafe-eval-8.23.0.min.js` is the package's own module that replaces those generators with
static functions. It must be loaded after `pixi-8.23.0.min.js`.

## Updating

Replace a file only with the same path from a newer published tarball whose SHA-512 equals the registry's
integrity value, rename it to the new version, and update the rows above together with every place that names
the file.
