# ADR 0008 — Private delivery of security findings on public repos

Status: Proposed (becomes Accepted when step 10.18 verifies the advisory API) · Date: 2026-11-10

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
- Hold only critical findings (amends ADR 0007, which held the whole review). OPEN POINT, decided when 10.8 is
  planned: non-critical findings posted at once with critical ones added after `/confirm` means two reviews per PR,
  which ADR 0007 rejected ("ONE review") — choose between two reviews and keeping one review with the critical
  findings moved to the private channel.
- If the advisory API or permission does not work as expected, fall back to email and record why here.

## Consequences
+ Vulnerability details reach only maintainers on public repos.
- One extra App permission, and installers must accept it.
- Advisories can be disabled on a repo; then the finding needs a fallback (log + neutral note) — specify in 10.18.
- A neutral note still tells readers that a review finished (no content). Not zero signal; acceptable.
- Contributors see less on public repos; the dashboard / advisory is where maintainers read details.

## Revisit when
GitHub changes advisory permissions, or a maintainer-only channel (email) is added for other uses.
