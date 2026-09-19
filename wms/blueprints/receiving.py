import io
import secrets
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
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    Box,
    BoxItem,
    Nomenclature,
    ReceivingDocument,
    ReceivingLine,
    Supplier,
    SupplierReturn,
    UnplacedStock,
    UnplacedStockLot,
    Warehouse,
)
from ..utils.document_access import get_owned_or_404
from ..utils.excel_io import export_receiving_to_excel, timestamp_for_filename
from ..utils.http import content_disposition
from ..utils.numbering import next_number
from ..utils.receiving_invoice_import import InvoiceParseError, parse_invoice

bp = Blueprint("receiving", __name__)


@bp.before_request
def _restrict_document_access():
    document_id = (request.view_args or {}).get("doc_id")
    if document_id is None:
        return None

    doc = ReceivingDocument.query.get_or_404(document_id)
    if current_user.can_view_invoice_receivings() and doc.is_from_invoice_import():
        # Раньше это правило действовало только на GET — право "видит все
        # приемки по накладным" выдается приемщику/зав. складом именно
        # чтобы они ВЕЛИ чужие приемки по накладным целиком (отправка на
        # пересчет/разбраковку, ввод количества и т.п.), а не только
        # смотрели на них. Без этого POST send-to-recount и все остальные
        # изменяющие действия на чужой приемке по накладной падали в 404
        # для всех, кроме автора и админа (см. get_owned_or_404 ниже) —
        # даже у тех, у кого есть это право.
        return None
    get_owned_or_404(ReceivingDocument, document_id)
    return None


def _visible_receiving_query():
    """Приемки, видимые пользователю в списке."""
    query = ReceivingDocument.query
    if current_user.is_admin:
        return query
    if current_user.can_view_invoice_receivings():
        return query.filter(
            or_(
                ReceivingDocument.created_by_id == current_user.id,
                ReceivingDocument.invoice_file_name.isnot(None),
            )
        )
    return query.filter(ReceivingDocument.created_by_id == current_user.id)


def _receiving_warehouses():
    """В приемке доступны только согласованные физические склады."""
    allowed_names = (
        "основной",
        "основной склад",
        "склад №2",
        "склад №2 (шоссейная 167)",
    )
    warehouses = (
        Warehouse.query.filter(
            Warehouse.is_active.is_(True),
            func.lower(func.trim(Warehouse.name)).in_(allowed_names),
        )
        .order_by(Warehouse.code)
        .all()
    )
    if warehouses:
        return warehouses
    # Обратная совместимость для старой базы, где физические склады могли
    # называться иначе: до переименования берем первые три по коду,
    # но склады-городá маркетплейсов в приемку никогда не попадают.
    return (
        Warehouse.query.filter_by(is_active=True, marketplace=None)
        .order_by(Warehouse.code)
        .limit(3)
        .all()
    )


def _receiving_warehouse_or_none(warehouse_id):
    if not warehouse_id:
        return None
    return next((wh for wh in _receiving_warehouses() if wh.id == warehouse_id), None)


