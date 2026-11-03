"""The five sold listings from the screenshots that motivated this bot.
Each one sold within ~30 minutes; the bot must flag all of them in November."""

import datetime as dt
import time

import pytest

from botvin.bot import Bot
from botvin.models import Item
from botvin.pricing import PriceModel
from botvin.scorer import Thresholds, evaluate
from botvin.storage import Store

NOV = dt.date(2026, 11, 3)
JUL = dt.date(2026, 7, 3)
model = PriceModel()


def make(title, total, brand="", size="L", status="Gera", desc="", fav=0, age_min=5, id_=1):
    return Item.from_api({
        "id": id_, "title": title, "price": {"amount": str(total - 1.5), "currency_code": "EUR"},
        "total_item_price": {"amount": str(total), "currency_code": "EUR"},
        "brand_title": brand, "size_title": size, "status": status, "description": desc,
        "favourite_count": fav, "url": f"/items/{id_}",
        "photo": {"url": "https://img/x.jpg", "high_resolution": {"timestamp": time.time() - age_min * 60}},
    })


EXAMPLES = [
    # title, total, brand field, condition, description
    ("Columbia kurtka zimowa", 19.94, "Columbia", "Gera", "Stan dobry, granatowa, noszona jeden sezon."),
    ("Kurtka Columbia", 15.13, "Columbia", "Gera", ""),
    ("Patagonian kevytuntuvatakki", 53.20, "Patagonia", "Labai gera", "Hyvässä kunnossa oleva Patagonian kevyt untuvatakki."),
    ("Kurtka męska The North Face L", 13.93, "The North Face", "Gera", "Stan dobry, uszkodzony zamek."),
    # Palace had no brand field and no brand in the title -- only visible in
    # the photo. With the brand typed in the title it must qualify:
    ("Palace gore-tex striukė L", 62.65, "", "Labai gera", "Nešiota vos keletą kartų."),
]


@pytest.mark.parametrize("title,total,brand,status,desc", EXAMPLES)
def test_screenshot_examples_are_deals_in_winter(title, total, brand, status, desc):
    item = make(title, total, brand, status=status, desc=desc)
    est = model.estimate(item, when=NOV)
    v = evaluate(item, est, Thresholds())
    assert v.is_deal, (est, v)


def test_damage_is_flagged_not_excluded():
    item = make("Kurtka męska The North Face L", 13.93, "The North Face", desc="uszkodzony zamek")
    est = model.estimate(item, when=NOV)
    assert "damage mentioned" in est.flags
    assert est.excluded is None


def test_categories_multilingual():
    assert model.detect_category("Patagonian kevytuntuvatakki") == "puffer"
    assert model.detect_category("Columbia kurtka zimowa") == "puffer"
    assert model.detect_category("Kurtka Columbia") == "jacket"
    assert model.detect_category("Labai geros būklės L dydžio striukė") == "jacket"
    assert model.detect_category("Nike Air Max 90") == "shoes"
    assert model.detect_category("Carhartt Detroit Daunenjacke") == "puffer"
    assert model.detect_category("Ralph Lauren sweter") == "sweater"


def test_seasonality_lowers_summer_value():
    item = make("Columbia kurtka zimowa", 19.94, "Columbia")
    assert model.estimate(item, when=JUL).resale < model.estimate(item, when=NOV).resale * 0.6


def test_fakes_and_unknown_brands_excluded():
    assert model.estimate(make("Moncler kurtka replica", 40, ""), when=NOV).excluded
    assert model.estimate(make("Kurtka zimowa", 10, "Zara"), when=NOV).excluded == "unknown brand"


def test_brand_alias_word_boundary():
    # "arab" must not match brand "Rab"; "boss" alone in title is fine.
    assert model.match_brand(make("Arabic style scarf", 5))[0] is None
    assert model.match_brand(make("Arcteryx Beta AR", 5))[0] == "Arc'teryx"


def test_not_a_deal_when_overpriced():
    item = make("Kurtka Columbia", 45.0, "Columbia")
    assert not evaluate(item, model.estimate(item, when=NOV), Thresholds()).is_deal


def test_old_listing_ignored():
    item = make("Kurtka Columbia", 15.13, "Columbia", age_min=300)
    v = evaluate(item, model.estimate(item, when=NOV), Thresholds())
    assert not v.is_deal and any("listed" in r for r in v.reasons)


