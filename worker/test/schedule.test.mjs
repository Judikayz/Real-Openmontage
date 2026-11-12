import test from "node:test"; import assert from "node:assert/strict";
import { nextRun, normalise, countOccurrences, zonedToUtc, validTz } from "../src/schedule.js";
const iso = ms => new Date(ms).toISOString();
const L = { tz: "Africa/Lagos" };

test("Lagos is UTC+1: daily 09:00 Lagos == 08:00 UTC", () => {
  assert.equal(iso(nextRun({ ...L, kind: "daily", time: "09:00" }, Date.parse("2026-11-12T07:30:00Z"))), "2026-11-12T08:00:00.000Z");
});
test("daily is strictly after 'from' (exact time rolls to next day)", () => {
  assert.equal(iso(nextRun({ ...L, kind: "daily", time: "09:00" }, Date.parse("2026-11-12T08:00:00Z"))), "2026-11-13T08:00:00.000Z");
});
test("daily near local midnight uses the Lagos date, not the UTC date", () => {
  // 23:30 UTC on Nov 12 is 00:30 Nov 13 in Lagos -> 07:00 Lagos on Nov 13 is next
  assert.equal(iso(nextRun({ ...L, kind: "daily", time: "07:00" }, Date.parse("2026-11-12T23:30:00Z"))), "2026-11-13T06:00:00.000Z");
});
test("weekly Mon/Wed 18:30 Lagos", () => {
  const s = { ...L, kind: "weekly", time: "18:30", weekdays: [1, 3] };
  const t1 = nextRun(s, Date.parse("2026-11-12T10:00:00Z")); // Thu Nov 12 -> next Mon Nov 16
  assert.equal(iso(t1), "2026-11-16T17:30:00.000Z");
  assert.equal(iso(nextRun(s, t1)), "2026-11-18T17:30:00.000Z"); // Wed
});
test("monthly day 31 clamps to last day of short months", () => {
  const s = { ...L, kind: "monthly", time: "10:00", day_of_month: 31 };
  assert.equal(iso(nextRun(s, Date.parse("2027-02-01T00:00:00Z"))), "2027-02-28T09:00:00.000Z");
  assert.equal(iso(nextRun(s, Date.parse("2027-02-28T09:00:00Z"))), "2027-03-31T09:00:00.000Z");
  assert.equal(iso(nextRun(s, Date.parse("2028-02-01T00:00:00Z"))), "2028-02-29T09:00:00.000Z"); // leap year
});
test("once: future returns the instant, past returns null", () => {
  const s = { ...L, kind: "once", run_at: "2026-12-01T09:30" };
  assert.equal(iso(nextRun(s, Date.parse("2026-11-12T00:00:00Z"))), "2026-12-01T08:30:00.000Z");
  assert.equal(nextRun(s, Date.parse("2026-12-02T00:00:00Z")), null);
});
test("DST zones also work (New York daily 09:00 across spring-forward)", () => {
  const s = { tz: "America/New_York", kind: "daily", time: "09:00" };
  assert.equal(iso(nextRun(s, Date.parse("2026-03-07T20:00:00Z"))), "2026-03-08T13:00:00.000Z"); // already EDT (UTC-4)
  assert.equal(iso(nextRun(s, Date.parse("2026-03-06T20:00:00Z"))), "2026-03-07T14:00:00.000Z"); // still EST (UTC-5)
});
test("countOccurrences counts missed daily runs", () => {
  const s = { ...L, kind: "daily", time: "09:00" };
  assert.equal(countOccurrences(s, Date.parse("2026-11-09T08:00:00Z"), Date.parse("2026-11-12T08:30:00Z")), 3);
});
test("normalise validates input", () => {
  const now = Date.parse("2026-11-12T00:00:00Z");
  assert.throws(() => normalise({ kind: "hourly" }, now), /kind must be/);
  assert.throws(() => normalise({ kind: "daily", time: "25:00" }, now), /HH:MM/);
  assert.throws(() => normalise({ kind: "weekly", time: "09:00", weekdays: [] }, now), /weekdays/);
  assert.throws(() => normalise({ kind: "weekly", time: "09:00", weekdays: [7] }, now), /weekdays/);
  assert.throws(() => normalise({ kind: "monthly", time: "09:00", day_of_month: 32 }, now), /day_of_month/);
  assert.throws(() => normalise({ kind: "daily", time: "09:00", tz: "Mars/Base" }, now), /timezone/);
  assert.throws(() => normalise({ kind: "once", run_at: "2020-01-01T09:00" }, now), /past/);
  assert.deepEqual(normalise({ kind: "weekly", time: "09:00", weekdays: [3, 1, 1] }, now), { kind: "weekly", tz: "Africa/Lagos", time: "09:00", weekdays: [1, 3] });
  assert.equal(validTz("Africa/Lagos"), true);
});
test("zonedToUtc round-trips", () => { assert.equal(iso(zonedToUtc(2026, 11, 12, 9, 0, "Africa/Lagos")), "2026-11-12T08:00:00.000Z"); });