def _next_redirect(doc_id):
    """Некоторые действия (переходы статуса, разбраковка) доступны и с
    обычной страницы приемки, и с мобильной сверки по накладной (см.
    add_line, тот же прием с next=confirm) — возвращаемся туда же, откуда
    пришли, вместо того чтобы всегда уводить на десктопную страницу."""
    if request.form.get("next") == "confirm":
        return redirect(url_for("receiving.confirm_invoice", doc_id=doc_id))
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/")
def list_documents():
    # "Приемка по накладной" — документы, созданные загрузкой файла
    # накладной (см. import_invoice_form): у них всегда проставлен
    # supplier_id (найденный/заведенный в справочник поставщик), в отличие
    # от ручного создания, где поставщик — просто свободный текст.
    unfinished_only = request.args.get("unfinished") == "on"
    invoice_only = request.args.get("invoice_only") == "on"
    supplier_q = request.args.get("supplier", "").strip()
    warehouse_id = request.args.get("warehouse_id", type=int)

    query = _visible_receiving_query()
    if unfinished_only:
        query = query.filter(ReceivingDocument.status != "completed")
    if invoice_only:
        query = query.filter(ReceivingDocument.supplier_id.isnot(None))
    if supplier_q:
        query = query.filter(ReceivingDocument.supplier.ilike(f"%{supplier_q}%"))
    if warehouse_id:
        query = query.filter(ReceivingDocument.warehouse_id == warehouse_id)

    documents = query.order_by(ReceivingDocument.created_at.desc()).all()

    # Возвраты и расхождения с накладной — одним batch-запросом сразу по
    # всем документам страницы, чтобы подсветить их в списке (не заходя в
    # каждый по отдельности), см. wms/templates/receiving/list.html.
    doc_ids = [d.id for d in documents]
    returns_count_by_doc = {}
    mismatch_doc_ids = set()
    total_qty_by_doc = {}
    if doc_ids:
        for doc_id, count in (
            db.session.query(SupplierReturn.receiving_document_id, db.func.count(SupplierReturn.id))
            .filter(SupplierReturn.receiving_document_id.in_(doc_ids))
            .group_by(SupplierReturn.receiving_document_id)
            .all()
        ):
            returns_count_by_doc[doc_id] = count
        mismatch_doc_ids = {
            row[0]
            for row in db.session.query(ReceivingLine.document_id)
            .filter(
                ReceivingLine.document_id.in_(doc_ids),
                ReceivingLine.expected_qty.isnot(None),
                ReceivingLine.expected_qty != ReceivingLine.qty,
            )
            .distinct()
            .all()
        }
        for doc_id, qty_sum in (
            db.session.query(ReceivingLine.document_id, db.func.sum(ReceivingLine.qty))
            .filter(ReceivingLine.document_id.in_(doc_ids))
            .group_by(ReceivingLine.document_id)
            .all()
        ):
            total_qty_by_doc[doc_id] = qty_sum or 0

    return render_template(
        "receiving/list.html",
        documents=documents,
        unfinished_only=unfinished_only,
        invoice_only=invoice_only,
        supplier_q=supplier_q,
        warehouse_id=warehouse_id,
        warehouses=_receiving_warehouses(),
        returns_count_by_doc=returns_count_by_doc,
        mismatch_doc_ids=mismatch_doc_ids,
        total_qty_by_doc=total_qty_by_doc,
    )


@bp.route("/returns")
def returns_list():
    """Возвраты поставщику (см. complete()/is_from_invoice_import) с
    видимостью, что из них уже забрала 1С (synced_to_1c_at, см.
    integration_1c.export_confirm), а что еще ждет выгрузки. Само
    подтверждение выгрузки ставит только интеграция — здесь только для
    чтения; ручное исключение из очереди (accounting_entered_at) делается
    отдельно, на странице integration_1c.pending."""
    unsynced_only = request.args.get("unsynced") == "on"

    query = SupplierReturn.query
    if not current_user.is_admin:
        query = query.join(ReceivingDocument)
        if current_user.can_view_invoice_receivings():
            query = query.filter(
                or_(
                    ReceivingDocument.created_by_id == current_user.id,
                    ReceivingDocument.invoice_file_name.isnot(None),
                )
            )
        else:
            query = query.filter(ReceivingDocument.created_by_id == current_user.id)
    if unsynced_only:
        query = query.filter(SupplierReturn.synced_to_1c_at.is_(None))

    returns = query.order_by(SupplierReturn.created_at.desc()).all()
    return render_template(
        "receiving/returns.html", returns=returns, unsynced_only=unsynced_only
    )


