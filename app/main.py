"""离线归档筛查网页：FastAPI 入口（HTML 页面 + JSON API）。"""
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

import service
from db import (ArchivePackage, Base, DeviceModel, EnumField, FirmwareSegment,
                SessionLocal, engine)
from examples import EXAMPLES, FIELDS_QC_MODE

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["tojson"] = lambda v, indent=2: json.dumps(
    v, ensure_ascii=False, indent=indent)
templates.env.filters["tojsonc"] = lambda v: json.dumps(
    v, ensure_ascii=False, separators=(",", ":"))


def _wait_for_db(retries=40, delay=1.0):
    for i in range(retries):
        try:
            with engine.connect():
                return
        except OperationalError:
            if i == retries - 1:
                raise
            time.sleep(delay)


@asynccontextmanager
async def lifespan(app):
    _wait_for_db()
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="水质平台 · 离线归档筛查", lifespan=lifespan)


# ---------------- 公共数据 ----------------

def _get_model_or_404(name: str) -> DeviceModel:
    with SessionLocal() as s:
        m = s.execute(select(DeviceModel).where(DeviceModel.name == name)).scalar_one_or_none()
        if m is None:
            raise HTTPException(404, f"型号 {name!r} 不存在")
        return DeviceModel(id=m.id, name=m.name)


def _model_context(name: str, error=None, notice=None, results=None, form=None):
    with SessionLocal() as s:
        model = s.execute(select(DeviceModel).where(DeviceModel.name == name)).scalar_one_or_none()
        if model is None:
            raise HTTPException(404, f"型号 {name!r} 不存在")
        fields = s.execute(select(EnumField).where(EnumField.model_id == model.id)
                           .order_by(EnumField.id)).scalars().all()
        pkgs = s.execute(select(ArchivePackage).where(ArchivePackage.model_id == model.id)
                         .order_by(ArchivePackage.id)).scalars().all()
        pkg_rows = []
        for p in pkgs:
            segs = s.execute(select(FirmwareSegment).where(
                FirmwareSegment.package_id == p.id).order_by(FirmwareSegment.id)
            ).scalars().all()
            pkg_rows.append({"pkg": p, "segments": segs})
        s.expunge_all()
        return {
            "model": model, "fields": fields, "pkg_rows": pkg_rows,
            "examples": EXAMPLES, "error": error, "notice": notice,
            "results": results, "form": form or {},
            "max_segments": service.MAX_SEGMENTS,
            "max_packages": service.MAX_PACKAGES,
        }


# ---------------- HTML 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as s:
        models = s.execute(select(DeviceModel).order_by(DeviceModel.name)).scalars().all()
        rows = []
        for m in models:
            nf = s.execute(select(EnumField).where(
                EnumField.model_id == m.id)).scalars().all()
            np = s.execute(select(ArchivePackage).where(
                ArchivePackage.model_id == m.id)).scalars().all()
            rows.append({"name": m.name, "field_count": len(nf),
                         "pkg_count": len(np)})
    return templates.TemplateResponse(request, "index.html", {"rows": rows})


@app.post("/models")
def create_model(request: Request, name: str = Form(...)):
    name = (name or "").strip()
    if not service.valid_name(name):
        with SessionLocal() as s:
            models = s.execute(select(DeviceModel).order_by(DeviceModel.name)).scalars().all()
            rows = [{"name": m.name, "field_count": 0, "pkg_count": 0} for m in models]
        return templates.TemplateResponse(
            request, "index.html", {"rows": rows,
                                    "error": "型号名只能由字母数字及 _ . - 组成，长度 1~64"},
            status_code=400)
    with SessionLocal() as s:
        if s.execute(select(DeviceModel).where(DeviceModel.name == name)).scalar_one_or_none():
            return RedirectResponse(f"/models/{name}", status_code=303)
        s.add(DeviceModel(name=name))
        s.commit()
    return RedirectResponse(f"/models/{name}", status_code=303)


@app.get("/models/{name}", response_class=HTMLResponse)
def model_page(request: Request, name: str):
    return templates.TemplateResponse(request, "model.html", _model_context(name))


@app.post("/models/{name}/fields", response_class=HTMLResponse)
def fields_html(request: Request, name: str,
                field1_key: str = Form(...), field1_label: str = Form(""),
                field1_values: str = Form(...),
                field2_key: str = Form(...), field2_label: str = Form(""),
                field2_values: str = Form(...)):
    model = _get_model_or_404(name)

    def _parse_values(text: str, key: str):
        vals = []
        for tok in (text or "").split(","):
            tok = tok.strip()
            if tok == "":
                continue
            try:
                vals.append(json.loads(tok))
            except json.JSONDecodeError:
                vals.append(tok)  # 未加引号的字符串枚举值
        if not vals:
            raise service.ServiceError(f"字段 {key} 至少需要一个枚举值")
        return vals

    try:
        fields_in = [
            {"field_key": field1_key.strip(), "label": field1_label.strip(),
             "values": _parse_values(field1_values, field1_key)},
            {"field_key": field2_key.strip(), "label": field2_label.strip(),
             "values": _parse_values(field2_values, field2_key)},
        ]
        service.setup_model_fields(model.id, fields_in)
    except service.ServiceError as e:
        return templates.TemplateResponse(
            request, "model.html", _model_context(name, error=f"枚举字段登记失败：{e}"),
            status_code=400)
    return RedirectResponse(f"/models/{name}", status_code=303)


