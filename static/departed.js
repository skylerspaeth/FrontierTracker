// A flight has departed once its departure instant is past. `depart` is local time at the ORIGIN (no zone), so it can't be
// compared to the viewer's clock directly. `depart_utc` pins the instant; we only trust it if the gap between the two
// is a believable UTC offset. Otherwise fall back to the most generous reading (origin at UTC-12), so we never hide a
// flight that hasn't left.
const ZONE = /(Z|[+-]\d\d:?\d\d)$/i;
function hasDeparted(f){
  const now = Date.now();
  const utc = f.depart_utc ? Date.parse(ZONE.test(f.depart_utc) ? f.depart_utc : f.depart_utc + "Z") : NaN;
  const local = Date.parse(String(f.depart || "").replace(ZONE, "") + "Z");
  if(!isNaN(utc) && !isNaN(local)){
    const off = (local - utc) / 60000;
    if(off >= -720 && off <= 840 && off % 15 === 0) return utc <= now;
  }
  return !isNaN(local) && local + 12 * 3600e3 <= now;
}
const upcoming = flights => (flights || []).filter(f => !hasDeparted(f));

if(typeof module !== "undefined") module.exports = {hasDeparted, upcoming};