@bp.route("/new", methods=["GET", "POST"])
def new_document():
    if request.method == "GET":
        warehouses = _receiving_warehouses()
        return render_template("receiving/new.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    supplier = request.form.get("supplier", "").strip()

    warehouse = _receiving_warehouse_or_none(warehouse_id)
    if not warehouse:
        flash("Для приемки выберите один из разрешенных складов", "danger")
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
    if request.method == "GET":
        warehouses = _receiving_warehouses()
        return render_template("receiving/import_invoice.html", warehouses=warehouses)

    warehouse_id = request.form.get("warehouse_id", type=int)
    warehouse = _receiving_warehouse_or_none(warehouse_id)
    if not warehouse:
        flash("Для приемки выберите один из разрешенных складов", "danger")
        return redirect(url_for("receiving.import_invoice_form"))

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Выберите файл накладной (.xlsx)", "danger")
        return redirect(url_for("receiving.import_invoice_form"))

    order_number = request.form.get("order_number", "").strip()
    file_bytes = file.read()

    try:
        invoice = parse_invoice(io.BytesIO(file_bytes))
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
            order_number=order_number or None,
            invoice_file_data=file_bytes,
            invoice_file_name=file.filename,
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
        "receiving/confirm_invoice.html",
        doc=doc,
        lines=lines,
        confirmed_count=confirmed_count,
        add_request_token=secrets.token_urlsafe(24),
    )


@bp.route("/<int:doc_id>/invoice-file")
def download_invoice_file(doc_id):
    """Оригинал файла накладной, сохраненный при загрузке (см.
    import_invoice_form) — на случай спора с поставщиком или сверки с
    бухгалтерией задним числом."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if not doc.invoice_file_data:
        flash("Файл накладной для этого документа не сохранен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    fname = doc.invoice_file_name or f"{doc.number}.xlsx"
    return Response(
        doc.invoice_file_data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)},
    )


@bp.route("/<int:doc_id>/lines/<int:line_id>/confirm", methods=["POST"])
def confirm_line(doc_id, line_id):
    """AJAX: сохраняет фактическое количество и отметку "принято" для одной
    строки накладной — без перезагрузки страницы, чтобы сверка на телефоне
    шла быстро, строка за строкой."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    # draft — обычная сверка при вводе; recounting — исправление количества
    # по факту пересчета (см. send_to_recount).
    if doc.status not in ("draft", "recounting"):
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

    warehouses = _receiving_warehouses()

    return render_template(
        "receiving/detail.html",
        doc=doc,
        lines=lines,
        active_box=active_box,
        packed_boxes=packed_boxes,
        warehouses=warehouses,
        add_request_token=secrets.token_urlsafe(24),
    )


def _existing_add_request(request_token):
    if not request_token:
        return None
    return ReceivingLine.query.filter_by(request_token=request_token).first()


def _commit_receiving_add(line, request_token):
    """Коммитит добавление и превращает гонку одинаковых запросов в
    безопасный повтор. Уникальный индекс защищает даже два одновременных
    запроса, пришедших в разные процессы приложения."""
    try:
        db.session.commit()
        return line, False
    except IntegrityError:
        db.session.rollback()
        existing = _existing_add_request(request_token)
        if existing:
            return existing, True
        raise


def _add_or_increment_line(doc, nomenclature, qty, request_token=None):
    existing = _existing_add_request(request_token)
    if existing:
        return existing, True
    line = ReceivingLine(
        document_id=doc.id,
        nomenclature_id=nomenclature.id,
        qty=qty,
        request_token=request_token or None,
    )
    db.session.add(line)
    return _commit_receiving_add(line, request_token)


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


