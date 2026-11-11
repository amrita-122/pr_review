# ADR 0008 — Private delivery of security findings on public repos

Status: Accepted (advisory API verified in step 10.18, 2026-11-11) · Date: 2026-11-10

## Context
Anything the bot writes to a public PR is public: inline comments, the summary, Check Runs, annotations, even
"1 critical finding pending". The vulnerable code is already visible in a public diff, but the bot ranks and labels
it ("SQL injection, critical, line 42"), which saves an attacker the work. For a security tool that is a defect.
ADR 0007 held critical findings for a human but still posted them publicly after `/confirm`.

## Options considered
1. Draft GitHub security advisory: private to maintainers, no new service. Needs the App permission
   `repository_advisories: write` (to verify, step 10.18).
2. Email / Slack to the maintainer: fully private, but a new service, secrets and an address to configure.
3. Check Run with details: still visible to anyone who can see the PR. Rejected.
4. Do nothing on public repos: the bot would not run on public repos at all.

## Decision
- Read `repository.private` from the webhook.
- **Private repo:** unchanged, inline comments (ADR 0007's hold still applies to held severities).
- **Public repo:** high and critical SECURITY findings go to a draft security advisory. The PR gets one neutral line at
  the end of the review ("review complete"), with no category, severity, file or line. Medium, low and non-security
  findings stay inline. The `/confirm` pending comment on a public repo must not name the severity either.
- Hold only critical findings (amends ADR 0007; decided in 10.8): non-critical findings are posted at once, and the
  held critical ones follow as a second review after `/confirm` (see ADR 0007's amendment). On a public repo the
  high/critical security findings never reach a PR, so no follow-up review exists there; they go to the advisory.
- If the advisory API or permission does not work as expected, fall back to email and record why here.

## Consequences
+ Vulnerability details reach only maintainers on public repos.
- One extra App permission, and installers must accept it.
- Advisories can be disabled on a repo; then the finding needs a fallback (log + neutral note) — specify in 10.18.
- A neutral note still tells readers that a review finished (no content). Not zero signal; acceptable.
- Contributors see less on public repos; the dashboard / advisory is where maintainers read details.

## Verification against GitHub's docs (step 10.18, 2026-11-10)
Read: [REST: repository advisories](https://docs.github.com/en/rest/security-advisories/repository-advisories),
[Creating a repository security advisory](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/creating-a-repository-security-advisory),
[Permissions required for GitHub Apps](https://docs.github.com/en/rest/overview/permissions-required-for-github-apps).

Confirmed by the docs:
- `POST /repos/{owner}/{repo}/security-advisories` creates a repository advisory. Required body: `summary`,
  `description`, `vulnerabilities` (each with `package.ecosystem`). Optional: `severity` OR `cvss_vector_string`,
  `cwe_ids`, `cve_id`, `credits`. Statuses: 201, 403, 404, 422. `summary` max 1024, `description` max 65535.
- The permission is "Repository security advisories" (write). The permissions page lists the endpoint for
  installation tokens (IAT).
- Per the API pages, an unpublished advisory is readable only by security managers, repository admins and
  collaborators on that advisory.

NOT confirmed (must be tried on a real public sandbox repo before this ADR is Accepted):
1. The REST page says the caller "must be a security manager or administrator" to create a draft. The docs do not
   say how that maps to a GitHub App installation token. Try it; if it is a 403, use the email channel below.
2. The "creating an advisory" page says "Anyone with read access to the repository can see the advisory" (credits
   section), which conflicts with the API pages. Check with a read-only account that a draft is invisible.
3. What happens when advisories are disabled or the repo is private: undocumented (403, 404 and 422 all possible).
4. The create call needs a `vulnerabilities` entry; for a code finding there is no package. Use ecosystem `other`
   and a placeholder name (to be tried).

Until 1-3 are checked the code treats the channel as unproven: it is behind `PRIVATE_CHANNEL` (default `none`) and
FAILS CLOSED (see below). Nothing in the PR text changes if the channel is unavailable.

## Sandbox result (step 10.18, 2026-11-11)

Tried on a real public repo (`review-bot-sandbox`, App installation token, dummy content only):
- The App permission `repository_advisories: write` was accepted on the installation.
- `POST /repos/{owner}/{repo}/security-advisories` with the installation token returned 201 and state `draft`.
- An anonymous caller got 404 for the draft by id and an empty list for the repo's advisories.
- The owner could read it. The token could close it (PATCH state=closed, 200) but there is no API delete, so
  a mistaken advisory needs a person to delete it in the Security tab.
- A closed advisory does not show on the default "Draft" tab (it is under "Closed").

Second sandbox round (2026-11-11, owner token, same repo):
- Visibility of a draft: the owner (admin) sees it; a second account with WRITE access (`push`+`triage`, not admin)
  sees nothing in the Security tab; a signed-out visitor gets 404. A true read-only role cannot be tested on a
  personal repo (GitHub answers 422 "Cannot assign ... permission of read"; read-only collaborators need an
  organization). Read is weaker than write, so a read-only user is expected to see nothing either; that part is
  inferred, not observed. This resolves doc conflict 2 above in favour of the API pages (admins/security managers only).
- Private repo: with the repo set to private, both `GET` and `POST .../security-advisories` returned 404 even for
  the owner with admin rights and a `repo`-scope token; after making it public the same POST returned 201. So on a
  private repo the endpoint is unavailable (404). This is fine: private repos do not use the channel (see Decision).
- "Advisories disabled": there is no per-repo switch for repository advisories. Turning off private vulnerability
  reporting (the only related setting) did NOT block creating drafts (still 201). So the only failure we have seen is
  404 on a private repo; 403 (missing permission) and 422 (bad body) remain possible. The fail-closed rule below
  already treats any non-2xx as "unavailable", which covers all of them.

Verified, so this ADR is Accepted. `PRIVATE_CHANNEL` stays `none` by default until the end-to-end check below
(a real planted finding on the public sandbox producing a draft and a neutral PR line) is done by hand (10.21).

## Fail-closed rule (implemented in 10.20)
On a public repo a high/critical security finding is NEVER written to a PR, whatever the channel does:
- channel works: draft advisory created; the PR gets the neutral "review complete" line;
- channel fails or is unavailable (disabled advisories, 403/404/422, no permission): the finding is dropped from
  the PR, kept in the database with `drop_reason = "private delivery failed"`, logged by reason only, and the run
  record counts it, so the maintainer can read it in the database/dashboard. The PR still gets only the neutral line.

## Revisit when
GitHub changes advisory permissions, or a maintainer-only channel (email) is added for other uses.
