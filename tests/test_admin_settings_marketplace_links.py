"""Ссылки настроек маркетплейсов собраны на административной странице."""


def test_marketplace_sections_are_shown_in_settings(client_logged_in):
    response = client_logged_in.get("/users")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'href="/integrations/ozon/"' in html
    assert 'href="/integrations/wb/"' in html
    assert 'href="/marketplace-export/ozon-mapping"' in html
    assert "Обмен с Ozon" in html
    assert "Обмен с WB" in html
    assert "Артикулы Ozon" in html


def test_marketplace_sections_are_removed_from_top_navigation(client_logged_in):
    response = client_logged_in.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'href="/integrations/ozon/"' not in html
    assert 'href="/integrations/wb/"' not in html
    assert 'href="/marketplace-export/ozon-mapping"' not in html
