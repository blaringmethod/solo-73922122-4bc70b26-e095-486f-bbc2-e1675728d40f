"""集成自测：对运行中的服务执行完整筛查流程（默认 http://127.0.0.1:8000）。
运行：BASE_URL=http://127.0.0.1:8000 python3 tests/selftest.py
覆盖：型号/字段登记、四个示例包录入与校验拒绝、双字段联合筛查结论、
待核（摘要矛盾）、跳过（全段不可能/计数为零）、HTML 页面冒烟。
"""
import os
import uuid

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ✔ {name}")


def main():
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.get("/api/health")
    ok("健康检查", r.status_code == 200 and r.json()["status"] == "ok")

    model = f"m{uuid.uuid4().hex[:10]}"
    r = c.post("/api/models", json={"name": model})
    ok("创建型号", r.status_code == 201, r.text)

    examples = c.get("/api/examples").json()
    field_def = examples["fields_example"]
    pkgs = examples["examples"]

    # ---- 枚举字段登记 ----
    print("枚举字段登记")
    r = c.post(f"/api/models/{model}/fields", json={"fields": field_def})
    ok("登记两个枚举字段", r.status_code == 201 and r.json()["ok"], r.text)
    r = c.post(f"/api/models/{model}/fields", json={"fields": field_def})
    ok("重复登记被拒(422)", r.status_code == 422, r.text)
    st = c.get(f"/api/models/{model}").json()
    ok("字段含qc与mode", {f["field_key"] for f in st["fields"]} == {"qc", "mode"})
    ok("缺省/null不进枚举清单",
       all(v not in ("missing", None) for f in st["fields"] for v in f["values"]))

    def post_pkg(doc):
        return c.post(f"/api/models/{model}/packages", json=doc)

    # ---- 四个示例包 ----
    print("示例包录入")
    for ex in pkgs:
        r = post_pkg(ex["doc"])
        ok(f"录入 {ex['doc']['package_no']}", r.status_code == 200 and r.json()["ok"], r.text)
    st = c.get(f"/api/models/{model}").json()
    ok("持久化4个包", len(st["packages"]) == 4)
    seg_counts = {p["package_no"]: len(p["segments"]) for p in st["packages"]}
    ok("包段数正确", seg_counts == {"PKG-A": 2, "PKG-B": 2, "PKG-C": 1, "PKG-D": 1})

    # ---- 录入校验 ----
    print("录入校验")
    r = post_pkg({"package_no": "PKG-A", "counts": {}, "segments": []})
    ok("重复包号/空段被拒", r.status_code == 422, r.text)
    bad_sum = {
        "package_no": "BAD-SUM",
        "counts": {"qc": {"missing": 0, "null": 0, 'value:"N"': 5, 'value:"Q"': 0},
                   "mode": {'value:"routine"': 5, 'value:"storm"': 0}},
        "segments": [{"segment": "v1", "message_count": 2,
                      "schema": st["packages"][3]["segments"][0]["schema"]}],
    }
    r = post_pkg(bad_sum)
    ok("状态和≠段条数和被拒", r.status_code == 422 and "矛盾" in r.text, r.text)
    bad_state = {
        "package_no": "BAD-STATE",
        "counts": {"qc": {"missing": 1, "null": 0, 'value:"N"': 0, 'value:"Q"': 0},
                   "mode": {'value:"routine"': 0, 'value:"storm"': 1}},
        "segments": [{"segment": "v2", "message_count": 1,
                      "schema": st["packages"][0]["segments"][1]["schema"]}],
    }
    bad_state["counts"]["qc"] = {"value:42": 1}
    r = post_pkg(bad_state)
    ok("枚举外状态键被拒", r.status_code == 422, r.text)
    bad_schema = {
        "package_no": "BAD-SCHEMA",
        "counts": {"qc": {"missing": 1, "null": 0, 'value:"N"': 0, 'value:"Q"': 0},
                   "mode": {'value:"routine"': 0, 'value:"storm"': 1}},
        "segments": [{"segment": "vx", "message_count": 1,
                      "schema": {"type": "object",
                                 "properties": {"a": {"type": "array"}},
                                 "additionalProperties": False}}],
    }
    r = post_pkg(bad_schema)
    ok("非法契约语法被拒", r.status_code == 422, r.text)
    st = c.get(f"/api/models/{model}").json()
    ok("拒绝后仍是4个包", len(st["packages"]) == 4)

    # ---- 筛查一：qc 缺省 × mode storm ----
    print("筛查：qc=missing × mode=storm")
    q = {"query": {"qc": "missing", "mode": 'value:"storm"'}}
    r = c.post(f"/api/models/{model}/screen", json=q)
    ok("筛查200", r.status_code == 200, r.text)
    body = r.json()
    by_no = {x["package_no"]: x for x in body["results"]}
    ok("返回4个包结论", len(body["results"]) == 4)

    a = by_no["PKG-A"]
    ok("PKG-A命中候选", a["verdict"] == "hit")
    ok("PKG-A仅v2可能命中", a["possible_segments"] == ["v2"])
    segv = {g["segment"]: g["verdict"] for g in a["segments"]}
    ok("PKG-A段判定v1=no/v2=yes", segv == {"v1": "no", "v2": "yes"})
    wit = next(g["witness"] for g in a["segments"] if g["segment"] == "v2")
    ok("PKG-A见证报文qc缺省且storm", "qc" not in wit and wit["mode"] == "storm", str(wit))

    b = by_no["PKG-B"]
    ok("PKG-B命中且v2/v3都可能", b["verdict"] == "hit"
       and set(b["possible_segments"]) == {"v2", "v3"})
    wit3 = next(g["witness"] for g in b["segments"] if g["segment"] == "v3")
    ok("PKG-B anyOf见证无qc", "qc" not in wit3 and wit3["mode"] == "storm", str(wit3))

    cc = by_no["PKG-C"]
    ok("PKG-C摘要矛盾待核", cc["verdict"] == "review"
       and any("矛盾" in x or "无法" in x for x in cc["reasons"]))

    d = by_no["PKG-D"]
    ok("PKG-D全段不可能跳过", d["verdict"] == "skip"
       and all(g["verdict"] == "no" for g in d["segments"]))

    # ---- 筛查二：qc 显式 null × mode routine ----
    print("筛查：qc=null × mode=routine")
    r = c.post(f"/api/models/{model}/screen",
               json={"query": {"qc": "null", "mode": 'value:"routine"'}})
    by_no2 = {x["package_no"]: x for x in r.json()["results"]}
    ok("PKG-A: v1不支持null/v2支持",
       {g["segment"]: g["verdict"] for g in by_no2["PKG-A"]["segments"]}
       == {"v1": "no", "v2": "yes"})

    # ---- 筛查三：整包计数为 0 导致跳过 ----
    zero_pkg = {
        "package_no": "PKG-E",
        "counts": {"qc": {"missing": 0, "null": 1, 'value:"N"': 2, 'value:"Q"': 1},
                   "mode": {'value:"routine"': 3, 'value:"storm"': 1}},
        "segments": [{"segment": "v2", "message_count": 4,
                      "schema": st["packages"][0]["segments"][1]["schema"]}],
    }
    r = post_pkg(zero_pkg)
    ok("录入PKG-E", r.status_code == 200, r.text)
    r = c.post(f"/api/models/{model}/screen",
               json={"query": {"qc": "missing", "mode": 'value:"storm"'}})
    e = {x["package_no"]: x for x in r.json()["results"]}["PKG-E"]
    ok("PKG-E摘要零计数跳过", e["verdict"] == "skip"
       and any("计数为 0" in x for x in e["reasons"]))

    # ---- 非法筛查请求 ----
    r = c.post(f"/api/models/{model}/screen", json={"query": {"qc": "missing"}})
    ok("缺字段筛查422", r.status_code == 422, r.text)
    r = c.post(f"/api/models/{model}/screen",
               json={"query": {"qc": "bogus", "mode": 'value:"storm"'}})
    ok("非法状态筛查422", r.status_code == 422, r.text)

    # ---- HTML 页面冒烟 ----
    print("HTML 页面")
    r = c.get("/")
    ok("首页可打开", r.status_code == 200 and "离线归档" in r.text)
    r = c.get(f"/models/{model}")
    ok("型号页含字段/包/筛查区", r.status_code == 200
       and "枚举字段" in r.text and "双字段联合筛查" in r.text
       and "PKG-A" in r.text and "missing" in r.text)
    ok("页面含anyOf示例", "anyOf" in r.text and "PKG-B" in r.text)
    # 网页表单执行筛查
    r = c.post(f"/models/{model}/screen",
               data={"qc": "missing", "mode": 'value:"storm"'})
    ok("网页筛查展示结论", r.status_code == 200 and "命中候选" in r.text
       and "可跳过" in r.text and "待核" in r.text, r.text)
    # 网页表单录包失败回显
    r = c.post(f"/models/{model}/packages", data={"package_text": "{not json"})
    ok("网页录包JSON错误回显", r.status_code == 400 and "JSON 解析失败" in r.text)

    print(f"\n全部 {len(PASS)} 项集成自测通过")
    return model


if __name__ == "__main__":
    used = main()
    print(f"MODEL={used}")
