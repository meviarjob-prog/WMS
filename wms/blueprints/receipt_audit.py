"""Проверка всех непринятых перемещений на Ozon по отчётам о приёмке —
только администратор («Перемещение» → «Проверка непринятых»).

Берём все завершённые, отгруженные и ещё не принятые перемещения на склады
Ozon и сверяем их с отчётами «Отчёт по поставке» (xlsx), которые Ozon
выдаёт после приёмки (загружаются сразу пачкой). Для каждого перемещения:

- всё совпало → «проводим» (принято как отправлено);
- не совпало, но только недостача (излишков нет) → автоматически исправляем:
  короба, которых нет в отчёте, возвращаем на склад-отправитель, в пришедших
  коробах правим количество до принятого (та же логика, что у приёмки по
  акту, см. movement._receive_from_act);
- излишек, нераспознанные позиции/ГМ, нет заявки на МП, либо похоже на
  ошибку отправки (недостача здесь ↔ излишек того же товара в другом
  перемещении) → ничего не меняем, показываем на ручную проверку.

Данные берутся из файлов: API Ozon в программе не подключен (см. чат)."""

import json
import math
import re
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import (
    MovementCargoPlace,
    MovementDocument,
    MovementLine,
    MovementReceiptDiscrepancy,
    Nomenclature,
    Warehouse,
)
from ..utils.acceptance_act import ActParseError, MAX_ACT_BYTES, parse_act
from .movement import _nomenclature_for_act_item, _receive_from_act, _reconcile_with_act

bp = Blueprint("receipt_audit", __name__)

RECENT_RECEIVED_DAYS = 60


@bp.before_request
def _admin_only():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(404)


def eligible_documents():
    """Непринятые перемещения на склады Ozon: завершены и переданы транспорту."""
    return (
        MovementDocument.query.join(Warehouse, Warehouse.id == MovementDocument.to_warehouse_id)
        .filter(
            MovementDocument.status == "completed",
            MovementDocument.shipped_at.isnot(None),
            MovementDocument.received_at.is_(None),
            Warehouse.marketplace == "ozon",
        )
        .order_by(MovementDocument.id)
        .all()
    )


def _request_ready(doc):
    return doc.marketplace_request_created_at is not None and bool((doc.marketplace_request_number or "").strip())


def _doc_contents(doc, cache=None):
    """[(номер короба, {id_товара: кол-во})] по строкам документа."""
    if cache is not None and doc.id in cache:
        return cache[doc.id]
    result = []
    for line in doc.lines.order_by(MovementLine.id.asc()).all():
        content = {}
        for item in line.box.items:
            content[item.nomenclature_id] = content.get(item.nomenclature_id, 0) + item.qty
        result.append((line.box.box_number, content))
    if cache is not None:
        cache[doc.id] = result
    return result


def _same_content(a, b):
    if set(a) != set(b):
        return False
    return all(abs(a[k] - b[k]) < 1e-6 for k in a)


def infer_cargo_mapping(doc, declared, contents_cache=None, lookup_cache=None):
    """ГМ → короб по заявленному составу (колонка «Заявлено» отчёта Ozon):
    ГМ соответствует короб с точно таким же содержимым. Для перемещений,
    у которых сопоставление не запоминалось при выгрузке состава ГМ.
    Возвращает (сопоставление, ГМ без пары). Кэши — чтобы не повторять
    запросы по одним и тем же ключам/документам при большой пачке отчётов."""
    contents = _doc_contents(doc, contents_cache)
    lookup = lookup_cache if lookup_cache is not None else {}
    used, mapping, unmatched = set(), {}, []
    for gm, items in declared.items():
        want, ok = {}, True
        for key, qty in items.items():
            if key not in lookup:
                lookup[key] = _nomenclature_for_act_item(key)[0]
            nomenclature = lookup[key]
            if nomenclature is None:
                ok = False
                break
            want[nomenclature.id] = want.get(nomenclature.id, 0) + qty
        if not ok or not want:
            unmatched.append(gm)
            continue
        for number, content in contents:
            if number not in used and _same_content(content, want):
                mapping[gm] = number
                used.add(number)
                break
        else:
            unmatched.append(gm)
    return mapping, unmatched


def _digits(text):
    return re.sub(r"\D", "", text or "")


