"""Mitigation + observability layer for the opaque Observathon agent.

Only stdlib and the bundled telemetry package are used. The wrapper is defensive:
it sanitizes untrusted notes, caches repeat requests, retries transient failures,
redacts PII, enforces refusal guardrails, recomputes totals when trace data is
available, and writes JSONL telemetry without raw customer text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

try:
    from telemetry.redact import redact as _telemetry_redact
except Exception:  # pragma: no cover - telemetry is bundled, but never crash.
    _telemetry_redact = None


_PROMPT_CACHE: str | None = None
_PROMPT_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_LOG_PATH = os.path.join(os.path.dirname(__file__), "telemetry_events.jsonl")

_TOTAL_RE = re.compile(r"(?im)^\s*Tong cong:\s*([0-9][0-9., ]*)\s*VND\s*$")
_ANY_TOTAL_RE = re.compile(r"(?i)\bTong cong:\s*[0-9][0-9., ]*\s*VND\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_VN_PHONE_RE = re.compile(r"(?<!\d)(?:\+84|0)(?:[\s.\-]?\d){9,10}(?!\d)")
_PHONE_LIKE_RE = re.compile(r"(?<!\d)(?:\d[\s.\-]?){9,12}(?!\d)")

_BAD_STATUS = {"loop", "max_steps", "no_action", "wrapper_error"}
_TOOL_ERROR_WORDS = ("tool_error", "exception", "traceback", "failed", "timeout")
_UNAVAILABLE_WORDS = (
    "out_of_stock",
    "out of stock",
    "het hang",
    "hết hàng",
    "not_found",
    "not found",
    "khong tim thay",
    "không tìm thấy",
    "not_served",
    "not served",
    "khong giao",
    "không giao",
    "khong du ton",
    "không đủ tồn",
    "khong du hang",
    "không đủ hàng",
    "unavailable",
)

_FULFILLMENT_REFUSAL_RE = re.compile(
    r"(?i)(khong the dat hang|không thể đặt hàng|khong du ton|không đủ tồn|"
    r"khong du hang|không đủ hàng|het hang|hết hàng|khong tim thay|không tìm thấy|"
    r"khong giao duoc|không giao được|dia diem khong giao|địa điểm không giao|not served|not_found|out_of_stock)"
)
_COUPON_ONLY_REFUSAL_RE = re.compile(
    r"(?i)(coupon|ma coupon|mã coupon|ma giam gia|mã giảm giá).{0,40}"
    r"(khong hop le|không hợp lệ|expired|het han|hết hạn)"
)

_NOTE_SEGMENT_RE = re.compile(
    r"(?is)\b("
    r"ghi\s*chu|ghi\s*chú|note|notes|instruction|instructions|system|developer|"
    r"ignore\s+previous|price\s+override|fake\s+tool\s+result|tool\s+result"
    r")\b\s*[:：-]?\s*([^.;\n\r]*([.;\n\r]|$))"
)


def _load_prompt() -> str | None:
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None:
        return _PROMPT_CACHE
    with _PROMPT_LOCK:
        if _PROMPT_CACHE is None:
            path = os.path.join(os.path.dirname(__file__), "prompt.txt")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _PROMPT_CACHE = f.read().strip()
            except Exception:
                _PROMPT_CACHE = ""
    return _PROMPT_CACHE or None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def _sanitize_question(question: Any) -> tuple[str, bool]:
    text = _normalize(str(question or ""))
    hit = False

    def repl(match: re.Match) -> str:
        nonlocal hit
        hit = True
        label = match.group(1)
        return f"{label}: [noi dung ghi chu khong dang tin da bi vo hieu hoa]. "

    sanitized = _NOTE_SEGMENT_RE.sub(repl, text)
    return sanitized.strip(), hit


def _redact_pii(text: Any) -> tuple[str, int]:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    protected: list[str] = []

    def protect_money(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"__OBS_MONEY_{len(protected) - 1}__"

    text = re.sub(r"(?i)\b(?:Tong cong:\s*)?[0-9][0-9., ]*\s*VND\b", protect_money, text)
    if _telemetry_redact:
        text, count = _telemetry_redact(text)
    else:
        count = 0
    for pattern, label in (
        (_EMAIL_RE, "EMAIL"),
        (_VN_PHONE_RE, "PHONE"),
        (_PHONE_LIKE_RE, "PHONE"),
    ):
        text, n = pattern.subn(f"[REDACTED:{label}]", text)
        count += n
    for idx, value in enumerate(protected):
        text = text.replace(f"__OBS_MONEY_{idx}__", value)
    text = re.sub(r"(?i)\s*\(?\s*lien he\s*:\s*\[REDACTED(?::[A-Z_]+)?\]\s*\)?", "", text).strip()
    return text, count


def _cache_key(question: str, config: dict[str, Any]) -> str:
    model = str(config.get("provider", "")) + "/" + str(config.get("model", ""))
    raw = json.dumps({"q": question, "model": model}, ensure_ascii=False, sort_keys=True)
    return "obs:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _copy_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        try:
            return copy.deepcopy(result)
        except Exception:
            return dict(result)
    return {"answer": str(result), "status": "ok", "steps": 0, "trace": [], "meta": {}}


def _flatten(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(_flatten(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_flatten(child))
    return nodes


def _text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    except Exception:
        return str(value).lower()


def _tool_name(node: dict[str, Any]) -> str | None:
    keys = ("tool", "tool_name", "name", "action", "function", "type")
    for key in keys:
        value = node.get(key)
        if isinstance(value, str):
            low = value.lower()
            for tool in ("check_stock", "get_discount", "calc_shipping"):
                if tool in low:
                    return tool
    blob = _text(node)
    for tool in ("check_stock", "get_discount", "calc_shipping"):
        if tool in blob:
            return tool
    return None


def _numeric_candidates(value: Any, path: str = "") -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            out.extend(_numeric_candidates(child, f"{path}.{key}".strip(".")))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            out.extend(_numeric_candidates(child, f"{path}.{idx}".strip(".")))
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int):
        out.append((path.lower(), float(value)))
    elif isinstance(value, float):
        out.append((path.lower(), float(value)))
    elif isinstance(value, str):
        s = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            out.append((path.lower(), float(s)))
    return out


def _first_num(node: dict[str, Any], include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> int | None:
    for path, num in _numeric_candidates(node):
        if any(part in path for part in include) and not any(part in path for part in exclude):
            return int(num)
    return None


def _first_float(node: dict[str, Any], include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> float | None:
    for path, num in _numeric_candidates(node):
        if any(part in path for part in include) and not any(part in path for part in exclude):
            return float(num)
    return None


def _quantity_from_question(question: str) -> int:
    text = _normalize(question).lower()
    patterns = (
        r"\bmua\s+(\d{1,3})\b",
        r"\bdat\s+(\d{1,3})\b",
        r"\bđặt\s+(\d{1,3})\b",
        r"\bso\s*luong\s*[:=]?\s*(\d{1,3})\b",
        r"\bsố\s*lượng\s*[:=]?\s*(\d{1,3})\b",
        r"\bqty\s*[:=]?\s*(\d{1,3})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            qty = int(match.group(1))
            if qty > 0:
                return qty
    return 1


def _required_tools(question: str) -> set[str]:
    text = _normalize(question).lower()
    required = {"check_stock"}
    if re.search(r"\b(coupon|ma|mã|code|voucher|sale|vip|winner|expired)\b", text):
        required.add("get_discount")
    if re.search(r"\b(giao|ship|shipping|den|đến|dia chi|địa chỉ)\b", text):
        required.add("calc_shipping")
    return required


def _collect_tools(trace: Any, meta: dict[str, Any]) -> list[str]:
    tools: list[str] = []
    raw = meta.get("tools_used")
    if isinstance(raw, list):
        for item in raw:
            name = _tool_name({"name": item}) if isinstance(item, str) else _tool_name(item) if isinstance(item, dict) else None
            if name:
                tools.append(name)
    for node in _flatten(trace):
        name = _tool_name(node)
        if name:
            tools.append(name)
    deduped: list[str] = []
    for tool in tools:
        if tool not in deduped:
            deduped.append(tool)
    return deduped


def _trace_has_unavailable(trace: Any) -> bool:
    blob = _text(trace)
    if any(word in blob for word in _UNAVAILABLE_WORDS):
        return True
    for node in _flatten(trace):
        name = _tool_name(node)
        if name in ("check_stock", "calc_shipping"):
            for key, value in node.items():
                low_key = str(key).lower()
                if any(part in low_key for part in ("in_stock", "available", "served")) and value is False:
                    return True
    return False


def _trace_has_tool_error(trace: Any) -> bool:
    blob = _text(trace)
    return any(word in blob for word in _TOOL_ERROR_WORDS)


def _extract_total_inputs(trace: Any, question: str = "") -> dict[str, int | None]:
    data: dict[str, Any] = {
        "unit_price": None,
        "quantity": _quantity_from_question(question),
        "discount_pct": 0,
        "shipping": 0,
        "unit_weight_kg": None,
        "shipping_weight_kg": None,
    }
    for node in _flatten(trace):
        name = _tool_name(node)
        if name == "check_stock":
            price = _first_num(
                node,
                ("unit_price", "unitprice", "price", "gia", "don_gia"),
                ("total", "subtotal", "discount", "shipping", "fee", "cost"),
            )
            qty = _first_num(node, ("quantity", "qty", "so_luong", "soluong"))
            weight = _first_float(node, ("weight", "kg"))
            if price is not None and price >= 0:
                data["unit_price"] = price
            if weight is not None and weight > 0:
                data["unit_weight_kg"] = weight
            if qty is not None and qty > 0 and not question:
                data["quantity"] = qty
        elif name == "get_discount":
            pct = _first_num(node, ("percent", "pct", "discount", "phan_tram"))
            if pct is not None and 0 <= pct <= 100:
                data["discount_pct"] = pct
        elif name == "calc_shipping":
            fee = _first_num(node, ("shipping", "ship", "fee", "cost", "phi"))
            ship_weight = _first_float(node, ("weight", "kg"))
            if fee is not None and fee >= 0:
                data["shipping"] = fee
            if ship_weight is not None and ship_weight > 0:
                data["shipping_weight_kg"] = ship_weight
    if data["unit_weight_kg"] and data["shipping_weight_kg"] and data["shipping"] is not None:
        actual_weight = float(data["unit_weight_kg"]) * int(data["quantity"] or 1)
        called_weight = float(data["shipping_weight_kg"])
        if abs(actual_weight - called_weight) > 0.01:
            base = int(data["shipping"]) - max(called_weight - 1.0, 0.0) * 5000
            adjusted = base + max(actual_weight - 1.0, 0.0) * 5000
            if adjusted >= 0:
                data["shipping"] = int(round(adjusted))
    if data["quantity"] is None:
        data["quantity"] = 1
    return data


def _computed_total(trace: Any, question: str = "", tools: list[str] | None = None) -> int | None:
    values = _extract_total_inputs(trace, question)
    if values["unit_price"] is None or values["quantity"] is None:
        return None
    subtotal = int(values["unit_price"]) * int(values["quantity"])
    discounted = subtotal * (100 - int(values["discount_pct"] or 0)) // 100
    return discounted + int(values["shipping"] or 0)


def _answer_total(answer: str) -> int | None:
    match = _TOTAL_RE.search(answer or "")
    if not match:
        match = _ANY_TOTAL_RE.search(answer or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1) if match.lastindex else match.group(0))
    return int(digits) if digits else None


def _replace_or_append_total(answer: str, total: int) -> str:
    line = f"Tong cong: {total} VND"
    answer = _ANY_TOTAL_RE.sub("", answer or "").strip()
    answer = re.sub(r"[ \t]+(\r?\n)", r"\1", answer)
    answer = re.sub(r"\n{3,}", "\n\n", answer)
    return (answer + "\n" + line).strip() if answer else line


def _refusal_answer(answer: str, trace: Any) -> str:
    blob = _text(trace)
    if "not_served" in blob or "not served" in blob or "khong giao" in blob or "không giao" in blob:
        base = "Khong the dat hang vi dia diem giao hang khong duoc ho tro."
    elif "not_found" in blob or "not found" in blob or "khong tim thay" in blob or "không tìm thấy" in blob:
        base = "Khong the dat hang vi khong tim thay san pham."
    else:
        base = "Khong the dat hang vi san pham khong co san hoac khong du ton kho."
    cleaned = _ANY_TOTAL_RE.sub("", answer or "").strip()
    if cleaned and "tong cong" not in cleaned.lower():
        return cleaned
    return base


def _detect_faults(result: dict[str, Any], tools: list[str], pii_count: int, total_fixed: bool, unavailable: bool) -> list[str]:
    faults: list[str] = []
    status = result.get("status")
    trace = result.get("trace", [])
    if status in _BAD_STATUS:
        faults.append("infinite_loop" if status in {"loop", "max_steps"} else str(status))
    if _trace_has_tool_error(trace):
        faults.append("tool_failure")
    if pii_count:
        faults.append("pii_leak")
    if total_fixed:
        faults.append("arithmetic_error")
    if unavailable and _answer_total(result.get("answer") or "") is not None:
        faults.append("fabrication")
    seen = set()
    for tool in tools:
        if tool in seen:
            faults.append("tool_overuse")
            break
        seen.add(tool)
    return sorted(set(faults))


def _log_event(payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with _LOG_LOCK:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _should_retry(result: dict[str, Any], question: str = "") -> bool:
    if result.get("status") in _BAD_STATUS:
        return True
    if not str(result.get("answer") or "").strip():
        return True
    if _COUPON_ONLY_REFUSAL_RE.search(str(result.get("answer") or "")) and _answer_total(str(result.get("answer") or "")) is None:
        return True
    trace = result.get("trace", [])
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    tools = _collect_tools(trace, meta)
    if not tools:
        return True
    return _trace_has_tool_error(trace)


def _fallback(error: Exception | None = None) -> dict[str, Any]:
    answer = "Xin loi, he thong tam thoi khong the xac nhan don hang. Vui long thu lai."
    error_message = None
    if error:
        error_message = str(error)
        error_message, _ = _redact_pii(error_message[:500])
    return {
        "answer": answer,
        "status": "wrapper_error" if error else "no_action",
        "steps": 0,
        "trace": [],
        "meta": {
            "wrapper_error": type(error).__name__ if error else None,
            "wrapper_error_message": error_message,
        },
    }


def mitigate(call_next, question, config, context):
    started = time.time()
    retry_count = 0
    cache_hit = False
    sanitized, injection_marker = _sanitize_question(question)
    pii_in_input = _redact_pii(str(question or ""))[1]
    key = _cache_key(sanitized, config if isinstance(config, dict) else {})
    cache = context.get("cache") if isinstance(context, dict) else None
    cache_lock = context.get("cache_lock") if isinstance(context, dict) else None

    try:
        if isinstance(cache, dict) and cache_lock is not None:
            with cache_lock:
                cached = cache.get(key)
            if cached is not None:
                cache_hit = True
                result = _copy_result(cached)
            else:
                result = None
        else:
            result = None

        if result is None:
            conf = dict(config or {})
            prompt = _load_prompt()
            if prompt:
                conf["system_prompt"] = prompt

            retry_cfg = conf.get("retry") if isinstance(conf.get("retry"), dict) else {}
            max_attempts = int(retry_cfg.get("max_attempts", 2) or 2)
            max_attempts = max(1, min(max_attempts, 3))
            backoff_ms = int(retry_cfg.get("backoff_ms", 100) or 0)

            last_error: Exception | None = None
            result = None
            for attempt in range(max_attempts):
                if attempt:
                    retry_count += 1
                    if backoff_ms > 0:
                        time.sleep(min(backoff_ms, 500) / 1000.0)
                try:
                    candidate = _copy_result(call_next(sanitized, conf))
                    result = candidate
                    if not _should_retry(candidate, sanitized):
                        break
                except Exception as exc:
                    last_error = exc
                    result = _fallback(exc)
            if result is None:
                result = _fallback(last_error)

            if isinstance(cache, dict) and cache_lock is not None and result.get("status") == "ok":
                with cache_lock:
                    cache[key] = _copy_result(result)

        result = _copy_result(result)
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        trace = result.get("trace", [])
        tools = _collect_tools(trace, meta)

        answer = result.get("answer") or ""
        unavailable = _trace_has_unavailable(trace)
        if _FULFILLMENT_REFUSAL_RE.search(answer or ""):
            unavailable = True
        if unavailable:
            answer = _refusal_answer(answer, trace)

        total_fixed = False
        if not unavailable:
            expected = _computed_total(trace, sanitized, tools)
            observed = _answer_total(answer)
            if expected is not None and observed != expected:
                answer = _replace_or_append_total(answer, expected)
                total_fixed = True

        answer, pii_in_answer = _redact_pii(answer)
        result["answer"] = answer
        if result.get("status") in {"loop", "max_steps", "no_action"} and (
            _answer_total(answer) is not None or _FULFILLMENT_REFUSAL_RE.search(answer or "")
        ):
            result["status"] = "ok"

        faults = _detect_faults(result, tools, pii_in_answer, total_fixed, unavailable)
        if injection_marker:
            faults.append("prompt_injection")
            faults = sorted(set(faults))

        wall_ms = int((time.time() - started) * 1000)
        event = {
            "qid": context.get("qid") if isinstance(context, dict) else None,
            "session_id": context.get("session_id") if isinstance(context, dict) else None,
            "turn_index": context.get("turn_index") if isinstance(context, dict) else None,
            "status": result.get("status"),
            "wrapper_error_type": meta.get("wrapper_error"),
            "wrapper_error_message": meta.get("wrapper_error_message"),
            "steps": result.get("steps"),
            "latency_ms": meta.get("latency_ms", wall_ms),
            "wall_ms": wall_ms,
            "usage": meta.get("usage", {}),
            "tools_used": tools,
            "retry_count": retry_count,
            "pii_found": bool(pii_in_input or pii_in_answer),
            "cache_hit": cache_hit,
            "detected_faults": faults,
        }
        _log_event(event)
        return result
    except Exception as exc:
        result = _fallback(exc)
        redacted_answer, _ = _redact_pii(result["answer"])
        result["answer"] = redacted_answer
        _log_event({
            "qid": context.get("qid") if isinstance(context, dict) else None,
            "session_id": context.get("session_id") if isinstance(context, dict) else None,
            "turn_index": context.get("turn_index") if isinstance(context, dict) else None,
            "status": "wrapper_error",
            "wrapper_error_type": type(exc).__name__,
            "wrapper_error_message": _redact_pii(str(exc)[:500])[0],
            "steps": 0,
            "latency_ms": int((time.time() - started) * 1000),
            "usage": {},
            "tools_used": [],
            "retry_count": retry_count,
            "pii_found": False,
            "cache_hit": cache_hit,
            "detected_faults": ["wrapper_error"],
        })
        return result
