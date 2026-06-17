# Neuron — Open Questions (decisions I need from you)

> These are the choices that meaningfully change *what* I build or *how*. Each has my
> **recommendation** so you can simply say "all recommended" if you like, or adjust any.
> Nothing is built until you approve `PLAN.md`.

---

### Q1 — Component naming
I've proposed an umbrella name **Neuron** with plain service names (`neuron-gateway`,
`neuron-directory`, `neuron-auditor`, `neuron-supervisor`, `neuron-console`,
`neuron-mediascan`, `neuron-scale`). No Element/ESS trademarks are used.
- **Recommendation:** keep these names.
- *Alternatives:* a neuroscience theme (e.g. Myelin/Cortex/Scribe/Sentinel), or your own
  scheme. Any preference, or shall I keep the plain names?

### Q2 — Code location
All new code under a single top-level **`neuron/`** directory; planning docs at repo root;
`docs/feature-analysis.md` stays where you asked. Synapse's tree stays untouched.
- **Recommendation:** confirm `neuron/` as the home for all new code.

### Q3 — Admin console front-end stack
- **Option A (recommended): FastAPI + Jinja2 + HTMX.** No JavaScript build toolchain;
  much gentler for a beginner; fast to build; server-rendered.
- **Option B: React + TypeScript SPA.** Industry standard, richer interactivity, but a
  heavier toolchain and a steeper learning curve.
- **Recommendation:** A (HTMX). Switchable later if you outgrow it.

### Q4 — Authentication model of your target Synapse
This drives which admin endpoints work. Are you running / planning to run **Matrix
Authentication Service (MAS)** (next-gen OIDC auth, MSC3861), or **classic Synapse auth**
(local passwords / legacy SSO)?
- **Why it matters:** under MAS, Synapse disables `reset_password`, set/get admin flag,
  login-as-user, shared-secret register, and account validity — those must be routed to
  MAS instead.
- **Recommendation:** build the **classic** path first (simpler), design the console with
  the **MAS** path as a Phase 2 add-on. Tell me if MAS is required from day one.

### Q5 — Implementation language
- **Recommendation:** **Python everywhere** (matches Synapse, one language to learn,
  strongest Matrix ecosystem). Confirm, or tell me if you'd prefer TypeScript for the bots
  (matrix-bot-sdk) or any service.

### Q6 — E2EE scope and the bot SDK
The audit/supervision bots need encryption support to read encrypted rooms.
- **SDK recommendation:** **matrix-nio** with the `olm` E2EE extra (well-documented,
  Python). Alternative: **mautrix-python** (also good, appservice-oriented).
- **Scope question:** is **plaintext-first, E2EE in a later phase** acceptable (my plan), or
  do you need encrypted-room auditing in the very first usable version?
- **Honest note:** decryption is *forward-only* — messages sent before the bot joined can't
  be read without key sharing/backup. Are you OK with that documented limitation?

### Q7 — Target deployment platform
- **Options:** (a) **docker-compose / single host** (simplest), (b) **Kubernetes** (matches
  ESS Pro, needed for real autoscaling/HA), (c) both.
- **Recommendation:** develop and validate on docker-compose; provide Kubernetes manifests
  for the HA blueprint (Phase 9). Tell me if k8s is the primary target so I prioritize it.

### Q8 — Federation firewall implementation style
- **Option A (recommended to start): a Python ASGI reverse proxy** — most readable, great
  for learning the policy logic, fine for small/medium scale.
- **Option B: nginx/HAProxy or Envoy `ext_authz`** — production-grade performance; our
  service becomes just the authorization decision-maker.
- **Recommendation:** A first, with B documented as the production path. OK?

### Q9 — Directory source for IAM (Feature 2)
Do you have (or will you have) a real **LDAP / Active Directory / SCIM / Microsoft Graph**
source, and which one(s)? If not, I'll build and test against a **test OpenLDAP/Samba**
container.
- **Recommendation:** target **LDAP/AD first** (most common) + a **SCIM** intake endpoint;
  validate against a dev OpenLDAP. Tell me your actual IdP if known so I prioritize it.

### Q10 — Media scanner: reuse vs reimplement
- **Option A: reuse the open-source `matrix-content-scanner-python`** as a deployed
  sidecar (fastest, battle-tested).
- **Option B: clean reimplementation** in our stack (more learning, more code to own).
- **Recommendation:** start by **deploying the open scanner (A)** to get value fast; do a
  clean reimplementation (B) only if you want it as a learning exercise.

### Q11 — License for the *new* Neuron code
This repo (Synapse) is dual-licensed **AGPL-3.0 OR Element Commercial**. What license do
you want for the new Neuron code we add?
- **Recommendation:** **AGPL-3.0-or-later** for consistency with the surrounding Synapse
  code and the Matrix ecosystem. (If you intend a different model, tell me now — it affects
  headers and `NOTICE`/`LICENSE` files we add under `neuron/`.)

### Q12 — Build order / priorities
My plan starts with the **admin console** (lowest risk, immediately useful), defers E2EE,
and does the large **directory sync** after the admin client matures.
- **Recommendation:** follow `PLAN.md` order.
- Tell me if any feature is urgent and should jump the queue (e.g. you need the **audit
  bot** or **federation firewall** first).

---

## How to respond
You can reply with something as short as:

> "Approved — all recommendations" (and I'll begin Phase 0),

or call out only the items you want to change (e.g. "Q3 → React, Q4 → MAS required,
Q12 → do the gateway first"). I will **not** write any implementation code until you
approve.
