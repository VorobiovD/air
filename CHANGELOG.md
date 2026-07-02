# Changelog

## [1.46.0](https://github.com/VorobiovD/air/compare/v1.45.0...v1.46.0) (2026-07-02)


### Features

* **learn:** AIR_LEARN_CRON_LIVE — cron as sole learn executor ([#243](https://github.com/VorobiovD/air/issues/243)) ([d91fa5d](https://github.com/VorobiovD/air/commit/d91fa5d3cd64666e70ee745487d9ed8769042903))
* **review:** AIR_NO_APPROVE advisory mode + AGENTS.md fallback ([#241](https://github.com/VorobiovD/air/issues/241)) ([a7de588](https://github.com/VorobiovD/air/commit/a7de5880e33e335f992ea0fbbe42c77edbdd6d58))
* **review:** v2 review format — professional, not scary (default on) ([#240](https://github.com/VorobiovD/air/issues/240)) ([65cfcae](https://github.com/VorobiovD/air/commit/65cfcaeaaf471582d822e283b454a325b58b6748))

## [1.45.0](https://github.com/VorobiovD/air/compare/v1.44.0...v1.45.0) (2026-07-01)


### Features

* **cost:** intro-aware Sonnet 5 pricing in cost telemetry ([#238](https://github.com/VorobiovD/air/issues/238)) ([e17fa14](https://github.com/VorobiovD/air/commit/e17fa147942111f1fc9f4739f2f463984adb8caf))

## [1.44.0](https://github.com/VorobiovD/air/compare/v1.43.0...v1.44.0) (2026-07-01)


### Features

* **learn:** scheduled out-of-band learn driver (autonomy trial) ([#231](https://github.com/VorobiovD/air/issues/231)) ([1fe1c5c](https://github.com/VorobiovD/air/commit/1fe1c5cbbac187720a7eca751dddc8a8389f9314))
* **models:** adopt Sonnet 5 for the sonnet tier (fleet flip) ([#237](https://github.com/VorobiovD/air/issues/237)) ([b31ca26](https://github.com/VorobiovD/air/commit/b31ca268a073a0b098cf029e4b19d456799506da))


### Bug Fixes

* **headless:** address review nits on the stream-retry ([#235](https://github.com/VorobiovD/air/issues/235)) ([510697f](https://github.com/VorobiovD/air/commit/510697f091f53016a362445a886c2ce3745e33a9))
* **headless:** retry transient mid-stream disconnects instead of dying ([#234](https://github.com/VorobiovD/air/issues/234)) ([3f3dc96](https://github.com/VorobiovD/air/commit/3f3dc96d35b16d64956520970dd5cae9ca9884e9))

## [1.43.0](https://github.com/VorobiovD/air/compare/v1.42.0...v1.43.0) (2026-06-28)


### Features

* **learn:** cost/cache/token telemetry for headless learn (air-stats parity) ([#229](https://github.com/VorobiovD/air/issues/229)) ([f518a32](https://github.com/VorobiovD/air/commit/f518a328ad345cf910a17b4dd3f0e371c55bbef0))

## [1.42.0](https://github.com/VorobiovD/air/compare/v1.41.0...v1.42.0) (2026-06-28)


### Features

* **learn:** Phase-2 — Batch API for headless learn (opt-in, 50%) + profile overflow guard ([#227](https://github.com/VorobiovD/air/issues/227)) ([b37b95c](https://github.com/VorobiovD/air/commit/b37b95cfb97048ec56a4696a691c68da3ddd9aca))

## [1.41.0](https://github.com/VorobiovD/air/compare/v1.40.0...v1.41.0) (2026-06-28)


### Features

* **learn:** MA-independent headless learn + CLI store-awareness ([#224](https://github.com/VorobiovD/air/issues/224)) ([3b7760f](https://github.com/VorobiovD/air/commit/3b7760f80b7191d61b2deffed036e8292350822c))
* **learn:** Phase-1b — headless REVIEW-HISTORY + PROJECT-PROFILE parity ([#226](https://github.com/VorobiovD/air/issues/226)) ([8f7be57](https://github.com/VorobiovD/air/commit/8f7be57b9b7b0844436f819a5ff506e0a3c18f20))


### Bug Fixes

* **billing:** preflight fail-fast on the "specified API usage limits" cap ([#222](https://github.com/VorobiovD/air/issues/222)) ([bac20bb](https://github.com/VorobiovD/air/commit/bac20bbc0c82f442072d38dc64ec7339e73bdb6b))

## [1.40.0](https://github.com/VorobiovD/air/compare/v1.39.0...v1.40.0) (2026-06-26)


### Features

* **cli:** wire [#198](https://github.com/VorobiovD/air/issues/198) origin-anchor into the CLI re-review pin ([#220](https://github.com/VorobiovD/air/issues/220)) ([ca47e36](https://github.com/VorobiovD/air/commit/ca47e36a8602074d0a6234c2408eb83ef4e3fff0))
* **context:** surface concurrent open PRs in managed/headless reviews ([#3](https://github.com/VorobiovD/air/issues/3)d) ([#219](https://github.com/VorobiovD/air/issues/219)) ([5ffb21b](https://github.com/VorobiovD/air/commit/5ffb21b6915b8e7ebaaf73176f82ec8382e90d48))
* remove experimental GitLab CLI support (GitHub-only) ([#215](https://github.com/VorobiovD/air/issues/215)) ([0668c21](https://github.com/VorobiovD/air/commit/0668c214d91a196f8cd953666bcb1b7d4e571580))
* share diff-hygiene with the CLI + SHA-validate the CLI gate ([#217](https://github.com/VorobiovD/air/issues/217)) ([c4e9ca8](https://github.com/VorobiovD/air/commit/c4e9ca8efce8b2c2a461e4cfcb88b10250b1f90a))
* **verdict:** [#198](https://github.com/VorobiovD/air/issues/198) origin-anchor — un-poison round-3+ re-review chains ([#218](https://github.com/VorobiovD/air/issues/218)) ([a1321cd](https://github.com/VorobiovD/air/commit/a1321cd688a69e0e0b111ad5b1c9f642df9dc884))


### Bug Fixes

* **review:** restore the --respond footer on managed/headless/solo reviews ([#221](https://github.com/VorobiovD/air/issues/221)) ([e9d4cfb](https://github.com/VorobiovD/air/commit/e9d4cfb361011acf57839f030e4c31813a698439))

## [1.39.0](https://github.com/VorobiovD/air/compare/v1.38.1...v1.39.0) (2026-06-25)


### Features

* **managed:** name the reviewer in PAT preflight + bot-fallback (Tier 1 visibility) ([#212](https://github.com/VorobiovD/air/issues/212)) ([7764326](https://github.com/VorobiovD/air/commit/77643265c91e735c20d164db2e5d4805e2f6e216))

## [1.38.1](https://github.com/VorobiovD/air/compare/v1.38.0...v1.38.1) (2026-06-25)


### Performance Improvements

* **headless:** 5m cache TTL everywhere — fix default + retire the heavy→1h over-charge ([#209](https://github.com/VorobiovD/air/issues/209)) ([412b224](https://github.com/VorobiovD/air/commit/412b2242e015cc95bb36ba21d2b6ce96fe6ed8f9))

## [1.38.0](https://github.com/VorobiovD/air/compare/v1.37.0...v1.38.0) (2026-06-24)


### Features

* **headless:** backfill a missing verdict on the already-reviewed-at-head skip ([#200](https://github.com/VorobiovD/air/issues/200)) ([68b8fe0](https://github.com/VorobiovD/air/commit/68b8fe034ad6c6c164b2c985dc1181c92b39b5d3))
* **headless:** per-agent cost + cache-read telemetry ([#202](https://github.com/VorobiovD/air/issues/202)) ([6b8c96e](https://github.com/VorobiovD/air/commit/6b8c96e56344c4bafc339a960ce941673651e02b))
* **headless:** wire promote fast-path — delta-review sibling staging-to-main promotes ([#205](https://github.com/VorobiovD/air/issues/205)) ([20209c7](https://github.com/VorobiovD/air/commit/20209c7f138631249efde7f575aeb1058d9a677a))


### Bug Fixes

* **headless:** raise diff cap to 500K (managed parity) — stop over-gating big promotes ([#206](https://github.com/VorobiovD/air/issues/206)) ([f087438](https://github.com/VorobiovD/air/commit/f087438bb5b8ff9df0a2649a206dbd50e57486b8))


### Performance Improvements

* **headless:** auto cache-TTL by PR weight (5m default, 1h heavy) — ~17% cheaper writes ([#204](https://github.com/VorobiovD/air/issues/204)) ([55a3ec4](https://github.com/VorobiovD/air/commit/55a3ec4dd60e79af3c502879a3aa87e17920fe17))
* **headless:** effort=medium for advisory lenses + telemetry nit/None-guard ([#203](https://github.com/VorobiovD/air/issues/203)) ([16b4344](https://github.com/VorobiovD/air/commit/16b434449321e91a417381a5acf38968b08c64f1))

## [1.37.0](https://github.com/VorobiovD/air/compare/v1.36.0...v1.37.0) (2026-06-23)


### Features

* **headless:** AIR_EXPECTED_REVIEWER assertion + docs (P4 polish) ([#195](https://github.com/VorobiovD/air/issues/195)) ([6baeb3c](https://github.com/VorobiovD/air/commit/6baeb3c943e79a450b15bc9857f0d76b1e85f169))
* **headless:** Codex external second-opinion pass (P3) ([#194](https://github.com/VorobiovD/air/issues/194)) ([f6198e0](https://github.com/VorobiovD/air/commit/f6198e0658789320bdb4cb6d7765a3d50106c19b))
* **headless:** dispatch the UI/copy reviewer on user-facing diffs (P3) ([#193](https://github.com/VorobiovD/air/issues/193)) ([08e85e7](https://github.com/VorobiovD/air/commit/08e85e70dd2fac198cfcaaf2d4cdf76e5e25e7ee))
* **headless:** experimental messages-api review mode (self-hosted loop, opt-in) ([#187](https://github.com/VorobiovD/air/issues/187)) ([44addd5](https://github.com/VorobiovD/air/commit/44addd5ab14d79bfd6123d5f87ee0f550e10358e))
* **headless:** feed precomp signals + learned patterns to headless reviews ([#189](https://github.com/VorobiovD/air/issues/189)) ([e15cfd2](https://github.com/VorobiovD/air/commit/e15cfd2063e82f2cb37fe73e896bc0423a20e071))
* **headless:** re-review mode + learning write-back (P2) ([#190](https://github.com/VorobiovD/air/issues/190)) ([07c84e2](https://github.com/VorobiovD/air/commit/07c84e2b59fa71b1824629e654e9fea3bd5470b5))


### Bug Fixes

* **learn:** correct wiki_cap "shipped whole" log + close the A/B follow-ups ([#196](https://github.com/VorobiovD/air/issues/196)) ([2998e6c](https://github.com/VorobiovD/air/commit/2998e6cc1440e5c0f3b4234086ef21ce844928c4))
* **learn:** deterministic wiki bloat-cap (stop the unbounded growth) ([#192](https://github.com/VorobiovD/air/issues/192)) ([53123c4](https://github.com/VorobiovD/air/commit/53123c4ef15e37144cd84c37387e6025792621d3))
* **learn:** raise + unify the learn poll/stream timeout (deadlock on a bloated wiki) ([#191](https://github.com/VorobiovD/air/issues/191)) ([24408b0](https://github.com/VorobiovD/air/commit/24408b0f3a54d45f4f007033fd4cc3c14a48cd70))
* **verdict:** stop re-review false-blocking already-fixed PRs ([#197](https://github.com/VorobiovD/air/issues/197)) ([3715b13](https://github.com/VorobiovD/air/commit/3715b13f97d80d8c8bf3c28094f29b79aa7c6ac1))

## [1.36.0](https://github.com/VorobiovD/air/compare/v1.35.1...v1.36.0) (2026-06-21)


### Features

* **direct-post:** post the verifier body to bypass coordinator relay drops (AIR_POST_VERIFIER_BODY) ([#185](https://github.com/VorobiovD/air/issues/185)) ([46b6c5d](https://github.com/VorobiovD/air/commit/46b6c5dcd52427bd67281ce19485480074d21b66))

## [1.35.1](https://github.com/VorobiovD/air/compare/v1.35.0...v1.35.1) (2026-06-21)


### Bug Fixes

* **learn:** atomic claim-lock so a busy repo can't fire concurrent learns ([#183](https://github.com/VorobiovD/air/issues/183)) ([89240cf](https://github.com/VorobiovD/air/commit/89240cfe203a491732fcce1923ef2c00fe85e22f))

## [1.35.0](https://github.com/VorobiovD/air/compare/v1.34.0...v1.35.0) (2026-06-20)


### Features

* **verifier:** emit the [sec:] exposure tag on all paths (CLI + solo + managed) ([#181](https://github.com/VorobiovD/air/issues/181)) ([7f73d02](https://github.com/VorobiovD/air/commit/7f73d0273d95429cfd447a40944fbe35a0c0323a))

## [1.34.0](https://github.com/VorobiovD/air/compare/v1.33.0...v1.34.0) (2026-06-20)


### Features

* **verdict:** deterministic fresh-gate exposure floor (AIR_CATEGORY_FLOOR) ([#180](https://github.com/VorobiovD/air/issues/180)) ([afbdfeb](https://github.com/VorobiovD/air/commit/afbdfebfedaf0d2729b0c8cb32f96f56b6914e60))


### Bug Fixes

* **workflow:** forward AIR_MA_COORDINATOR_MODEL caller var to the driver ([#178](https://github.com/VorobiovD/air/issues/178)) ([e92d887](https://github.com/VorobiovD/air/commit/e92d887411153468a85e32c463d278a78fe13757))

## [1.33.0](https://github.com/VorobiovD/air/compare/v1.32.3...v1.33.0) (2026-06-19)


### Features

* **multiagent:** opt-in cheaper MA coordinator tier (AIR_MA_COORDINATOR_MODEL) ([#176](https://github.com/VorobiovD/air/issues/176)) ([a603209](https://github.com/VorobiovD/air/commit/a603209e9b36c35c4ef330b71b22002950711a11))

## [1.32.3](https://github.com/VorobiovD/air/compare/v1.32.2...v1.32.3) (2026-06-18)


### Bug Fixes

* **verdict:** dismiss cross-account stale blocks orphaned by PAT rotation ([#173](https://github.com/VorobiovD/air/issues/173)) ([81237dd](https://github.com/VorobiovD/air/commit/81237dd80a95da96613582fc2a60ff5447f0ece5))

## [1.32.2](https://github.com/VorobiovD/air/compare/v1.32.1...v1.32.2) (2026-06-17)


### Bug Fixes

* **review:** point store-backed agents at the real /mnt/memory subdir ([#172](https://github.com/VorobiovD/air/issues/172)) ([602e5de](https://github.com/VorobiovD/air/commit/602e5de77629a420f22b5b2419b74813920b11d9))
* **verifier:** forbid downgrading a confirmed security exposure ([#170](https://github.com/VorobiovD/air/issues/170)) ([f4060b3](https://github.com/VorobiovD/air/commit/f4060b390d7f42835b242fe5090703379bf7033a))

## [1.32.1](https://github.com/VorobiovD/air/compare/v1.32.0...v1.32.1) (2026-06-15)


### Bug Fixes

* a hung codex no longer takes down the whole review (kill its process group) ([#166](https://github.com/VorobiovD/air/issues/166)) ([293e479](https://github.com/VorobiovD/air/commit/293e479b87ade3f226fe387b310a2c831fec195b))
* **coordinator:** fail loud when run outside the managed runtime ([#168](https://github.com/VorobiovD/air/issues/168)) ([5e29d29](https://github.com/VorobiovD/air/commit/5e29d29aba0b2d2ef4e982aaac586a6f1d2c8667))
* stop the solo reviewer downgrading PHI/auth exposures below blocker ([#164](https://github.com/VorobiovD/air/issues/164)) ([f834a15](https://github.com/VorobiovD/air/commit/f834a15e36513ee549c5a085773b2a9b8f5d73df))
* **verdict:** decorated/synonym statuses no longer resurrect a fixed finding ([#167](https://github.com/VorobiovD/air/issues/167)) ([9dfaa88](https://github.com/VorobiovD/air/commit/9dfaa88d487e030d996d031e513fc64dcdbe953e))

## [1.32.0](https://github.com/VorobiovD/air/compare/v1.31.0...v1.32.0) (2026-06-14)


### Features

* deterministic re-review severity-pin + deferred-findings ledger ([#158](https://github.com/VorobiovD/air/issues/158)) ([612bfa6](https://github.com/VorobiovD/air/commit/612bfa67bf3352ced6e62f5446852c9dcc2d0fa7))


### Bug Fixes

* honor real round-2 fixes with hunk-level line evidence ([#162](https://github.com/VorobiovD/air/issues/162)) ([adf3346](https://github.com/VorobiovD/air/commit/adf33463e9ba66917a23865dc2bf9be903f5fbfd))
* join multiagent session output blocks with newlines ([#160](https://github.com/VorobiovD/air/issues/160)) ([4f055d2](https://github.com/VorobiovD/air/commit/4f055d2a936cfcbee2cb0823ff4fd4b6d57099f1))
* re-review ledger — number-identity pinning + round-2 coverage ([#161](https://github.com/VorobiovD/air/issues/161)) ([2917d5f](https://github.com/VorobiovD/air/commit/2917d5f9f75bc9ca6189465ec895d76de0a632f1))

## [1.31.0](https://github.com/VorobiovD/air/compare/v1.30.0...v1.31.0) (2026-06-13)


### Features

* CLI solo mode — one Fable agent, six lenses, advisory-first ([#156](https://github.com/VorobiovD/air/issues/156)) ([3e68c26](https://github.com/VorobiovD/air/commit/3e68c2616078c253ef2c2148d2a464ff80b65a0c))

## [1.30.0](https://github.com/VorobiovD/air/compare/v1.29.0...v1.30.0) (2026-06-12)


### Features

* **ci:** pass AIR_MULTIAGENT caller variable through to the driver ([#150](https://github.com/VorobiovD/air/issues/150)) ([d5f77ed](https://github.com/VorobiovD/air/commit/d5f77edbfb6c81be2dbe00677824852f7fa7abfe))
* **managed:** diff hygiene, conversation tail-cap, codex skip on tiny deltas ([#146](https://github.com/VorobiovD/air/issues/146)) ([69571a4](https://github.com/VorobiovD/air/commit/69571a486b34b3d81524e0e92b615c87a5d0c873))
* **managed:** multiagent workspace-handoff behind AIR_MULTIAGENT (PR6') ([#148](https://github.com/VorobiovD/air/issues/148)) ([84da727](https://github.com/VorobiovD/air/commit/84da727928a21d43df6185aeff99181ea18cf7d9))
* **managed:** overlap codex with precomp + parallel blame/churn ([#147](https://github.com/VorobiovD/air/issues/147)) ([53f73cf](https://github.com/VorobiovD/air/commit/53f73cff0a1e77ca91fa8e4cede902b67f4f9409))
* one verdict-gating contract for CLI and managed (PR5) ([#151](https://github.com/VorobiovD/air/issues/151)) ([06bde91](https://github.com/VorobiovD/air/commit/06bde913470bed572529bf6d36dc6c3f00a1bbbc))


### Bug Fixes

* **managed:** A/B-surfaced fixes — watchdog attribution, capped-overlap guard, UI fail-open ([#149](https://github.com/VorobiovD/air/issues/149)) ([abe3420](https://github.com/VorobiovD/air/commit/abe3420b1e559391178833bf75abeae04ced14f4))
* **managed:** GitHub I/O discipline, fence-aware extraction, verdict backfill ([#143](https://github.com/VorobiovD/air/issues/143)) ([e48024d](https://github.com/VorobiovD/air/commit/e48024de49a601dab299411374f106b54dec3388))
* **managed:** incident fixes — non-UTF-8 crash, interrupt-on-cancel, orphan salvage ([#155](https://github.com/VorobiovD/air/issues/155)) ([e562d70](https://github.com/VorobiovD/air/commit/e562d70118d69c4f11893022a667d5d7148a7930))
* **managed:** keep coordinator delegation alive on enforcing runtimes ([#152](https://github.com/VorobiovD/air/issues/152)) ([1f2ad12](https://github.com/VorobiovD/air/commit/1f2ad12a4f1678c61bea85fa385c840ac0bb037c))
* **managed:** real-time decision logs + session attribution; org-clean docs ([#154](https://github.com/VorobiovD/air/issues/154)) ([c622ecc](https://github.com/VorobiovD/air/commit/c622ecc0cefbb87a859d2f61b88695d08478e1b9))

## [1.29.0](https://github.com/VorobiovD/air/compare/v1.28.1...v1.29.0) (2026-06-08)


### Features

* **ci:** resolve review_mode from a caller AIR_REVIEW_MODE variable ([#138](https://github.com/VorobiovD/air/issues/138)) ([a790d4e](https://github.com/VorobiovD/air/commit/a790d4edee8a9c15f72a449c3aef4dbcb04cdb2b))

## [1.28.1](https://github.com/VorobiovD/air/compare/v1.28.0...v1.28.1) (2026-06-08)


### Bug Fixes

* **agents:** ui-copy gate no longer treats internal docs/ as user-facing ([#136](https://github.com/VorobiovD/air/issues/136)) ([861c3a6](https://github.com/VorobiovD/air/commit/861c3a6f4f1a6e6812571d1cef5932f8c5b6ccbf))

## [1.28.0](https://github.com/VorobiovD/air/compare/v1.27.0...v1.28.0) (2026-06-08)


### Features

* **agents:** ui-copy reviewer covers CLI/TUI copy via PROJECT-PROFILE opt-in ([#134](https://github.com/VorobiovD/air/issues/134)) ([928d331](https://github.com/VorobiovD/air/commit/928d331444f23d2516910eaffda38eddcd76e8e8))

## [1.27.0](https://github.com/VorobiovD/air/compare/v1.26.0...v1.27.0) (2026-06-08)


### Features

* **agents:** UI / business-audience copy reviewer (air-ui-copy-reviewer) ([#130](https://github.com/VorobiovD/air/issues/130)) ([9389a56](https://github.com/VorobiovD/air/commit/9389a567a1fc7e7c2e92d409d2ede63bc85018d0))

## [1.26.0](https://github.com/VorobiovD/air/compare/v1.25.0...v1.26.0) (2026-06-08)


### Features

* **ci:** enable promote fast-path via a caller repo/org variable ([#128](https://github.com/VorobiovD/air/issues/128)) ([5fcbb5f](https://github.com/VorobiovD/air/commit/5fcbb5fd4fd6977e15a4648d16425939f06aa3bc))

## [1.25.0](https://github.com/VorobiovD/air/compare/v1.24.0...v1.25.0) (2026-06-08)


### Features

* **managed:** promote fast-path — re-review sibling promotes instead of full re-read ([#126](https://github.com/VorobiovD/air/issues/126)) ([8e3571d](https://github.com/VorobiovD/air/commit/8e3571dff3343123e2b7d8bc9193b1692d4779a7))

## [1.24.0](https://github.com/VorobiovD/air/compare/v1.23.0...v1.24.0) (2026-06-08)


### Features

* **prompts:** safe cost/quality fixes from the 06-07 review audit ([#124](https://github.com/VorobiovD/air/issues/124)) ([311eeb2](https://github.com/VorobiovD/air/commit/311eeb22a71890eeacee68ff18a9bd52b96ac52a))

## [1.23.0](https://github.com/VorobiovD/air/compare/v1.22.0...v1.23.0) (2026-06-07)


### Features

* **ci:** auth preflight — fail $0 on a missing/expired/no-access review token ([#122](https://github.com/VorobiovD/air/issues/122)) ([d931e4c](https://github.com/VorobiovD/air/commit/d931e4c0ff7dc7a2e3a038915dc8fd4f40e0ce3c))

## [1.22.0](https://github.com/VorobiovD/air/compare/v1.21.0...v1.22.0) (2026-06-05)


### Features

* **managed:** deterministic store→wiki mirror render ([#119](https://github.com/VorobiovD/air/issues/119)) ([7612058](https://github.com/VorobiovD/air/commit/76120587ef99d65021f56ba476f062b79b4051b0))

## [1.21.0](https://github.com/VorobiovD/air/compare/v1.20.0...v1.21.0) (2026-06-05)


### Features

* **managed:** opt-in single-agent solo review mode (AIR_REVIEW_MODE) ([#117](https://github.com/VorobiovD/air/issues/117)) ([b15e19f](https://github.com/VorobiovD/air/commit/b15e19f2df1a4a928b94021ba4f68da62abc0b10))

## [1.20.0](https://github.com/VorobiovD/air/compare/v1.19.2...v1.20.0) (2026-06-04)


### Features

* **managed:** optional expected_reviewer identity assertion ([#116](https://github.com/VorobiovD/air/issues/116)) ([ce01a55](https://github.com/VorobiovD/air/commit/ce01a55db003cee070739504dedaa272550c3803))


### Bug Fixes

* **managed:** retry transient preflight billing_error with backoff ([#113](https://github.com/VorobiovD/air/issues/113)) ([1e476fd](https://github.com/VorobiovD/air/commit/1e476fd7369fe2ed425152c5236df0376fb2ce6b))

## [1.19.2](https://github.com/VorobiovD/air/compare/v1.19.1...v1.19.2) (2026-06-03)


### Bug Fixes

* **managed:** default coordinator to inline mode via explicit MODE header ([#112](https://github.com/VorobiovD/air/issues/112)) ([2e7e3c6](https://github.com/VorobiovD/air/commit/2e7e3c67001136ff31c286d48448cc447dc43e30))
* **managed:** fail loud on codex sandbox/inability apology ([#110](https://github.com/VorobiovD/air/issues/110)) ([9c639b2](https://github.com/VorobiovD/air/commit/9c639b262e97fff0b4712ad678e30a05bd4040f6))

## [1.19.1](https://github.com/VorobiovD/air/compare/v1.19.0...v1.19.1) (2026-06-03)


### Bug Fixes

* **codex:** disable bwrap via ~/.codex/config.toml (review ignores the global bypass flag) ([#107](https://github.com/VorobiovD/air/issues/107)) ([7b7c686](https://github.com/VorobiovD/air/commit/7b7c6866074b74f99fd0d2432044a916706a4a53))
* **managed:** pre-post dedup re-check so double-triggered runs don't stack duplicate reviews ([#109](https://github.com/VorobiovD/air/issues/109)) ([25734d0](https://github.com/VorobiovD/air/commit/25734d02057b98a881eeb35ac9b62d9bd9536c8d))

## [1.19.0](https://github.com/VorobiovD/air/compare/v1.18.0...v1.19.0) (2026-06-03)


### Features

* **agents:** targeted context retrieval (Pattern A) — grep pattern files, not whole reads ([#105](https://github.com/VorobiovD/air/issues/105)) ([52fda9e](https://github.com/VorobiovD/air/commit/52fda9e955c3a6c6129712574f09c6ef5067e206))


### Bug Fixes

* **learn:** bound glossary/history/profile growth (kill append-without-cap bloat) ([#101](https://github.com/VorobiovD/air/issues/101)) ([020c0dd](https://github.com/VorobiovD/air/commit/020c0dd33f90506e8d91f1d27f7f53d18bdcdaf2))
* **learn:** make glossary/history caps surgical — preserve rules, gotchas, lifetime aggregates ([#102](https://github.com/VorobiovD/air/issues/102)) ([35dd31e](https://github.com/VorobiovD/air/commit/35dd31e51a5531080e3116b2fbf782f8375b0ae6))
* **migrate:** byte-bound overflow chunking so no memory exceeds the 100KB cap ([#99](https://github.com/VorobiovD/air/issues/99)) ([118e654](https://github.com/VorobiovD/air/commit/118e654087ea6a4b641573e8fbfc419b38947501))

## [1.18.0](https://github.com/VorobiovD/air/compare/v1.17.0...v1.18.0) (2026-06-03)


### Features

* **ci:** fresh input on managed-review + dogfood caller ([#94](https://github.com/VorobiovD/air/issues/94)) ([fc21dcd](https://github.com/VorobiovD/air/commit/fc21dcd6c6b407d60cae5cb40b6ddbf6a16c9413))
* **managed:** agent version pinning via agent_versions workflow input ([#95](https://github.com/VorobiovD/air/issues/95)) ([1e2c57c](https://github.com/VorobiovD/air/commit/1e2c57c8481aece7f075942dfc2241a9d26e13d4))
* **managed:** file-handoff for coordinator inputs via Files-API mounts ([#92](https://github.com/VorobiovD/air/issues/92)) ([344eaaa](https://github.com/VorobiovD/air/commit/344eaaa7a55411b18888fed887c657b826ce05ca))


### Bug Fixes

* **managed:** gate file-handoff behind AIR_FILE_HANDOFF — threads are isolated containers ([#96](https://github.com/VorobiovD/air/issues/96)) ([43f5cdc](https://github.com/VorobiovD/air/commit/43f5cdc592cf0cd50b1ed8f771e32d48800b2d1e))

## [1.17.0](https://github.com/VorobiovD/air/compare/v1.16.0...v1.17.0) (2026-06-02)


### Features

* **managed:** fail loud on run-failed outcomes + billing canary preflight ([#87](https://github.com/VorobiovD/air/issues/87)) ([4a54613](https://github.com/VorobiovD/air/commit/4a546133e272722a1d2905a6bb0a9052616ef384))
* **managed:** per-repo memory-store pattern backend — repo-D pilot ([#90](https://github.com/VorobiovD/air/issues/90)) ([e729dca](https://github.com/VorobiovD/air/commit/e729dca039f2cef9ef99b2baa3844761283ccc34))
* **review:** cross-PR awareness + session-efficiency guidance ([#80](https://github.com/VorobiovD/air/issues/80)) ([108089d](https://github.com/VorobiovD/air/commit/108089d5c69333d4120c9ae5bf7f3b5a5886f3bf))


### Bug Fixes

* **managed:** accept Reviewed-at footer on 12-char SHA prefix match ([#89](https://github.com/VorobiovD/air/issues/89)) ([145078b](https://github.com/VorobiovD/air/commit/145078b77436a23794abf12748fb0a3e05fa32c8))
* **store:** live API list shapes — drop depth param, accept memory_metadata type ([#91](https://github.com/VorobiovD/air/issues/91)) ([540fd14](https://github.com/VorobiovD/air/commit/540fd1491f7ab53173408b8c1ffdac57be52fb06))

## [1.16.0](https://github.com/VorobiovD/air/compare/v1.15.0...v1.16.0) (2026-06-02)


### Features

* **managed:** bump Opus alias 4.7 → 4.8 + correct cost docs ([#84](https://github.com/VorobiovD/air/issues/84)) ([be7342a](https://github.com/VorobiovD/air/commit/be7342a61e070a07c2fe73222ebae15343e63d88))
* **managed:** cooldown debounce + respond-driven re-request ([#86](https://github.com/VorobiovD/air/issues/86)) ([8b408f7](https://github.com/VorobiovD/air/commit/8b408f7ccd43b7e72f9f8fa6507d2af06bdda2d0))


### Bug Fixes

* **managed:** agent removal is archive, not DELETE ([#83](https://github.com/VorobiovD/air/issues/83)) ([be93b39](https://github.com/VorobiovD/air/commit/be93b39cd512d265aca93d93fb8069b6944b2060))

## [1.15.0](https://github.com/VorobiovD/air/compare/v1.14.0...v1.15.0) (2026-06-02)


### Features

* **learn:** auto-trigger every 15 reviews / 14 days, learner on Sonnet ([#81](https://github.com/VorobiovD/air/issues/81)) ([6dfd726](https://github.com/VorobiovD/air/commit/6dfd7264f0cdd1cc2b05bdea50ff448ae32496de))

## [1.14.0](https://github.com/VorobiovD/air/compare/v1.13.0...v1.14.0) (2026-05-23)


### Features

* **managed:** fast-mode Opus on code-reviewer + security-auditor ([#74](https://github.com/VorobiovD/air/issues/74)) ([671e2cc](https://github.com/VorobiovD/air/commit/671e2ccb8cefe81d8c53173d683bf3427ddd70b5))
* **prompts:** drop the PASS/FAIL row table from security audits ([#77](https://github.com/VorobiovD/air/issues/77)) ([a167b38](https://github.com/VorobiovD/air/commit/a167b38377b81ad3bd3d19ea05bec7b310c28e4f))
* **prompts:** security audit — FAIL-only 4-col table (Check|Category|Why|Result) ([#78](https://github.com/VorobiovD/air/issues/78)) ([8b1e447](https://github.com/VorobiovD/air/commit/8b1e44749edfcf70c8fbd480bea1eae8c987f820))

## [1.13.0](https://github.com/VorobiovD/air/compare/v1.12.6...v1.13.0) (2026-05-14)


### Features

* **prompts+respond:** exposure escalation + CLAUDE.md gotcha grep + paired-doc drift + gate-output symmetry + category-symmetric respond gate ([#70](https://github.com/VorobiovD/air/issues/70)) ([6945b02](https://github.com/VorobiovD/air/commit/6945b025537995c2aada2255bc993b189ffd4d69))

## [1.12.6](https://github.com/VorobiovD/air/compare/v1.12.5...v1.12.6) (2026-05-05)


### Bug Fixes

* drop word-boundary in Reviewed-at footer regex ([#67](https://github.com/VorobiovD/air/issues/67)) ([97af181](https://github.com/VorobiovD/air/commit/97af1815452dbdaee391d459c66326dff18ccdc5))

## [1.12.5](https://github.com/VorobiovD/air/compare/v1.12.4...v1.12.5) (2026-05-05)


### Documentation

* roadmap absorbs v1.12.1-v1.12.5 + deferred-finding section ([#65](https://github.com/VorobiovD/air/issues/65)) ([2613a55](https://github.com/VorobiovD/air/commit/2613a554b7a27cd219de8842a63c07d41afc9535))

## [1.12.4](https://github.com/VorobiovD/air/compare/v1.12.3...v1.12.4) (2026-05-05)


### Bug Fixes

* poll REST when SSE stream closes mid-session ([#62](https://github.com/VorobiovD/air/issues/62)) ([03e43f2](https://github.com/VorobiovD/air/commit/03e43f2200305333abe153226e2e3882bf5f3fd9))

## [1.12.3](https://github.com/VorobiovD/air/compare/v1.12.2...v1.12.3) (2026-05-05)


### Bug Fixes

* remove Bash tool from coordinator — closes regurgitation root cause ([#59](https://github.com/VorobiovD/air/issues/59)) ([1cbaae6](https://github.com/VorobiovD/air/commit/1cbaae6a37e1e6f1d1a65f7f3480fdca87cd67ba))
* SSE/REST race in run_session — retry drain on eventually-consistent events ([#61](https://github.com/VorobiovD/air/issues/61)) ([f75eea3](https://github.com/VorobiovD/air/commit/f75eea3a09b46b654cf3b6e90d35aeda50ffb6a4))

## [1.12.2](https://github.com/VorobiovD/air/compare/v1.12.1...v1.12.2) (2026-05-05)


### Bug Fixes

* dump coordinator_out on SHA-mismatch for diagnosis ([#57](https://github.com/VorobiovD/air/issues/57)) ([97df5e4](https://github.com/VorobiovD/air/commit/97df5e4dd8bd3c3713ca28264297e0ed30efe8e3))

## [1.12.1](https://github.com/VorobiovD/air/compare/v1.12.0...v1.12.1) (2026-05-05)


### Bug Fixes

* structured run-failed comment + 422 retry on review post ([#54](https://github.com/VorobiovD/air/issues/54)) ([a3d1208](https://github.com/VorobiovD/air/commit/a3d120816746d6ef7e30cbff6ed9cc9c0ad18d45))

## [1.12.0](https://github.com/VorobiovD/air/compare/v1.11.0...v1.12.0) (2026-05-05)


### Features

* blocker-only re-review gate + carry-forward suppression + workflow concurrency ([#51](https://github.com/VorobiovD/air/issues/51)) ([59c73db](https://github.com/VorobiovD/air/commit/59c73dbf8587e5015495539c27a8d19850c51863))
* severity-aware verdict gate + DEFERRED status for re-reviews ([#49](https://github.com/VorobiovD/air/issues/49)) ([2bcc5af](https://github.com/VorobiovD/air/commit/2bcc5af96ceac8b297eb17ac54cef7737454e9ef))

## [1.11.0](https://github.com/VorobiovD/air/compare/v1.10.0...v1.11.0) (2026-04-29)


### Features

* pre-computation + tier verifier/git-history models ([#46](https://github.com/VorobiovD/air/issues/46)) ([98cda8b](https://github.com/VorobiovD/air/commit/98cda8b03ba4fb618ed535ef542b3ba90e257165))


### Bug Fixes

* extract review through &lt;agent-notification&gt; wrappers in coordinator output ([#47](https://github.com/VorobiovD/air/issues/47)) ([3ab594f](https://github.com/VorobiovD/air/commit/3ab594f302767dfd0179c5b36b4400a5b5b3246c))

## [1.10.0](https://github.com/VorobiovD/air/compare/v1.9.0...v1.10.0) (2026-04-29)


### Features

* submit formal review verdict (APPROVE / REQUEST_CHANGES) in managed mode ([#45](https://github.com/VorobiovD/air/issues/45)) ([d306b0a](https://github.com/VorobiovD/air/commit/d306b0a5014b253645ca77ac9767c7662375b2f2))


### Bug Fixes

* bump coordinator + GHA timeouts to fit larger re-reviews ([#43](https://github.com/VorobiovD/air/issues/43)) ([34ac6e7](https://github.com/VorobiovD/air/commit/34ac6e7555b9554f4d4f2dbb7165d1cd6d2d9ed7))

## [1.9.0](https://github.com/VorobiovD/air/compare/v1.8.0...v1.9.0) (2026-04-28)


### Features

* add Codex as a 5th managed-review specialist (opt-in) ([#38](https://github.com/VorobiovD/air/issues/38)) ([229229f](https://github.com/VorobiovD/air/commit/229229f24b25e4314b7dbbece1b68d6a9e648327))
* automate releases with release-please ([#36](https://github.com/VorobiovD/air/issues/36)) ([4ec0a24](https://github.com/VorobiovD/air/commit/4ec0a24b0118f6a9a33d4d754311ac2b5589d879))
* give reviewers current PR conversation context ([#40](https://github.com/VorobiovD/air/issues/40)) ([52ed8a1](https://github.com/VorobiovD/air/commit/52ed8a1f65c2c4c304c0bbae0ecbef47404eeabc))
* migrate managed reviews to multi-agent coordinator (5 sessions → 1) ([f86ff3f](https://github.com/VorobiovD/air/commit/f86ff3fd2bb93b8b55edd9d3c79d06d928375e06))
* wiki-backed shared /air:learn counter ([#39](https://github.com/VorobiovD/air/issues/39)) ([c48a624](https://github.com/VorobiovD/air/commit/c48a6247ffe2821de6241b35a2603788810d51fa))


### Bug Fixes

* pass AIR_BOT_TOKEN to the Codex target-repo checkout ([#42](https://github.com/VorobiovD/air/issues/42)) ([6fedaac](https://github.com/VorobiovD/air/commit/6fedaac2fa5d2d2dacff3ae83bf358a25a9d890d))

## Changelog

All notable changes to this project will be documented in this file. See [Conventional Commits](https://conventionalcommits.org) for commit guidelines.

Starting with v1.9.0, this file is auto-maintained by [release-please](https://github.com/googleapis/release-please). Releases v1.0.0 through v1.8.0 are documented on the [GitHub Releases page](https://github.com/VorobiovD/air/releases).
