"""Tests the page's fuzzy city matcher (needs Node). Run from phase2/:  python tests/test_fuzzy.py"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).parent.parent
sys.path.insert(0, str(root / "src"))
from cities import CITIES  # noqa: E402

if not shutil.which("node"):
    print("fuzzy tests SKIPPED (node not installed)")
    sys.exit(0)

html = (root / "src/web/index.html").read_text(encoding="utf-8")
m = re.search(r"// FUZZY-START\n(.*?)// FUZZY-END", html, re.S)
assert m, "fuzzy block markers missing"
items = [{"name": c["name"], "t": c["tier"]} for c in CITIES]

# (query, expected first result). None = expect NO result.
cases = [
    ("Delhi", "Delhi"), ("delhi", "Delhi"), ("dehli", "Delhi"), ("New Delhi", "Delhi"),
    ("mumbai", "Mumbai"), ("mumbay", "Mumbai"), ("bombay", "Mumbai"), ("mumbi", "Mumbai"),
    ("kolkata", "Kolkata"), ("kolkatta", "Kolkata"), ("calcutta", "Kolkata"),
    ("chennai", "Chennai"), ("chenai", "Chennai"), ("madras", "Chennai"),
    ("bangalore", "Bengaluru"), ("banglore", "Bengaluru"), ("bengaluru", "Bengaluru"), ("beng", "Bengaluru"),
    ("hydrabad", "Hyderabad"), ("hyderbad", "Hyderabad"),
    ("ahmedabd", "Ahmedabad"), ("ahemdabad", "Ahmedabad"),
    ("luknow", "Lucknow"), ("lakhnau", None),
    ("jaypur", "Jaipur"), ("jaipure", "Jaipur"),
    ("kharagpure", "Kharagpur"), ("khargpur", "Kharagpur"), ("kharag", "Kharagpur"),
    ("pune", "Pune"), ("poona", "Pune"),
    ("vizag", "Visakhapatnam"), ("trivandrum", "Thiruvananthapuram"), ("cochin", "Kochi"),
    ("mysore", "Mysuru"), ("baroda", "Vadodara"), ("gurgaon", "Gurugram"), ("allahabad", "Prayagraj"),
    ("calicut", "Kozhikode"), ("pondicherry", "Puducherry"), ("banaras", "Varanasi"), ("benares", "Varanasi"),
    ("varansi", "Varanasi"), ("bhubaneshwar", "Bhubaneswar"), ("guwahti", "Guwahati"),
    ("simla", "Shimla"), ("panjim", "Panaji"), ("mangalore", "Mangaluru"), ("belgaum", "Belagavi"),
    ("dehradun", "Dehradun"), ("dehradoon", "Dehradun"), ("leh", "Leh"),
    ("zzzzzz", None), ("qx", None), ("a", None), ("", None),
]
js = m.group(1) + """
var items = %s, cases = %s, bad = [];
cases.forEach(function (c) {
  var r = fuzzyFind(c[0], items, 6), top = r.length ? r[0].name : null;
  if (top !== c[1]) bad.push(c[0] + " -> " + top + " (wanted " + c[1] + ")");
});
// every alias must point at a real city
Object.keys(ALIASES).forEach(function (a) {
  if (!items.some(function (i) { return i.name === ALIASES[a]; })) bad.push("alias " + a + " -> missing city " + ALIASES[a]);
});
// ranking: exact beats fuzzy; big cities win ties; result list is capped
var r = fuzzyFind("pune", items, 6);
if (r[0].name !== "Pune") bad.push("exact not first");
if (fuzzyFind("ba", items, 6).length > 6) bad.push("limit ignored");
console.log(JSON.stringify(bad));
""" % (json.dumps(items), json.dumps(cases))
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
    f.write(js)
out = subprocess.run(["node", f.name], capture_output=True, text=True)
assert out.returncode == 0, out.stderr
bad = json.loads(out.stdout.strip().splitlines()[-1])
assert not bad, "\n".join(bad)
print(f"fuzzy tests OK ({len(cases)} queries)")
