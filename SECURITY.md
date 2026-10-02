# Security review

Review date: **2026-10-02**. This is a bounded code and dependency review, not a claim that the project has no vulnerabilities.

## Scope and checks

| Check | Result |
| --- | --- |
| Manual code review + independent review | Bot access/callbacks, owner edits, source binding, paths, SQL, publication and input handling inspected |
| `pip-audit 2.10.1` against the pinned lock | No known advisories after the pytest patch |
| `Bandit 1.9.4`, `app/` and `scripts/` | No findings |
| `Gitleaks 8.30.1`, Git history | No detected credentials; this does not prove absence of all sensitive data |
| `Trivy 0.75.0`, built application and PostgreSQL images | Remaining image findings listed below |
| Synthetic tests with isolated local PostgreSQL | **39 passed**, no skips; includes access, paths, revisions, publication and oversized text |

The demo import was repeated: eight messages and eight revisions, with no new revision on replay. Database migrations reached `0009_bot_responses`; schema comparison found no pending operations. Live Telegram was not configured or exercised.

## Fixed in this review

- **Publication denial of service:** an oversized line containing a source URL could retry on a fresh page forever. It now splits when necessary and always consumes input.
- **Homework formatting denial of service:** an oversized first line could become a repeated title and produce a negative body width. The formatter now uses a bounded title and keeps the original content in the body.
- **Telegram text limits:** both formatters count UTF-16 units, including emoji. Regression checks preserve all content and keep each part below the limit.
- **Test dependency:** pytest was updated from 8.4.2 to 9.0.3 for [CVE-2025-71176](https://github.com/advisories/GHSA-6w46-j5rx-g56g).
- **Build/runtime dependencies:** the build updates Debian packages, including the patched PCRE2 package for [CVE-2026-103111](https://security-tracker.debian.org/tracker/CVE-2026-103111). It uses pinned pip 26.2.1 during installation, checks dependency consistency, then removes pip and its unnecessary vendored libraries from the runtime image. Rebuild the image to change dependencies.
- **Container privileges:** application services drop Linux capabilities and enable `no-new-privileges`. PostgreSQL retains the privileges required by its official entrypoint. No database ports or Docker socket are published to the host.
- **Lifecycle:** this project uses the isolated `study-chat-local` Compose project, disables restart policies, and runs locally only during active work. All project services were stopped after verification; persistent volumes were retained.

## Remaining findings and trust boundaries

### Container image inventory

The application image's Debian 13.7 packages still have **166 package/advisory matches**: 44 high, 60 medium, 60 low and 2 unknown. The scanner supplies no patched version for these matches in this distribution. The Python package inventory has no remaining findings. Counts describe installed components, not 166 independently demonstrated attack paths.

The PostgreSQL 15 Alpine 3.24.2 image has no reported Alpine-package findings, but its bundled `gosu` binary produces **46 Go-component matches**: 1 critical, 21 high, 21 medium, 2 low and 1 unknown. Those components are maintained upstream in the official image. The scanner's Go HTTP/TLS advisories do not by themselves demonstrate a remotely reachable attack in this deployment: `gosu` is used by the entrypoint to switch to the fixed database user, not as an HTTP/TLS server. This is a reachability assessment based on the [gosu source](https://github.com/tianon/gosu) and [PostgreSQL entrypoint](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh), not a blanket dismissal of every finding.

Refresh official base images and rescan on subsequent maintenance. Do not treat a clean Python lock audit as a clean operating-system image. No major database-version change, database reset or custom replacement of the upstream entrypoint was made to suppress scanner results.

### Homework authenticity

An explicit subject and task can be parsed from **any member of the bound source group**. `confirmed` describes parser evidence; it is not proof that a verified teacher authored the message. An untrusted group member could insert a misleading task. Source references and owner review help inspection but do not enforce a trusted-teacher allowlist. A stricter author policy would require an explicit product decision.

### Operational boundaries

- Bot command access is restricted by chat IDs; owner actions additionally verify the user. Adding a command group grants its members access to the educational summary.
- The collector is read-only, with private account/session and group-binding checks. Source, command groups and publication channel must remain distinct.
- Source text and raw payloads in PostgreSQL are sensitive when using a real group. There is no automatic retention policy or database encryption implemented by this application.
- Keep `.env`, exports, sessions, media and backups private. Public examples are invented; no credential scanner proves anonymization by itself.
- Application processes still use the base image's default user. Capability restrictions reduce privileges but do not replace a future tested non-root setup with compatible existing-volume permissions.
- This review did not include a host/SSH/firewall audit, a network penetration test, load testing, real Telegram conversations or exhaustive proof of parser behavior.

Avoid including real messages, credentials or user IDs in public issues or diagnostic output.
