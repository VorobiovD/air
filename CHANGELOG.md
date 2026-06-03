# Changelog

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
* **managed:** per-repo memory-store pattern backend — svc-transcribe pilot ([#90](https://github.com/VorobiovD/air/issues/90)) ([e729dca](https://github.com/VorobiovD/air/commit/e729dca039f2cef9ef99b2baa3844761283ccc34))
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
