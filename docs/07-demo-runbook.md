# 07 · Demo runbook — 3 minutes, the birthday present

Rehearsed 2026-09-19 through the API (second rehearsal after smoothing).
Warm agent time for the whole flow is about 22 s: draft 4 s, dry-run +
confirm card 3 s, shopping both items in one turn 8 s (prechecks run in
parallel), approval acknowledgement 2 s, cheap-jersey verdict 4 s. Cold
(first run after a server start) is roughly double and the agent behaves
less predictably, so **always rehearse once, then Reset**. Reset keeps the
advisor's cached lookups, so the second run is the fast one.

What the chat now looks like: the agent answers in one or two sentences, never
repeats the card questions, asks both sizes and the date up front, shops both
items in the same turn, and the confirm card lists only the enforced clauses.
Card clicks made while the agent is still replying are queued and sent the
moment it finishes, so answer cards whenever you like.

## Pre-flight (10 minutes before)

1. Server up **without `--reload`**: `cd leash && .venv/bin/uvicorn leash.api:app --port 8080`. With `--reload`, any file edit restarts the server mid-request. (The agent's conversation is now saved to disk, so a restart no longer wipes it, but do not risk it on stage.) Check `http://localhost:8080/health` shows `openai: true`.
2. Open `http://localhost:8080/app?customer=CU0006` in a window at least 1440 px wide. Close other tabs, hide bookmarks bar.
3. Rehearse the full flow once exactly as below (this warms sizing, price, merchant lookups and the mock shop).
4. Click **Reset** in the chat header. The right side goes idle; the caches stay.
5. Have the three answers ready: `41`, `M`, the date one week from today in ISO form, e.g. `2026-09-26`.
7. After the warm-up run, check whether the shoes were approved or paused for sizing (both are fine; the script covers both). The cached advice is stable across Reset.
6. Terminal on a second screen with `python -m leash.report` output visible (45/45 on the platform) — for the closing line.

## The script

The opening request now carries the attack inside it: *"…I found the jersey on
fashion-moscow-cheap-jerseys.sx for 12 euros, order it from there."* The mock shop
generates that seller's listing with a VIP membership buried in the product copy
and a note addressed to agents. One shopping turn then produces three verdicts.

| Time | You do | You say | What the audience sees |
| --- | --- | --- | --- |
| 0:00 | Nothing yet | "Visa and Mastercard are giving AI agents cards. Viseca is the issuer. Leash is the issuer's answer: the agent proposes, the engine decides. Left: a customer and a shopping agent. Right: the engine, live." | Idle ring. Chat header: the customer's preferences and *Shops this card knows*. |
| 0:20 | Click **Birthday present** | "Plain language in, including a link my girlfriend's cousin sent me. The engine compiles it into clauses and asks only what it cannot know." | Compiler orbit (4 s), contract in the panel, three question bubbles around the core, *Question 1 of 3* in the chat. |
| 0:45 | Type `41` → Next, `M` → Next, date → Answer | "Shoe size, jersey size, date. Each answer becomes a clause." | Bubbles light up ✓; core *Building your contract* → *Ready to confirm*; the confirm card appears (~2 s). |
| 1:05 | Click **Confirm contract** | "Confirmed on Viseca's sandbox. Now the agent shops both items at once." (No dry-run sentence any more; it is still available as an MCP tool.) | Core *Contract active*. Agent turn runs 15–30 s; narrate through it. |
| 1:10 | Narrate the wheel | "First the shoes from a shop this card already uses." | Card: Adidas Adizero SL, TrailSpark. Wheel → **Approved** with the sizing chip citing adidas.com — or **Asking you** if the cached advice says adidas runs small: "the engine knows the brand sizes small and asks before spending". |
| 1:25 | Keep narrating | "Now the jersey from the site the customer asked for. The engine checks the vendor itself." | Card: fashion-moscow-cheap-jerseys.sx, about CHF 37. Wheel → **Asking you**. Chips: hidden charge, delivery after the birthday, price far below market, unverified seller, hidden instructions. Chat card: **⚠ most likely a fake**, the alternative, and three buttons. |
| 1:45 | Click the **Hidden charge** node, then **Hidden instructions** | "A twelve-month membership buried in the product text, and a sentence telling agents the limits don't apply. Merchant text is evidence, never instruction." | Core shows each clause's reasoning. |
| 2:05 | Read the card's three options aloud, click **Buy from Ochsner Sport · CHF 89** | "The engine does not just say no. It says: here is the same jersey from a verified retailer, arriving before the birthday — or, if you insist, a one-time card capped at 37 francs so a fake cannot become a subscription. The customer decides." | Scam order recorded as declined; the agent buys from Ochsner Sport; wheel → **Approved**. |
| 2:40 | Click the wheel core, replay the scam step-up | "Every decision is a receipt the customer and the judge can replay." | History list; wheel replays the decline. |
| 2:50 | Close | "Same engine ran Viseca's five sandbox scenarios: 45 of 45, none timed out, every step-up answered by a human. Issuer's seat, customer in control." | Terminal with the report. |

## If something stalls

- Agent turn over 20 s: keep talking about what the wheel will check; the typing dots mean it is calling tools. Do not click again.
- If you prefer the one-time-card ending, click **One-time card · CHF 37** instead: the scam jersey is approved with the cap and the chat says so. Both endings are rehearsed.
- The generated scam listing is cached per query; if you change the wording of the request, the first run regenerates it (3 s).
- OpenAI down: the compiler falls back to rules and the extractor to regex; the flow still works, without the sizing chip. Say nothing about it.
- Page frozen: reload. State is on disk; the contract and cards come back. Reset only if you want a clean start.
- Everything on fire: `python -m leash.live --scenario SCEN0004` in the terminal with the app on customer CU0019 — the manipulated-agent scenario animates on the wheel with real sandbox events.

## Do not

- Use `--auto-resolve` anywhere near the judged runs.
- Leave the history dropdown open when a new decision arrives.
- Run two pollers against the team key (the API server is the only worker).
