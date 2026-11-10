"""Диагностика: почему штрихкод не попадает в "Что нужно отправить" на
дашборде плана отгрузок, хотя остаток по нему как будто есть.

Запуск на сервере (из корня репозитория):
    python3 scripts/diagnose_barcode.py 2012962030009

Печатает:
  - точное совпадение по номенклатуре с таким штрихкодом; если точного
    совпадения нет — похожие (подстрока, то же число без учета ведущих
    нулей/пробелов) с repr(), чтобы увидеть побайтово даже невидимые
    символы;
  - остаток (в коробах + неразмещенный) по каждой найденной номенклатуре,
    по складам;
  - строки плана отгрузок с этим штрихкодом: площадка/город/план/факт/
    приоритет/новинка — и на какую именно номенклатуру они ссылаются
    (nomenclature_id=None значит точного совпадения не нашлось при
    загрузке плана)."""

import sys

sys.path.insert(0, ".")

from wms import create_app
from wms.extensions import db
from wms.models import Box, BoxItem, Nomenclature, ShipmentPlanLine, UnplacedStock, Warehouse


def _find_similar(barcode):
    candidates = Nomenclature.query.filter(Nomenclature.barcode.like(f"%{barcode}%")).all()
    # Запасной вариант, если барк0д в номенклатуре короче/длиннее искомого
    # (например, потерялся ведущий ноль при чтении Excel как числа) —
    # сравниваем числовое значение, а не подстроку.
    try:
        target_int = int(barcode)
    except ValueError:
        target_int = None
    if target_int is not None:
        existing_ids = {n.id for n in candidates}
        for n in Nomenclature.query.filter(Nomenclature.barcode.isnot(None)).all():
            if n.id in existing_ids:
                continue
            try:
                same_number = int(n.barcode.strip()) == target_int
            except (ValueError, AttributeError):
                same_number = False
            if same_number:
                candidates.append(n)
    return candidates


def _print_stock(n):
    print(f"\n=== Остаток по номенклатуре id={n.id}  barcode={n.barcode!r}  name={n.name!r} ===")
    rows = (
        db.session.query(UnplacedStock.warehouse_id, db.func.sum(UnplacedStock.qty))
        .filter(UnplacedStock.nomenclature_id == n.id)
        .group_by(UnplacedStock.warehouse_id)
        .all()
    )
    for warehouse_id, qty in rows:
        wh = Warehouse.query.get(warehouse_id)
        print(f"  неразмещенный (на разбраковке): склад {wh.name if wh else warehouse_id!r} — {qty}")

    rows = (
        db.session.query(Box.warehouse_id, db.func.sum(BoxItem.qty))
        .join(BoxItem, BoxItem.box_id == Box.id)
        .filter(BoxItem.nomenclature_id == n.id)
        .group_by(Box.warehouse_id)
        .all()
    )
    for warehouse_id, qty in rows:
        wh = Warehouse.query.get(warehouse_id)
        print(f"  в коробах (готово к отгрузке): склад {wh.name if wh else warehouse_id!r} — {qty}")


def main(barcode):
    app = create_app()
    with app.app_context():
        print(f"=== Номенклатура со штрихкодом {barcode!r} ===")
        noms = Nomenclature.query.filter_by(barcode=barcode).all()

        if noms:
            for n in noms:
                print(f"  id={n.id}  sku={n.sku!r}  name={n.name!r}  category_id={n.category_id}")
            if len(noms) > 1:
                print(
                    f"\n!!! Найдено {len(noms)} записей номенклатуры с одинаковым штрихкодом — "
                    "вероятная причина: план и остаток ссылаются на РАЗНЫЕ id."
                )
        else:
            print("НЕ НАЙДЕНО ни одной номенклатуры с ТОЧНО таким штрихкодом.")
            similar = _find_similar(barcode)
            if similar:
                print(
                    "Но нашлись похожие — сверьте repr() ниже посимвольно с искомым "
                    f"{barcode!r} (пробел/0 в начале/скрытый символ и т.п.):"
                )
                for n in similar:
                    print(f"  id={n.id}  barcode={n.barcode!r}  sku={n.sku!r}  name={n.name!r}")
                noms = similar
            else:
                print("  Похожих тоже не нашлось — номенклатуры с этим товаром в WMS нет вообще.")

        for n in noms:
            _print_stock(n)

        print(f"\n=== Строки плана отгрузок со штрихкодом {barcode!r} ===")
        lines = ShipmentPlanLine.query.filter_by(barcode=barcode).all()
        if not lines:
            print("НЕ НАЙДЕНО ни одной строки плана с таким штрихкодом.")
        for line in lines:
            plan = line.plan
            wh = line.warehouse
            print(
                f"  план={plan.marketplace if plan else '?'}  город={wh.marketplace_city if wh else '?'}  "
                f"nomenclature_id={line.nomenclature_id}  planned_qty={line.planned_qty}  "
                f"fulfilled_qty={line.fulfilled_qty}  priority={line.priority}  "
                f"novelty_marketplace={line.novelty_marketplace}  "
                f"distributed_target_qty={line.distributed_target_qty}  "
                f"remaining_qty={line.remaining_qty()}"
            )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python3 scripts/diagnose_barcode.py <штрихкод>")
        sys.exit(1)
    main(sys.argv[1])
