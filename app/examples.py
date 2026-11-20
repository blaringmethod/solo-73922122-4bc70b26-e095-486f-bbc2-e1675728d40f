"""内置示例：离线归档包录入与双字段联合筛查。供网页一键填入。

场景字段：
- qc   质控码，枚举 ["N", "Q"]（另有隐式状态 missing 缺省 / null 显式空值）
- mode 工况，枚举 ["routine", "storm"]（同上）
"""

# ---------------- 枚举字段定义 ----------------

FIELDS_QC_MODE = [
    {"field_key": "qc", "label": "质控码", "values": ["N", "Q"]},
    {"field_key": "mode", "label": "工况", "values": ["routine", "storm"]},
]

# ---------------- 固件契约 ----------------

def _flat(props, required):
    return {"type": "object", "properties": props,
            "required": required, "additionalProperties": False}


# v1：qc 必填且只认 N/Q；mode 必填，只认 routine/storm
SEG_V1 = _flat(
    {"qc": {"enum": ["N", "Q"]},
     "mode": {"enum": ["routine", "storm"]},
     "temp": {"type": "integer", "minimum": -40, "maximum": 100}},
    ["qc", "mode", "temp"])

# v2：qc 可缺省、可取 null；mode 仍必填
SEG_V2 = _flat(
    {"qc": {"enum": ["N", "Q", None]},
     "mode": {"enum": ["routine", "storm"]},
     "temp": {"type": "integer", "minimum": -40, "maximum": 100}},
    ["mode", "temp"])

# v3：顶层 anyOf —— 常规报文（qc+mode）或风暴报文（mode=storm，无 qc）
SEG_V3_ANYOF = {"anyOf": [
    _flat({"qc": {"enum": ["N", "Q", None]},
           "mode": {"enum": ["routine", "storm"]},
           "temp": {"type": "integer", "minimum": -40, "maximum": 100}},
          ["mode", "temp"]),
    _flat({"mode": {"enum": ["storm"]},
           "level": {"type": "integer", "enum": [0, 1, 2, 3]}},
          ["mode", "level"]),
]}

# 旧固件：mode 只认 routine —— 不可能产生 storm 报文
SEG_OLD = _flat(
    {"qc": {"enum": ["N", "Q"]},
     "mode": {"enum": ["routine"]},
     "temp": {"type": "integer", "minimum": -10, "maximum": 50}},
    ["qc", "mode", "temp"])


def _counts(qc_part, mode_part):
    return {"qc": qc_part, "mode": mode_part}


# ---------------- 示例包 ----------------

# 包 A：两段（v1+v2），共 6 条；查 qc 缺省 × storm 时仅 v2 可能命中
PACKAGE_A = {
    "package_no": "PKG-A",
    "note": "新旧固件混装：v1 的 qc 必填，v2 允许缺省/null",
    "counts": _counts(
        {"missing": 2, "null": 1, 'value:"N"': 2, 'value:"Q"': 1},
        {'value:"routine"': 4, 'value:"storm"': 2}),
    "segments": [
        {"segment": "v1", "message_count": 3, "schema": SEG_V1},
        {"segment": "v2", "message_count": 3, "schema": SEG_V2},
    ],
}

# 包 B：含 anyOf 段，共 5 条；v3 的 storm 分支可无 qc
PACKAGE_B = {
    "package_no": "PKG-B",
    "note": "v3 顶层 anyOf：风暴报文分支允许 qc 缺省",
    "counts": _counts(
        {"missing": 2, "null": 0, 'value:"N"': 2, 'value:"Q"': 1},
        {'value:"routine"': 2, 'value:"storm"': 3}),
    "segments": [
        {"segment": "v2", "message_count": 2, "schema": SEG_V2},
        {"segment": "v3", "message_count": 3, "schema": SEG_V3_ANYOF},
    ],
}

# 包 C：旧固件只认 routine，摘要却登记 2 条 storm —— 摘要与契约矛盾，应待核
PACKAGE_C_CONFLICT = {
    "package_no": "PKG-C",
    "note": "摘要登记 storm=2，但唯一段契约的 mode 枚举只有 routine",
    "counts": _counts(
        {"missing": 0, "null": 0, 'value:"N"': 3, 'value:"Q"': 1},
        {'value:"routine"': 2, 'value:"storm"': 2}),
    "segments": [
        {"segment": "v0", "message_count": 4, "schema": SEG_OLD},
    ],
}

# 包 D：单段 v1，qc/mode 均必填 —— 查 qc 缺省 × storm 时所有段不可能命中，可跳过
PACKAGE_D_SKIP = {
    "package_no": "PKG-D",
    "note": "单段 v1：qc、mode 均必填且无 null，缺省报文不存在",
    "counts": _counts(
        {"missing": 0, "null": 0, 'value:"N"': 3, 'value:"Q"': 1},
        {'value:"routine"': 3, 'value:"storm"': 1}),
    "segments": [
        {"segment": "v1", "message_count": 4, "schema": SEG_V1},
    ],
}

EXAMPLES = [
    {
        "title": "示例一：新旧固件混装（PKG-A）",
        "story": ("包内 v1 固件 qc 必填、v2 固件允许 qc 缺省或显式 null。"
                  "选择「qc=缺省 × mode=storm」筛查时，v1 被证明不可能命中，"
                  "v2 可能命中，整包须保留并定位到 v2 段。"),
        "doc": PACKAGE_A,
    },
    {
        "title": "示例二：顶层 anyOf 固件（PKG-B）",
        "story": ("v3 固件用顶层 anyOf 同时接受常规报文与风暴报文，风暴分支没有 qc 字段。"
                  "筛查「qc=缺省 × mode=storm」时，求解器对 anyOf 各分支分别证明，"
                  "v2 与 v3 两段均可能命中。"),
        "doc": PACKAGE_B,
    },
    {
        "title": "示例三：摘要与契约矛盾（PKG-C，待核）",
        "story": ("整包摘要登记 2 条 storm 工况报文，但唯一段 v0 的契约里 mode 枚举只有 routine。"
                  "没有任何固件段能产生 storm 报文，摘要与契约矛盾，整包标为待核。"),
        "doc": PACKAGE_C_CONFLICT,
    },
    {
        "title": "示例四：全部段不可能命中（PKG-D，可跳过）",
        "story": ("单段 v1 的 qc、mode 均为必填且不接受 null。查「qc=缺省 × mode=storm」时，"
                  "唯一固件段被 UNSAT 证明不可能命中，且整包缺省计数为 0，方可跳过该包。"),
        "doc": PACKAGE_D_SKIP,
    },
]
