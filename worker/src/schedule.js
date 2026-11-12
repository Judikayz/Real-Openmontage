// Pure schedule logic (no Cloudflare APIs) so it can be unit-tested in plain Node.
// Supports once / daily / weekly / monthly in any IANA timezone (default Africa/Lagos, UTC+1, no DST).
export const DEFAULT_TZ = "Africa/Lagos";
const pad = n => String(n).padStart(2, "0");

export function validTz(tz) { try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); return true; } catch { return false; } }

function partsInTz(ms, tz) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", weekday: "short" });
  const o = {}; for (const p of f.formatToParts(new Date(ms))) o[p.type] = p.value;
  return { y: +o.year, m: +o.month, d: +o.day, h: +o.hour % 24, mi: +o.minute, dow: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(o.weekday) };
}
function offsetMs(ms, tz) { const p = partsInTz(ms, tz); return Date.UTC(p.y, p.m - 1, p.d, p.h, p.mi) - Math.floor(ms / 60000) * 60000; }

// local wall-clock time in tz -> UTC ms (handles DST; a non-existent local time moves forward)
export function zonedToUtc(y, m, d, h, mi, tz) {
  const guess = Date.UTC(y, m - 1, d, h, mi);
  let t = guess - offsetMs(guess, tz);
  t = guess - offsetMs(t, tz);
  return t;
}
const daysInMonth = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();

// Parse "YYYY-MM-DDTHH:MM" (local to tz) for one-time schedules.
function parseLocal(s) { const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(s || ""); return m ? m.slice(1).map(Number) : null; }

/** Next occurrence strictly after `fromMs`, as UTC ms, or null (one-time already past). */
export function nextRun(s, fromMs) {
  const tz = s.tz || DEFAULT_TZ;
  if (s.kind === "once") {
    const p = parseLocal(s.run_at); if (!p) return null;
    const t = zonedToUtc(p[0], p[1], p[2], p[3], p[4], tz); return t > fromMs ? t : null;
  }
  const [hh, mm] = (s.time || "00:00").split(":").map(Number);
  const today = partsInTz(fromMs, tz);
  for (let i = 0; i < 800; i++) {
    const base = Date.UTC(today.y, today.m - 1, today.d + i);
    const dt = new Date(base), y = dt.getUTCFullYear(), m = dt.getUTCMonth() + 1, d = dt.getUTCDate(), dow = dt.getUTCDay();
    let ok = false;
    if (s.kind === "daily") ok = true;
    else if (s.kind === "weekly") ok = (s.weekdays || []).includes(dow);
    else if (s.kind === "monthly") ok = d === Math.min(s.day_of_month || 1, daysInMonth(y, m)); // day 31 -> last day of short months
    if (!ok) continue;
    const t = zonedToUtc(y, m, d, hh, mm, tz);
    if (t > fromMs) return t;
  }
  return null;
}

/** Number of scheduled occurrences in (afterMs, upToMs] — used to report how many runs were missed. */
export function countOccurrences(s, afterMs, upToMs, cap = 100) {
  let n = 0, t = afterMs;
  while (n < cap) { const nx = nextRun(s, t); if (nx === null || nx > upToMs) break; n++; t = nx; }
  return n;
}

/** Validate and normalise user input. Throws Error(message) on bad input. */
export function normalise(input, nowMs = Date.now()) {
  const kind = input.kind; if (!["once", "daily", "weekly", "monthly"].includes(kind)) throw new Error("kind must be once, daily, weekly or monthly");
  const tz = input.tz || DEFAULT_TZ; if (!validTz(tz)) throw new Error(`unknown timezone: ${tz}`);
  const out = { kind, tz };
  if (kind === "once") {
    if (!parseLocal(input.run_at)) throw new Error("run_at must look like 2026-12-01T09:30 (local time in the schedule's timezone)");
    out.run_at = input.run_at; if (nextRun(out, nowMs) === null) throw new Error("run_at is in the past");
  } else {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(input.time || "")) throw new Error("time must be HH:MM (24h)");
    out.time = input.time;
    if (kind === "weekly") {
      const w = input.weekdays; if (!Array.isArray(w) || !w.length || !w.every(x => Number.isInteger(x) && x >= 0 && x <= 6)) throw new Error("weekdays must be a non-empty list of 0-6 (0 = Sunday)");
      out.weekdays = [...new Set(w)].sort();
    }
    if (kind === "monthly") { const d = input.day_of_month; if (!Number.isInteger(d) || d < 1 || d > 31) throw new Error("day_of_month must be 1-31"); out.day_of_month = d; }
  }
  return out;
}
export const describe = s => s.kind === "once" ? `once at ${s.run_at} (${s.tz})` : s.kind === "daily" ? `daily at ${s.time} (${s.tz})`
  : s.kind === "weekly" ? `weekly on ${(s.weekdays || []).map(d => ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d]).join("/")} at ${s.time} (${s.tz})` : `monthly on day ${s.day_of_month} at ${s.time} (${s.tz})`;
