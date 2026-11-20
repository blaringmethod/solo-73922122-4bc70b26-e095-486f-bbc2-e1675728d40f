"""单元自测：状态键解析、z3 字段状态可满足性、anyOf 分支、纯 Python 复核、筛查服务。
运行：python3 tests/test_units.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# 用独立临时 SQLite 库，避免污染工作目录
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from sqlalchemy import select  # noqa: E402

import service  # noqa: E402
from db import (ArchivePackage, Base, DeviceModel, EnumField, SessionLocal,  # noqa: E402
                engine)
from examples import (FIELDS_QC_MODE, PACKAGE_A, PACKAGE_B, PACKAGE_C_CONFLICT,  # noqa: E402
                      PACKAGE_D_SKIP, SEG_OLD, SEG_V1, SEG_V2, SEG_V3_ANYOF)
from schema_spec import (MISSING, NULL, SchemaError, message_has_state,  # noqa: E402
                         message_valid, parse_schema, parse_state,
                         value_state)
from solver import SAT_NO, SAT_UNKNOWN, SAT_YES, find_witness  # noqa: E402

PASS = []


def ok(name, cond):
    assert cond, f"FAILED: {name}"
    PASS.append(name)


def S(doc):
    return parse_schema(doc)


# ---------- 状态键 ----------
ok("状态:缺省键", parse_state("missing") == (MISSING, None))
ok("状态:null键", parse_state("null") == (NULL, None))
ok("状态:整数值键", parse_state("value:3") == ("value", 3))
ok("状态:字符串值键", parse_state('value:"N"') == ("value", "N"))
ok("状态:value_state往返", value_state("N") == 'value:"N"' and value_state(3) == "value:3"
   and value_state(None) == NULL)
try:
    parse_state("value:abc")
    raise AssertionError("FAILED: 非法状态键未报错")
except SchemaError:
    PASS.append("状态:非法键拒绝")

# ---------- 单字段三态可满足性 ----------
v1 = S(SEG_V1)  # qc 必填、mode 必填
v2 = S(SEG_V2)  # qc 可选可 null、mode 必填
verdict, w = find_witness(v1, [("qc", MISSING)])
ok("v1:qc缺省不可能", verdict == SAT_NO and w is None)
verdict, w = find_witness(v2, [("qc", MISSING)])
ok("v2:qc缺省可能且复核", verdict == SAT_YES and message_valid(w, v2)
   and message_has_state(w, "qc", MISSING))
verdict, w = find_witness(v2, [("qc", NULL)])
ok("v2:qc显式null可能且复核", verdict == SAT_YES and message_valid(w, v2)
   and message_has_state(w, "qc", NULL) and w["qc"] is None)
verdict, _ = find_witness(v1, [("qc", NULL)])
ok("v1:qc为null不可能(枚举无null)", verdict == SAT_NO)
verdict, w = find_witness(v1, [("qc", 'value:"Q"')])
ok("v1:qc=Q可能且复核", verdict == SAT_YES and w["qc"] == "Q")
verdict, _ = find_witness(v1, [("qc", 'value:"X"')])
ok("v1:qc=越界枚举不可能", verdict == SAT_NO)
verdict, _ = find_witness(v1, [("mode", 'value:"storm"')])
ok("v1:mode=storm可能(单字段)", verdict == SAT_YES)
verdict, _ = find_witness(S(SEG_OLD), [("mode", 'value:"storm"')])
ok("OLD:mode=storm不可能(枚举只有routine)", verdict == SAT_NO)

# 未声明字段：只能缺省
verdict, _ = find_witness(v1, [("unknown_field", MISSING)])
ok("未声明字段:缺省可满足", verdict == SAT_YES)
verdict, _ = find_witness(v1, [("unknown_field", 'value:"x"')])
ok("未声明字段:取值不可能", verdict == SAT_NO)

# ---------- 双字段同时命中 ----------
verdict, w = find_witness(v1, [("qc", MISSING), ("mode", 'value:"storm"')])
ok("v1:qc缺省×storm不可能", verdict == SAT_NO)
verdict, w = find_witness(v2, [("qc", MISSING), ("mode", 'value:"storm"')])
ok("v2:qc缺省×storm可能且复核", verdict == SAT_YES and message_valid(w, v2)
   and "qc" not in w and w["mode"] == "storm")
verdict, w = find_witness(v2, [("qc", NULL), ("mode", 'value:"routine"')])
ok("v2:qc=null×routine可能且复核", verdict == SAT_YES and w["qc"] is None
   and w["mode"] == "routine")
# 两个条件互斥时 UNSAT（同一字段不可能既缺省又显式 null）
verdict, _ = find_witness(v2, [("qc", MISSING), ("qc", NULL)])
ok("同字段互斥状态UNSAT", verdict == SAT_NO)

# ---------- 顶层 anyOf ----------
v3 = S(SEG_V3_ANYOF)
verdict, w = find_witness(v3, [("qc", MISSING), ("mode", 'value:"storm"')])
ok("anyOf:缺省qc×storm走风暴分支", verdict == SAT_YES and message_valid(w, v3)
   and "qc" not in w and w["mode"] == "storm")
verdict, w = find_witness(v3, [("qc", 'value:"N"'), ("mode", 'value:"routine"')])
ok("anyOf:qc=N×routine走常规分支", verdict == SAT_YES and w["qc"] == "N"
   and w["mode"] == "routine")
verdict, _ = find_witness(v3, [("qc", MISSING), ("mode", 'value:"routine"')])
# 常规分支 qc 可缺省（非 required），所以可满足
ok("anyOf:常规分支qc也可缺省", verdict == SAT_YES)
verdict, _ = find_witness(v3, [("mode", 'value:"flood"')])
ok("anyOf:越界枚举全分支不可能", verdict == SAT_NO)

# 见证报文必须满足契约（复核机制存在性：SAT 必有 witness）
_verdict, _w = find_witness(v3, [("qc", NULL), ("mode", 'value:"storm"')])
ok("anyOf:null见证复核", _verdict == SAT_YES and message_valid(_w, v3) and _w["qc"] is None)

# ---------- 受限语法仍被强制 ----------
def expect_error(name, doc):
    try:
        parse_schema(doc)
    except SchemaError:
        PASS.append(name)
        return
    raise AssertionError(f"FAILED(应拒绝但未拒绝): {name}")

expect_error("语法:嵌套对象", {"type": "object", "properties": {"a": {"type": "object", "properties": {}}},
                          "additionalProperties": False})
expect_error("语法:数组", {"type": "object", "properties": {"a": {"type": "array"}},
                     "additionalProperties": False})
expect_error("语法:布尔枚举", {"type": "object", "properties": {"a": {"enum": [True]}},
                          "additionalProperties": False})
expect_error("语法:缺additionalProperties", {"type": "object", "properties": {}})
expect_error("语法:anyOf混用", {"anyOf": [{"type": "object", "properties": {}, "additionalProperties": False}],
                            "type": "object"})

# ---------- 服务层：建库与录入校验 ----------
Base.metadata.create_all(engine)
with SessionLocal() as s:
    s.add(DeviceModel(name="UT-MODEL"))
    s.commit()
mid = s.execute(select(DeviceModel).where(DeviceModel.name == "UT-MODEL")).scalar_one().id

service.setup_model_fields(mid, FIELDS_QC_MODE)
ok("字段:登记两个枚举字段", True)
try:
    service.setup_model_fields(mid, FIELDS_QC_MODE)
    raise AssertionError("FAILED: 重复登记未拒绝")
except service.ServiceError:
    PASS.append("字段:重复登记拒绝")

# 守恒校验：状态条数和 != 段条数和
bad_pkg = {
    "package_no": "BAD-1",
    "counts": {"qc": {"missing": 0, "null": 0, 'value:"N"': 2, 'value:"Q"': 0},
               "mode": {'value:"routine"': 2, 'value:"storm"': 0}},
    "segments": [{"segment": "v1", "message_count": 3, "schema": SEG_V1}],
}
try:
    service.create_package(mid, bad_pkg["package_no"], bad_pkg["counts"], None, bad_pkg["segments"])
    raise AssertionError("FAILED: 守恒矛盾未拒绝")
except service.ServiceError as e:
    ok("录入:守恒矛盾拒绝", "矛盾" in str(e))

# 非法状态键
bad_pkg2 = {
    "package_no": "BAD-2",
    "counts": {"qc": {"missing": 1, "null": 0, 'value:"N"': 0, 'value:"Q"': 0},
               "mode": {'value:"routine"': 0, 'value:"storm"': 1}},
    "segments": [{"segment": "v2", "message_count": 1, "schema": SEG_V2}],
}
bad2_counts = {"qc": {"bogus": 1}, "mode": {'value:"routine"': 1}}
try:
    service.create_package(mid, "BAD-2", bad2_counts, None, bad_pkg2["segments"])
    raise AssertionError("FAILED: 非法状态键未拒绝")
except service.ServiceError:
    PASS.append("录入:非法状态键拒绝")

# 负条数
try:
    service.create_package(mid, "BAD-3",
                           {"qc": {"missing": -1}, "mode": {'value:"routine"': 0}},
                           None, [{"segment": "v2", "message_count": 0, "schema": SEG_V2}])
    raise AssertionError("FAILED: 负条数未拒绝")
except service.ServiceError:
    PASS.append("录入:负条数拒绝")

# 正常录入四个示例包
for ex in (PACKAGE_A, PACKAGE_B, PACKAGE_C_CONFLICT, PACKAGE_D_SKIP):
    service.create_package(mid, ex["package_no"], ex["counts"], ex.get("note"), ex["segments"])
ok("录入:四个示例包落库", True)

# 重复包号拒绝
try:
    service.create_package(mid, PACKAGE_A["package_no"], PACKAGE_A["counts"], None, PACKAGE_A["segments"])
    raise AssertionError("FAILED: 重复包号未拒绝")
except service.ServiceError:
    PASS.append("录入:重复包号拒绝")

# ---------- 服务层：筛查结论 ----------
query = {"qc": MISSING, "mode": 'value:"storm"'}
results, fields = service.screen_model(mid, query)
by_no = {r.package_no: r for r in results}
ok("筛查:返回全部4个包", len(results) == 4)

# PKG-A: v1 no / v2 yes => hit，possible=[v2]
ra = by_no["PKG-A"]
ok("PKG-A:命中候选", ra.verdict == "hit")
ok("PKG-A:仅v2可能命中", ra.possible_segments == ["v2"])
ok("PKG-A:v1不可能v2可能",
   {g.segment: g.verdict for g in ra.segments} == {"v1": SAT_NO, "v2": SAT_YES})

# PKG-B: v2 yes / v3 yes => hit
rb = by_no["PKG-B"]
ok("PKG-B:命中候选且两段都可能", rb.verdict == "hit"
   and set(rb.possible_segments) == {"v2", "v3"})

# PKG-C: 摘要 storm=2 但唯一段不可能产生 storm => review
rc = by_no["PKG-C"]
ok("PKG-C:矛盾标待核", rc.verdict == "review")
ok("PKG-C:矛盾原因入列", any("矛盾" in x or "无法" in x for x in rc.reasons))

# PKG-D: 唯一段不可能 + 缺省计数为0 => skip
rd = by_no["PKG-D"]
ok("PKG-D:全部段不可能才跳过", rd.verdict == "skip"
   and all(g.verdict == SAT_NO for g in rd.segments))

# 整包计数为 0 时即使契约允许也跳过
query2 = {"qc": 'value:"N"', "mode": 'value:"storm"'}
r2 = {r.package_no: r for r in service.screen_model(mid, query2)[0]}
# PKG-B 的 qc=N 计数为 2、storm 为 3，均为正 => 仍按契约判定
ok("PKG-B:qc=N×storm按契约判定", r2["PKG-B"].verdict in ("hit", "review"))
# 构造一个 qc 缺省计数为 0 但契约允许缺省的包 => skip（摘要排除）
zero_pkg = {
    "package_no": "PKG-E",
    "counts": {"qc": {"missing": 0, "null": 1, 'value:"N"': 2, 'value:"Q"': 1},
               "mode": {'value:"routine"': 3, 'value:"storm"': 1}},
    "segments": [{"segment": "v2", "message_count": 4, "schema": SEG_V2}],
}
service.create_package(mid, "PKG-E", zero_pkg["counts"], None, zero_pkg["segments"])
re = {r.package_no: r for r in service.screen_model(mid, query)[0]}["PKG-E"]
ok("PKG-E:摘要计数0导致跳过", re.verdict == "skip"
   and any("计数为 0" in x for x in re.reasons))

# 非法筛查请求
try:
    service.screen_model(mid, {"qc": MISSING})
    raise AssertionError("FAILED: 缺字段筛查未拒绝")
except service.ServiceError:
    PASS.append("筛查:必须两个字段各选一状态")

# 零条数段：契约虽允许缺省，但段内无报文，不得计为可能命中
zero_seg_pkg = {
    "package_no": "PKG-F",
    "counts": {"qc": {"missing": 0, "null": 0, 'value:"N"': 2, 'value:"Q"': 0},
               "mode": {'value:"routine"': 1, 'value:"storm"': 1}},
    "segments": [
        {"segment": "v2-empty", "message_count": 0, "schema": SEG_V2},
        {"segment": "v1", "message_count": 2, "schema": SEG_V1},
    ],
}
service.create_package(mid, "PKG-F", zero_seg_pkg["counts"], None, zero_seg_pkg["segments"])
rf = {r.package_no: r for r in service.screen_model(mid, query)[0]}["PKG-F"]
seg_map = {g.segment: g for g in rf.segments}
ok("PKG-F:零条数段不判命中", seg_map["v2-empty"].verdict == SAT_NO
   and "条数为 0" in seg_map["v2-empty"].reasons[0])
ok("PKG-F:非空段v1不可能=>跳过", rf.verdict == "skip" and rf.possible_segments == [])

# 枚举字段对象检查
with SessionLocal() as s:
    efs = s.execute(select(EnumField).where(EnumField.model_id == mid)
                    .order_by(EnumField.id)).scalars().all()
ok("字段:缺省与null不进枚举清单",
   all(MISSING not in f.values and None not in f.values for f in efs))
with SessionLocal() as s:
    pkgs = s.execute(select(ArchivePackage).where(ArchivePackage.model_id == mid)
                    ).scalars().all()
ok("持久化:共6个包", len(pkgs) == 6)

print(f"全部 {len(PASS)} 项单元自测通过")
