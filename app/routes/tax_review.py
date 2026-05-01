"""Tax review SSE streaming endpoints."""
import json
import logging

from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask_login import login_required

from app import db
from app.config import URL_PREFIX
from app.routes.helpers import _row_list

logger = logging.getLogger(__name__)

bp = Blueprint("tax_review", __name__)


@bp.route(URL_PREFIX + "/api/tax-review")
@login_required
def api_tax_review():
    year = request.args.get("year")
    entity_id = request.args.get("entity_id", type=int)

    docs = _row_list(db.get_analyzed_documents(entity_id=entity_id, tax_year=year, limit=500))
    filed = db.list_filed_returns(entity_id=entity_id)
    filed_this_year = next((r for r in filed if r["tax_year"] == year), None)

    summary_lines = [f"Tax Year: {year}"]
    if entity_id:
        conn = db.get_connection()
        try:
            ent = conn.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
            if ent:
                summary_lines.append(f"Entity: {ent['name']}")
        finally:
            conn.close()

    summary_lines.append(f"Total documents analyzed: {len(docs)}")

    income_docs = [d for d in docs if d.get("category") == "income"]
    expense_docs = [d for d in docs if d.get("category") == "expense"]
    deduction_docs = [d for d in docs if d.get("category") == "deduction"]
    capital_docs = [d for d in docs if d.get("doc_type") == "capital_improvement"]
    low_conf_docs = [d for d in docs if (d.get("confidence") or 1.0) < 0.6]

    total_income = sum(d.get("amount") or 0 for d in income_docs)
    total_expense = sum(d.get("amount") or 0 for d in expense_docs)
    total_deductions = sum(d.get("amount") or 0 for d in deduction_docs)

    summary_lines += [f"\nINCOME ({len(income_docs)} docs, ${total_income:,.2f} total):"]
    for d in sorted(income_docs, key=lambda x: -(x.get("amount") or 0))[:15]:
        summary_lines.append(f"  - [{d.get('doc_type')}] {d.get('vendor','')} "
                             f"${d.get('amount') or 0:,.2f} ({d.get('date','?')})")

    summary_lines += [f"\nEXPENSES ({len(expense_docs)} docs, ${total_expense:,.2f} total):"]
    for d in sorted(expense_docs, key=lambda x: -(x.get("amount") or 0))[:20]:
        summary_lines.append(f"  - [{d.get('doc_type')}] {d.get('vendor','')} "
                             f"${d.get('amount') or 0:,.2f} ({d.get('date','?')}) "
                             f"doc#{d.get('paperless_doc_id')}")

    if deduction_docs:
        summary_lines.append(f"\nDEDUCTIONS ({len(deduction_docs)} docs, ${total_deductions:,.2f}):")
        for d in deduction_docs[:10]:
            summary_lines.append(f"  - [{d.get('doc_type')}] {d.get('vendor','')} "
                                 f"${d.get('amount') or 0:,.2f}")

    if capital_docs:
        summary_lines.append(f"\nCAPITAL IMPROVEMENTS ({len(capital_docs)} items):")
        for d in capital_docs:
            summary_lines.append(f"  - {d.get('vendor','')} ${d.get('amount') or 0:,.2f} "
                                 f"({d.get('date','?')}) — needs depreciation schedule")

    if low_conf_docs:
        summary_lines.append(f"\nLOW CONFIDENCE ITEMS ({len(low_conf_docs)} docs <60%):")
        for d in low_conf_docs[:10]:
            summary_lines.append(
                f"  - doc#{d.get('paperless_doc_id')} [{d.get('doc_type')}] "
                f"{d.get('vendor','')} ${d.get('amount') or 0:,.2f} "
                f"(conf:{int((d.get('confidence') or 0)*100)}%)"
            )

    if filed_this_year:
        summary_lines.append(f"\nFILED RETURN DATA for {year}:")
        summary_lines.append(f"  AGI: ${filed_this_year.get('agi') or '?':,}")
        summary_lines.append(f"  Total income: ${filed_this_year.get('total_income') or '?':,}")
        summary_lines.append(f"  Total deductions: ${filed_this_year.get('total_deductions') or '?':,}")
        summary_lines.append(f"  Tax owed: ${filed_this_year.get('total_tax') or '?':,}")
        refund = filed_this_year.get("refund_amount")
        owed = filed_this_year.get("amount_owed")
        if refund:
            summary_lines.append(f"  Refund: ${refund:,}")
        if owed:
            summary_lines.append(f"  Amount owed: ${owed:,}")
    else:
        summary_lines.append(f"\nNO FILED RETURN DATA for {year}.")

    doc_summary = "\n".join(summary_lines)

    prompt = f"""You are an expert US tax accountant reviewing financial documents for a client's tax year {year}.

Here is a summary of all documents analyzed for this year:

{doc_summary}

Please act as the client's tax accountant and:
1. Identify items that need clarification or additional documentation
2. Flag potential issues, missing documents, or inconsistencies
3. Note any capital improvements that need depreciation schedules
4. Ask specific questions about unclear items
5. Compare analyzed amounts to filed return amounts if available and flag discrepancies
6. Note any income sources that might be missing
7. Flag any deductions that seem high or unusual
8. Note which expenses may be deductible vs. non-deductible

Format your response as a structured report with numbered questions and flagged items.
Use markdown formatting. Be specific — reference vendor names, amounts, and document IDs where relevant."""

    from app.llm_client import LLMClient
    from app import config as _cfg

    llm_provider = db.get_setting("llm_provider") or _cfg.LLM_PROVIDER
    llm_api_key = db.get_setting("llm_api_key") or _cfg.LLM_API_KEY
    llm_model = db.get_setting("llm_model") or _cfg.LLM_MODEL

    def generate():
        try:
            client = LLMClient(provider=llm_provider, api_key=llm_api_key, model=llm_model)
            for chunk in client.stream_text(prompt):
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        except AttributeError:
            try:
                result = client.chat([{"role": "user", "content": prompt}])
                text = result if isinstance(result, str) else str(result)
                yield f"data: {json.dumps({'chunk': text})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route(URL_PREFIX + "/api/tax-review/followup", methods=["POST"])