def assign_reports(reports, docs):
    """Подбирает перемещение к каждому отчёту. reports — список словарей
    {name, boxes, declared}. Возвращает (пары, нераспознанные отчёты):
    пара = {report, doc, key_to_box, how, inferred}. Неоднозначность
    (ничья по грузоместам, два перемещения с одним номером заявки, несколько
    подходящих по составу) не разрешаем «по первому попавшемуся» — отчёт
    остаётся нераспознанным, его разберут вручную."""
    docs_by_id = {d.id: d for d in docs}
    places_by_doc = {}
    for place in MovementCargoPlace.query.filter(MovementCargoPlace.document_id.in_(list(docs_by_id) or [0])).all():
        places_by_doc.setdefault(place.document_id, {})[place.cargo_barcode] = place.box_number

    contents_cache, lookup_cache = {}, {}
    pairs, orphans, taken = [], [], set()

    def free_docs():
        return [d for d in docs if d.id not in taken]

    for report in reports:
        keys = set(report["boxes"]) | set(report["declared"])
        chosen, key_to_box, how, inferred = None, None, "", False

        # 1) грузоместа, запомненные при выгрузке «Состав ГМ»
        overlaps = sorted(
            ((len(keys & set(places_by_doc.get(doc.id, {}))), doc.id) for doc in free_docs()), reverse=True
        )
        if overlaps and overlaps[0][0] > 0 and (len(overlaps) == 1 or overlaps[1][0] < overlaps[0][0]):
            chosen = docs_by_id[overlaps[0][1]]
            key_to_box, how = places_by_doc[chosen.id], "по грузоместам, запомненным при выгрузке"

        # 2) номер поставки в имени файла = № заявки на МП (полное совпадение числа)
        if chosen is None:
            numbers = set(re.findall(r"\d{5,}", report["name"]))
            candidates = [
                d for d in free_docs()
                if _digits(d.marketplace_request_number) and _digits(d.marketplace_request_number) in numbers
            ]
            if len(candidates) == 1:
                chosen = candidates[0]
                how = "по номеру поставки в имени файла"
                if keys & set(places_by_doc.get(chosen.id, {})):
                    key_to_box = places_by_doc[chosen.id]
                else:
                    key_to_box, _unmatched = infer_cargo_mapping(
                        chosen, report["declared"], contents_cache, lookup_cache
                    )
                    how += ", ГМ сопоставлены по составу"
                    inferred = True

        # 3) только по составу заявленного — если подходит ровно одно перемещение
        if chosen is None and report["declared"]:
            fits = []
            for doc in free_docs():
                if doc.id in places_by_doc:
                    continue
                mapping, unmatched = infer_cargo_mapping(doc, report["declared"], contents_cache, lookup_cache)
                if mapping and not unmatched:
                    fits.append((doc, mapping))
            if len(fits) == 1:
                chosen, key_to_box = fits[0]
                how, inferred = "по составу грузомест (заявлено)", True

        if chosen is None:
            orphans.append(report)
            continue
        taken.add(chosen.id)
        pairs.append({
            "report": report, "doc": chosen, "key_to_box": key_to_box or {}, "how": how, "inferred": inferred,
        })
    return pairs, orphans


def _ambiguous_identical_boxes(doc, rec):
    """Если ГМ сопоставлены по составу, одинаковые короба неотличимы: какой
    из них не доехал, определить нельзя. Возвращает номера пар «не доехал /
    доехал с таким же составом»."""
    contents = dict(_doc_contents(doc))
    present = [b["box_number"] for b in rec["boxes"]]
    conflicts = []
    for missing in rec["missing"]:
        content = contents.get(missing["box_number"])
        if content is None:
            continue
        for number in present:
            if _same_content(content, contents.get(number, {})):
                conflicts.append(missing["box_number"])
                break
    return conflicts


def analyze(doc, rec, inferred=False):
    """Классификация одного перемещения по результату сверки."""
    reasons = []
    surplus, short = [], []
    deficit, excess = {}, {}   # id товара -> кол-во
    names = {}

    for missing in rec["missing"]:
        for item in missing["line"].box.items:
            deficit[item.nomenclature_id] = deficit.get(item.nomenclature_id, 0) + item.qty
            names[item.nomenclature_id] = item.nomenclature.name
    for box in rec["boxes"]:
        for row in box["rows"]:
            nid = row["nomenclature"].id
            names[nid] = row["nomenclature"].name
            diff = row["accepted"] - row["expected"]
            if diff > 0:
                surplus.append((box["box_number"], row["nomenclature"].name, row["expected"], row["accepted"]))
                excess[nid] = excess.get(nid, 0) + diff
            elif diff < 0:
                short.append((box["box_number"], row["nomenclature"].name, row["expected"], row["accepted"]))
                deficit[nid] = deficit.get(nid, 0) - diff

    if rec["error"]:
        reasons.append(rec["error"])
    else:
        if rec["unknown_keys"]:
            reasons.append("В отчёте есть ГМ, которых нет в этом перемещении: " + ", ".join(map(str, rec["unknown_keys"][:5])))
        if rec["unidentified"]:
            reasons.append("В отчёте есть неопознанные позиции")
        if surplus:
            reasons.append("Принято больше отправленного: " + "; ".join(
                f"{box}: {name} {sent:g} → {got:g}" for box, name, sent, got in surplus[:4]))
        if not _request_ready(doc):
            reasons.append("Не внесён номер заявки на МП или не стоит отметка «Заявка создана» — провести нельзя")
        if inferred:
            conflicts = _ambiguous_identical_boxes(doc, rec)
            if conflicts:
                reasons.append(
                    "Грузоместа сопоставлены по составу, а среди коробов есть одинаковые — нельзя определить, "
                    "какой именно не доехал (" + ", ".join(conflicts[:4]) + "). Выгрузите «Состав ГМ» или проверьте вручную"
                )
    if reasons:
        status = "manual"
    elif not rec["missing"] and not short:
        status = "match"
    else:
        status = "auto_fix"
    return {
        "doc": doc, "rec": rec, "status": status, "reasons": reasons,
        "deficit": deficit, "excess": excess, "names": names,
        "missing_boxes": [m["box_number"] for m in rec["missing"]],
        "short_rows": short, "surplus_rows": surplus,
    }


