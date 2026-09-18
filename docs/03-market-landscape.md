# 03 · Who else is solving this, and where the gap is

Researched 2026-09-18. Purpose: know what judges have already seen, borrow the
right vocabulary, and pick a differentiator nobody in the room can dismiss as
"that's just Mastercard spend controls".

## Network-level standards (the rails Viseca will actually plug into)

| Standard | Owner | What it gives | Issuer's role |
| --- | --- | --- | --- |
| **Visa Intelligent Commerce** | Visa | Agent-scoped tokens, Agentic Directory of verified agents/merchants, Agent Score, Large Transaction Model for fraud | Issuer sees agent identity on the auth, applies rules |
| **Trusted Agent Protocol (TAP)** | Visa + Cloudflare, Oct 2025 | HTTP Message Signatures (RFC 9421 / Web Bot Auth), Ed25519 keys in a Visa directory → merchant can verify "this agent is legit" | Merchant-side; complementary |
| **Mastercard Agent Pay / Agentic Tokens** | Mastercard, pilots Feb 2026 | Token binds cardholder + registered agent + mandate scope. **Consent is policy-based, not per-transaction**: per-txn cap, monthly cap, allowed MCCs, expiry, optional step-up rules. Revocation in the issuer app kills the token at the network. | **The issuer holds the policy and runs step-up + revoke.** This is Viseca's seat. |
| **AP2 (Agent Payments Protocol)** | Google + 60 partners incl. Mastercard, PayPal, Amex, Sep 2025 | Three signed mandates as W3C Verifiable Credentials: **Intent** (what the user pre-authorised: price limits, timing, rules of engagement), **Cart** (exact items and prices, "what you see is what you pay for"), **Payment** (links method to cart). Human-present vs human-not-present flows. Non-repudiable audit trail. | Conceptual model we should adopt: our compiled policy = Intent Mandate; each live event = a proposed Cart; our decision = whether to issue the Payment Mandate |
| **Stripe ACP / Link wallet for agents / Issuing for agents** (Apr 2026) | Stripe | Shared Payment Tokens, single-use scoped cards, programmatic spend controls, real-time auth decisioning | Developer infra; shows the primitive set the market considers table stakes |
| **x402 / MPP** | Coinbase / others | Machine-to-machine micropayments over HTTP 402 | Not relevant to consumer card control |

## Startups (accelerator-backed where known)

| Company | Backing | Control primitive | Gap relative to Viseca's brief |
| --- | --- | --- | --- |
| **Allowance** | YC 2026 ("spend control layer for AI agents") | One-time virtual cards; amount cap, merchant lock, real-time approve/deny, revoke anytime; agent never sees the PAN | No cart-vs-intent check, no policy from natural language, no explanation |
| **Nekuda** | $5M seed (Madrona, Amex Ventures, Visa Ventures); Visa Intelligent Commerce launch partner | Secure Agent Wallet + "Agentic Mandates" SDK | Mandate is a scope envelope; no semantic item/terms verification |
| **Skyfire** | $9.5M (a16z CSX, Coinbase Ventures) | KYAPay — "Know Your Agent" signed JWT identity; Agent Checkout | Identity only |
| **Payman** | — | `payman.ask()` — intent parsing, policy enforcement, risk control, bank integrations | Closest in spirit (intent → policy), B2B-bank oriented |
| **Basis Theory** | $33M Series B; leads Agentic Commerce Consortium | PCI vault for agent credentials | Credential storage, not decisioning |
| **Prava** | — | Passkey-based user approval on tokenised purchases; on Visa IC | Approval UX only |
| **Forter** | — | Trusted Agentic Commerce Protocol (JWS/JWE); saw +18,510 % agentic traffic day-over-day at ChatGPT checkout launch, +50 % fraud in automated modes | Merchant-side fraud |
| **Cascade, Clam, Agentic Fabriq** | YC W26 | Generic agent action guardrails, "Okta for agents", enterprise agent security | Not payment-specific |
| **Maven** | YC W26 | Payments for voice agents | Different surface |

## The pattern, and the gap

Every product above ships the same five primitives: **amount cap, period cap,
merchant/MCC allowlist, expiry, revoke** — usually plus a manual approve/deny
toggle. Judges have seen this. Mastercard Agent Pay *is* this.

What almost nobody does:

1. **Verify the cart against the intent.** Right item type, right attributes
   (size 43, not 42), right terms (returnable ≥ 14 days, not final sale), no
   unrequested add-ons, right *kind* of seller. AP2 assumes the cart is honest
   once signed; it does not check it against the intent semantically.
2. **Treat merchant text as an attack surface.** Indirect prompt injection is
   now the dominant real-world injection vector (>55 % of observed incidents in
   2026 per CSA). Zscaler documented live campaigns where 4 of 26 models executed
   a fraudulent payment from planted text. Unit 42 observed web-based IPI in the
   wild. The Viseca data plants two payloads in `item_details`.
3. **Explain and calibrate.** Nobody shows the customer *what their policy would
   have done to their own past purchases* before they confirm it, and nobody
   emits clause-level evidence per decision.

Those three are exactly where SCEN0002 and SCEN0004 point. That is our lane.

## Vocabulary to borrow in the pitch

- "Intent Mandate → Cart → Payment" (AP2) — judges from Visa/Mastercard land recognise it.
- "Agentic token / policy-based consent / issuer-side step-up and revocation" (Mastercard).
- "Instructions vs data boundary" (IPI literature) — the architectural reason our engine is injection-resistant *by construction*, not by classifier accuracy.

## Sources

- Viseca case repo — https://github.com/START-Hack/viseca-2026
- Allowance YC launch — https://www.ycombinator.com/launches/QS4-allowance-virtual-cards-for-ai-agents
- Rye, "Agentic Commerce Landscape 2026" — https://rye.com/blog/agentic-commerce-startups
- Google AP2 announcement — https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol
- Mastercard Agent Pay explained — https://eco.com/support/en/articles/15192001-what-is-mastercard-agent-pay-ai-agent-commerce-protocol-in-2026
- Mastercard agentic token framework — https://www.mastercard.com/global/en/news-and-trends/stories/2025/agentic-commerce-framework.html
- Visa Trusted Agent Protocol — https://corporate.visa.com/en/sites/visa-perspectives/newsroom/visa-unveils-trusted-agent-protocol-for-ai-commerce.html
- Cloudflare, "Securing agentic commerce" — https://blog.cloudflare.com/secure-agentic-commerce/
- Visa Intelligent Commerce developer page — https://developer.visa.com/capabilities/visa-intelligent-commerce
- Stripe Link wallet for agents — https://agenticplug.ai/blog/what-is-stripe-link-wallet-for-ai-agents
- YC W26 agent infrastructure — https://www.buildmvpfast.com/blog/yc-w26-batch-agent-infrastructure-boom
- Nekuda seed — https://www.finsmes.com/2025/05/nekuda-raises-5m-in-funding.html
- Skyfire / Nekuda / Basis Theory overview — https://stellagent.ai/insights/agentic-commerce-infra-startups
- Unit 42, web-based IPI in the wild — https://unit42.paloaltonetworks.com/ai-agent-prompt-injection/
- CSA research note on IPI 2026 — https://labs.cloudsecurityalliance.org/research/csa-research-note-indirect-prompt-injection-in-the-wild-2026/
- Protocol comparison (MPP, ACP, AP2, x402) — https://www.crossmint.com/learn/agentic-payments-protocols-compared
