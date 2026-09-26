"""读取并检查种业联合研发转化后台的领域资料。"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

REQUIRED_COLLECTIONS = (
    "parties",
    "crop_categories",
    "indicators",
    "germplasm",
    "material_transfers",
    "lineage_branches",
    "crosses",
    "requirements",
    "trial_protocols",
    "trial_observations",
    "registrations",
    "cultivation_techniques",
    "licenses",
    "promotion_feedback",
    "events",
)

EVENT_TARGET = {
    "joint_confirmation": "licenses",
    "ip_adjustment": "licenses",
    "area_report": "promotion_feedback",
    "withdrawal": "registrations",
}

RESULT_KINDS = {"positive", "negative", "failed"}
REQUIREMENT_STATUS = {"testing", "supported", "partial", "unsupported"}


def load_context(path: Path) -> dict:
    """读取领域资料，完成结构与关联检查后返回。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {"domain", "version", "sample_id", "facts", "constraints", *REQUIRED_COLLECTIONS}
    if not required.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 2 or not value["facts"] or not value["constraints"]:
        raise ValueError("领域资料内容不完整")
    validate_integrity(value)
    return value


def validate_integrity(context: dict) -> None:
    """检查集合间引用、参与方隔离、作用范围与时间依据，违反时抛出 ValueError。"""
    parties = _index(context["parties"], "参与方")
    categories = _index(context["crop_categories"], "特色品类")
    indicators = _index(context["indicators"], "试验指标")
    germplasm = _index(context["germplasm"], "种质材料")
    transfers = _index(context["material_transfers"], "材料移交")
    branches = _index(context["lineage_branches"], "谱系分支")
    crosses = _index(context["crosses"], "亲本组合")
    requirements = _index(context["requirements"], "企业需求")
    protocols = _index(context["trial_protocols"], "试验方案")
    observations = _index(context["trial_observations"], "试验数据")
    registrations = _index(context["registrations"], "品种审定")
    techniques = _index(context["cultivation_techniques"], "配套技术")
    licenses = _index(context["licenses"], "许可")
    feedback = _index(context["promotion_feedback"], "推广反馈")
    events = _index(context["events"], "事件")

    # 材料移交必须先建立索引，供谱系与未公开性状隔离校验
    for transfer in transfers.values():
        if transfer["material"] not in germplasm:
            raise ValueError(f"材料移交{transfer['id']}引用了未知种质材料")
        for party_id in (transfer["from"], transfer["to"]):
            if party_id not in parties:
                raise ValueError(f"材料移交{transfer['id']}涉及未知参与方")
        if not transfer["purpose"]:
            raise ValueError(f"材料移交{transfer['id']}缺少用途说明")
        if transfer["quantity"]["value"] <= 0:
            raise ValueError(f"材料移交{transfer['id']}缺少有效数量")
        if not transfer["generation"]:
            raise ValueError(f"材料移交{transfer['id']}缺少繁殖代次")
        _parse_date(transfer["transferred_at"], f"材料移交{transfer['id']}")
        if transfer["branch"] not in branches:
            raise ValueError(f"材料移交{transfer['id']}未关联谱系分支")

    # 种质材料：未公开性状只对保存方或实际接收过该材料的团队可见
    material_receivers: dict[str, set[str]] = {}
    for transfer in transfers.values():
        material_receivers.setdefault(transfer["material"], set()).add(transfer["to"])
    for material in germplasm.values():
        if material["category"] not in categories:
            raise ValueError(f"种质材料{material['id']}引用了未知特色品类")
        if material["holder"] not in parties:
            raise ValueError(f"种质材料{material['id']}的保存单位未知")
        allowed = {material["holder"]} | material_receivers.get(material["id"], set())
        for trait in material.get("restricted_traits", []):
            visible = set(trait["visible_to"])
            if not visible or not visible <= allowed:
                raise ValueError(f"种质材料{material['id']}的未公开性状未按参与方隔离")

    # 谱系分支：同一资源由多个团队使用时各自可追踪
    for branch in branches.values():
        if branch["material"] not in germplasm:
            raise ValueError(f"谱系分支{branch['id']}引用了未知种质材料")
        if branch["maintained_by"] not in parties:
            raise ValueError(f"谱系分支{branch['id']}的维护团队未知")
        transfer = transfers.get(branch["origin_transfer"])
        if transfer is None:
            raise ValueError(f"谱系分支{branch['id']}缺少来源移交记录")
        if transfer["material"] != branch["material"] or transfer["to"] != branch["maintained_by"]:
            raise ValueError(f"谱系分支{branch['id']}无法追踪到材料移交")
        if transfer["branch"] != branch["id"]:
            raise ValueError(f"谱系分支{branch['id']}与材料移交记录不一致")
        for entry in branch["entries"]:
            if not entry["generation"]:
                raise ValueError(f"谱系分支{branch['id']}缺少繁殖代次记录")
            _parse_date(entry["recorded_at"], f"谱系分支{branch['id']}")

    # 亲本组合：父母本可追到谱系分支或种质材料
    for cross in crosses.values():
        if cross["made_by"] not in parties:
            raise ValueError(f"亲本组合{cross['id']}的组配单位未知")
        _parse_date(cross["made_at"], f"亲本组合{cross['id']}")
        for role in ("female", "male"):
            parent = cross[role]
            branch_id = parent.get("branch")
            if branch_id:
                branch = branches.get(branch_id)
                if branch is None:
                    raise ValueError(f"亲本组合{cross['id']}引用了未知谱系分支")
                if parent["germplasm"] != branch["material"]:
                    raise ValueError(f"亲本组合{cross['id']}的亲本材料与谱系分支不一致")
            elif parent["germplasm"] not in germplasm:
                raise ValueError(f"亲本组合{cross['id']}的亲本未引用已知种质材料")

    # 企业需求：必须转成试验指标，证据与指标对应
    for req in requirements.values():
        if req["raised_by"] not in parties:
            raise ValueError(f"企业需求{req['id']}的提出方未知")
        _parse_date(req["raised_at"], f"企业需求{req['id']}")
        if not req["indicator_ids"]:
            raise ValueError(f"企业需求{req['id']}尚未转成试验指标")
        for indicator_id in req["indicator_ids"]:
            if indicator_id not in indicators:
                raise ValueError(f"企业需求{req['id']}引用了未知试验指标")
        if req["status"] not in REQUIREMENT_STATUS:
            raise ValueError(f"企业需求{req['id']}的证据支持状态无效")
        for obs_id in req["evidence"]:
            obs = observations.get(obs_id)
            if obs is None:
                raise ValueError(f"企业需求{req['id']}引用了未知试验数据")
            if obs["indicator"] not in req["indicator_ids"]:
                raise ValueError(f"企业需求{req['id']}的证据与试验指标不对应")

    # 试验方案与多点多年数据
    for protocol in protocols.values():
        if protocol["cross"] not in crosses:
            raise ValueError(f"试验方案{protocol['id']}引用了未知亲本组合")
        for indicator_id in protocol["indicator_ids"]:
            if indicator_id not in indicators:
                raise ValueError(f"试验方案{protocol['id']}引用了未知试验指标")
        if not protocol["sites"] or not protocol["years"]:
            raise ValueError(f"试验方案{protocol['id']}缺少试验点或年份")
        for site in protocol["sites"]:
            if parties.get(site, {}).get("kind") != "trial_site":
                raise ValueError(f"试验方案{protocol['id']}包含未知试验点")

    for obs in observations.values():
        protocol = protocols.get(obs["protocol"])
        if protocol is None:
            raise ValueError(f"试验数据{obs['id']}引用了未知试验方案")
        if obs["site"] not in protocol["sites"] or obs["year"] not in protocol["years"]:
            raise ValueError(f"试验数据{obs['id']}与试验方案的多点多年安排不一致")
        if obs["indicator"] not in protocol["indicator_ids"]:
            raise ValueError(f"试验数据{obs['id']}的指标不在试验方案内")
        if obs["result"] not in RESULT_KINDS:
            raise ValueError(f"试验数据{obs['id']}的结果类型无效")
        if obs["recorded_by"] not in parties:
            raise ValueError(f"试验数据{obs['id']}的记录单位未知")
        _parse_date(obs["recorded_at"], f"试验数据{obs['id']}")

    # 品种审定：版本与区域构成明确范围，撤回留下时间依据
    for reg in registrations.values():
        if reg["cross"] not in crosses:
            raise ValueError(f"品种审定{reg['id']}引用了未知亲本组合")
        if not reg["version"]:
            raise ValueError(f"品种审定{reg['id']}缺少审定版本")
        _parse_date(reg["approved_at"], f"品种审定{reg['id']}")
        if not reg["scope"]["regions"]:
            raise ValueError(f"品种审定{reg['id']}缺少明确适用范围")
        withdrawn_at = reg.get("withdrawn_at")
        if reg["status"] == "withdrawn":
            if not withdrawn_at:
                raise ValueError(f"品种审定{reg['id']}已撤回但缺少时间依据")
            _parse_date(withdrawn_at, f"品种审定{reg['id']}")
        elif withdrawn_at:
            raise ValueError(f"品种审定{reg['id']}在有效期内不应带有撤回时间")

    # 配套技术：变化只作用于审定范围内的明确区域
    for tech in techniques.values():
        reg = registrations.get(tech["registration"])
        if reg is None:
            raise ValueError(f"配套技术{tech['id']}引用了未知品种审定")
        regions = tech["applies_to"]["regions"]
        if not regions:
            raise ValueError(f"配套技术{tech['id']}缺少明确作用范围")
        if not set(regions) <= set(reg["scope"]["regions"]):
            raise ValueError(f"配套技术{tech['id']}超出了品种审定的明确范围")
        _parse_date(tech["issued_at"], f"配套技术{tech['id']}")

    # 许可：商业条款按参与方隔离，知识产权比例调整须双方确认
    for lic in licenses.values():
        if lic["registration"] not in registrations:
            raise ValueError(f"许可{lic['id']}引用了未知品种审定")
        counterparties = {lic["licensor"], lic["licensee"]}
        if not counterparties <= set(parties):
            raise ValueError(f"许可{lic['id']}涉及未知参与方")
        _parse_date(lic["granted_at"], f"许可{lic['id']}")
        _check_shares(lic["ip_shares"], parties, f"许可{lic['id']}")
        if set(lic["terms_visible_to"]) != counterparties:
            raise ValueError(f"许可{lic['id']}的商业条款未按参与方隔离")
        for adj in lic["adjustments"]:
            _parse_datetime(adj["adjusted_at"], f"许可{lic['id']}的比例调整")
            _check_shares(adj["ip_shares"], parties, f"许可{lic['id']}的比例调整")
            if set(adj["confirmed_by"]) != counterparties:
                raise ValueError(f"许可{lic['id']}的知识产权比例调整未经合作双方确认")
        terminated_at = lic.get("terminated_at")
        if lic["status"] == "terminated":
            if not terminated_at:
                raise ValueError(f"许可{lic['id']}已终止但缺少时间依据")
            _parse_date(terminated_at, f"许可{lic['id']}")
        elif terminated_at:
            raise ValueError(f"许可{lic['id']}在有效期内不应带有终止时间")

    # 推广反馈：面积回传
    for fb in feedback.values():
        if fb["license"] not in licenses:
            raise ValueError(f"推广反馈{fb['id']}引用了未知许可")
        if fb["reported_by"] not in parties:
            raise ValueError(f"推广反馈{fb['id']}的回传方未知")
        _parse_date(fb["reported_at"], f"推广反馈{fb['id']}")
        if fb["area_mu"] < 0:
            raise ValueError(f"推广反馈{fb['id']}的推广面积无效")

    # 事件：合作方确认、比例调整、面积回传和品种撤回的时间依据
    targets = {"licenses": licenses, "promotion_feedback": feedback, "registrations": registrations}
    for event in events.values():
        kind = event["kind"]
        if kind not in EVENT_TARGET:
            raise ValueError(f"事件{event['id']}的类型无效")
        happened_at = _parse_datetime(event["at"], f"事件{event['id']}")
        if not event["parties"]:
            raise ValueError(f"事件{event['id']}缺少参与方")
        for party_id in event["parties"]:
            if party_id not in parties:
                raise ValueError(f"事件{event['id']}涉及未知参与方")
        target = targets[EVENT_TARGET[kind]].get(event["ref"])
        if target is None:
            raise ValueError(f"事件{event['id']}引用了未知记录")
        if kind == "withdrawal":
            if target["status"] != "withdrawn" or target["withdrawn_at"] != happened_at.date().isoformat():
                raise ValueError(f"事件{event['id']}与品种撤回记录不一致")
        elif kind == "area_report":
            if target["reported_at"] != happened_at.date().isoformat():
                raise ValueError(f"事件{event['id']}与推广面积回传记录不一致")
        elif kind == "ip_adjustment":
            if set(event["parties"]) != {target["licensor"], target["licensee"]}:
                raise ValueError(f"事件{event['id']}的知识产权比例调整须由合作双方确认")
            if not any(adj["adjusted_at"] == event["at"] for adj in target["adjustments"]):
                raise ValueError(f"事件{event['id']}缺少对应的许可调整记录")
        elif kind == "joint_confirmation":
            if set(event["parties"]) != {target["licensor"], target["licensee"]}:
                raise ValueError(f"事件{event['id']}须由合作双方同时确认")
            if target["granted_at"] != happened_at.date().isoformat():
                raise ValueError(f"事件{event['id']}与许可记录不一致")


