/* Race-focused test for the audience count gate.
   Simulates a stale fetch settling inside the debounce gap after a filter
   change: it must never flip the state back to ok for the old audience. */
const { createCountGate } = require("../app/web/static/count-gate.js");

const fail = (msg) => { console.error("FAIL: " + msg); process.exit(1); };
const tick = () => new Promise((resolve) => setImmediate(resolve));

// Controllable timers and fetches
let timers = [];
const later = (fn) => { timers.push(fn); return fn; };
const sooner = (t) => { timers = timers.filter((x) => x !== t); };
const flights = [];
const fetchJson = () => new Promise((resolve, reject) => flights.push({ resolve, reject }));

const gate = createCountGate(fetchJson, 350);
const seen = [];
gate.onChange((s) => seen.push(s.status));

(async () => {
  // 1. Initial fetch for the old audience goes out.
  gate.refresh();
  if (flights.length !== 1) fail("initial refresh did not fetch");

  // 2. User changes a filter: state must go unknown NOW, before the debounce.
  gate.schedule(later, sooner);
  if (gate.getState().status !== "unknown") fail("schedule did not reset state to unknown");
  if (seen[seen.length - 1] !== "unknown") fail("listeners not told about the unknown state");

  // 3. The stale fetch settles inside the debounce gap - it must be ignored.
  flights[0].resolve({ count: 5, eligible: 5 });
  await tick();
  if (gate.getState().status !== "unknown") fail("stale fetch flipped state to " + gate.getState().status + " (race)");

  // 4. Debounce fires, latest fetch wins.
  if (timers.length !== 1) fail("debounce timer not scheduled");
  timers[0]();
  if (flights.length !== 2) fail("debounced fetch did not fire");
  flights[1].resolve({ count: 2, eligible: 1 });
  await tick();
  const st = gate.getState();
  if (st.status !== "ok" || st.count !== 2 || st.eligible !== 1) fail("latest fetch did not win: " + JSON.stringify(st));

  // 5. Rapid successive changes: only the last debounce survives.
  gate.schedule(later, sooner);
  gate.schedule(later, sooner);
  if (timers.length !== 1) fail("debounce did not collapse rapid changes");
  timers[0]();
  flights[2].resolve({ count: 9, eligible: 9 });
  await tick();
  if (gate.getState().eligible !== 9) fail("rapid-change race: " + JSON.stringify(gate.getState()));

  // 6. Error path: failed fetch flips to error, and a stale failure after a
  //    newer change is ignored too.
  gate.schedule(later, sooner);
  timers[0]();
  flights[3].reject(new Error("down"));
  await tick();
  if (gate.getState().status !== "error") fail("error state not set");
  gate.schedule(later, sooner);
  await tick();
  if (gate.getState().status !== "unknown") fail("schedule after error did not reset state");

  console.log("race test passed");
})().catch((err) => fail(err.stack || String(err)));