class FakeClient:
    def __init__(self, batches):
        self.batches = batches
    def search(self, **kw):
        return self.batches.pop(0)
    def description(self, item_id):
        return ""


class FakeNotifier:
    def __init__(self):
        self.sent = []
    def send(self, item, est, v, heating_up=False):
        self.sent.append((item.id, heating_up))


def test_bot_alerts_only_on_new_items_after_first_sweep(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None: 1.25)
    old = make("Kurtka Columbia", 15.13, "Columbia", id_=1)
    new = make("Kurtka Columbia", 15.13, "Columbia", id_=2)
    junk = make("Zara jacket", 5, "Zara", id_=3)
    client = FakeClient([[old], [old, new, junk], [old, new, junk]])
    notifier = FakeNotifier()
    bot = Bot({"feeds": [{"name": "t"}]}, client=client, store=Store(tmp_path / "t.db"),
              notifier=notifier, model=PriceModel())
    bot.run(once=True)                     # first sweep: record only
    assert notifier.sent == []
    bot.poll_feed({"name": "t"})           # new item appears
    bot.poll_feed({"name": "t"})           # nothing new
    assert notifier.sent == [(2, False)]


def test_new_api_params_use_attribute_ids_and_drop_blanks():
    from botvin.vinted import build_params
    p = build_params("", brand_ids=[53, 88], status_ids=[2, 3], price_to=60)
    assert p["attribute_ids[brand]"] == "53,88"
    assert p["attribute_ids[status]"] == "2,3"
    assert "search_text" not in p and "price_from" not in p and "brand_ids" not in p
    assert p["price_to"] == 60 and p["order"] == "newest_first"


def test_item_box_fallback():
    it = Item.from_api({"id": 5, "title": "Kurtka", "price": {"amount": "10", "currency_code": "EUR"},
                        "url": "/items/5-kurtka",
                        "item_box": {"first_line": "Columbia", "second_line": "L · Gera"}})
    assert (it.brand, it.size, it.condition) == ("Columbia", "L", "good")
    assert it.url == "https://www.vinted.lt/items/5-kurtka" and it.uploaded_at is None


def test_demand_levels():
    from botvin.scorer import measure_demand
    t = Thresholds()
    assert measure_demand(5, 60, 10, t).level == "hot"        # 30 likes/h
    assert measure_demand(1, 10, 15, t).level == "warm"       # 4 likes/h
    assert measure_demand(0, 5, 20, t).level == "normal"
    assert measure_demand(3, 10, None, t).level == "unknown"  # age unknown
    # 1 like within 20 s must not count as 180/h
    assert measure_demand(1, 2, 0.3, t).likes_per_hour == 20
    assert measure_demand(1, 2, 0.3, t).level == "warm"
    # relative to category: 6/h when comparable items get 2/h -> hot
    assert measure_demand(3, 30, 30, t, category_rate=2.0, category_samples=8).level == "hot"


def test_hot_demand_relaxes_margin():
    from botvin.scorer import measure_demand
    item = make("Kurtka Columbia", 28.0, "Columbia")      # ~45 resale, profit ~13.5
    est = model.estimate(item, when=NOV)
    t = Thresholds(min_profit=15)
    assert not evaluate(item, est, t, measure_demand(0, 5, 10, t)).is_deal
    v = evaluate(item, est, t, measure_demand(6, 80, 10, t))
    assert v.is_deal and v.priority == "high"


def test_bot_alerts_when_existing_item_heats_up(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None: 1.25)
    # Profit ~13.5: not a deal at min_profit 15 with no likes, a deal once hot.
    cold = make("Kurtka Columbia", 28.0, "Columbia", id_=7, fav=0)
    cold.uploaded_at = None
    hot = make("Kurtka Columbia", 28.0, "Columbia", id_=7, fav=6)
    hot.uploaded_at = None
    client = FakeClient([[], [cold], [hot], [hot]])
    notifier = FakeNotifier()
    store = Store(tmp_path / "t.db")
    bot = Bot({"feeds": [{"name": "t"}], "thresholds": {"min_profit": 15}}, client=client,
              store=store, notifier=notifier, model=PriceModel())
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    assert notifier.sent == []
    # pretend 10 minutes passed since first sighting
    store.db.execute("UPDATE tracking SET first_seen = first_seen - 600 WHERE id=7")
    bot.poll_feed({"name": "t"})
    bot.poll_feed({"name": "t"})
    assert notifier.sent == [(7, True)]
