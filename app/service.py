"""归档筛查业务：录入校验 -> 摘要/契约一致性 -> 双字段联合筛查。

判定原则（不按报文条数展开，不枚举报文）：
- 每个固件段用 z3 对契约做「双字段同时命中」可满足性证明（yes/no/unknown），
  支持顶层 anyOf；
- 整包计数（摘要）与各段契约必须同时成立：摘要里某状态条数为正，却没有任何
  固件段能产生该状态 => 摘要与契约矛盾，整包标「待核」；
- 只有所有固件段都被证明不可能命中（no），才可跳过该包；
- 任一段可能命中（yes）即列为命中候选段；出现 unknown 或矛盾 => 待核。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from db import (ArchivePackage, DeviceModel, EnumField, FirmwareSegment,
                SessionLocal)
from schema_spec import (MISSING, NULL, SchemaError, parse_schema, parse_state,
                         value_state)
from solver import SAT_NO, SAT_UNKNOWN, SAT_YES, find_witness

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
MAX_FIELDS = 2
MAX_SEGMENTS = 8
MAX_PACKAGES = 32
MAX_MESSAGE_COUNT = 10**9


class ServiceError(ValueError):
    """录入数据不合法。"""


@dataclass
class SegmentResult:
    segment: str
    verdict: str          # yes | no | unknown
    witness: dict | None = None
    reasons: list = field(default_factory=list)


@dataclass
class PackageResult:
    package_no: str
    verdict: str          # hit | skip | review
    summary: dict
    segments: list = field(default_factory=list)  # list[SegmentResult]
    reasons: list = field(default_factory=list)
    possible_segments: list = field(default_factory=list)


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


# ---------------- 录入校验 ----------------

def _check_name(name: str, what: str):
    if not valid_name(name):
        raise ServiceError(f"{what}只能由字母数字及 _ . - 组成，长度 1~64")


def _norm_count(v, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ServiceError(f"{where}: 条数必须是非负整数")
    if v < 0:
        raise ServiceError(f"{where}: 条数不能为负")
    if v > MAX_MESSAGE_COUNT:
        raise ServiceError(f"{where}: 条数超出上限 {MAX_MESSAGE_COUNT}")
    return v


def _validate_enum_values(values, where: str) -> list:
    if not isinstance(values, list) or not values:
        raise ServiceError(f"{where}: 枚举值清单必须是非空数组")
    seen = set()
    norm = []
    for v in values:
        if v is None or isinstance(v, bool):
            raise ServiceError(f"{where}: 枚举值只允许字符串/整数；"
                               "缺省与显式 null 是独立隐式状态，不列入枚举清单")
        if not isinstance(v, (str, int)):
            raise ServiceError(f"{where}: 枚举值只允许字符串/整数")
        if v in seen:
            raise ServiceError(f"{where}: 枚举值 {v!r} 重复")
        seen.add(v)
        norm.append(v)
    norm.sort(key=lambda x: (isinstance(x, str), x))
    return norm


def setup_model_fields(model_id: int, fields_in: list) -> None:
    """型号首次登记两个枚举字段（幂等：已存在则报错）。"""
    if len(fields_in) != MAX_FIELDS:
        raise ServiceError(f"每个型号必须登记恰好 {MAX_FIELDS} 个枚举字段")
    with SessionLocal() as s:
        model = s.get(DeviceModel, model_id)
        if model is None:
            raise ServiceError("型号不存在")
        existing = s.execute(select(EnumField).where(EnumField.model_id == model_id)
                             ).scalars().all()
        if existing:
            raise ServiceError("枚举字段已登记，不可修改")
        keys = set()
        for i, fin in enumerate(fields_in):
            fkey = (fin.get("field_key") or "").strip()
            label = (fin.get("label") or "").strip() or fkey
            _check_name(fkey, f"字段{i+1}字段名")
            if fkey in keys:
                raise ServiceError(f"字段名 {fkey!r} 重复")
            keys.add(fkey)
            values = _validate_enum_values(fin.get("values"), f"字段 {fkey}")
            s.add(EnumField(model_id=model_id, field_key=fkey, label=label, values=values))
        s.commit()


def _load_fields(s, model_id: int):
    rows = s.execute(select(EnumField).where(EnumField.model_id == model_id)
                     .order_by(EnumField.id)).scalars().all()
    if len(rows) != MAX_FIELDS:
        raise ServiceError("请先在型号页登记两个枚举字段")
    return rows


def _allowed_state_map(enum_field: EnumField) -> dict:
    """枚举字段 -> 允许的全部状态键（含缺省、显式 null）。"""
    states = {MISSING, NULL}
    for v in enum_field.values:
        states.add(value_state(v))
    return states


def _validate_counts(raw_counts, enum_fields, where: str) -> dict:
    if not isinstance(raw_counts, dict):
        raise ServiceError(f"{where}: 整包计数必须是对象")
    norm = {}
    for ef in enum_fields:
        allowed = _allowed_state_map(ef)
        part = raw_counts.get(ef.field_key)
        if part is None:
            raise ServiceError(f"{where}: 缺少字段 {ef.field_key!r} 的状态计数")
        if not isinstance(part, dict):
            raise ServiceError(f"{where}.{ef.field_key}: 计数必须是对象")
        clean = {}
        for state_key, n in part.items():
            try:
                kind, value = parse_state(state_key)
            except SchemaError as e:
                raise ServiceError(f"{where}.{ef.field_key}: {e}")
            if state_key not in allowed:
                raise ServiceError(
                    f"{where}.{ef.field_key}: 状态 {state_key!r} 不在字段枚举范围内"
                    "（含缺省 missing / 显式 null）")
            clean[state_key] = _norm_count(n, f"{where}.{ef.field_key}.{state_key}")
        # 未列出的状态补 0（显式零与缺省键语义一致，均按零处理）
        for st in allowed:
            clean.setdefault(st, 0)
        norm[ef.field_key] = clean
    unknown = set(raw_counts) - {ef.field_key for ef in enum_fields}
    if unknown:
        raise ServiceError(f"{where}: 出现未登记字段的计数 {sorted(unknown)}")
    return norm


def create_package(model_id: int, package_no: str, raw_counts, note,
                   segments_in: list) -> ArchivePackage:
    """录入一个离线归档包（含各固件段报文条数与契约），失败整体不落库。"""
    _check_name(package_no, "包号")
    if not isinstance(segments_in, list) or not segments_in:
        raise ServiceError("每个包至少包含一个固件段")
    if len(segments_in) > MAX_SEGMENTS:
        raise ServiceError(f"每包固件段至多 {MAX_SEGMENTS} 个")

    parsed_segments = []
    seen_seg = set()
    seg_total = 0
    for i, seg in enumerate(segments_in):
        seg_name = (seg.get("segment") or "").strip()
        _check_name(seg_name, f"固件段{i+1}标识")
        if seg_name in seen_seg:
            raise ServiceError(f"固件段 {seg_name!r} 重复")
        seen_seg.add(seg_name)
        n = _norm_count(seg.get("message_count"), f"固件段 {seg_name} 报文条数")
        try:
            schema = parse_schema(seg.get("schema"))
        except SchemaError as e:
            raise ServiceError(f"固件段 {seg_name} 契约不符合受限语法：{e}")
        parsed_segments.append((seg_name, n, schema, seg.get("schema")))
        seg_total += n

    with SessionLocal() as s:
        model = s.get(DeviceModel, model_id)
        if model is None:
            raise ServiceError("型号不存在")
        enum_fields = _load_fields(s, model_id)
        pkg_count = s.execute(
            select(ArchivePackage).where(ArchivePackage.model_id == model_id)
        ).scalars().all()
        if len(pkg_count) >= MAX_PACKAGES:
            raise ServiceError(f"每型号归档包至多 {MAX_PACKAGES} 个")
        dup = s.execute(select(ArchivePackage).where(
            ArchivePackage.model_id == model_id,
            ArchivePackage.package_no == package_no)).scalar_one_or_none()
        if dup is not None:
            raise ServiceError(f"包号 {package_no!r} 已存在")

        counts = _validate_counts(raw_counts, enum_fields, "整包计数")

        # 摘要守恒：每个字段各状态条数之和必须等于各段报文条数之和
        for ef in enum_fields:
            total = sum(counts[ef.field_key].values())
            if total != seg_total:
                raise ServiceError(
                    f"字段 {ef.field_key!r} 各状态条数之和 {total} "
                    f"≠ 各固件段报文条数之和 {seg_total}（摘要与段条数矛盾）")

        pkg = ArchivePackage(model_id=model_id, package_no=package_no,
                             counts=counts, note=(note or None))
        s.add(pkg)
        s.flush()  # 取 pkg.id
        for seg_name, n, _schema, raw_doc in parsed_segments:
            s.add(FirmwareSegment(package_id=pkg.id, segment=seg_name,
                                  message_count=n, doc=raw_doc))
        s.commit()
        s.refresh(pkg)
        return pkg


# ---------------- 筛查 ----------------

def check_consistency(pkg: ArchivePackage, segments, enum_fields) -> list:
    """整包摘要与各段契约是否同时成立。返回矛盾原因列表（空=一致）。

    不按报文条数展开：只证明「摘要中条数为正的状态，至少存在一个固件段能产生」。
    若全部段都不可能产生（no）或无法判定（unknown），按矛盾/待核处理。
    """
    problems = []
    for ef in enum_fields:
        positive = [st for st, n in pkg.counts[ef.field_key].items() if n > 0]
        for st in positive:
            verdicts = []
            for seg in segments:
                if seg["message_count"] <= 0:
                    continue  # 零条数段不承载任何报文
                v, _ = find_witness(seg["schema"], [(ef.field_key, st)])
                verdicts.append(v)
            if SAT_YES in verdicts:
                continue
            if SAT_UNKNOWN in verdicts:
                problems.append(
                    f"字段 {ef.field_key!r} 状态 {st} 摘要计数为正，"
                    "但各固件段是否能产生该状态无法完成证明（z3 超时）")
            else:
                problems.append(
                    f"字段 {ef.field_key!r} 状态 {st} 摘要计数为正，"
                    "但所有固件段的契约都不可能产生该状态（摘要与契约矛盾）")
    return problems


def screen_package(pkg: ArchivePackage, enum_fields, query: dict) -> PackageResult:
    """对一个包执行双字段联合筛查。

    query: {field_key: state_key}，恰好两个字段各一个待查状态。
    """
    if len(query) != MAX_FIELDS or set(query) != {ef.field_key for ef in enum_fields}:
        raise ServiceError("必须为两个枚举字段各选择一个待查状态")
    conditions = []
    for ef in enum_fields:
        st = query[ef.field_key]
        if st not in _allowed_state_map(ef):
            raise ServiceError(f"字段 {ef.field_key!r} 的待查状态 {st!r} 非法")
        conditions.append((ef.field_key, st))

    with SessionLocal() as s:
        seg_rows = s.execute(select(FirmwareSegment).where(
            FirmwareSegment.package_id == pkg.id).order_by(FirmwareSegment.id)
        ).scalars().all()
        segments = [{"segment": r.segment, "message_count": r.message_count,
                     "schema": parse_schema(r.doc)} for r in seg_rows]

    reasons = []
    seg_results = []
    possible = []
    has_unknown = False

    # 1) 各固件段：双字段同时命中的可满足性（契约级证明，不展开条数）
    for seg in segments:
        if seg["message_count"] <= 0:
            # 零条数段不含任何报文，不可能贡献命中
            sr = SegmentResult(segment=seg["segment"], verdict=SAT_NO)
            sr.reasons.append("该固件段报文条数为 0，不含任何报文")
            seg_results.append(sr)
            continue
        verdict, witness = find_witness(seg["schema"], conditions)
        sr = SegmentResult(segment=seg["segment"], verdict=verdict, witness=witness)
        if verdict == SAT_YES:
            possible.append(seg["segment"])
            sr.reasons.append("契约允许同时命中两个待查状态的报文")
        elif verdict == SAT_NO:
            sr.reasons.append("契约已证明不存在同时命中两个待查状态的报文（UNSAT）")
        else:
            has_unknown = True
            sr.reasons.append("z3 求解超时/未知，无法判定")
        seg_results.append(sr)

    # 2) 整包摘要：两个待查状态的整包计数必须都为正，否则没有命中报文
    summary = {}
    summary_block = False
    for ef in enum_fields:
        n = pkg.counts[ef.field_key].get(query[ef.field_key], 0)
        summary[ef.field_key] = {"state": query[ef.field_key], "count": n}
        if n <= 0:
            summary_block = True
            reasons.append(
                f"字段 {ef.field_key!r} 待查状态 {query[ef.field_key]!r} "
                "整包计数为 0，整包不存在该状态的报文")

    # 3) 摘要与契约一致性（全部正计数状态都须能由至少一个段产生）
    consistency_problems = check_consistency(pkg, segments, enum_fields)
    if consistency_problems:
        reasons.extend(consistency_problems)

    # 4) 汇总结论
    if consistency_problems or has_unknown:
        verdict = "review"
        if consistency_problems:
            reasons.append("摘要与契约矛盾或无法判定，整包标为待核")
    elif summary_block:
        # 整包计数为 0：联合命中不可能（计数是报文条数的精确摘要）
        verdict = "skip"
        reasons.append("整包计数已排除同时命中，跳过该包")
    elif not possible:
        # 所有固件段都证明不可能命中 —— 唯一允许跳过的契约情形
        verdict = "skip"
        reasons.append("所有固件段都不可能命中，跳过该包")
    else:
        verdict = "hit"
        reasons.append("整包计数为正且存在契约允许同时命中的固件段")

    return PackageResult(package_no=pkg.package_no, verdict=verdict,
                         summary=summary, segments=seg_results,
                         reasons=reasons, possible_segments=possible)


def screen_model(model_id: int, query: dict) -> list:
    """对型号下全部归档包执行筛查，返回 PackageResult 列表。"""
    with SessionLocal() as s:
        enum_fields = _load_fields(s, model_id)
        pkgs = s.execute(select(ArchivePackage).where(
            ArchivePackage.model_id == model_id).order_by(ArchivePackage.id)
        ).scalars().all()
        data = [(p.id, p.package_no, p.counts) for p in pkgs]

    results = []
    for pid, pno, counts in data:
        pkg = ArchivePackage(id=pid, package_no=pno, counts=counts)
        results.append(screen_package(pkg, enum_fields, query))
    return results, enum_fields
