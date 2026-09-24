# Problem

## One-line version

Owner-operator truckers lose good loads because reaching the broker is manual and slow, and on a first-come board the fastest carrier wins.

## Who has the problem (wedge customer)

Small carriers and owner-operators (1-3 trucks) who find freight on the DAT One load board. They pay roughly $59-$339/month for DAT (our first customer pays about $200/month). They are usually driving, fueling, or sleeping when a good load posts. There is no dispatcher. The owner is the dispatcher.

## How it works today

1. The trucker searches DAT One and finds a relevant load.
2. If the load provides email contact, they copy the broker email address or use DAT's optional Connected Email flow.
3. They manually send an inquiry containing enough load details for the broker to identify it.
4. They wait for the broker to reply, then go back and forth on rate, equipment/team status, driver information, documents and pickup details.
5. They move to a phone call or rate confirmation when the negotiation reaches a protected decision.

The initial send takes manual attention, and every subsequent reply arrives at an unpredictable moment. The negotiation may consist of only a few terse messages, but missing one can lose the load.

## Why that loses

- Loads are first-come. Good-paying loads are gone within minutes of posting. DAT itself built 5-minute alarms and real-time results because speed decides who gets the load.
- A broker on a hot load gets flooded with emails. The first few credible replies get the call.
- The trucker cannot respond while driving. Every alarm they miss is a load someone else takes.
- Follow-ups and document requests fall through the cracks, so even loads they did reach get lost later.
- The copy is repetitive. Same intro, same MC number, same "truck available in X at Y" every time.

## Evidence the pain is real

- DAT One is the largest freight load board in North America, with about 722,500 loads posted per business day and 266M+ per year.
- DAT built Connected Email, real-time search results and load-match alarms into its core product, which shows that speed-to-contact matters.
- dispatchGo (https://dispatchgo.app) is a paid Chrome extension whose whole pitch is one-click broker emails on DAT loads, "respond faster than competitors." People already pay to save clicks. Nobody (that we found) removes the human from the loop entirely.

## What "solved" looks like

- The trucker pastes the broker email and load facts into our platform, sees the economics and sends from their own Gmail account.
- The system detects and follows that conversation automatically.
- Broker offers are evaluated against the trucker's configured envelope using total pay, loaded miles, deadhead and all-in rate per mile.
- Permitted counters and verified factual answers are drafted or sent automatically.
- The trucker is alerted when the broker wants to call, requests sensitive driver information, appears to accept a price, or sends a rate confirmation.
- The trucker keeps their normal DAT account. Nothing we do can get it banned.

## The general pattern (why this is bigger than trucking)

Any business where inbound opportunities are time-sensitive and the first credible response wins has the same shape:

| Vertical | Opportunity source | Counterparty | "Commit" decisions to escalate |
|---|---|---|---|
| Trucking (wedge) | User-created DAT email thread; approved DAT API later | Freight broker | Calls, sensitive driver data, price acceptance, rate confirmations and contracts |
| Contractors (ContractorOps) | Lead inbox, Angi/Thumbtack/Yelp lead emails, web forms | Homeowner | Quotes, scope, start dates |
| Others later | Any platform that emails alerts, any CRM | Buyer / lead | Price, terms, guarantees |

Each vertical has: a source that emits opportunities (usually by email), a counterparty to contact fast, a set of routine replies that are safe to automate, and a small set of commit decisions only the human can make.

We build trucking first, all the way down, but every piece below the source connector and the playbook content must be vertical-agnostic.

## Non-goals (for now)

- Scraping DAT or any load board.
- Negotiating outside the customer's explicit envelope or completing a binding booking on the customer's behalf.
- Phone calls to brokers (many loads list a phone number instead of email; out of scope for MVP).
- Booking loads inside DAT.
- Fleet management, invoicing, factoring, ELD.