def trace_license(context: dict, license_id: str) -> dict:
    """从许可追到品种审定、亲本组合、种质来源、试验数据与推广反馈。"""
    lic = _find(context["licenses"], license_id, "许可")
    reg = _find(context["registrations"], lic["registration"], "品种审定")
    cross = _find(context["crosses"], reg["cross"], "亲本组合")
    parents = {}
    for role in ("female", "male"):
        parent = cross[role]
        branch = (
            _find(context["lineage_branches"], parent["branch"], "谱系分支")
            if parent.get("branch")
            else None
        )
        transfer = (
            _find(context["material_transfers"], branch["origin_transfer"], "材料移交")
            if branch
            else None
        )
        material_id = branch["material"] if branch else parent["germplasm"]
        parents[role] = {
            "germplasm": _find(context["germplasm"], material_id, "种质材料"),
            "branch": branch,
            "transfer": transfer,
        }
    protocols = [p for p in context["trial_protocols"] if p["cross"] == cross["id"]]
    protocol_ids = {p["id"] for p in protocols}
    observations = [o for o in context["trial_observations"] if o["protocol"] in protocol_ids]
    techniques = [t for t in context["cultivation_techniques"] if t["registration"] == reg["id"]]
    feedback = [f for f in context["promotion_feedback"] if f["license"] == lic["id"]]
    return {
        "license": lic,
        "registration": reg,
        "cross": cross,
        "parents": parents,
        "protocols": protocols,
        "observations": observations,
        "techniques": techniques,
        "feedback": feedback,
    }


def _index(rows: list[dict], label: str) -> dict:
    table = {}
    for row in rows:
        row_id = row.get("id")
        if not row_id or row_id in table:
            raise ValueError(f"{label}存在缺失或重复的标识: {row_id!r}")
        table[row_id] = row
    return table


def _find(rows: list[dict], row_id: str, label: str) -> dict:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise ValueError(f"未找到{label}: {row_id}")


def _check_shares(shares: list[dict], parties: dict, label: str) -> None:
    if len(shares) < 2:
        raise ValueError(f"{label}的知识产权比例缺少参与方")
    total = 0.0
    for share in shares:
        if share["party"] not in parties:
            raise ValueError(f"{label}的知识产权比例涉及未知参与方")
        total += share["share"]
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"{label}的知识产权比例之和必须为1")


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}不是有效日期: {value!r}") from None


def _parse_datetime(value: str, label: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}不是有效时间: {value!r}") from None
