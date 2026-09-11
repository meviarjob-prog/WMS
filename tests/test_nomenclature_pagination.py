"""Список номенклатуры (nomenclature.list_nomenclature) — постраничный,
см. NOMENCLATURE_PAGE_SIZE в blueprints/nomenclature.py: без разбивки на
страницы каталог из тысяч позиций рендерил всё разом (в каждой строке еще
и полный select видов товара), из-за чего страница открывалась заметно
медленнее, чем должна."""

from wms.extensions import db
from wms.models import Nomenclature


def _make_items(n, prefix="PG"):
    for i in range(n):
        db.session.add(
            Nomenclature(
                sku=f"{prefix}-{i:04d}",
                barcode=f"{9000000000000 + i}",
                name=f"Товар {prefix} {i:04d}",
                unit="шт",
            )
        )
    db.session.commit()


def test_list_shows_only_one_page_worth_of_items(db, client_logged_in):
    _make_items(150)

    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)

    # Каждая позиция отрисовывается дважды — десктопной строкой и мобильной
    # карточкой (переключение между ними чисто на CSS), отсюда x2.
    assert html.count("Товар PG") == 100 * 2  # NOMENCLATURE_PAGE_SIZE
    assert "Стр. 1 из 2" in html


def test_list_second_page_shows_remaining_items(db, client_logged_in):
    _make_items(150)

    html = client_logged_in.get("/nomenclature/?page=2").get_data(as_text=True)

    assert html.count("Товар PG") == 50 * 2


def test_list_pagination_hidden_when_everything_fits_on_one_page(db, client_logged_in):
    _make_items(5)

    html = client_logged_in.get("/nomenclature/").get_data(as_text=True)

    assert "Стр." not in html
