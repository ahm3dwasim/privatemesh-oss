# Roadmap

## v0 — core loop (target: Sep 24)
- [x] Token Factory client, Nemotron tier router, token usage accounting
- [x] Memory store: SQLite, per-memory consent flag, retrieval returns approved memories only
- [x] Skill registry: manifest per skill (tools, scopes, risk)
- [x] Permission broker: scope check on every tool call, pending approvals, deny by default
- [x] Append-only audit log (hash-chained)
- [x] Agent loop: plan (Nemotron) → recall → skill calls via broker → answer
- [x] Skills: notes, email draft (never sends), message send (fake transport), Tavily research
      (written and tested against a mocked Tavily — never yet run against the live API)
- [x] FastAPI endpoints: chat, memories (+approve/forget), grants, approvals, audit,
      audit verify, usage, turns/{id}/model_saw
- [x] Web app: chat with inline receipts, approvals, audit timeline, memory consent, usage
- [ ] Deployed demo URL

## v1 — depth (Oct 4–15)
- [ ] Memory editing, Skills & Permissions panel (the memory panel and audit timeline shipped in v0)
- [ ] Voice input; phone PWA with thread handoff
- [ ] Calendar (read + propose) and email draft (never send) skills
- [ ] Deploy API on Nebius Serverless Endpoints

## v2 — proof & polish (Oct 16–28)
- [ ] Eval suite: permission refusal, memory scoping, cost per task
- [ ] Architecture diagram, privacy data-flow doc
- [ ] Demo video, Devpost write-up, product feedback