@login_required
def api_tax_review_followup():
    data = request.get_json(silent=True) or {}
    year = data.get("year", "")
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "messages required"}), 400

    from app.llm_client import LLMClient
    from app import config as _cfg

    llm_provider = db.get_setting("llm_provider") or _cfg.LLM_PROVIDER
    llm_api_key = db.get_setting("llm_api_key") or _cfg.LLM_API_KEY
    llm_model = db.get_setting("llm_model") or _cfg.LLM_MODEL

    system = (
        f"You are an expert US tax accountant helping a client with their {year} tax return. "
        "You have already reviewed all their financial documents and generated an initial review. "
        "Continue the conversation, answering their questions and clarifying items from your review. "
        "Be specific: reference amounts, document IDs, vendor names, and tax rules where relevant. "
        "Use markdown formatting."
    )

    def generate():
        try:
            if llm_provider == "anthropic":
                # Phase 12: route streaming through the proxy chain (LMRH-aware)
                # before falling back to direct Anthropic SDK. Tax-review is a
                # reasoning-heavy task — cascade=auto opt-in lives in the hint.
                from app.llm_client import proxy_call
                ac = None
                endpoint_id = None
                try:
                    ac, endpoint_id = proxy_call.get_streaming_anthropic_client("tax-review")
                except proxy_call.NoProxyAvailable:
                    import anthropic
                    ac = anthropic.Anthropic(api_key=llm_api_key)

                try:
                    with ac.messages.stream(
                        model=llm_model,
                        max_tokens=4096,
                        system=system,
                        messages=messages,
                    ) as stream:
                        for chunk in stream.text_stream:
                            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
                    if endpoint_id:
                        proxy_call.mark_endpoint_success(endpoint_id)
                except Exception:
                    if endpoint_id:
                        proxy_call.mark_endpoint_failure(endpoint_id)
                    raise
            else:
                import openai as _oai
                oai = _oai.OpenAI(api_key=llm_api_key)
                stream = oai.chat.completions.create(
                    model=llm_model,
                    messages=[{"role": "system", "content": system}] + messages,
                    stream=True,
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield f"data: {json.dumps({'chunk': delta})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
