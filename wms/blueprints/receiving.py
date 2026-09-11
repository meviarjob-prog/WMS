from datetime import datetime

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func

from ..extensions import db
from ..models import Box, BoxItem, Nomenclature, ReceivingDocument, ReceivingLine, Supplier, UnplacedStock
from ..utils.excel_io import export_receiving_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.receiving_invoice_import import InvoiceParseError, parse_invoice

bp = Blueprint("receiving", __name__)


@bp.route("/")
def list_documents():
    # "Приемка по накладной" — документы, созданные загрузкой файла
    # накладной (см. import_invoice_form): у них всегда проставлен
    # supplier_id (найденный/заведенный в справочник поставщик), в отличие
    # от ручного создания, где поставщик — просто свободный текст.
    unfinished_only = request.args.get("unfinished") == "on"
    invoice_only = request.args.get("invoice_only") == "on"

    query = ReceivingDocument.query
    if unfinished_only:
        query = query.filter_by(status="draft")
    if invoice_only:
        query = query.filter(ReceivingDocument.supplier_id.isnot(None))

    documents = query.order_by(ReceivingDocument.created_at.desc()).all()
    return render_template(
        "receiving/list.html",
        documents=documents,
        unfinished_only=unfinished_only,
        invoice_only=invoice_only,
    )


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    from ..models import Warehouse

    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("receiving/new.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    supplier = request.form.get("supplier", "").strip()

    if not warehouse_id:
        flash("Выберите склад приемки", "danger")
        return redirect(url_for("receiving.new_document"))

    doc = ReceivingDocument(
        number=next_number("receiving"),
        warehouse_id=warehouse_id,
        supplier=supplier,
        created_by_id=current_user.id,
    )
    db.session.add(doc)
    db.session.commit()
    flash(f"Документ приемки {doc.number} создан", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id))


def _find_or_create_supplier(name, inn, phone):
    supplier = None
    if inn:
        supplier = Supplier.query.filter_by(inn=inn).first()
    if not supplier:
        supplier = Supplier.query.filter_by(name=name).first()
    if not supplier:
        supplier = Supplier(name=name, inn=inn, phone=phone)
        db.session.add(supplier)
        db.session.flush()
    return supplier


def _find_nomenclature_for_invoice_row(row):
    """Сопоставляет строку накладной с номенклатурой по штрихкоду (если в
    файле есть такая колонка — сейчас ее нет, но может появиться) либо по
    точному названию товара. Код 1С ("Код") — внутренняя нумерация
    поставщика и с sku в номенклатуре не связана, поэтому не используется."""
    barcode = row.get("barcode")
    if barcode:
        item = Nomenclature.query.filter_by(barcode=barcode).first()
        if item:
            return item
    name = row["name"].strip()
    return Nomenclature.query.filter(func.lower(Nomenclature.name) == name.lower()).first()


