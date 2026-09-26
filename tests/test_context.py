import copy
import unittest
from pathlib import Path

from src.context import load_context, trace_license, validate_integrity

FIXTURE = Path("fixtures/context.json")


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.context = load_context(FIXTURE)

    def test_fixture_matches_domain(self):
        self.assertEqual(self.context["domain"], "seed-research-transfer")
        self.assertGreaterEqual(self.context["version"], 2)
        self.assertGreaterEqual(len(self.context["constraints"]), 1)

    def test_requirements_map_to_indicators_and_evidence(self):
        requirements = {r["id"]: r for r in self.context["requirements"]}
        observations = {o["id"]: o for o in self.context["trial_observations"]}
        req = requirements["req-taste-1"]
        # 口感要求已转成三个可检验指标
        self.assertEqual(
            set(req["indicator_ids"]),
            {"ind-peel-thick", "ind-sugar", "ind-amylopectin"},
        )
        # 科研人员可据多点多年证据判断需求获得支持
        self.assertEqual(req["status"], "supported")
        sites = {observations[e]["site"] for e in req["evidence"]}
        years = {observations[e]["year"] for e in req["evidence"]}
        self.assertEqual(sites, {"site-jx", "site-sd", "site-yn"})
        self.assertEqual(years, {2024, 2025})

    def test_failed_and_negative_observations_are_retained(self):
        results = {o["id"]: o["result"] for o in self.context["trial_observations"]}
        self.assertEqual(results["obs-tn8-24-yn-sugar-fail"], "failed")
        self.assertEqual(results["obs-tn8-25-sd-lodging"], "negative")
        self.assertEqual(results["obs-hj2-23-yn-rot"], "negative")

    def test_same_resource_has_trackable_branch_per_team(self):
        branches = [
            b for b in self.context["lineage_branches"] if b["material"] == "gm-nuomi-12"
        ]
        teams = {b["maintained_by"] for b in branches}
        self.assertEqual(teams, {"ent-feng", "inst-sc"})
        transfers = {t["id"]: t for t in self.context["material_transfers"]}
        for branch in branches:
            transfer = transfers[branch["origin_transfer"]]
            self.assertEqual(transfer["to"], branch["maintained_by"])
            self.assertTrue(transfer["purpose"])
            self.assertGreater(transfer["quantity"]["value"], 0)
            self.assertTrue(transfer["generation"])

    def test_license_traces_back_to_material_and_field_performance(self):
        trace = trace_license(self.context, "lic-tn8-feng")
        # 从上市品种追到材料来源
        self.assertEqual(trace["registration"]["variety_name"], "华甜糯8号（虚构）")
        self.assertEqual(trace["cross"]["code"], "TN-8")
        female = trace["parents"]["female"]
        self.assertEqual(female["germplasm"]["id"], "gm-nuomi-12")
        self.assertIsNotNone(female["branch"])
        self.assertIsNotNone(female["transfer"])
        self.assertEqual(trace["parents"]["male"]["germplasm"]["id"], "gm-tian-3")
        # 追到试验条件（多点多年）与实际种植表现
        self.assertEqual(len({p["id"] for p in trace["protocols"]}), 1)
        sites = {o["site"] for o in trace["observations"]}
        years = {o["year"] for o in trace["observations"]}
        self.assertEqual(sites, {"site-jx", "site-sd", "site-yn"})
        self.assertEqual(years, {2024, 2025})
        self.assertGreaterEqual(len(trace["feedback"]), 2)
        # 高原配套技术只作用于云南
        tech_regions = {
            t["version"]: set(t["applies_to"]["regions"]) for t in trace["techniques"]
        }
        self.assertEqual(tech_regions["v1.0-高原"], {"云南"})

    def test_withdrawn_variety_keeps_timed_evidence(self):
        reg = next(r for r in self.context["registrations"] if r["id"] == "reg-hj2")
        self.assertEqual(reg["status"], "withdrawn")
        self.assertEqual(reg["withdrawn_at"], "2025-11-02")
        event = next(e for e in self.context["events"] if e["kind"] == "withdrawal")
        self.assertTrue(event["at"].startswith(reg["withdrawn_at"]))

    def test_mutated_fixture_is_rejected(self):
        def mutated():
            return copy.deepcopy(self.context)

        # 材料移交缺用途
        data = mutated()
        data["material_transfers"][0]["purpose"] = ""
        with self.assertRaises(ValueError):
            validate_integrity(data)

        # 知识产权比例之和不为1
        data = mutated()
        data["licenses"][0]["ip_shares"][1]["share"] = 0.5
        with self.assertRaises(ValueError):
            validate_integrity(data)

        # 比例调整未经双方同时确认
        data = mutated()
        data["licenses"][0]["adjustments"][0]["confirmed_by"] = ["inst-hd"]
        with self.assertRaises(ValueError):
            validate_integrity(data)

        # 未公开性状对非参与方可见
        data = mutated()
        data["germplasm"][0]["restricted_traits"][0]["visible_to"] = ["ent-feng", "site-jx"]
        with self.assertRaises(ValueError):
            validate_integrity(data)

        # 配套技术超出审定明确范围
        data = mutated()
        data["cultivation_techniques"][0]["applies_to"]["regions"] = ["广东"]
        with self.assertRaises(ValueError):
            validate_integrity(data)

        # 撤回事件与撤回记录日期不一致
        data = mutated()
        data["events"][1]["at"] = "2025-11-03T09:00:00+08:00"
        with self.assertRaises(ValueError):
            validate_integrity(data)


if __name__ == "__main__":
    unittest.main()