@app.post("/models/{name}/packages", response_class=HTMLResponse)
def package_html(request: Request, name: str, package_text: str = Form(...)):
    model = _get_model_or_404(name)
    try:
        payload = json.loads(package_text)
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            request, "model.html",
            _model_context(name, error=f"包 JSON 解析失败：{e}", form={"package_text": package_text}),
            status_code=400)
    try:
        pkg = service.create_package(
            model.id, str(payload.get("package_no", "")).strip(),
            payload.get("counts"), payload.get("note"),
            payload.get("segments"))
    except service.ServiceError as e:
        return templates.TemplateResponse(
            request, "model.html",
            _model_context(name, error=f"归档包录入被拒：{e}", form={"package_text": package_text}),
            status_code=400)
    return RedirectResponse(f"/models/{name}?added={pkg.package_no}", status_code=303)


@app.post("/models/{name}/screen", response_class=HTMLResponse)
async def screen_html(request: Request, name: str):
    model = _get_model_or_404(name)
    form_data = await request.form()
    form = {k: str(v) for k, v in form_data.items()}
    try:
        results, _fields = service.screen_model(model.id, form)
    except service.ServiceError as e:
        return templates.TemplateResponse(
            request, "model.html", _model_context(name, error=f"筛查失败：{e}"),
            status_code=400)
    return templates.TemplateResponse(
        request, "model.html", _model_context(name, results=results, form=form))


# ---------------- JSON API ----------------

class ModelIn(BaseModel):
    name: str


class FieldIn(BaseModel):
    field_key: str
    label: str = ""
    values: list


class FieldsIn(BaseModel):
    fields: list[FieldIn]


class SegmentIn(BaseModel):
    segment: str
    message_count: int
    schema_: dict = Field(alias="schema")

    model_config = ConfigDict(populate_by_name=True)


class PackageIn(BaseModel):
    package_no: str
    counts: dict
    note: str | None = None
    segments: list[SegmentIn]


class ScreenIn(BaseModel):
    query: dict


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/models", status_code=201)
def api_create_model(body: ModelIn):
    name = body.name.strip()
    if not service.valid_name(name):
        raise HTTPException(422, "型号名只能由字母数字及 _ . - 组成，长度 1~64")
    with SessionLocal() as s:
        if s.execute(select(DeviceModel).where(DeviceModel.name == name)).scalar_one_or_none():
            raise HTTPException(409, f"型号 {name!r} 已存在")
        s.add(DeviceModel(name=name))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise HTTPException(409, f"型号 {name!r} 已存在")
    return {"ok": True, "name": name}


@app.get("/api/models/{name}")
def api_model_state(name: str):
    with SessionLocal() as s:
        model = s.execute(select(DeviceModel).where(DeviceModel.name == name)).scalar_one_or_none()
        if model is None:
            raise HTTPException(404, f"型号 {name!r} 不存在")
        fields = s.execute(select(EnumField).where(EnumField.model_id == model.id)
                           .order_by(EnumField.id)).scalars().all()
        pkgs = s.execute(select(ArchivePackage).where(ArchivePackage.model_id == model.id)
                         .order_by(ArchivePackage.id)).scalars().all()
        out_pkgs = []
        for p in pkgs:
            segs = s.execute(select(FirmwareSegment).where(
                FirmwareSegment.package_id == p.id).order_by(FirmwareSegment.id)
            ).scalars().all()
            out_pkgs.append({
                "package_no": p.package_no, "note": p.note, "counts": p.counts,
                "segments": [{"segment": g.segment, "message_count": g.message_count,
                              "schema": g.doc} for g in segs],
            })
        return {
            "name": model.name,
            "fields": [{"field_key": f.field_key, "label": f.label,
                        "values": f.values} for f in fields],
            "packages": out_pkgs,
        }


@app.post("/api/models/{name}/fields", status_code=201)
def api_setup_fields(name: str, body: FieldsIn):
    model = _get_model_or_404(name)
    try:
        service.setup_model_fields(model.id, [f.model_dump() for f in body.fields])
    except service.ServiceError as e:
        raise HTTPException(422, str(e))
    return {"ok": True}


@app.post("/api/models/{name}/packages")
def api_create_package(name: str, body: PackageIn):
    model = _get_model_or_404(name)
    segments = [{"segment": g.segment, "message_count": g.message_count,
                 "schema": g.schema_} for g in body.segments]
    try:
        pkg = service.create_package(model.id, body.package_no.strip(),
                                     body.counts, body.note, segments)
    except service.ServiceError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "package_no": pkg.package_no}


@app.post("/api/models/{name}/screen")
def api_screen(name: str, body: ScreenIn):
    model = _get_model_or_404(name)
    try:
        results, fields = service.screen_model(model.id, body.query)
    except service.ServiceError as e:
        raise HTTPException(422, str(e))
    return {
        "ok": True,
        "fields": [{"field_key": f.field_key, "label": f.label} for f in fields],
        "results": [
            {
                "package_no": r.package_no, "verdict": r.verdict,
                "possible_segments": r.possible_segments,
                "summary": r.summary, "reasons": r.reasons,
                "segments": [{"segment": g.segment, "verdict": g.verdict,
                              "witness": g.witness, "reasons": g.reasons}
                             for g in r.segments],
            } for r in results
        ],
    }


@app.get("/api/examples")
def api_examples():
    return {"fields_example": FIELDS_QC_MODE, "examples": EXAMPLES}