@bp.route("/import-invoice", methods=["GET", "POST"])
def import_invoice_form():
    """Загрузка приходной накладной из 1С (см.
    utils.receiving_invoice_import) — вместо ручного создания документа и
    добавления позиций одна за другой: номер накладной становится номером
    приемки, поставщик определяется из файла (и заводится в справочник,
    если его еще нет), товары и количество подставляются из накладной.
    Дальше кладовщик сверяет их на мобильной форме (см. confirm_invoice)."""
    from ..models import Warehouse

    if request.method == "GET":
        warehouses = Warehouse.query.filter_by(is_active=True).order_by(Warehouse.code).all()
        return render_template("receiving/import_invoice.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    if not warehouse_id:
        flash("Выберите склад приемки", "danger")
        return redirect(url_for("receiving.import_invoice_form"))

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл накладной (.xlsx)", "danger")
        return redirect(url_for("receiving.import_invoice_form"))

    try:
        invoice = parse_invoice(file.stream)
    except InvoiceParseError as exc:
        flash(f"Не удалось разобрать файл накладной: {exc}", "danger")
        return redirect(url_for("receiving.import_invoice_form"))
    except Exception as exc:  # noqa: BLE001 — файл пришел от внешнего источника (1С)
        current_app.logger.exception("Не удалось прочитать файл накладной")
        flash(f"Не удалось прочитать файл: {exc}", "danger")
        return redirect(url_for("receiving.import_invoice_form"))

    if ReceivingDocument.query.filter_by(number=invoice.invoice_number).first():
        flash(f"Накладная № {invoice.invoice_number} уже была загружена раньше", "danger")
        return redirect(url_for("receiving.import_invoice_form"))

    try:
        supplier = _find_or_create_supplier(invoice.supplier_name, invoice.supplier_inn, invoice.supplier_phone)

        doc = ReceivingDocument(
            number=invoice.invoice_number,
            warehouse_id=warehouse_id,
            supplier=supplier.name,
            supplier_id=supplier.id,
            created_by_id=current_user.id,
        )
        db.session.add(doc)
        db.session.flush()

        matched = 0
        unmatched_names = []
        for row in invoice.rows:
            item = _find_nomenclature_for_invoice_row(row)
            if not item:
                unmatched_names.append(row["name"])
                continue
            db.session.add(
                ReceivingLine(
                    document_id=doc.id,
                    nomenclature_id=item.id,
                    qty=row["qty"],
                    expected_qty=row["qty"],
                )
            )
            matched += 1
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("Не удалось создать документ приемки из накладной")
        flash(
            "Файл разобран, но не удалось создать документ приемки — попробуйте еще раз "
            "или обратитесь к администратору",
            "danger",
        )
        return redirect(url_for("receiving.import_invoice_form"))

    message = f"Накладная № {doc.number} загружена: {matched} поз. от «{supplier.name}»"
    if unmatched_names:
        shown = ", ".join(unmatched_names[:5])
        more = "…" if len(unmatched_names) > 5 else ""
        message += (
            f". Не найдено в номенклатуре по названию/штрихкоду: {len(unmatched_names)} поз. "
            f"({shown}{more}) — добавьте их в документ вручную"
        )
    flash(message, "warning" if unmatched_names else "success")
    return redirect(url_for("receiving.confirm_invoice", doc_id=doc.id))


@bp.route("/<int:doc_id>/confirm")
def confirm_invoice(doc_id):
    """Упрощенная мобильная форма приемки по загруженной накладной —
    специально без коробов/ячеек и прочих возможностей обычной приемки:
    список товаров, ожидаемое количество, поле для фактического и галочка
    "принято", чтобы кладовщику было удобно свериться с телефона."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    lines = doc.lines.order_by(ReceivingLine.id).all()
    confirmed_count = sum(1 for line in lines if line.confirmed)
    return render_template(
        "receiving/confirm_invoice.html", doc=doc, lines=lines, confirmed_count=confirmed_count
    )


@bp.route("/<int:doc_id>/lines/<int:line_id>/confirm", methods=["POST"])
def confirm_line(doc_id, line_id):
    """AJAX: сохраняет фактическое количество и отметку "принято" для одной
    строки накладной — без перезагрузки страницы, чтобы сверка на телефоне
    шла быстро, строка за строкой."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    data = request.get_json(silent=True) or {}
    try:
        qty = float(data.get("qty"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректное количество"}), 400
    if qty < 0:
        return jsonify({"ok": False, "error": "Количество не может быть отрицательным"}), 400

    if line.box_id:
        box_item = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=line.nomenclature_id).first()
        if box_item:
            box_item.qty += qty - line.qty
            if box_item.qty <= 0:
                db.session.delete(box_item)

    line.qty = qty
    line.confirmed = bool(data.get("confirmed"))
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "confirmed_count": doc.lines.filter_by(confirmed=True).count(),
            "total_count": doc.lines.count(),
        }
    )


