# Product Requirements Document (PRD)

**Product:** FlexyVotes, a secure voting platform for paid public voting and institutional elections
**Market:** Ghana first (GHS, mobile money, USSD, Ghana levy and VAT), built for other countries
**Source requirements:** [FEATURES.md](FEATURES.md) · **Coverage:** [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)

## 1. Problem

Two kinds of customers need online voting, and today they are served badly.

- **Entertainment and awards organizers** run "pay to vote" competitions. Their biggest
  risks are lost money (unverified payments, chargebacks, payment-bypass bugs) and
  manipulated results (vote farms, stolen cards, bots). Fans want to pay with mobile money
  or USSD, not only with cards.
- **Institutions** (universities, SRCs, unions, professional bodies, cooperatives,
  companies) run elections where every eligible member gets exactly one secret ballot. Their
  biggest risks are disputed outcomes: double voting, ineligible voters, officials who can
  see or change ballots, and no evidence to settle a challenge.

## 2. Goals

1. One election engine serves both use cases. Payments are an optional plug-in, never
   required for elections.
2. **Integrity:** a vote is counted only if it is valid, at most once, and only when it
   was paid for (if payment applies).
3. **Secrecy (institutional):** nobody, including platform staff and election officials,
   can link a ballot to a voter.
4. **Verifiability:** voters can check their ballot was counted; anyone can check the
   published result matches the certified data.
5. **Accountability:** every administrative action is attributable and recorded in a log
   that can't be quietly edited.
6. **Reach:** the voting flow works on a basic phone browser, on slow connections, with
   assistive technology, and in English or French. Paid voting also works over USSD.
7. **Operable as a SaaS:** multi-tenant organizations, plans and invoices, monitoring,
   backups.

### Non-goals

- Binding national or public elections: no legal certification, and no in-person polling
  hardware.
- Coercion resistance against a voter who is being watched while voting. Receipts are
  designed not to prove *how* someone voted, but someone standing over the voter is out of
  scope.
- Native mobile apps. The web UI is responsive, and the REST API can back an app later.

## 3. Users and roles

| Persona | Needs |
|---|---|
| **Fan / public voter** | Find a contestant, pay quickly (MoMo, card, USSD), see the vote counted |
| **Institutional voter** | Sign in with what the institution provides, understand the ballot, vote once, get proof it counted |
| **Organizer / election administrator** | Set up an election, import voters, run it, publish results |
| **Election reviewer** | Independently approve the configuration before it goes live |
| **Results officer** | Tally, certify and publish; run recounts |
| **Finance officer** | Payments, refunds, reconciliation, revenue, invoices |
| **Fraud analyst** | Review held payments and anomaly alerts |
| **Election officer** | Support voters during polling: credentials, incidents |
| **Auditor / observer** | Read-only oversight: audit log, results, verification bundle |
| **Candidate** | Maintain profile and manifesto; see campaign statistics |
| **Support agent** | Answer tickets from voters and organizers |
| **Platform admin** | Organizations, organizer approval, system health |

These map onto 12 built-in roles (Super Admin, Organization Admin, Election Administrator,
Election Reviewer, Election Officer, Election Auditor, Candidate Manager, Finance Officer,
Support Agent, Fraud Analyst, Results Officer, Voter). A role can be granted for the whole
platform, for one organization, or for one election. The permission matrix is in
[SECURITY.md](SECURITY.md#3-access-control).

## 4. Functional requirements

### 4.1 Elections (both products)
- **Election setup:** an election has positions (categories), candidates, a schedule, a
  timezone, a currency and a results-visibility setting (live, after close, after
  publish).
- **Lifecycle:** Draft → Review → Approved → Scheduled → Open ⇄ Paused → Closed →
  Tallying → Certified → Published → Archived.
- **Separation of duties:** the submitter can't approve the election, and the tallier
  can't certify the result.
- **Automatic transitions:** voting opens and closes on schedule.
- **Change control:** after approval, configuration changes send the election back for
  re-approval. Voting-time changes are restricted. Each scope can be frozen, and
  unfreezing needs dual approval.

### 4.2 Paid voting
- **Buying votes:** a fan picks a contestant and buys votes by count or as a bundle (with
  bonus votes and promotions). Discount codes are optional.
- **Payment channels:** card, mobile money, bank and USSD, all through Paystack.
- **Crediting:** votes are credited only after server-side confirmation of the exact
  amount and currency, exactly once, even with duplicate webhooks, retries or concurrent
  callbacks.
- **Limits:** a minimum and maximum per transaction, per-voter vote and spend caps, and
  country restrictions.
- **Money operations:**
  - Refunds; those above a threshold need dual approval.
  - Chargebacks reverse votes.
  - Reconciliation against Paystack runs every 15 minutes.
  - Revenue reports, with platform fee and organizer share.
- **Fraud:** every payment gets a risk score. High-risk payments are held for an analyst
  instead of being credited.

### 4.3 Institutional elections
- **Voter roll:** import from CSV or Excel, add individually, or let voters self-register
  with an allowed email domain. Each voter has a status: eligible, verified, voted,
  suspended or ineligible.
- **Constituencies:** a tree, for example faculty → department. Positions can be limited
  to a constituency, and eligibility rules use attributes and constituencies.
- **Sign-in methods** (any combination per election):
  - voter ID and access code;
  - email OTP;
  - SMS OTP;
  - Google or Microsoft SSO;
  - LDAP or Active Directory;
  - a platform account.

  A second factor can be required.
- **Ballot types:** single choice, FPTP, multiple choice (min/max), approval, ranked
  (IRV/STV), score and referendum. Abstention is allowed where configured.
- **Casting:** the voter reviews their choices and must confirm explicitly. A ballot can't
  be cast twice under any race, refresh or retry.
- **Receipt:** a ballot tracker the voter can check on the public bulletin board.
- **Credentials:** issue, export, resend and reset. Bulk reset needs dual approval.
- **Turnout:** aggregate turnout monitoring while voting is open.
- **Results:** tally, then certify (with a signature), then publish, plus recounts.
  Exports in CSV, Excel and PDF.
- **Integrity controls:** disputes from voters and observers, incidents, evidence with
  checksums, and legal hold.

### 4.4 Platform
- **Tenancy:** multi-tenant organizations. Each user gets a personal organization for
  legacy events, and can switch between organizations.
- **Billing:** plans (Free, Professional, Enterprise, High Assurance), each with limits and
  features. Invoices include the Ghana levy and VAT. Coupons are supported.
- **Notifications:** email, SMS, WhatsApp and in-app, with templates, retries and
  de-duplication.
- **Candidate portal:** invitation link, profile, manifesto documents and campaign
  statistics.
- **Support desk:** a public contact form that creates tickets, plus a staff ticket queue.
- **Carried over from the original app:** ticketing with QR check-in, guest list and
  merchandise store.
- **REST API v1:** token auth, idempotency keys, OpenAPI docs.

### 4.5 Voter experience and accessibility
- Every voting step works without JavaScript.
- Target: WCAG 2.1 AA. That means labels, visible focus, contrast, error summaries and skip
  links.
- High-contrast, large-text and low-bandwidth modes.
- English and French. Times show in the election's timezone, and the voter can override
  it.

## 5. Non-functional requirements

| Area | Requirement |
|---|---|
| Security | OWASP ASVS L2 controls; Argon2 passwords; MFA for staff; strict CSP; PII and secrets encrypted at rest; no secrets in code |
| Integrity | Hash-chained audit, append-only at database level; signed configuration and results |
| Availability | Stateless web and workers behind a load balancer; health checks; queue backpressure |
| Performance | Casting and crediting each take one row lock plus one insert; tested under concurrent load |
| Recovery | RPO ≤ 5 min (RDS PITR); RTO ≤ 1 h; monthly restore tests ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)) |
| Observability | Prometheus metrics, alert rules, JSON logs with correlation IDs, Sentry, OpenTelemetry |
| Portability | One Docker image; AWS ECS Fargate or a single EC2 host; port set through `.env` |

## 6. Success measures

- Zero votes credited without a confirmed, matching payment. Reconciliation discrepancies
  are resolved within 24 hours.
- Zero double-cast ballots. The audit chain verifies clean every 6 hours.
- 95% of voters finish casting in under 3 minutes after signing in.
- Every certified result verifies with `tools/verify_election.py`.
- Restore tests meet the RTO every month.
