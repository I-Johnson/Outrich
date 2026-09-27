/* Audience count gate: latest-filter-wins sequencing with a fail-closed state.
   Used by the campaign builder in the browser and by the race test in node. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  else { root.CountGate = api; }
})(typeof self !== "undefined" ? self : this, function () {
  function createCountGate(fetchJson, debounceMs) {
    // fetchJson: () => Promise resolving to {count, eligible}; rejects on failure.
    let state = { status: "unknown", count: null, eligible: null };
    let seq = 0;
    let timer = null;
    const listeners = [];
    const emit = () => listeners.forEach((fn) => fn(state));
    const run = (mySeq) => {
      return fetchJson().then((data) => {
        if (mySeq !== seq) return; // a newer filter won the race
        state = { status: "ok", count: data.count, eligible: data.eligible };
        emit();
      }).catch(() => {
        if (mySeq !== seq) return;
        state = { status: "error", count: null, eligible: null };
        emit();
      });
    };
    const refresh = () => run(++seq);
    const schedule = (later, sooner) => {
      // Invalidate any in-flight fetch immediately - before the debounce - so a
      // stale response settling in the gap can never flip the state back to ok.
      seq += 1;
      state = { status: "unknown", count: null, eligible: null };
      emit();
      const setT = later || setTimeout;
      const clearT = sooner || clearTimeout;
      clearT(timer);
      const mySeq = seq;
      timer = setT(() => run(mySeq), debounceMs == null ? 350 : debounceMs);
    };
    const onChange = (fn) => listeners.push(fn);
    return { schedule, refresh, onChange, getState: () => state };
  }
  return { createCountGate };
});
