"""«Алина-режим»: правила свободным текстом, подмешиваются в системный промт классификатора."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

router = APIRouter()


async def _all_rules(conn):
    cur = await conn.execute(
        "SELECT id, rule_text, active, created_at, updated_at FROM user_rules ORDER BY active DESC, id DESC"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    return rows


@router.get("/rules")
async def rules_page(request: Request):
    conn = request.app.state.db
    rows = await _all_rules(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "rules.html",
        {"rules": rows, "active": "rules"},
    )


@router.post("/rules/add")
async def add_rule(request: Request, rule_text: str = Form(...)):
    text = rule_text.strip()
    if not text:
        raise HTTPException(400, "Пустое правило")
    conn = request.app.state.db
    await conn.execute(
        "INSERT INTO user_rules (rule_text, active) VALUES (?, 1)",
        (text,),
    )
    await conn.commit()
    rows = await _all_rules(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/rules_list.html",
        {"rules": rows},
    )


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: int):
    conn = request.app.state.db
    await conn.execute(
        "UPDATE user_rules SET active = 1 - active, updated_at = datetime('now') WHERE id=?",
        (rule_id,),
    )
    await conn.commit()
    rows = await _all_rules(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/rules_list.html",
        {"rules": rows},
    )


@router.post("/rules/{rule_id}/edit")
async def edit_rule(request: Request, rule_id: int, rule_text: str = Form(...)):
    text = rule_text.strip()
    if not text:
        raise HTTPException(400, "Пустое правило")
    conn = request.app.state.db
    await conn.execute(
        "UPDATE user_rules SET rule_text=?, updated_at=datetime('now') WHERE id=?",
        (text, rule_id),
    )
    await conn.commit()
    rows = await _all_rules(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/rules_list.html",
        {"rules": rows},
    )


@router.post("/rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int):
    conn = request.app.state.db
    await conn.execute("DELETE FROM user_rules WHERE id=?", (rule_id,))
    await conn.commit()
    rows = await _all_rules(conn)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "partials/rules_list.html",
        {"rules": rows},
    )
