import copy
import json
import unittest
from pathlib import Path

from src import backend as B
from src.schema_lite import validate as schema_validate

FIXTURE = Path("fixtures/backend.json")
SCHEMA = Path("contracts/backend.schema.json")


def data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FixtureIntegrityTest(unittest.TestCase):
    def test_fixture_passes_structural_schema(self):
        errors = schema_validate(data(), json.loads(SCHEMA.read_text(encoding="utf-8")))
        self.assertEqual(errors, [])

    def test_fixture_passes_semantic_validation(self):
        self.assertEqual(B.validate_backend(data()), [])

    def test_load_backend_rejects_tampered_data(self):
        d = data()
        d["ip_shares"][0]["shares"][0]["share_pct"] = 42  # 合计不再是100
        self.assertTrue(any("比例合计" in x for x in B.validate_backend(d)))

    def test_load_backend_accepts_fixture_file(self):
        B.load_backend(FIXTURE)


class TransferAndLineageTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_transfer_states_purpose_quantity_generation(self):
        for tr in self.d["material_transfers"]:
            self.assertTrue(tr["purpose"])
            self.assertTrue(tr["quantity"])
            self.assertTrue(tr["generation"])
            self.assertTrue(tr["propagation_generation"])
            self.assertTrue(tr["confirmed_by"])

    def test_same_resource_keeps_independent_traceable_branches_per_team(self):
        branches = B.lineage_branches(self.d, "GP-007")
        self.assertEqual(set(branches), {"TM-CORN", "TM-MINOR"})
        corn = [e["label"] for e in branches["TM-CORN"]]
        minor = [e["label"] for e in branches["TM-MINOR"]]
        self.assertIn("组合C-2023-007 F1", corn)
        self.assertEqual(minor, ["TM-MINOR:S7"])  # 独立编号分支，未与玉米组批次混繁
        # 两分支各自挂在各自的移交单上，互不混繁
        self.assertEqual(branches["TM-CORN"][0]["via_transfer_id"], "T-01")
        self.assertEqual(branches["TM-MINOR"][0]["via_transfer_id"], "T-02")

    def test_cross_team_lineage_chaining_is_rejected(self):
        d = data()
        # 把玉米组的事件错误地接到杂粮组事件上
        for ev in d["lineage_events"]:
            if ev["id"] == "L-2023-C07":
                ev["from_event_id"] = "L-2023-107"  # 属于 TM-MINOR
        errors = B.validate_backend(d)
        self.assertTrue(any("跨团队串联" in x for x in errors), errors)

    def test_transfer_without_branch_is_rejected(self):
        d = data()
        d["lineage_events"] = [e for e in d["lineage_events"] if e["id"] != "L-2023-101"]
        errors = B.validate_backend(d)
        self.assertTrue(any("谱系分支起点" in x for x in errors), errors)


class DemandEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_enterprise_demands_raised_at_material_stage_become_indicators(self):
        for dm in self.d["demands"]:
            self.assertTrue(dm["indicators"], f"{dm['id']} 未转化为指标")
        material_stage = [dm for dm in self.d["demands"] if dm["stage"] == "breeding_material"]
        self.assertGreaterEqual(len(material_stage), 3)

    def test_supported_demand(self):
        r = B.demand_evidence(self.d, "D-03")  # 机械化
        self.assertEqual(r["status"], "supported")
        self.assertEqual(r["site_count"], 1)
        self.assertEqual(r["year_count"], 2)  # 商丘 2024/2025

    def test_negative_result_makes_demand_not_supported_but_is_retained(self):
        r = B.demand_evidence(self.d, "D-02")  # 加工：宜昌出籽率未达标
        self.assertEqual(r["status"], "not_supported")
        self.assertIn("OBS-006", r["negative_points"])
        # 原始负面观测仍在库中、未被删除
        self.assertTrue(any(o["id"] == "OBS-006" and o["negative"]
                            for o in self.d["observations"]))

    def test_pending_demand_has_no_evidence(self):
        r = B.demand_evidence(self.d, "D-04")
        self.assertEqual(r["status"], "pending_evidence")
        self.assertEqual(r["site_year_count"], 0)

    def test_demand_without_indicator_rejected(self):
        d = data()
        d["demands"][0]["indicators"] = []
        self.assertTrue(any("未转化" in x for x in B.validate_backend(d)))


class MultiSiteMultiYearAndNegativesTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_variety_covers_multiple_sites_and_years(self):
        t = B.trace_variety(self.d, "V-ZN2026001")
        ms = t["multi_site_multi_year"]
        self.assertEqual(ms["site_count"], 3)
        self.assertEqual(ms["year_count"], 2)

    def test_negative_results_are_registered_with_decisions(self):
        for n in self.d["negative_results"]:
            self.assertTrue(n["summary"])
            self.assertTrue(n["decision"])
        # 负面登记引用的观测必须带 negative 标记
        errs = B.validate_backend(self.d)
        self.assertFalse(any("negative=false" in x for x in errs))

    def test_flipping_negative_flag_is_detected(self):
        d = data()
        for o in d["observations"]:
            if o["id"] == "OBS-006":
                o["negative"] = False
        self.assertTrue(any("negative=false" in x for x in B.validate_backend(d)))


class VisibilityIsolationTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_commercial_terms_hidden_from_outside_party(self):
        view = B.view_for_party(self.d, "ENT-02")  # 不是 L-001 当事方
        lic = next(l for l in view["licenses"] if l["id"] == "L-001")
        self.assertTrue(lic["commercial_terms"].get("redacted"))
        self.assertNotIn("royalty_pct_of_revenue", lic["commercial_terms"])
        # 授权范围等非商业信息仍可见
        self.assertIn("黄淮海夏播区", lic["scope"]["regions"])

    def test_commercial_terms_visible_to_license_party(self):
        view = B.view_for_party(self.d, "ENT-01")
        lic = next(l for l in view["licenses"] if l["id"] == "L-001")
        self.assertEqual(lic["commercial_terms"]["royalty_pct_of_revenue"], 6.0)

    def test_undisclosed_traits_isolated(self):
        outside = B.view_for_party(self.d, "ENT-02")
        g = next(x for x in outside["germplasm"] if x["id"] == "GP-007")
        self.assertEqual(g["undisclosed_traits"], [])
        inside = B.view_for_party(self.d, "ORG-IAAS")
        g2 = next(x for x in inside["germplasm"] if x["id"] == "GP-007")
        self.assertTrue(g2["undisclosed_traits"])

    def test_restricted_observation_isolated(self):
        self.assertEqual(len(B.view_for_party(self.d, "ENT-02")["observations"]),
                         len(self.d["observations"]) - 1)
        # 被授权的第三方大学可见
        ids = {o["id"] for o in B.view_for_party(self.d, "ORG-UNIV")["observations"]}
        self.assertIn("OBS-014", ids)

    def test_view_does_not_mutate_source(self):
        before = copy.deepcopy(self.d["licenses"])
        B.view_for_party(self.d, "ENT-02")
        self.assertEqual(before, self.d["licenses"])


class ApprovalScopeAndChangesTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_approval_version_is_scoped_to_regions(self):
        ap = next(a for a in self.d["approvals"] if a["id"] == "A-2026-042")
        self.assertNotIn("西南山地春播区", ap["scope"]["regions"])
        # 负面点次数据仍随审定附件保留，只是不适宜区域被排除
        self.assertIn("TR-2024-YC", ap["attached_trial_ids"])

    def test_change_set_only_affects_explicit_scope(self):
        cs = next(c for c in self.d["approval_change_sets"])
        self.assertEqual(cs["scope"]["regions"], ["黄淮海夏播区"])
        self.assertTrue(cs["confirmed_by"])
        # 长江中下游的配套技术不受该变更影响
        pk = next(p for p in self.d["technique_packages"] if p["id"] == "PK-01")
        self.assertEqual(pk["scope"]["regions"], ["长江中下游夏播区"])

    def test_technique_packages_effective_by_region_and_date(self):
        # 长江中下游 5月仅 PK-01 生效
        hf = B.effective_packages(self.d, "V-ZN2026001", "长江中下游夏播区", "2026-05-01")
        self.assertEqual([p["id"] for p in hf], ["PK-01"])
        # 黄淮海在 PK-02 发布前无生效包，发布后为 PK-02
        self.assertEqual(B.effective_packages(self.d, "V-ZN2026001", "黄淮海夏播区", "2026-05-01"), [])
        sq = B.effective_packages(self.d, "V-ZN2026001", "黄淮海夏播区", "2026-07-01")
        self.assertEqual([p["id"] for p in sq], ["PK-02"])


class IpAndConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_shares_sum_to_100_and_all_holders_confirm(self):
        for ip in self.d["ip_shares"]:
            self.assertEqual(sum(s["share_pct"] for s in ip["shares"]), 100)
            holders = {s["party_id"] for s in ip["shares"]}
            self.assertTrue(holders.issubset(set(ip["confirmed_by"])))

    def test_share_adjustment_is_versioned_with_timestamp(self):
        hist = [x for x in self.d["ip_shares"] if x["variety_id"] == "V-ZN2026001"]
        hist.sort(key=lambda x: x["version"])
        self.assertEqual([x["version"] for x in hist], [1, 2])
        self.assertEqual(hist[1]["effective_from"], "2026-05-06")
        ent = [s for s in hist[1]["shares"] if s["party_id"] == "ENT-01"][0]
        self.assertEqual(ent["share_pct"], 35)  # 30 -> 35
        # 调整由合作方同时确认，留有确认时间
        cnf = next(c for c in self.d["confirmations"] if c["id"] == "CNF-06")
        self.assertEqual(cnf["at"], "2026-05-07")

    def test_bad_share_total_rejected(self):
        d = data()
        d["ip_shares"][0]["shares"][0]["share_pct"] = 70
        self.assertTrue(any("比例合计" in x for x in B.validate_backend(d)))

    def test_missing_holder_confirmation_rejected(self):
        d = data()
        d["ip_shares"][0]["confirmed_by"].remove("ORG-UNIV")
        self.assertTrue(any("未同时确认" in x for x in B.validate_backend(d)))


class LicenseAdoptionWithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_area_feedback_is_returned_with_timestamp(self):
        rows = [f for f in self.d["adoption_feedback"] if f["variety_id"] == "V-ZN2026001"]
        self.assertTrue(all(f["reported_at"] and f["planted_area_mu"] >= 0 for f in rows))
        t = B.trace_variety(self.d, "V-ZN2026001")
        self.assertEqual(t["planted_area_mu_by_region"]["黄淮海夏播区"], 8500)

    def test_active_license_in_force(self):
        lic = B.licenses_in_force(self.d, "V-ZN2026001", "黄淮海夏播区", "2026-08-01")
        self.assertIn("L-001", [x["id"] for x in lic])

    def test_withdrawal_terminates_license_and_has_time_basis(self):
        w = next(x for x in self.d["withdrawals"])
        self.assertEqual(w["variety_id"], "V-ZN2025009")
        self.assertTrue(w["noticed_at"] and w["effective_at"])
        self.assertTrue(w["evidence"])  # 负面结果+大区监测证据链
        # 关联许可已终止
        lic = next(l for l in self.d["licenses"] if l["id"] == "L-002")
        self.assertEqual(lic["status"], "terminated")
        self.assertEqual(lic["terminated_by_withdrawal"], "W-01")
        # 撤回后无在效许可，品种状态为 withdrawn
        self.assertEqual(B.licenses_in_force(self.d, "V-ZN2025009", "北方春播早熟区", "2026-09-01"), [])
        variety = next(v for v in self.d["varieties"] if v["id"] == "V-ZN2025009")
        self.assertEqual(variety["status"], "withdrawn")
        # 撤回经各方同时确认
        cnf = next(c for c in self.d["confirmations"] if c["id"] == "CNF-10")
        self.assertIn("ENT-03", cnf["parties"])


class EndToEndTraceTest(unittest.TestCase):
    def setUp(self):
        self.d = data()

    def test_market_variety_traces_back_to_source(self):
        t = B.trace_variety(self.d, "V-ZN2026001")
        src = t["material_source"]
        self.assertEqual(src["parents"]["female"]["material_id"], "GP-021")
        self.assertEqual(src["parents"]["male"]["material_id"], "GP-007")
        self.assertTrue(src["parents"]["male"]["origin"])
        # 材料来源含繁殖代次与持有单位
        self.assertEqual(src["parents"]["male"]["generation"], "S6")
        self.assertEqual(src["parents"]["male"]["holder_org"], "ORG-IAAS")

    def test_trace_contains_conditions_rights_and_performance(self):
        t = B.trace_variety(self.d, "V-ZN2026001")
        # 试验条件：播期/天气/负责人随点年保留
        first = t["multi_site_multi_year"]["trials"][0]
        self.assertIn("weather_note", first)
        self.assertTrue(first["observations"])
        # 许可权利
        self.assertTrue(any(l["id"] == "L-001" for l in t["licenses"]))
        # 实际种植表现
        self.assertTrue(t["adoption_feedback"])
        # 审定版本与知产历史
        self.assertTrue(t["approvals"])
        self.assertEqual([x["version"] for x in t["ip_share_history"]], [1, 2])

    def test_timeline_is_sorted_and_timestamped(self):
        tl = B.timeline(self.d, "V-ZN2026001")
        dates = [e["at"] for e in tl]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(all(e["at"] for e in tl))
        ids = {e["id"] for e in tl}
        # 时间线沿材料来源追溯到父母本移交与企业需求
        self.assertIn("T-01", ids)
        self.assertIn("D-01", ids)
        # 不含另一品种的撤回
        self.assertNotIn("W-01", ids)


class SchemaLiteTest(unittest.TestCase):
    def test_basic_keywords(self):
        schema = {"type": "object", "required": ["a"],
                  "properties": {"a": {"type": "string", "minLength": 1},
                                 "b": {"type": "integer", "minimum": 1}},
                  "additionalProperties": False}
        self.assertEqual(schema_validate({"a": "x", "b": 2}, schema), [])
        self.assertTrue(schema_validate({"b": 0}, schema))  # 缺 a 且 b 越界
        self.assertTrue(schema_validate({"a": "x", "c": 1}, schema))  # 额外字段


if __name__ == "__main__":
    unittest.main()