def _receive_item_into_box(doc, box, item, qty, request_token=None):
    """Приемка сразу в короб — товар физически упаковывается в момент
    приемки, минуя неразмещенный остаток (см. complete(): строки с box_id
    в него не идут).

    Если у этого же товара на этом складе уже есть неразмещенный остаток
    (висит с прошлой, еще не до конца размещенной приемки) — считаем, что
    физически это те же единицы, которые наконец кладут в короб, и
    списываем их со старого остатка вместо того, чтобы задваивать учет
    (остаток "висел" неразмещенным — и теперь еще и в коробе)."""
    existing = _existing_add_request(request_token)
    if existing:
        return existing, None, True

    dedup_qty = min(qty, UnplacedStock.available(doc.warehouse_id, item.id))
    if dedup_qty > 0:
        UnplacedStock.consume(doc.warehouse_id, item.id, dedup_qty)

    box_item = BoxItem.query.filter_by(box_id=box.id, nomenclature_id=item.id).first()
    if box_item:
        box_item.qty += qty
    else:
        box_item = BoxItem(box_id=box.id, nomenclature_id=item.id, qty=qty)
        db.session.add(box_item)

    line = ReceivingLine(
        document_id=doc.id,
        nomenclature_id=item.id,
        qty=qty,
        box_id=box.id,
        request_token=request_token or None,
    )
    db.session.add(line)
    line, duplicate = _commit_receiving_add(line, request_token)
    if duplicate:
        return line, None, True
    warning = _box_category_warning(box, item)
    return line, warning, False


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

    payload = request.json or {}
    barcode = payload.get("barcode", "").strip()
    qty = float(payload.get("qty", 1) or 1)
    request_token = str(payload.get("request_token", ""))[:64]
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, warning, duplicate = _receive_item_into_box(doc, box, item, qty, request_token)
    resp = {
        "ok": True,
        "duplicate": duplicate,
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

    request_token = request.form.get("request_token", "")[:64]
    _line, warning, duplicate = _receive_item_into_box(doc, box, item, qty, request_token)
    if duplicate:
        flash("Повторный запрос распознан — товар второй раз не добавлен", "info")
        return redirect(url_for("receiving.detail", doc_id=doc.id))
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

    payload = request.json or {}
    barcode = payload.get("barcode", "").strip()
    qty = payload.get("qty", 1) or 1
    request_token = str(payload.get("request_token", ""))[:64]
    item = Nomenclature.query.filter_by(barcode=barcode).first()
    if not item:
        return jsonify({"ok": False, "error": f"Товар со штрихкодом '{barcode}' не найден"}), 404

    line, duplicate = _add_or_increment_line(doc, item, float(qty), request_token)
    return jsonify(
        {
            "ok": True,
            "duplicate": duplicate,
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

    request_token = request.form.get("request_token", "")[:64]
    _line, duplicate = _add_or_increment_line(doc, item, qty, request_token)
    if duplicate:
        flash("Повторный запрос распознан — товар второй раз не добавлен", "info")
        return redirect(redirect_url)
    flash(f"Добавлено: {item.name} ({qty} {item.unit})", "success")
    return redirect(redirect_url)


@bp.route("/<int:doc_id>/lines/<int:line_id>/update", methods=["POST"])
def update_line(doc_id, line_id):
    doc = ReceivingDocument.query.get_or_404(doc_id)
    # draft — обычная правка при вводе; recounting — исправление количества
    # по факту пересчета (см. send_to_recount), если оно не сошлось с тем,
    # что внесли при приемке.
    if doc.status not in ("draft", "recounting"):
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    qty = request.form.get("qty", type=float)
    if qty is None or qty < 0:
        flash("Укажите корректное количество", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    if line.box_id:
        box_item = BoxItem.query.filter_by(box_id=line.box_id, nomenclature_id=line.nomenclature_id).first()
        if box_item:
            box_item.qty += qty - line.qty
            if box_item.qty <= 0:
                db.session.delete(box_item)

    line.qty = qty
    if doc.status == "recounting" and line.expected_qty is not None:
        line.confirmed = True
    db.session.commit()
    flash(f"Количество обновлено: {line.nomenclature.name} — {qty} {line.nomenclature.unit}", "success")
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/lines/update-bulk", methods=["POST"])
def update_lines_bulk(doc_id):
    """Сохранить количество сразу по всем строкам одной кнопкой — чтобы не
    перезагружать страницу после правки каждой отдельной строки (см.
    update_line). Поля формы — qty_<line_id>; строки без изменений и с
    некорректным значением просто пропускаются, без прерывания остальных."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status not in ("draft", "recounting"):
        flash("Документ уже завершен", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    updated = 0
    for line in doc.lines:
        raw = request.form.get(f"qty_{line.id}")
        if raw is None:
            continue
        try:
            qty = float(raw)
        except ValueError:
            continue
        if qty < 0:
            continue

        if doc.status == "recounting" and line.expected_qty is not None:
            line.confirmed = True
        if qty == line.qty:
            continue

        if line.box_id:
            box_item = BoxItem.query.filter_by(
                box_id=line.box_id, nomenclature_id=line.nomenclature_id
            ).first()
            if box_item:
                box_item.qty += qty - line.qty
                if box_item.qty <= 0:
                    db.session.delete(box_item)

        line.qty = qty
        updated += 1

    db.session.commit()
    if updated:
        flash(f"Количество обновлено: {updated} поз.", "success")
    else:
        flash("Изменений не найдено", "info")
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
    """Администратор может удалить приемку в ЛЮБОМ статусе. Черновик/
    пересчет/разбраковка еще не повлияли на остатки — удаляются как есть.
    Завершенная приемка уже зачислила неразмещенный остаток (см.
    complete()) — сначала отменяем этот эффект, точно так же, как при
    возврате на разбраковку (см. revert_to_sorting): убираем зачисленное
    и еще не выгруженные в 1С возвраты поставщику. Если часть остатка уже
    размещена в короба — как и там, откатить нельзя, документ не
    удаляется (иначе непонятно, какие физические единицы забирать назад)."""
    if not current_user.is_admin:
        flash("Удалять документы может только администратор", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    doc = ReceivingDocument.query.get_or_404(doc_id)
    synced_returns = 0

    if doc.status == "completed":
        lots = UnplacedStockLot.query.filter_by(receiving_document_id=doc.id).all()
        already_placed = [lot for lot in lots if lot.qty_remaining < lot.qty_received]
        if already_placed:
            names = ", ".join(sorted({lot.nomenclature.name for lot in already_placed}))
            flash(
                f"Нельзя удалить — товар уже частично размещен в короба: {names}",
                "danger",
            )
            return redirect(url_for("receiving.detail", doc_id=doc_id))

        for lot in lots:
            row = UnplacedStock.query.filter_by(
                warehouse_id=doc.warehouse_id, nomenclature_id=lot.nomenclature_id
            ).first()
            if row:
                row.qty = max(row.qty - lot.qty_remaining, 0)
            db.session.delete(lot)

        synced_returns = (
            SupplierReturn.query.filter_by(receiving_document_id=doc.id)
            .filter(SupplierReturn.synced_to_1c_at.isnot(None))
            .count()
        )
        SupplierReturn.query.filter_by(receiving_document_id=doc.id, synced_to_1c_at=None).delete()

    number = doc.number
    db.session.delete(doc)
    db.session.commit()
    message = f"Документ приемки {number} удален"
    if synced_returns:
        message += (
            f". Внимание: по ней уже выгружен(о) в 1С {synced_returns} возврат(ов) "
            f"поставщику — они не отменены, сверьте вручную."
        )
    flash(message, "warning" if synced_returns else "success")
    return redirect(url_for("receiving.list_documents"))


@bp.route("/<int:doc_id>/change-warehouse", methods=["POST"])
def change_warehouse(doc_id):
    """Исправление ошибочно выбранного склада — например, сразу после
    загрузки приходной накладной (см. import_invoice_form) заметили, что
    выбрали не тот склад. Доступно, пока приемка не завершена — после
    завершения склад уже зафиксирован в неразмещенном остатке и партиях
    (см. UnplacedStock/UnplacedStockLot), менять его задним числом нельзя.
    Короба, уже упакованные в рамках этой приемки, физически остаются на
    прежнем складе (у Box свой warehouse_id) — поэтому пока такие есть,
    сначала нужно разобраться с ними, иначе документ будет указывать на
    один склад, а короба физически лежать на другом."""
    from ..models import Warehouse

    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status == "completed":
        flash("Приемка уже завершена — склад изменить нельзя", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    packed_box_ids = {line.box_id for line in doc.lines if line.box_id}
    if packed_box_ids:
        flash(
            "В этой приемке уже есть товар, упакованный в короб — сменить склад нельзя, "
            "пока эти короба привязаны к приемке",
            "danger",
        )
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    warehouse_id = request.form.get("warehouse_id", type=int)
    warehouse = _receiving_warehouse_or_none(warehouse_id)
    if not warehouse:
        flash("Для приемки выберите один из разрешенных складов", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc_id))

    doc.warehouse_id = warehouse.id
    db.session.commit()
    flash(f"Склад приемки изменен на «{warehouse.name}»", "success")
    return redirect(url_for("receiving.detail", doc_id=doc_id))


@bp.route("/<int:doc_id>/send-to-recount", methods=["POST"])
def send_to_recount(doc_id):
    """draft -> recounting. Товар физически пересчитывают заново — на этом
    этапе можно поправить qty по строкам (см. update_line), если пересчет
    разошелся с тем, что внесли при самой приемке. Пересчет/разбраковка
    нужны только для приемок, загруженных из накладной (номер приемки —
    настоящий номер накладной, есть с чем сверять возврат в 1С); обычная
    приемка в короба завершается сразу из черновика, см. complete()."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if not doc.is_from_invoice_import():
        flash("Эта приемка не из накладной — пересчет и разбраковка ей не нужны, завершите ее сразу", "danger")
        return _next_redirect(doc.id)

    if doc.status != "draft":
        flash("Документ уже отправлен дальше по процессу", "danger")
        return _next_redirect(doc.id)

    if doc.lines.count() == 0:
        flash("В документе нет позиций", "danger")
        return _next_redirect(doc.id)

    doc.status = "recounting"
    doc.recounting_started_at = datetime.utcnow()
    db.session.commit()
    flash(f"Приемка {doc.number} отправлена на пересчет", "success")
    return _next_redirect(doc.id)


@bp.route("/<int:doc_id>/send-to-sorting", methods=["POST"])
def send_to_sorting(doc_id):
    """recounting -> sorting. Пересчет подтвержден — дальше выделяем брак
    построчно (см. update_defect)."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "recounting":
        flash("Документ не находится на пересчете", "danger")
        return _next_redirect(doc.id)

    doc.status = "sorting"
    doc.sorting_started_at = datetime.utcnow()
    db.session.commit()
    flash(f"Приемка {doc.number} отправлена на разбраковку", "success")
    return _next_redirect(doc.id)


@bp.route("/<int:doc_id>/lines/<int:line_id>/update-defect", methods=["POST"])
def update_defect(doc_id, line_id):
    """Выделение кол-ва брака построчно на этапе "Разбраковка". Обычным
    сотрудникам доступно только для приемок, загруженных из файла накладной
    (see ReceivingDocument.is_from_invoice_import) — иначе номер приемки не
    настоящий номер накладной, и 1С возврат не сопоставит; администратор
    может выделить брак в любом случае и завести возврат в 1С вручную."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "sorting":
        flash("Документ не находится на разбраковке", "danger")
        return _next_redirect(doc.id)

    if not doc.is_from_invoice_import() and not current_user.is_admin:
        flash(
            "Выделение брака доступно только для приемок, загруженных из накладной, "
            "либо администратору",
            "danger",
        )
        return _next_redirect(doc.id)

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    if line.box_id:
        flash("Товар уже упакован в короб при приемке — разбраковке не подлежит", "danger")
        return _next_redirect(doc.id)
    if line.line_completed_at is not None:
        flash("Строка уже завершена — брак больше нельзя поменять", "danger")
        return _next_redirect(doc.id)

    defect_qty = request.form.get("defect_qty", type=float) or 0
    if defect_qty < 0 or defect_qty > line.qty:
        flash("Кол-во брака не может быть отрицательным или больше принятого", "danger")
        return _next_redirect(doc.id)

    line.defect_qty = defect_qty
    # Строка из накладной, которую не трогали на пересчете (кол-во совпало
    # с накладной, расхождения не было) остается confirmed=False — и
    # complete() пропускает такую строку ЦЕЛИКОМ (см. комментарий там),
    # включая уже выделенный здесь брак: ни остаток, ни возврат поставщику
    # не создались бы, хотя человек только что явно поработал с этой
    # строкой. Выделение брака — такое же явное подтверждение принятого
    # qty, как и правка количества на пересчете (см. update_line), поэтому
    # тоже снимает пометку "не подтверждено".
    if line.expected_qty is not None:
        line.confirmed = True
    db.session.commit()
    return _next_redirect(doc.id)


def _credit_receiving_line(doc, line):
    """Зачисляет годное количество строки в неразмещенный остаток и, если
    есть брак, заводит возврат поставщику — общая логика для завершения
    приемки целиком (complete()) и по отдельной строке (complete_line()).
    Расхождение с накладной не является возвратом — SupplierReturn
    создается только из явно указанного defect_qty."""
    good_qty = line.good_qty()
    if good_qty > 0:
        UnplacedStock.add(doc.warehouse_id, line.nomenclature_id, good_qty, receiving_document=doc)
    if line.defect_qty:
        db.session.add(
            SupplierReturn(
                warehouse_id=doc.warehouse_id,
                nomenclature_id=line.nomenclature_id,
                qty=line.defect_qty,
                comment=f"Брак при разбраковке приемки {doc.number}",
                created_by_id=current_user.id,
                receiving_document_id=doc.id,
                supplier_name=doc.supplier,
                invoice_number=doc.number if doc.is_from_invoice_import() else None,
            )
        )


@bp.route("/<int:doc_id>/complete", methods=["POST"])
def complete(doc_id):
    """Приемка из накладной проходит пересчет/разбраковку и завершается из
    sorting (см. send_to_recount/send_to_sorting). Обычная приемка в короба
    статусов не имеет вообще — завершается сразу из черновика, как и до
    появления пересчета/разбраковки. Строки, уже завершенные по отдельности
    (см. complete_line — на разбраковке можно завершать построчно, не
    дожидаясь проверки остальных) пропускаются здесь, чтобы не зачислить их
    дважды — эта кнопка довершает только то, что еще не завершили."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.is_from_invoice_import():
        if doc.status != "sorting":
            flash("Сначала пройдите этапы «Пересчет» и «Разбраковка»", "danger")
            return _next_redirect(doc.id)
    elif doc.status != "draft":
        flash("Документ уже завершен", "danger")
        return _next_redirect(doc.id)

    for line in doc.lines:
        if line.box_id or line.line_completed_at is not None:
            # box_id — уже физически упаковано в короб во время приемки,
            # минуя неразмещенный остаток и разбраковку. line_completed_at —
            # уже завершено отдельно через "Готово" на этой же строке.
            continue
        # У строки из накладной qty до подтверждения равно заявленному
        # поставщиком количеству. Неподтвержденная позиция не является
        # фактически принятой и не должна создавать остаток на нашем складе.
        if line.expected_qty is not None and not line.confirmed:
            continue
        _credit_receiving_line(doc, line)
        line.line_completed_at = datetime.utcnow()

    doc.status = "completed"
    doc.completed_at = datetime.utcnow()
    db.session.commit()
    flash(
        f"Приемка {doc.number} завершена. Годный товар без короба зачислен в неразмещенный "
        f"остаток склада «{doc.warehouse.name}» — разместите его в короба и ячейки через "
        f"«Размещение». Товар, упакованный в короб прямо при приемке, останется в коробе — его "
        f"нужно только расставить по ячейкам.",
        "success",
    )
    return _next_redirect(doc.id)


@bp.route("/<int:doc_id>/lines/<int:line_id>/complete-line", methods=["POST"])
def complete_line(doc_id, line_id):
    """Завершает ОДНУ строку разбраковки по отдельности, не дожидаясь, пока
    проверят остальные строки документа (раньше приемку можно было
    завершить только целиком кнопкой "Завершить приемку" — см. complete()).
    Как только завершена последняя еще не завершенная строка, документ
    целиком переходит в completed — так же, как при обычном завершении."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if doc.status != "sorting":
        flash("Документ не находится на разбраковке", "danger")
        return _next_redirect(doc.id)

    if not doc.is_from_invoice_import() and not current_user.is_admin:
        flash(
            "Завершение строк по отдельности доступно только для приемок, загруженных "
            "из накладной, либо администратору",
            "danger",
        )
        return _next_redirect(doc.id)

    line = ReceivingLine.query.filter_by(id=line_id, document_id=doc_id).first_or_404()
    if line.box_id:
        flash("Товар уже упакован в короб — эта строка уже учтена", "danger")
        return _next_redirect(doc.id)
    if line.line_completed_at is not None:
        flash("Строка уже завершена", "danger")
        return _next_redirect(doc.id)
    if line.expected_qty is not None and not line.confirmed:
        flash("Сначала подтвердите фактическое количество на пересчете", "danger")
        return _next_redirect(doc.id)

    _credit_receiving_line(doc, line)
    line.line_completed_at = datetime.utcnow()

    remaining = [l for l in doc.lines if not l.box_id and l.line_completed_at is None]
    if not remaining:
        doc.status = "completed"
        doc.completed_at = datetime.utcnow()

    db.session.commit()

    if not remaining:
        flash(
            f"Строка «{line.nomenclature.name}» завершена — это была последняя, приемка "
            f"{doc.number} полностью завершена. Годный товар без короба зачислен в "
            f"неразмещенный остаток склада «{doc.warehouse.name}».",
            "success",
        )
    else:
        flash(f"Строка «{line.nomenclature.name}» завершена", "success")
    return _next_redirect(doc.id)


@bp.route("/<int:doc_id>/revert-to-sorting", methods=["POST"])
def revert_to_sorting(doc_id):
    """Админ может вернуть завершенную приемку на разбраковку задним числом
    (например, приемки, завершенные еще до появления пересчета/разбраковки,
    или ошиблись с браком). Безопасно, только пока НИЧЕГО из зачисленного
    по этой приемке остатка еще не размещено в короба — см.
    UnplacedStockLot.qty_remaining. Если часть уже разместили, откатить
    нельзя: непонятно, какие именно физические единицы из общего остатка
    (там могли уже перемешаться с другими партиями) забирать обратно."""
    doc = ReceivingDocument.query.get_or_404(doc_id)
    if not current_user.is_admin:
        flash("Вернуть приемку на разбраковку может только администратор", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if not doc.is_from_invoice_import():
        flash("Эта приемка не из накладной — разбраковки у нее не было, откатывать некуда", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    if doc.status != "completed":
        flash("Приемка не завершена", "danger")
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    lots = UnplacedStockLot.query.filter_by(receiving_document_id=doc.id).all()
    already_placed = [lot for lot in lots if lot.qty_remaining < lot.qty_received]
    if already_placed:
        names = ", ".join(sorted({lot.nomenclature.name for lot in already_placed}))
        flash(
            f"Нельзя вернуть на разбраковку — товар уже частично размещен в короба: {names}",
            "danger",
        )
        return redirect(url_for("receiving.detail", doc_id=doc.id))

    for lot in lots:
        row = UnplacedStock.query.filter_by(
            warehouse_id=doc.warehouse_id, nomenclature_id=lot.nomenclature_id
        ).first()
        if row:
            row.qty = max(row.qty - lot.qty_remaining, 0)
        db.session.delete(lot)

    # Возвраты, которые 1С уже забрала, отменить нельзя — предупреждаем и
    # оставляем как есть; неподтвержденные (еще не выгруженные) удаляем —
    # разбраковка сейчас пройдет заново и решит по браку заново.
    synced_returns = SupplierReturn.query.filter_by(
        receiving_document_id=doc.id
    ).filter(SupplierReturn.synced_to_1c_at.isnot(None)).count()
    SupplierReturn.query.filter_by(
        receiving_document_id=doc.id, synced_to_1c_at=None
    ).delete()

    # Строки, завершенные по отдельности (см. complete_line), тоже
    # откатываются — иначе после возврата на разбраковку они остались бы
    # помеченными "Готово" без возможности поправить брак или завершить
    # заново, хотя их зачисленный остаток/возврат уже отменены выше.
    for line in doc.lines:
        if not line.box_id:
            line.line_completed_at = None

    doc.status = "sorting"
    doc.completed_at = None
    db.session.commit()

    message = f"Приемка {doc.number} возвращена на разбраковку."
    if synced_returns:
        message += (
            f" Внимание: по ней уже выгружен(о) в 1С {synced_returns} возврат(ов) поставщику — "
            f"они не отменены, сверьте вручную."
        )
    flash(message, "warning" if synced_returns else "success")
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
