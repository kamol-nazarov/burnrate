// Runs before the real head prefetch. Every API response is explicitly settled.
window.raceRequests = [];
window.raceTicks = [];
window.raceUnhandled = [];
window.addEventListener("unhandledrejection", event => {
  raceUnhandled.push(String(event.reason));
  event.preventDefault();
});
window.setInterval = callback => { raceTicks.push(callback); return raceTicks.length; };
window.fetch = (url, options = {}) => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  // Deliberately ignore aborts: exercise ownership even if transport completes late.
  raceRequests.push({url: String(url), options, resolve, reject, settled: false});
  return promise;
};
window.raceSummary = key => {
  const data = structuredClone(RACE_FIXTURES.summary);
  data.window.key = key;
  data.window.to = new Date().toISOString();
  data.generatedAt = data.window.to;
  return data;
};
if (RACE_CASE.startsWith("snapshot_")) {
  const summary = raceSummary(RACE_CASE === "snapshot_mismatch" ? "1w" : "1d");
  if (RACE_CASE === "snapshot_expired") summary.window.to = "2000-01-01T00:00:00Z";
  const storedAt = RACE_CASE === "snapshot_stored_expired" ? "2000-01-01T00:00:00Z" : new Date().toISOString();
  localStorage.setItem("burnrate:snapshot:v1:1d", JSON.stringify({storedAt, summary}));
}