def _recent_excess():
    """{id товара: [(номер перемещения, излишек)]} по уже принятым за
    последние RECENT_RECEIVED_DAYS дней перемещениям (там излишек уже
    записан расхождением) — вдруг недостача здесь это излишек там."""
    since = datetime.utcnow() - timedelta(days=RECENT_RECEIVED_DAYS)
    found = {}
    rows = (
        db.session.query(MovementDocument.number, MovementReceiptDiscrepancy)
        .join(MovementReceiptDiscrepancy, MovementReceiptDiscrepancy.document_id == MovementDocument.id)
        .filter(MovementDocument.received_at >= since)
        .all()
    )
    for number, discrepancy in rows:
        if discrepancy.received_qty > discrepancy.expected_qty:
            found.setdefault(discrepancy.nomenclature_id, []).append(
                (number, discrepancy.received_qty - discrepancy.expected_qty)
            )
    return found


def cross_check(results):
    """Недостача по товару в одном перемещении и излишек того же товара в
    другом (в этой же пачке или в недавно принятых) — похоже на ошибку при
    отправке: короб/товар уехал не в ту поставку. Такие случаи — только на
    ручную проверку: помечаем оба перемещения."""
    recent = _recent_excess()
    for a in results:
        for nid, short_qty in a["deficit"].items():
            partners = []
            for b in results:
                if b is a:
                    continue
                other = b["excess"].get(nid, 0)
                if other > 0:
                    partners.append((b["doc"].number, other, b))
            for number, other in recent.get(nid, []):
                if number != a["doc"].number:
                    partners.append((number, other, None))
            if not partners:
                continue
            name = a["names"].get(nid, "товар")
            for number, other, b in partners:
                exact = "в том же количестве" if abs(other - short_qty) < 1e-6 else f"излишек {other:g}"
                a["reasons"].append(
                    f"Возможна ошибка при отправке: недостача «{name}» {short_qty:g} шт — "
                    f"в перемещении {number} тот же товар принят с излишком ({exact})"
                )
                if b is not None:
                    b["reasons"].append(
                        f"Возможна ошибка при отправке: излишек «{name}» {other:g} шт — "
                        f"в {a['doc'].number} того же товара не хватило ({short_qty:g} шт)"
                    )
                    b["status"] = "manual"
            a["status"] = "manual"
    return results


def _parse_reports(files):
    reports, errors = [], []
    for upload in files:
        if not upload or not upload.filename:
            continue
        data = upload.read(MAX_ACT_BYTES + 1)
        try:
            boxes, _total, declared = parse_act(data, prefer_article=True, with_declared=True)
        except ActParseError as exc:
            errors.append(f"{upload.filename}: {exc}")
            continue
        reports.append({"name": upload.filename, "boxes": boxes, "declared": declared})
    return reports, errors


@bp.route("/", methods=["GET"])
def index():
    docs = eligible_documents()
    ids = [d.id for d in docs] or [0]
    line_counts = dict(
        db.session.query(MovementLine.document_id, db.func.count(MovementLine.id))
        .filter(MovementLine.document_id.in_(ids)).group_by(MovementLine.document_id).all()
    )
    saved_ids = {
        row[0] for row in db.session.query(MovementCargoPlace.document_id)
        .filter(MovementCargoPlace.document_id.in_(ids)).distinct().all()
    }
    return render_template(
        "receipt_audit/index.html", docs=docs, ready=_request_ready,
        line_counts=line_counts, saved_ids=saved_ids,
    )