@bp.route("/<int:doc_id>")
def detail(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    lines = doc.lines.all()

    # "Активный" короб — как и в Размещении: выбирается сканированием/
    # созданием и хранится в адресе страницы (?box=ID), чтобы дальше
    # сканировать в него товар штрихкод за штрихкодом. Пока короб не выбран,
    # приемка работает как раньше — просто по количеству, в неразмещенный
    # остаток.
    active_box = None
    active_box_id = request.args.get("box", type=int)
    if active_box_id:
        active_box = Box.query.filter_by(id=active_box_id, warehouse_id=doc.warehouse_id).first()

    packed_box_ids = {line.box_id for line in lines if line.box_id}
    packed_boxes = (
        Box.query.filter(Box.id.in_(packed_box_ids)).all() if packed_box_ids else []
    )

    return render_template(
        "receiving/detail.html",
        doc=doc,
        lines=lines,
        active_box=active_box,
        packed_boxes=packed_boxes,
    )


def _add_or_increment_line(doc, nomenclature, qty):
    line = ReceivingLine(document_id=doc.id, nomenclature_id=nomenclature.id, qty=qty)
    db.session.add(line)
    db.session.commit()
    return line


def _box_category_warning(box, item):
    """Если суммарное количество товара ВИДА item в этом коробе превысило
    порог, заданный у вида (см. ProductCategory.box_qty_warning, настройка —
    «Производство» → «Настройки») — предупреждение для приемщика: похоже
    на лишний скан/ошибку, а не настоящую такую партию в одном коробе.
    None — вид не задан, порог не настроен, либо порог еще не превышен."""
    category = item.category
    if not category or not category.box_qty_warning:
        return None

    total = (
        db.session.query(func.sum(BoxItem.qty))
        .join(Nomenclature, BoxItem.nomenclature_id == Nomenclature.id)
        .filter(BoxItem.box_id == box.id, Nomenclature.category_id == category.id)
        .scalar()
    ) or 0
    if total <= category.box_qty_warning:
        return None
    return {"category": category.name, "qty": total, "threshold": category.box_qty_warning}


def _receive_item_into_box(doc, box, item, qty):
    """Приемка сразу в короб — товар физически упаковывается в момент
    приемки, минуя неразмещенный остаток (см. complete(): строки с box_id
    в него не идут)."""
    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    if box_item:
        box_item.qty += qty
    else:
        box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(box_item)

    line = ReceivingLine(document_id=doc.id, nomenclature_id=item.id, qty=qty, box_id=box.id)
    db.session.add(line)
    db.session.commit()
    warning = _box_category_warning(box, item)
    return line, warning


@bp.route("/<int:doc_id>/boxes/select", methods=["POST"])
def select_box(doc_id):
    """Сканирование/ввод номера короба — берем любой открытый короб этого
    склада, в т.ч. заготовленный заранее массовым созданием и печатью."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    box_number = request.form.get("box_number", "").strip()
    box = Box.find_by_scanned_code(box_number, warehouse_id=doc.warehouse_id)
    if not box:
        flash(f"Короб '{box_number}' не найден на складе «{doc.warehouse.name}»", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    box.mark_scanned(current_user)
    db.session.commit()

    item_count = box.items.count()
    if item_count > 0:
        # Не блокируем — короб мог использоваться раньше (перемещением,
        # предыдущей приемкой) и это ожидаемо, просто предупреждаем, чтобы
        # не спутать с другим коробом по ошибке.
        flash(
            f"В коробе {box.box_number} уже есть товар ({item_count} поз.) — "
            f"отсканированный товар добавится туда же.",
            "info",
        )
    return redirect(url_for("receiving.detail", doc_id=doc.id, box=box.id))


@bp.route("/<int:doc_id>/boxes/create", methods=["POST"])
def create_box(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    box = Box(box_number=next_number("box"), warehouse_id=doc.warehouse_id, status="open")
    db.session.add(box)
    db.session.commit()
    flash(f"Короб {box.box_number} создан", "success")
    return redirect(url_for("receiving.detail", doc_id=doc.id, box=box.id))


@bp.route("/<int:doc_id>/boxes/<int:box_id>/lines/add-by-barcode", methods=["POST"])
def add_line_to_box_by_barcode(doc_id, box_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if not box:
        return jsonify({"ok": False, "error": "Короб не найден"}), 404

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = float((request.json or {}).get("qty", 1) or 1)
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, warning = _receive_item_into_box(doc, box, item, qty)
    resp = {
        "ok": True,
        "line": {"id": line.id, "name": item.name, "sku": item.sku, "qty": line.qty},
        "box_item_count": box.items.count(),
    }
    if warning:
        resp["warning"] = warning
    return jsonify(resp)


@bp.route("/<int:doc_id>/boxes/<int:box_id>/lines/add", methods=["POST"])
def add_line_to_box(doc_id, box_id):
    """Ручное добавление товара в активный короб (поиск "содержит")."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    box = Box.query.filter_by(id=box_id, warehouse_id=doc.warehouse_id).first()
    if doc.status != "draft" or not box:
        flash("Документ уже завершен или короб не найден", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id, box=box_id))

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 1
    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id, box=box_id))

    _line, warning = _receive_item_into_box(doc, box, item, qty)
    flash(f"В короб {box.box_number} добавлено: {item.name} ({qty} {item.unit})", "success")
    if warning:
        flash(
            f"В коробе {box.box_number} уже {warning['qty']:g} шт. вида «{warning['category']}» "
            f"— больше обычного порога ({warning['threshold']:g}). Проверьте, не попало ли лишнее.",
            "warning",
        )
    # Сбрасываем активный короб — оператор сразу готов сканировать следующий
    # короб; чтобы добавить что-то еще в этот же короб, достаточно
    # отсканировать его номер повторно (find_by_scanned_code найдет его
    # независимо от того, был ли он активен только что).
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/lines/add-by-barcode", methods=["POST"])
def add_line_by_barcode(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        return jsonify({"ok": False, "error": "Документ уже завершен"}), 400

    barcode = (request.json or {}).get("barcode", "").strip()
    qty = (request.json or {}).get("qty", 1) or 1
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line = _add_or_increment_line(doc, item, float(qty))
    return jsonify(
        {
            "ok": True,
            "line": {"id": line.id, "name": item.name, "sku": item.sku, "qty": line.qty},
        }
    )


@bp.route("/<int:doc_id>/lines/add", methods=["POST"])
def add_line(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    # Ту же форму используют и обычная приемка (receiving/detail.html), и
    # мобильная сверка по накладной (receiving/confirm_invoice.html) — для
    # товара, которого не было в накладной. Возвращаем туда же, откуда
    # пришли.
    next_page = request.form.get("next")
    redirect_url = (
        url_for("receiving.confirm_invoice", doc_id=doc.id)
        if next_page == "confirm"
        else url_for("receiving.detail", doc_id=doc.id)
    )

    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(redirect_url)

    nomenclature_id = request.form.get("nomenclature_id", type=int)
    qty = request.form.get("qty", type=float) or 1

    item = Nomenclature.query.get(nomenclature_id)
    if not item:
        flash("Товар не найден", "danger")
        return redirect(redirect_url)

    _add_or_increment_line(doc, item, qty)
    flash(f"Добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(redirect_url)


@bp.route("/<int:doc_id>/lines/<int:line_id>/update", methods=["POST"])
def update_line(doc_id, line_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    qty = request.form.get("qty", type=float)
    if qty is None or qty <= 0:
        flash("Укажите корректное количество", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    if line.box_id:
        box_item = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=line.nomenclature_id).first()
        if box_item:
            box_item.qty += qty - line.qty
            if box_item.qty <= 0:
                db.session.delete(box_item)

    line.qty = qty
    db.session.commit()
    flash(f"Количество обновлено: {line.nomenclature.name} — {qty} {line.nomenclature.unit}", "success")
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/lines/<int:line_id>/delete", methods=["POST"])
def delete_line(doc_id, line_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    if line.box_id:
        box_item = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=line.nomenclature_id).first()
        if box_item:
            box_item.qty -= line.qty
            if box_item.qty <= 0:
                db.session.delete(box_item)
    db.session.delete(line)
    db.session.commit()
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/delete", methods=["POST"])
def delete_document(doc_id):
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Можно удалить только черновик — завершенный документ уже повлиял на остатки", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    flash(f"Документ приемки {number} удален", "success")
    return redirect(url_for("receiving.list_documents"))


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if doc.lines.count() == 0:
        flash("В документе нет позиций", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    for line in doc.lines:
        if line.box_id:
            # Уже физически упаковано в короб во время приемки — минуя
            # неразмещенный остаток. Короб останется без ячейки, пока его
            # не разместят обычным способом через «Размещение».
            continue
        UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, line.qty)

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(
        f"Приемка {doc.number} завершена. Товар без короба зачислен в неразмещенный остаток "
        f"склада «{doc.warehouse.name}» — разместите его в короба и ячейки через «Размещение». "
        f"Товар, упакованный в короб прямо при приемке, останется в коробе — его нужно только "
        f"расставить по ячейкам.",
        "success",
    )
    return redirect(url_for("receiving.detail", doc_id=doc.id))


@bp.route("/<int:doc_id>/export.xlsx")
def export_document(doc_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    data = export_receiving_to_excel([doc])
    fname = f"{doc.number}_{timestamp_for_filename()}.xlsx"
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )
