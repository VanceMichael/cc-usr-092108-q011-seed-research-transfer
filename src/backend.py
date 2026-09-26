"""联合研发转化后台：语义校验、按参与方可见性、谱系与追溯查询。

数据从材料（germplasm）→ 亲本组合（crosses）→ 试验方案/多点多年
（protocols/trials/observations，含负面结果）→ 品种审定（approvals，
版本化、带区域范围）→ 配套技术（technique_packages）→ 许可（licenses，
商业条款隔离）→ 推广反馈（adoption_feedback）→ 撤回（withdrawals）。

所有结构性约束见 contracts/backend.schema.json；本模块补充引用完整性、
业务规则与追溯能力。
"""

import copy
import json
from pathlib import Path

from src.schema_lite import validate as _schema_validate

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contracts" / "backend.schema.json"


# ---------------------------------------------------------------- 载入与校验

def load_backend(path: str | Path) -> dict:
    """读取后台资料，先过结构合约再做语义检查；任一不过即抛 ValueError。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_backend(data)
    if errors:
        raise ValueError("后台资料校验失败：\n  - " + "\n  - ".join(errors))
    return data


def structural_errors(data: dict) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_validate(data, schema)


def validate_backend(data: dict) -> list[str]:
    """结构 + 语义校验，返回全部错误信息（空列表表示通过）。"""
    errors = structural_errors(data)
    errors += _semantic_errors(data)
    return errors


def _ids(rows, key="id"):
    return {row[key] for row in rows}


def _semantic_errors(d: dict) -> list[str]:
    e: list[str] = []
    actors = {a["id"]: a for a in d["actors"]}
    teams = {t["id"]: t for t in d["teams"]}
    sites = {s["id"]: s for s in d["sites"]}
    categories = {c["id"]: c for c in d["special_categories"]}
    germ = {g["id"]: g for g in d["germplasm"]}
    varieties = {v["id"]: v for v in d["varieties"]}
    approvals = {a["id"]: a for a in d["approvals"]}
    protocols = {p["id"]: p for p in d["trial_protocols"]}
    trials = {t["id"]: t for t in d["trials"]}
    crosses = {c["id"]: c for c in d["crosses"]}
    confirmations = {c["id"]: c for c in d["confirmations"]}

    def party_exists(pid, where):
        if pid != "*" and pid not in actors:
            e.append(f"{where}: 引用了不存在的参与方 {pid!r}")

    # 团队归属
    for t in d["teams"]:
        if t["owner_org"] not in actors:
            e.append(f"team {t['id']}: owner_org {t['owner_org']} 不是已登记参与方")

    # 参与方/团队/站点/品类引用
    for p in d["participations"]:
        party_exists(p["party_id"], f"participation {p['id']}")
    for s in d["sites"]:
        if s["operator_org"] not in actors:
            e.append(f"site {s['id']}: operator_org 未登记")
    for c in d["special_categories"]:
        if c["lead_team"] not in teams:
            e.append(f"category {c['id']}: lead_team 不存在")

    # 种质
    for g in d["germplasm"]:
        if g["holder_org"] not in actors:
            e.append(f"germplasm {g['id']}: holder_org 未登记")
        if g["custodian_team"] not in teams:
            e.append(f"germplasm {g['id']}: custodian_team 不存在")
        if g.get("category_id") and g["category_id"] not in categories:
            e.append(f"germplasm {g['id']}: category_id 不存在")
        if g["lineage_root_id"] not in germ:
            e.append(f"germplasm {g['id']}: lineage_root_id 未指向已登记材料")
        for u in g.get("undisclosed_traits", []):
            for pid in u["visible_to"]:
                party_exists(pid, f"germplasm {g['id']} undisclosed_traits")

    # 移交：用途/数量/代次已由 schema 保证非空；此处校验引用与确认方
    for tr in d["material_transfers"]:
        if tr["material_id"] not in germ:
            e.append(f"transfer {tr['id']}: material_id 不存在")
        if tr["to_team"] not in teams:
            e.append(f"transfer {tr['id']}: to_team 不存在")
        for pid in tr["confirmed_by"]:
            party_exists(pid, f"transfer {tr['id']} confirmed_by")
        for cid in tr.get("confirmation_ids", []):
            if cid not in confirmations:
                e.append(f"transfer {tr['id']}: confirmation {cid} 不存在")

    # 谱系事件：同团队链、跨团队须有移交、cross 关联
    events = {ev["id"]: ev for ev in d["lineage_events"]}
    for ev in d["lineage_events"]:
        if ev["material_id"] not in germ:
            e.append(f"lineage_event {ev['id']}: material_id 不存在")
        if ev["team_id"] not in teams:
            e.append(f"lineage_event {ev['id']}: team_id 不存在")
        prev = ev.get("from_event_id")
        if prev is not None:
            if prev not in events:
                e.append(f"lineage_event {ev['id']}: from_event_id {prev} 不存在")
            elif events[prev]["team_id"] != ev["team_id"]:
                e.append(f"lineage_event {ev['id']}: 跨团队串联 {prev}，"
                         "不同团队应各自成链并经移交关联")
        via = ev.get("via_transfer_id")
        if via is not None:
            if via not in {x["id"] for x in d["material_transfers"]}:
                e.append(f"lineage_event {ev['id']}: via_transfer_id 不存在")
        if ev.get("cross_id") and ev["cross_id"] not in crosses:
            e.append(f"lineage_event {ev['id']}: cross_id 不存在")
        if ev.get("selected_for") and ev["selected_for"] not in varieties:
            e.append(f"lineage_event {ev['id']}: selected_for 品种不存在")

    # 同一资源多团队使用：每个使用团队都要有对应移交 + 独立谱系分支起点
    for tr in d["material_transfers"]:
        mid, team = tr["material_id"], tr["to_team"]
        roots = [ev for ev in d["lineage_events"]
                 if ev["material_id"] == mid and ev["team_id"] == team
                 and ev.get("via_transfer_id") == tr["id"]]
        if not roots:
            e.append(f"transfer {tr['id']}: 材料 {mid} 移交至团队 {team}，"
                     "但缺少该团队可追踪的谱系分支起点")

    # 亲本组合
    for c in d["crosses"]:
        if c["female_parent"] not in germ or c["male_parent"] not in germ:
            e.append(f"cross {c['id']}: 亲本材料不存在")
        if c["team_id"] not in teams:
            e.append(f"cross {c['id']}: team_id 不存在")
        if c.get("target_variety_id") and c["target_variety_id"] not in varieties:
            e.append(f"cross {c['id']}: target_variety_id 不存在")

    # 需求→指标：材料阶段提出的需求必须至少绑定一个试验指标
    for dm in d["demands"]:
        party_exists(dm["raised_by"], f"demand {dm['id']}")
        if not dm["indicators"]:
            e.append(f"demand {dm['id']}: 企业需求未转化为任何试验指标")

    # 方案/试验
    for p in d["trial_protocols"]:
        for sid in p["site_ids"]:
            if sid not in sites:
                e.append(f"protocol {p['id']}: site {sid} 不存在")
        if p.get("cross_id") and p["cross_id"] not in crosses:
            e.append(f"protocol {p['id']}: cross_id 不存在")
        if p.get("variety_id") and p["variety_id"] not in varieties:
            e.append(f"protocol {p['id']}: variety_id 不存在")
        codes = [m["code"] for m in p["metrics"]]
        if len(codes) != len(set(codes)):
            e.append(f"protocol {p['id']}: 指标编码重复")
    metric_index = {(p["id"], m["code"]): m
                    for p in d["trial_protocols"] for m in p["metrics"]}
    for t in d["trials"]:
        if t["protocol_id"] not in protocols:
            e.append(f"trial {t['id']}: protocol_id 不存在")
        elif t["site_id"] not in protocols[t["protocol_id"]]["site_ids"]:
            e.append(f"trial {t['id']}: 站点不在其方案范围内")
        if t["variety_id"] not in varieties:
            e.append(f"trial {t['id']}: variety_id 不存在")
        if t["site_id"] not in sites:
            e.append(f"trial {t['id']}: site_id 不存在")

    # 观测：必须挂在某方案指标上；负面标记与负面结果登记一致
    neg_ids = {oid for n in d["negative_results"] for oid in n.get("observation_ids", [])}
    for o in d["observations"]:
        if o["trial_id"] not in trials:
            e.append(f"observation {o['id']}: trial_id 不存在")
        else:
            proto = trials[o["trial_id"]]["protocol_id"]
            if (proto, o["metric_code"]) not in metric_index:
                e.append(f"observation {o['id']}: 指标 {o['metric_code']} "
                         f"不在方案 {proto} 中")
        for pid in o["visible_to"]:
            party_exists(pid, f"observation {o['id']}")
        if o["id"] in neg_ids and not o["negative"]:
            e.append(f"observation {o['id']}: 被负面结果登记引用但 negative=false")

    # 负面结果引用
    for n in d["negative_results"]:
        if n["variety_id"] not in varieties:
            e.append(f"negative_result {n['id']}: variety_id 不存在")
        for oid in n.get("observation_ids", []):
            if oid not in {x["id"] for x in d["observations"]}:
                e.append(f"negative_result {n['id']}: observation {oid} 不存在")

    # 品种 / 审定
    for v in d["varieties"]:
        if v["cross_id"] not in crosses:
            e.append(f"variety {v['id']}: cross_id 不存在")
        for pid in v["applicant_orgs"]:
            party_exists(pid, f"variety {v['id']} applicants")
        cap = v.get("current_approval_id")
        if cap and cap not in approvals:
            e.append(f"variety {v['id']}: current_approval_id 不存在")
    for a in d["approvals"]:
        if a["variety_id"] not in varieties:
            e.append(f"approval {a['id']}: variety_id 不存在")
        for tid in a.get("attached_trial_ids", []):
            if tid not in trials:
                e.append(f"approval {a['id']}: trial {tid} 不存在")
        sup = a.get("supersedes")
        if sup and sup not in approvals:
            e.append(f"approval {a['id']}: supersedes 不存在")
        _scope_parties(a["scope"], sites, e, f"approval {a['id']}")

    # 变更集只作用于明确范围
    for cs in d["approval_change_sets"]:
        if cs["variety_id"] not in varieties:
            e.append(f"change_set {cs['id']}: variety_id 不存在")
        _scope_parties(cs["scope"], sites, e, f"change_set {cs['id']}")
        for pid in cs["confirmed_by"]:
            party_exists(pid, f"change_set {cs['id']}")
        for cid in cs.get("confirmation_ids", []):
            if cid not in confirmations:
                e.append(f"change_set {cs['id']}: confirmation 不存在")

    # 配套技术
    pkg_ids = {p["id"]: p for p in d["technique_packages"]}
    for pk in d["technique_packages"]:
        if pk["variety_id"] not in varieties:
            e.append(f"technique {pk['id']}: variety_id 不存在")
        _scope_parties(pk["scope"], sites, e, f"technique {pk['id']}")
        if pk.get("supersedes") and pk["supersedes"] not in pkg_ids:
            e.append(f"technique {pk['id']}: supersedes 不存在")

    # 知识产权：合计100、确认方覆盖全部持份方；比例调整需多方确认
    for ip in d["ip_shares"]:
        if ip["variety_id"] not in varieties:
            e.append(f"ip_share {ip['id']}: variety_id 不存在")
        total = round(sum(s["share_pct"] for s in ip["shares"]), 6)
        if total != 100:
            e.append(f"ip_share {ip['id']}: 比例合计为 {total}，应为 100")
        holders = {s["party_id"] for s in ip["shares"]}
        conf = set(ip["confirmed_by"])
        missing = holders - conf
        if missing:
            e.append(f"ip_share {ip['id']}: 持份方 {sorted(missing)} 未同时确认")
        for pid in holders | conf:
            party_exists(pid, f"ip_share {ip['id']}")
        for cid in ip.get("confirmation_ids", []):
            if cid not in confirmations:
                e.append(f"ip_share {ip['id']}: confirmation {cid} 不存在")

    # 确认记录
    for c in d["confirmations"]:
        for pid in c["parties"]:
            party_exists(pid, f"confirmation {c['id']}")

    # 许可
    withdrawals = {w["id"]: w for w in d["withdrawals"]}
    for lic in d["licenses"]:
        if lic["variety_id"] not in varieties:
            e.append(f"license {lic['id']}: variety_id 不存在")
        party_exists(lic["licensor"], f"license {lic['id']}")
        party_exists(lic["licensee"], f"license {lic['id']}")
        _scope_parties(lic["scope"], sites, e, f"license {lic['id']}")
        ct = lic.get("commercial_terms")
        if ct:
            for pid in ct["visible_to"]:
                party_exists(pid, f"license {lic['id']} commercial_terms")
        tw = lic.get("terminated_by_withdrawal")
        if tw:
            if tw not in withdrawals:
                e.append(f"license {lic['id']}: terminated_by_withdrawal 不存在")
            elif lic["status"] != "terminated":
                e.append(f"license {lic['id']}: 已关联撤回但状态不是 terminated")

    # 推广反馈
    for af in d["adoption_feedback"]:
        if af["variety_id"] not in varieties:
            e.append(f"feedback {af['id']}: variety_id 不存在")
        if af.get("site_id") and af["site_id"] not in sites:
            e.append(f"feedback {af['id']}: site_id 不存在")
        party_exists(af["reported_by"], f"feedback {af['id']}")
        for pid in af["visible_to"]:
            party_exists(pid, f"feedback {af['id']}")

    # 撤回
    for w in d["withdrawals"]:
        if w["variety_id"] not in varieties:
            e.append(f"withdrawal {w['id']}: variety_id 不存在")
        _scope_parties(w["scope"], sites, e, f"withdrawal {w['id']}")
        for pid in w["confirmed_by"]:
            party_exists(pid, f"withdrawal {w['id']}")
        for ref in w["evidence"]:
            known = {x["id"] for x in d["negative_results"]} | \
                    {o["id"] for o in d["observations"]} | \
                    {a["id"] for a in d["adoption_feedback"]}
            if ref not in known:
                e.append(f"withdrawal {w['id']}: 证据 {ref} 不存在")

    # 品种状态与撤回一致性
    for w in d["withdrawals"]:
        if varieties[w["variety_id"]]["status"] != "withdrawn":
            e.append(f"variety {w['variety_id']}: 存在撤回 {w['id']} 但状态不是 withdrawn")

    return e


def _scope_parties(scope: dict, sites: dict, e: list[str], where: str):
    for sid in scope.get("site_ids", []):
        if sid not in sites:
            e.append(f"{where}: scope 引用了不存在站点 {sid}")


# ---------------------------------------------------------------- 按参与方可见性

def _visible(visible_to: list[str], party_id: str) -> bool:
    return "*" in visible_to or party_id in visible_to


def view_for_party(data: dict, party_id: str) -> dict:
    """返回某参与方视角的资料副本：隐藏未公开性状与商业条款、受限观测。"""
    d = copy.deepcopy(data)

    for g in d["germplasm"]:
        g["undisclosed_traits"] = [
            u for u in g.get("undisclosed_traits", []) if _visible(u["visible_to"], party_id)
        ]

    d["observations"] = [
        o for o in d["observations"] if _visible(o["visible_to"], party_id)
    ]

    for lic in d["licenses"]:
        ct = lic.get("commercial_terms")
        if ct and not _visible(ct["visible_to"], party_id):
            # 保留许可存在与授权范围，仅隐去金额费率等商业条款
            lic["commercial_terms"] = {"visible_to": ct["visible_to"], "redacted": True}

    d["adoption_feedback"] = [
        a for a in d["adoption_feedback"] if _visible(a["visible_to"], party_id)
    ]

    d["_view_for"] = party_id
    return d


# ---------------------------------------------------------------- 谱系

def lineage_branches(data: dict, material_id: str) -> dict[str, list[dict]]:
    """同一资源在各团队的可追踪谱系分支：team_id → 按时间排序的事件链。"""
    branches: dict[str, list[dict]] = {}
    for ev in data["lineage_events"]:
        if ev["material_id"] == material_id:
            branches.setdefault(ev["team_id"], []).append(ev)
    for team in branches:
        branches[team].sort(key=lambda x: x["at"])
    return branches


def material_source_trace(data: dict, variety_id: str) -> dict:
    """品种 → 组合 → 父母本材料及来源/代次（材料来源追溯）。"""
    variety = next(v for v in data["varieties"] if v["id"] == variety_id)
    cross = next(c for c in data["crosses"] if c["id"] == variety["cross_id"])
    germ = {g["id"]: g for g in data["germplasm"]}
    out = {"variety_id": variety_id, "cross": cross, "parents": {}}
    for role, pid in (("female", cross["female_parent"]), ("male", cross["male_parent"])):
        g = germ[pid]
        out["parents"][role] = {
            "material_id": pid, "name": g["name"], "origin": g.get("origin"),
            "generation": g["generation"], "holder_org": g["holder_org"],
            "branches": {team: [ev["label"] for ev in chain]
                         for team, chain in lineage_branches(data, pid).items()},
        }
    return out


# ---------------------------------------------------------------- 需求→证据

def demand_evidence(data: dict, demand_id: str) -> dict:
    """判断企业需求是否获得试验证据支持，并列出多点多年达成情况。

    汇总该需求各指标在所有点-年的观测：给出点数、年数、达标与未达标点次，
    以及总体结论（supported / not_supported / pending_evidence）。
    """
    demand = next(x for x in data["demands"] if x["id"] == demand_id)
    indicator_ids = set(demand["indicators"])

    protocols = data["trial_protocols"]
    # 指标 → 方案中的考核口径
    metric_defs = {}
    for p in protocols:
        for m in p["metrics"]:
            if m.get("indicator_id") in indicator_ids:
                metric_defs[(p["id"], m["code"])] = m
    # 方案指标的判定（method 文本中的阈值，结合指标类别给出规则）
    judge = _threshold_judge()

    trials = {t["id"]: t for t in data["trials"]}
    points: list[dict] = []
    site_years: set[tuple] = set()
    for o in data["observations"]:
        t = trials[o["trial_id"]]
        proto = t["protocol_id"]
        mdef = metric_defs.get((proto, o["metric_code"]))
        if not mdef:
            continue
        site_years.add((t["site_id"], t["year"]))
        ok = judge(o["metric_code"], o["value"])
        points.append({
            "observation_id": o["id"], "metric_code": o["metric_code"],
            "site_id": t["site_id"], "year": t["year"], "value": o["value"],
            "negative": o["negative"], "meets_threshold": ok,
            "method": mdef["method"],
        })

    measured_sites = {p["site_id"] for p in points}
    measured_years = {p["year"] for p in points}
    fails = [p for p in points if p["negative"] or p["meets_threshold"] is False]

    if not points:
        status = "pending_evidence"
    elif fails:
        status = "not_supported"
    else:
        status = "supported"

    return {
        "demand_id": demand_id,
        "raised_by": demand["raised_by"],
        "stage": demand["stage"],
        "statement": demand["statement"],
        "indicators": sorted(indicator_ids),
        "site_count": len(measured_sites),
        "year_count": len(measured_years),
        "site_year_count": len(site_years),
        "points": points,
        "negative_points": [p["observation_id"] for p in points if p["negative"]],
        "status": status,
    }


def _threshold_judge():
    """依据方案 method 中编码的阈值构造判定；仅覆盖样例所用指标。"""
    rules = {
        "感官品质总评分": lambda v: v >= 8.0,
        "皮渣率": lambda v: v <= 12.0,
        "出籽率": lambda v: v >= 68.0,
        "机收损失率": lambda v: v <= 5.0,
        "倒伏倒折率": lambda v: v <= 8.0,
        "茎腐病田间诱发鉴定": lambda v: v <= 20.0,
    }
    return lambda code, value: rules[code](value) if code in rules else None


# ---------------------------------------------------------------- 品种全链追溯

def trace_variety(data: dict, variety_id: str) -> dict:
    """企业视角：上市品种一直追到材料来源、试验条件、许可权利与种植表现。"""
    varieties = {v["id"]: v for v in data["varieties"]}
    if variety_id not in varieties:
        raise KeyError(variety_id)
    cross = next(c for c in data["crosses"] if c["id"] == varieties[variety_id]["cross_id"])

    # 试验条件
    trial_rows = [t for t in data["trials"] if t["variety_id"] == variety_id]
    proto_ids = {t["protocol_id"] for t in trial_rows}
    trial_detail = []
    for t in sorted(trial_rows, key=lambda x: (x["year"], x["site_id"])):
        obs = [o for o in data["observations"] if o["trial_id"] == t["id"]]
        trial_detail.append({
            "trial_id": t["id"], "site_id": t["site_id"], "year": t["year"],
            "season": t["season"], "sowing_at": t.get("sowing_at"),
            "harvest_at": t.get("harvest_at"), "weather_note": t.get("weather_note"),
            "manager_person": t.get("manager_person"),
            "observations": [{"metric": o["metric_code"], "value": o["value"],
                              "negative": o["negative"]} for o in obs],
        })

    approvals = [a for a in data["approvals"] if a["variety_id"] == variety_id]
    change_sets = [c for c in data["approval_change_sets"] if c["variety_id"] == variety_id]
    packages = [p for p in data["technique_packages"] if p["variety_id"] == variety_id]
    licenses = [l for l in data["licenses"] if l["variety_id"] == variety_id]
    feedback = [f for f in data["adoption_feedback"] if f["variety_id"] == variety_id]
    negatives = [n for n in data["negative_results"] if n["variety_id"] == variety_id]
    withdrawals = [w for w in data["withdrawals"] if w["variety_id"] == variety_id]
    ip = [x for x in data["ip_shares"] if x["variety_id"] == variety_id]

    total_area = {}
    for f in feedback:
        total_area[f["region"]] = total_area.get(f["region"], 0) + f["planted_area_mu"]

    return {
        "variety": varieties[variety_id],
        "material_source": material_source_trace(data, variety_id),
        "cross": cross,
        "protocols": [p for p in data["trial_protocols"] if p["id"] in proto_ids],
        "multi_site_multi_year": {
            "site_count": len({t["site_id"] for t in trial_rows}),
            "year_count": len({t["year"] for t in trial_rows}),
            "trials": trial_detail,
        },
        "negative_results": negatives,
        "approvals": approvals,
        "change_sets": change_sets,
        "technique_packages": packages,
        "ip_share_history": sorted(ip, key=lambda x: (x["effective_from"], x["version"])),
        "licenses": licenses,
        "adoption_feedback": feedback,
        "planted_area_mu_by_region": total_area,
        "withdrawals": withdrawals,
    }


# ---------------------------------------------------------------- 范围生效解析

def effective_packages(data: dict, variety_id: str, region: str, on_date: str) -> list[dict]:
    """某品种在某区域、某日生效的配套技术（范围匹配 + 未被取代 + 已发布）。"""
    result = []
    for pk in data["technique_packages"]:
        if pk["variety_id"] != variety_id:
            continue
        if pk["issued_at"] > on_date:
            continue
        if region not in pk["scope"].get("regions", []):
            continue
        # 被同品种更新包取代则失效
        superseded = any(other.get("supersedes") == pk["id"]
                         and other["issued_at"] <= on_date
                         and other["variety_id"] == variety_id
                         for other in data["technique_packages"])
        if not superseded:
            result.append(pk)
    return result


def licenses_in_force(data: dict, variety_id: str, region: str, on_date: str) -> list[dict]:
    """某区域、某日处于授权期内且范围覆盖、未被撤回终止的许可。"""
    out = []
    for lic in data["licenses"]:
        if lic["variety_id"] != variety_id or lic["status"] != "active":
            continue
        if not (lic["term"]["start"] <= on_date <= lic["term"].get("end", "9999-12-31")):
            continue
        if region not in lic["scope"].get("regions", []):
            continue
        out.append(lic)
    return out


# ---------------------------------------------------------------- 时间线

_TIMELINE_SOURCES = [
    ("material_transfers", "transferred_at", "材料移交"),
    ("crosses", "made_at", "亲本组合配制"),
    ("demands", "raised_at", "企业需求提出"),
    ("trial_protocols", "approved_at", "试验方案批准"),
    ("approvals", "approved_at", "品种审定"),
    ("approval_change_sets", "issued_at", "区域适配/版本变更"),
    ("technique_packages", "issued_at", "配套技术发布"),
    ("ip_shares", "effective_from", "知识产权比例生效"),
    ("confirmations", "at", "多方确认"),
    ("licenses", "signed_at", "许可签署"),
    ("adoption_feedback", "reported_at", "推广面积回传"),
    ("negative_results", "recorded_at", "负面结果登记"),
    ("withdrawals", "noticed_at", "品种撤回"),
]


def _variety_scope_ids(data: dict, variety_id: str) -> dict[str, set[str]]:
    """计算某品种时间线允许出现的记录ID：直接挂品种的记录，加上材料阶段祖先
    （品种组合、父母本材料的移交、其方案实际考核的企业需求），以及与这些记录
    相关的多方确认。"""
    allowed: dict[str, set[str]] = {sec: set() for sec, _, _ in _TIMELINE_SOURCES}

    tagged = ["trial_protocols", "approvals", "approval_change_sets",
              "technique_packages", "ip_shares", "licenses",
              "adoption_feedback", "negative_results", "withdrawals"]
    for sec in tagged:
        for row in data.get(sec, []):
            if row.get("variety_id") == variety_id:
                allowed[sec].add(row["id"])

    variety = next(v for v in data["varieties"] if v["id"] == variety_id)
    cross_id = variety["cross_id"]
    allowed["crosses"].add(cross_id)
    cross = next(c for c in data["crosses"] if c["id"] == cross_id)
    parent_ids = {cross["female_parent"], cross["male_parent"]}
    for tr in data["material_transfers"]:
        if tr["material_id"] in parent_ids:
            allowed["material_transfers"].add(tr["id"])

    # 仅纳入本品种方案中实际转化/考核到的企业需求
    proto_ids = {p["id"] for p in data["trial_protocols"]
                 if p.get("variety_id") == variety_id}
    used_indicators = {m.get("indicator_id")
                       for p in data["trial_protocols"] if p["id"] in proto_ids
                       for m in p["metrics"] if m.get("indicator_id")}
    for dm in data["demands"]:
        if set(dm["indicators"]) & used_indicators:
            allowed["demands"].add(dm["id"])

    known_entities = set().union(*allowed.values())
    for conf in data["confirmations"]:
        if set(conf.get("related", [])) & known_entities:
            allowed["confirmations"].add(conf["id"])
    return allowed


def timeline(data: dict, variety_id: str | None = None) -> list[dict]:
    """关键动作的时间依据；可按品种过滤（沿材料来源祖先链收敛）。"""
    allowed = _variety_scope_ids(data, variety_id) if variety_id else None
    events = []
    for section, date_field, label in _TIMELINE_SOURCES:
        for row in data.get(section, []):
            if allowed is not None and row.get("id") not in allowed[section]:
                continue
            events.append({
                "at": row.get(date_field),
                "label": label,
                "section": section,
                "id": row.get("id"),
                "variety_id": row.get("variety_id"),
            })
    return sorted(events, key=lambda x: (x["at"] or "", x["section"], x["id"]))