@bp.route("/check", methods=["POST"])
def check():
    reports, errors = _parse_reports(request.files.getlist("reports"))
    for error in errors:
        flash(error, "danger")
    if not reports:
        flash("Загрузите отчёты Ozon о приёмке (xlsx) — один или несколько файлов", "danger")
        return redirect(url_for("receipt_audit.index"))

    docs = eligible_documents()
    pairs, orphans = assign_reports(reports, docs)
    results = []
    for pair in pairs:
        rec = _reconcile_with_act(pair["doc"], pair["report"]["boxes"], key_to_box=pair["key_to_box"])
        result = analyze(pair["doc"], rec, inferred=pair["inferred"])
        result["file"], result["how"] = pair["report"]["name"], pair["how"]
        results.append(result)
    cross_check(results)

    matched_ids = {r["doc"].id for r in results}
    no_data = [d for d in docs if d.id not in matched_ids]
    order = {"manual": 0, "auto_fix": 1, "match": 2}
    results.sort(key=lambda r: (order[r["status"]], r["doc"].id))

    plan = [
        {"doc_id": r["doc"].id, "payload": r["rec"]["payload"]}
        for r in results if r["status"] in ("match", "auto_fix")
    ]
    counts = {s: sum(1 for r in results if r["status"] == s) for s in order}
    return render_template(
        "receipt_audit/result.html", results=results, orphans=orphans, no_data=no_data,
        counts=counts, plan_json=json.dumps(plan, ensure_ascii=False),
    )


@bp.route("/apply", methods=["POST"])
def apply():
    try:
        plan = json.loads(request.form.get("plan", "[]"))
        assert isinstance(plan, list)
    except (ValueError, AssertionError):
        flash("Данные проверки повреждены — загрузите отчёты заново", "danger")
        return redirect(url_for("receipt_audit.index"))

    eligible = {d.id: d for d in eligible_documents()}
    done, fixed, returned_boxes, skipped = [], [], 0, []
    seen = set()
    for item in plan:
        if not isinstance(item, dict):
            continue
        doc_id = item.get("doc_id")
        payload = item.get("payload")
        if doc_id in seen:
            continue            # один и тот же документ дважды в плане — второй раз не проводим
        seen.add(doc_id)
        doc = eligible.get(doc_id)
        if doc is None or not isinstance(payload, dict):
            skipped.append(f"#{doc_id} (уже принято или изменилось)")
            continue
        if not _request_ready(doc):
            skipped.append(f"{doc.number} (нет заявки на МП)")
            continue
        verdict = _verify_payload(doc, payload)
        if verdict:
            skipped.append(f"{doc.number} ({verdict})")
            continue
        number = doc.number
        try:
            outcome = _receive_from_act(doc, payload)
        except Exception:  # noqa: BLE001 — один сбойный документ не должен рушить остальные
            db.session.rollback()
            current_app.logger.exception("Проверка непринятых: не удалось провести %s", number)
            skipped.append(f"{number} (ошибка при проведении, см. журнал)")
            continue
        if outcome is None:
            skipped.append(f"{number} (нет коробов из отчёта)")
            continue
        if outcome["returned"] or outcome["received"] != outcome["sent"]:
            fixed.append(number)
            returned_boxes += len(outcome["returned"])
        else:
            done.append(number)
    flash(
        f"Проведено без изменений: {len(done)}; исправлено и проведено: {len(fixed)} "
        f"(возвращено коробов на склад: {returned_boxes}).",
        "success",
    )
    if skipped:
        flash("Пропущено: " + "; ".join(skipped), "warning")
    return redirect(url_for("receipt_audit.index"))


def _verify_payload(doc, payload):
    """Страховка перед записью: состав коробов мог измениться после проверки
    или данные подменены — принимаем только то, что не превышает
    отправленное и ссылается на существующие товары. Возвращает причину отказа."""
    contents = dict(_doc_contents(doc))
    if not payload:
        return "нет принятых коробов"
    nids = set()
    for number, items in payload.items():
        if number not in contents or not isinstance(items, dict):
            return "короб не из этого перемещения"
        for nid, qty in items.items():
            try:
                nid_int, qty_f = int(nid), float(qty)
            except (TypeError, ValueError):
                return "некорректные данные"
            if not math.isfinite(qty_f) or qty_f < 0 or qty_f > contents[number].get(nid_int, 0) + 1e-6:
                return "после проверки состав коробов изменился или принято больше отправленного"
            nids.add(nid_int)
    if nids and Nomenclature.query.filter(Nomenclature.id.in_(nids)).count() != len(nids):
        return "неизвестный товар"
    return None
