# Changelog

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
