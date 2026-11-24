// Tests for static/departed.js. Run:  node --test tests/
const test = require("node:test");
const assert = require("node:assert");
const {hasDeparted, upcoming} = require("../static/departed.js");

const H = 3600e3;
const iso = ms => new Date(ms).toISOString().slice(0, 19);
// A flight leaving `min` minutes from now from an origin `off` hours from UTC: local string is shifted by `off`.
const flight = (min, off, zone = "") => {
  const now = Date.now();
  return {depart: iso(now + min * 60e3 + off * H), depart_utc: iso(now + min * 60e3) + zone};
};

test("uses the UTC instant, whatever the origin's zone", () => {
  for (const off of [-8, -7, -5, -4, 0, 2, 9]) {
    assert.equal(hasDeparted(flight(-30, off)), true, `30 min ago at UTC${off}`);
    assert.equal(hasDeparted(flight(30, off)), false, `in 30 min at UTC${off}`);
  }
});

test("a local time that looks past in the viewer's zone is not hidden if the flight is still ahead", () => {
  // Origin far west (UTC-8): local clock reads 8 h behind UTC, so a naive local-vs-UTC compare would call it departed.
  assert.equal(hasDeparted(flight(10, -8)), false);
});

test("accepts an explicit Z or offset on depart_utc", () => {
  assert.equal(hasDeparted(flight(30, -5, "Z")), false);
  assert.equal(hasDeparted(flight(-5, 2, "Z")), true);
  const now = Date.now();
  const f = {depart: iso(now + 60e3), depart_utc: new Date(now + 60e3).toISOString().slice(0, 19) + "+00:00"};
  assert.equal(hasDeparted(f), false);
});

test("without depart_utc, only hides flights that are past under every possible zone (UTC-12)", () => {
  const now = Date.now();
  assert.equal(hasDeparted({depart: iso(now - 1 * H)}), false);    // could be UTC-7 and still ahead
  assert.equal(hasDeparted({depart: iso(now - 11 * H)}), false);
  assert.equal(hasDeparted({depart: iso(now - 13 * H)}), true);
});

test("an implausible offset between local and UTC falls back instead of trusting depart_utc", () => {
  const now = Date.now();
  assert.equal(hasDeparted({depart: iso(now - H), depart_utc: iso(now + 30 * H)}), false);
  assert.equal(hasDeparted({depart: iso(now - H), depart_utc: "garbage"}), false);
});

test("missing or unparseable depart never hides a flight", () => {
  assert.equal(hasDeparted({}), false);
  assert.equal(hasDeparted({depart: "nope"}), false);
});

test("upcoming() filters a list and tolerates null", () => {
  const list = [flight(-30, -7), flight(30, -7)];
  assert.equal(upcoming(list).length, 1);
  assert.deepEqual(upcoming(null), []);
});
