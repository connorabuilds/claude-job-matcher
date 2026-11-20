"""Tests for the job-matcher helper script. Standard library only: python3 -m unittest discover tests"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/job-matcher/skills/run/scripts"))
import jm  # noqa: E402

PROFILE = json.loads((ROOT / "examples/sample-profile.json").read_text())
NOW = datetime.now(timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


GREENHOUSE = {"jobs": [
    {"id": 1, "title": "Product Manager, Checkout", "location": {"name": "Chicago, IL"}, "first_published": iso(2),
     "absolute_url": "https://example.com/jobs/1", "company_name": "Example Shop",
     "content": "&lt;p&gt;Own checkout.&lt;/p&gt;&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;3+ years of product management&lt;/li&gt;&lt;/ul&gt;"},
    {"id": 2, "title": "Staff Product Manager", "location": {"name": "Chicago, IL"}, "first_published": iso(1), "content": ""},
    {"id": 3, "title": "Product Manager", "location": {"name": "London, UK"}, "first_published": iso(1), "content": ""},
    {"id": 4, "title": "Product Manager, Search", "location": {"name": "Remote - United States"}, "first_published": iso(3), "content": "x"},
    {"id": 5, "title": "Product Manager, Ads", "location": {"name": "Austin, TX"}, "first_published": iso(3), "content": "Hybrid, 3 days in Austin."},
    {"id": 6, "title": "Product Manager, Old", "location": {"name": "Chicago, IL"}, "first_published": iso(40), "content": ""},
    {"id": 7, "title": "Software Engineer", "location": {"name": "Chicago, IL"}, "first_published": iso(1), "content": ""},
    {"id": 8, "title": "Product Manager, Growth", "location": {"name": "Austin, TX"}, "first_published": iso(2),
     "content": "This role can be performed remotely from anywhere in the United States."},
]}
ASHBY = {"organizationName": "Example Labs", "jobs": [
    {"id": "a", "title": "Associate Product Manager", "location": "New York", "secondaryLocations": [{"location": "Chicago"}],
     "publishedAt": iso(1), "workplaceType": "Hybrid", "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/example/a",
     "descriptionHtml": "<p>Hybrid role</p>", "compensation": {"compensationTierSummary": "$120K - $140K"}},
    {"id": "b", "title": "Product Manager", "location": "San Francisco", "publishedAt": iso(1), "workplaceType": "Hybrid",
     "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/example/b", "descriptionHtml": "<p>Hybrid in SF</p>"},
]}
LEVER = [
    {"text": "Product Operations Manager", "createdAt": int((NOW - timedelta(days=4)).timestamp() * 1000),
     "categories": {"location": "Remote", "allLocations": ["Remote"]}, "workplaceType": "remote",
     "hostedUrl": "https://jobs.lever.co/example/1", "descriptionPlain": "Run ops.",
     "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}], "salaryRange": {"min": 100000, "max": 130000, "currency": "USD", "interval": "per-year-salary"}},
]


def fake_http(url, timeout=20.0):
    if "greenhouse" in url:
        return GREENHOUSE
    if "ashbyhq" in url:
        return ASHBY
    if "lever" in url:
        return LEVER
    raise AssertionError(url)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = jm.main(argv)
    return code, out.getvalue(), err.getvalue()


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.F = jm.Filters(PROFILE)

    def test_titles(self):
        self.assertTrue(self.F.title_ok("Senior Product Manager, Payments"))
        self.assertTrue(self.F.title_ok("Associate Product Manager"))
        self.assertFalse(self.F.title_ok("Staff Product Manager"))
        self.assertFalse(self.F.title_ok("Director of Product Management"))
        self.assertFalse(self.F.title_ok("Software Engineer"))
        self.assertFalse(self.F.title_ok("Product Manager Intern"))

    def test_word_boundaries(self):
        self.assertFalse(self.F.title_ok("Product Managerial Accountant"))
        F = jm.Filters({"target_titles": ["Analyst"], "filters": {"title_exclude": ["vp"]}})
        self.assertTrue(F.title_ok("Data Analyst, VPN team"))
        self.assertFalse(F.title_ok("VP, Analyst Relations"))

    def test_locations(self):
        self.assertTrue(self.F.location_ok("Chicago, IL"))
        self.assertTrue(self.F.location_ok("Remote - United States"))
        self.assertTrue(self.F.location_ok("United States"))
        self.assertFalse(self.F.location_ok("London, UK"))
        self.assertFalse(self.F.location_ok("Remote - Canada"))
        self.assertFalse(self.F.location_ok("Austin, TX"))
        self.assertTrue(self.F.location_ok("Austin, TX", "Open to fully remote candidates in the US"))
        self.assertTrue(self.F.location_ok("Austin, TX", "", ats_remote=True))

    def test_remote_off(self):
        p = json.loads(json.dumps(PROFILE))
        p["locations"]["remote_ok"] = False
        F = jm.Filters(p)
        self.assertFalse(F.location_ok("Remote - United States"))
        self.assertTrue(F.location_ok("Chicago, IL"))

    def test_excluded_company_whole_word(self):
        F = jm.Filters({"excluded_companies": ["Bolt"]})
        self.assertTrue(F.company_excluded("Bolt"))
        self.assertFalse(F.company_excluded("Boltzmann Labs"))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self._orig = jm.http_json
        jm.http_json = fake_http
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "p.json").write_text(json.dumps(PROFILE))
        (self.tmp / "b.json").write_text(json.dumps({"greenhouse": ["ex"], "ashby": [{"slug": "exl", "name": "Example Labs"}],
                                                     "lever": [{"slug": "exo", "name": "Example Ops"}]}))

    def tearDown(self):
        jm.http_json = self._orig

    def fetch(self, *extra):
        code, out, err = run(["fetch", "--profile", str(self.tmp / "p.json"), "--boards", str(self.tmp / "b.json"), *extra])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_fetch_filters(self):
        got = {(j["source"], j["title"]) for j in self.fetch()}
        self.assertEqual(got, {
            ("greenhouse", "Product Manager, Checkout"),
            ("greenhouse", "Product Manager, Search"),
            ("greenhouse", "Product Manager, Growth"),
            ("ashby", "Associate Product Manager"),
            ("lever", "Product Operations Manager"),
        })

    def test_fields(self):
        rows = {j["title"]: j for j in self.fetch()}
        gh = rows["Product Manager, Checkout"]
        self.assertEqual(gh["company"], "Example Shop")
        self.assertIn("3+ years of product management", gh["description"])
        self.assertNotIn("<", gh["description"])
        ab = rows["Associate Product Manager"]
        self.assertEqual(ab["workplace"], "Hybrid")
        self.assertEqual(ab["salary"], "$120K - $140K")
        self.assertEqual(rows["Product Operations Manager"]["company"], "Example Ops")
        self.assertIn("SQL", rows["Product Operations Manager"]["description"])
        self.assertTrue(all(j["key"] for j in rows.values()))
        self.assertTrue(all("_full" not in j for j in rows.values()))

    def test_state_skips_seen(self):
        st = jm.empty_state()
        st["seen"][jm.h10(jm.job_key("Example Shop", "Product Manager, Checkout"))] = "260101"
        st["recent"] = [{"k": "x", "u": "https://jobs.lever.co/example/1", "d": "2026-01-01"}]
        (self.tmp / "s.json").write_text(json.dumps(st))
        titles = {j["title"] for j in self.fetch("--state", str(self.tmp / "s.json"))}
        self.assertNotIn("Product Manager, Checkout", titles)
        self.assertNotIn("Product Operations Manager", titles)  # same link seen before
        self.assertIn("Product Manager, Search", titles)

    def test_network_failure(self):
        def boom(url, timeout=20.0):
            raise OSError("blocked")
        jm.http_json = boom
        code, out, err = run(["fetch", "--profile", str(self.tmp / "p.json"), "--boards", str(self.tmp / "b.json")])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"], "network")


class StateAndPageTests(unittest.TestCase):
    def test_roundtrip_and_escaping(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "p.json").write_text(json.dumps(PROFILE))
        scored = {"run": {"candidates": 3, "notes": ["first run"]},
                  "discovered_boards": {"ashby": [{"slug": "newco", "name": "NewCo"}]},
                  "roles": [
                      {"company": "Evil </script><script>alert(1)</script>", "title": "Product Manager", "url": "https://x.test/1",
                       "score": 8, "breakdown": [{"points": 3, "reason": "core fit", "quote": "own the roadmap"},
                                                 {"points": -1, "reason": "gap: Kafka"}, {"points": "cap 7", "reason": "domain"}],
                       "flags": ["Process: take-home"], "fit_note": "Good.", "unconfirmed": ["travel"]},
                      {"company": "LowCo", "title": "Product Analyst", "url": "https://x.test/2", "score": 4},
                      {"company": "NoLink", "title": "Product Manager", "url": "", "score": 9},
                  ]}
        (tmp / "sc.json").write_text(json.dumps(scored))
        code, _, err = run(["build", "--profile", str(tmp / "p.json"), "--scored", str(tmp / "sc.json"),
                            "--out", str(tmp / "d.html"), "--date", "2026-01-15"])
        self.assertEqual(code, 0, err)
        page = (tmp / "d.html").read_text()
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertNotIn("<script>alert(1)", page)
        self.assertEqual(page.count('class="card"'), 1)  # below bar and no-link roles get no card
        self.assertIn("−1 gap: Kafka", page)
        code, out, _ = run(["state-extract", str(tmp / "d.html")])
        st = json.loads(out)
        self.assertEqual(len(st["seen"]), 3)
        self.assertEqual(len(st["recent"]), 1)
        self.assertEqual(st["boards"]["ashby"], [{"slug": "newco", "name": "NewCo"}])
        self.assertEqual(st["runs"][-1]["shown"], 1)
        self.assertEqual(st["seen"][jm.h10(jm.job_key("LowCo", "Product Analyst"))], "260115")

        # second day: yesterday's match appears under "Earlier this week", the state carries over
        (tmp / "s.json").write_text(out)
        (tmp / "sc2.json").write_text(json.dumps({"roles": []}))
        run(["build", "--profile", str(tmp / "p.json"), "--state", str(tmp / "s.json"), "--scored", str(tmp / "sc2.json"),
             "--out", str(tmp / "d2.html"), "--date", "2026-01-16"])
        page2 = (tmp / "d2.html").read_text()
        self.assertIn("Earlier this week", page2)
        self.assertIn("No new roles cleared your bar today", page2)

    def test_retention(self):
        st = jm.empty_state()
        st["seen"]["old"] = "200101"
        st["seen"]["new"] = "260110"
        st["recent"] = [{"k": "a", "d": "2026-01-01"}, {"k": "b", "d": "2026-01-12"}]
        out = jm.merge_state(st, [], date(2026, 1, 15), 6)
        self.assertEqual(set(out["seen"]), {"new"})
        self.assertEqual([r["k"] for r in out["recent"]], ["b"])

    def test_memory_stays_small(self):
        st = jm.empty_state()
        day = date(2026, 1, 1)
        for i in range(70):  # 70 days x 30 scored roles a day
            roles = [{"company": f"Co{i}-{n}", "title": "Product Manager", "url": f"https://x.test/{i}/{n}", "score": n % 10}
                     for n in range(30)]
            st = jm.merge_state(st, roles, day + timedelta(days=i), 6)
        size = len(json.dumps(st, separators=(",", ":")))
        self.assertLess(size, 60_000, size)

    def test_extract_raw_json(self):
        tmp = Path(tempfile.mkdtemp()) / "s.json"
        tmp.write_text(json.dumps({"seen": {"abc": "260101"}}))
        code, out, _ = run(["state-extract", str(tmp)])
        self.assertEqual(json.loads(out)["seen"], {"abc": "260101"})

    def test_missing_state_block(self):
        tmp = Path(tempfile.mkdtemp()) / "x.html"
        tmp.write_text("<html></html>")
        code, out, _ = run(["state-extract", str(tmp)])
        self.assertEqual(json.loads(out)["seen"], {})


class MiscTests(unittest.TestCase):
    def test_job_key(self):
        self.assertEqual(jm.job_key("Acme, Inc.", "Product Manager - Payments"), jm.job_key("acme", "product manager, payments"))

    def test_excerpt_keeps_requirements(self):
        text = "Intro. " * 1200 + "Requirements: 5+ years of product management. " + "Benefits. " * 50
        ex = jm.excerpt(text)
        self.assertIn("5+ years of product management", ex)
        self.assertLess(len(ex), len(text))

    def test_excerpt_keeps_eligibility_line(self):
        text = "Intro. " * 600 + "Requirements: SQL. " + "More. " * 900 + "This role is not eligible to be hired in OR or WA."
        self.assertIn("not eligible to be hired in OR", jm.excerpt(text))

    def test_redact(self):
        r = jm.redact("Jo Doe\n12 Oak Street, Town\njo@x.com | 555-123-4567 | linkedin.com/in/jodoe\nSQL")
        for s in ("jo@x.com", "555-123-4567", "linkedin.com", "Oak Street"):
            self.assertNotIn(s, r)
        self.assertIn("SQL", r)

    def test_slug_variants(self):
        self.assertIn("examplelabs", jm._slug_variants("Example Labs"))
        self.assertIn("example-labs", jm._slug_variants("Example Labs"))
        self.assertIn("example", jm._slug_variants("Example Labs"))


if __name__ == "__main__":
    unittest.main()
