"""用 z3 判定：固件契约是否允许命中指定字段状态的报文。

筛查需要的不是抽样报文，而是对「契约语言集合」的证明：
- UNSAT  => 该固件段不可能产生同时命中两个待查状态的报文，可排除；
- SAT    => 从模型还原一条同时命中的报文，并用独立纯 Python 判定器复核；
- UNKNOWN（超时）=> 无法判定，交由上层标「待核」。

报文编码（沿用契约平台的变量方案）：对每个字段建立 4 个变量 —— 是否出现
present、值类型 kind(0=null, 1=integer, 2=string)、整数值 ival、
字符串值在字段字符串表中的下标 sval。
"""
from __future__ import annotations

from z3 import And, Bool, Implies, Int, Not, Or, Solver, is_true, sat, unknown

from schema_spec import (MISSING, NULL, Schema, message_has_state,
                         message_valid, parse_state)

KIND_NULL, KIND_INT, KIND_STR = 0, 1, 2
SOLVER_TIMEOUT_MS = 15000

# 三值结论
SAT_YES = "yes"        # 存在命中报文
SAT_NO = "no"          # 不存在（UNSAT，已证明不可能）
SAT_UNKNOWN = "unknown"


class _Encoder:
    def __init__(self, fields, str_tables):
        self.fields = fields
        self.str_tables = str_tables  # field -> sorted list[str]
        self.present, self.kind, self.ival, self.sval = {}, {}, {}, {}
        for i, f in enumerate(fields):
            self.present[f] = Bool(f"p{i}")
            self.kind[f] = Int(f"k{i}")
            self.ival[f] = Int(f"v{i}")
            self.sval[f] = Int(f"s{i}")

    def wellformed(self):
        cons = []
        for f in self.fields:
            p, k, v, s = self.present[f], self.kind[f], self.ival[f], self.sval[f]
            tbl = self.str_tables.get(f) or []
            cons.append(Or(k == KIND_NULL, k == KIND_INT, k == KIND_STR))
            # 缺省字段取确定默认值，保证还原报文干净
            cons.append(Implies(Not(p), And(k == KIND_NULL, v == 0, s == 0)))
            cons.append(And(s >= 0, s < len(tbl)) if tbl else (s == 0))
        return cons

    def field_ok(self, f, spec):
        k, v, s = self.kind[f], self.ival[f], self.sval[f]
        opts = []
        if spec.allow_null:
            opts.append(k == KIND_NULL)
        if spec.int_kind == "range":
            c = [k == KIND_INT]
            if spec.int_lo is not None:
                c.append(v >= spec.int_lo)
            if spec.int_hi is not None:
                c.append(v <= spec.int_hi)
            opts.append(And(*c))
        elif spec.int_kind == "set":
            opts.append(And(k == KIND_INT, Or(*[v == x for x in sorted(spec.int_set)])))
        if spec.str_set:
            tbl = self.str_tables[f]
            opts.append(And(k == KIND_STR, Or(*[s == tbl.index(x) for x in sorted(spec.str_set)])))
        return Or(*opts)

    def branch_ok(self, branch):
        cons = []
        props = branch.properties
        for f in self.fields:
            if f in props:
                c = self.field_ok(f, props[f])
                if f in branch.required:
                    cons.append(And(self.present[f], c))
                else:
                    cons.append(Implies(self.present[f], c))
            else:
                # additionalProperties: false —— 未声明字段不得出现
                cons.append(Not(self.present[f]))
        return And(*cons)

    def schema_ok(self, schema: Schema):
        return Or(*[self.branch_ok(b) for b in schema.branches])

    def state_ok(self, field_name: str, kind: str, value):
        """约束某字段呈现指定状态（missing / null / value）。"""
        p, k, v, s = self.present[field_name], self.kind[field_name], \
            self.ival[field_name], self.sval[field_name]
        if kind == MISSING:
            return Not(p)
        if kind == NULL:
            return And(p, k == KIND_NULL)
        # value
        if isinstance(value, bool):
            raise ValueError("枚举值不允许布尔")
        if isinstance(value, int):
            return And(p, k == KIND_INT, v == value)
        if isinstance(value, str):
            tbl = self.str_tables[field_name]
            return And(p, k == KIND_STR, s == tbl.index(value))
        raise ValueError(f"不支持的枚举值 {value!r}")


def _all_field_names(schema: Schema):
    names = set()
    for b in schema.branches:
        names |= set(b.properties)
    return sorted(names)


def _build_str_tables(fields, schema: Schema):
    str_tables = {}
    for f in fields:
        strs = set()
        for b in schema.branches:
            spec = b.properties.get(f)
            if spec and spec.str_set:
                strs |= set(spec.str_set)
        if strs:
            str_tables[f] = sorted(strs)
    return str_tables


def find_witness(schema: Schema, conditions):
    """求一条满足 schema 且在若干字段上同时命中指定状态的报文。

    conditions: [(field_name, state_key), ...]，状态键见 schema_spec
    （missing / null / value:...）。
    返回 (结论, 报文或 None)；结论为 yes/no/unknown。
    """
    parsed = []
    for field_name, state_key in conditions:
        kind, value = parse_state(state_key)
        parsed.append((field_name, kind, value))

    fields = _all_field_names(schema)
    str_tables = _build_str_tables(fields, schema)

    # 字符串枚举值若不在任何分支的字符串表里，必然无解
    for field_name, kind, value in parsed:
        if kind == "value" and isinstance(value, str):
            if value not in str_tables.get(field_name, []):
                return SAT_NO, None
        if field_name not in fields:
            # 契约里根本没有该字段：additionalProperties:false 下不可能出现，
            # 只有 missing 状态可满足
            if kind != MISSING:
                return SAT_NO, None

    enc = _Encoder(fields, str_tables)
    s = Solver()
    s.set("timeout", SOLVER_TIMEOUT_MS)
    s.add(enc.wellformed())
    s.add(enc.schema_ok(schema))
    for field_name, kind, value in parsed:
        if kind == MISSING and field_name not in fields:
            continue  # 未声明字段天然缺省
        s.add(enc.state_ok(field_name, kind, value))

    r = s.check()
    if r == unknown:
        return SAT_UNKNOWN, None
    if r != sat:
        return SAT_NO, None

    m = s.model()
    msg = {}
    for f in fields:
        if is_true(m.eval(enc.present[f], model_completion=True)):
            k = m.eval(enc.kind[f], model_completion=True).as_long()
            if k == KIND_NULL:
                msg[f] = None
            elif k == KIND_INT:
                msg[f] = m.eval(enc.ival[f], model_completion=True).as_long()
            else:
                msg[f] = str_tables[f][m.eval(enc.sval[f], model_completion=True).as_long()]

    # 独立复核：报文必须合法，且每个状态条件确实成立
    if not message_valid(msg, schema):
        raise RuntimeError(f"内部错误：z3 见证报文未通过契约复核 {msg!r}")
    for field_name, kind, value in parsed:
        if not message_has_state(msg, field_name, kind, value):
            raise RuntimeError(
                f"内部错误：z3 见证报文状态复核失败 {msg!r} 字段 {field_name} 状态 {kind}/{value!r}")
    return SAT_YES, msg


def check_segment_states(schema: Schema, field_state_pairs):
    """对一个固件段批量判定多组「双字段同时命中」条件。

    field_state_pairs: [((field_a, state_a), (field_b, state_b)), ...]
    返回 {条件索引: (结论, 见证报文或 None)}。
    """
    out = {}
    for i, pair in enumerate(field_state_pairs):
        out[i] = find_witness(schema, list(pair))
    return out
