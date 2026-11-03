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
    def item_page(self, url):
        return {"uploaded_at": None, "source": "none", "description": ""}


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
    bot = Bot({"feeds": [{"name": "t"}], "filters": {"adult_catalogs": []}}, client=client,
              store=Store(tmp_path / "t.db"),
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
    assert measure_demand(6, 60, 10, t).level == "hot"        # 36 likes/h
    assert measure_demand(3, 10, 5, t).level == "warm"        # 3 likes in 5 min: promising, not proof
    assert measure_demand(3, 10, 10, t).level == "warm"       # 18 likes/h, not judged yet
    assert measure_demand(0, 5, 5, t).level == "normal"       # too early to judge
    assert measure_demand(3, 10, None, t).level == "unknown"  # age unknown
    # The user's rules: 5 likes in an hour, or 10 likes in an hour, is not a deal.
    assert measure_demand(5, 100, 60, t).level == "cold"
    assert measure_demand(10, 100, 60, t).level == "cold"
    assert measure_demand(4, 0, 60, t).level == "cold"        # the Arc'teryx: 4 likes in 60 min
    assert measure_demand(2, 10, 20, t).level == "cold"       # 6 likes/h after 20 min
    # Lots of views but nobody likes it -> cold even if the rate looks OK
    assert measure_demand(6, 200, 15, t).level == "cold"
    # 1 like within 20 s must not count as 180/h
    assert measure_demand(1, 2, 0.3, t).likes_per_hour == 20
    assert measure_demand(1, 2, 0.3, t).level == "normal"
    # Being "2x usual for this brand" no longer makes an item hot
    assert measure_demand(4, 0, 60, t, category_rate=1.8, category_samples=8).level == "cold"


def test_hot_demand_relaxes_margin():
    from botvin.scorer import measure_demand
    item = make("Kurtka Columbia", 28.0, "Columbia")      # ~45 resale, profit ~13.5
    est = model.estimate(item, when=NOV)
    t = Thresholds(min_profit=15)
    assert not evaluate(item, est, t, measure_demand(0, 5, 10, t)).is_deal
    v = evaluate(item, est, t, measure_demand(6, 60, 10, t))
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
    bot = Bot({"feeds": [{"name": "t"}], "thresholds": {"min_profit": 15},
               "filters": {"adult_catalogs": []}}, client=client,
              store=store, notifier=notifier, model=PriceModel())
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    assert notifier.sent == []
    # pretend 10 minutes passed since first sighting
    store.db.execute("UPDATE tracking SET first_seen = first_seen - 600 WHERE id=7")
    bot.poll_feed({"name": "t"})
    bot.poll_feed({"name": "t"})
    assert notifier.sent == [(7, True)]


def test_item_page_upload_time():
    from botvin.itempage import parse_item_page
    now = 1_790_000_000.0
    html_rel = '<div><span>L · Labai gera · Oakley · Įkelta prieš 19 val.</span></div>'
    r = parse_item_page(html_rel, now)
    assert r["source"] == "relative text" and (now - r["uploaded_at"]) == 19 * 3600
    html_json = ('<meta property="og:description" content="Stan dobry, uszkodzony zamek.">'
                 '<script>self.__next_f.push([1,"{\\"created_at_ts\\":%d,\\"updated_at_ts\\":%d}"])</script>'
                 % (now - 1500, now - 60))
    r = parse_item_page(html_json, now)
    assert r["uploaded_at"] == now - 1500 and r["source"] == "created_at_ts"
    assert r["description"] == "Stan dobry, uszkodzony zamek."
    assert parse_item_page("<p>nothing here</p>", now)["uploaded_at"] is None


def test_oakley_known():
    assert model.match_brand(make("Bluza Oakley", 19.94, "Oakley"))[0] == "Oakley"


def test_title_brand_stuffing_ignored_when_brand_field_set():
    it = make("buty trekingowe aku goretex damskie 37 lowa scarpa merrell mammut", 17, "AKU")
    assert model.match_brand(it)[0] is None
    # ...but an empty brand field still falls back to the title
    assert model.match_brand(make("Kurtka Columbia", 15, ""))[0] == "Columbia"


def test_latvian_estonian_categories():
    assert model.detect_category("The North Face silta flīsa jaka ar kapuci") == "fleece"
    assert model.detect_category("Columbia talvejope") == "puffer"
    assert model.detect_category("Patagonia jope") == "jacket"
    it = make("The North Face silta flīsa jaka ar kapuci", 20, "The North Face",
              desc="Youth/Junior XL izmērs.")
    assert model.estimate(it, when=NOV).excluded == "kids item"


@pytest.mark.parametrize("size,reason", [
    ("140 cm", "kids size"), ("152 cm / 11-12 m.", "kids size"), ("10 metų", "kids size"),
    ("12 years", "kids size"), ("10 ans / 140 cm", "kids size"), ("18-24 mois", "kids size"),
    ("6XL", "extreme size"), ("8 lat", "kids size"), ("18 mėn.", "kids size"),
    ("XXL", "extreme size"), ("XXL / 44 / 16", "extreme size"), ("3XL", "extreme size"),
    ("XS", "extreme size"), ("XXS / 32 / 4", "extreme size"), ("XXXL", "extreme size"),
])
def test_bad_sizes_excluded(size, reason):
    est = model.estimate(make("Kurtka Columbia", 10, "Columbia", size=size), when=NOV)
    assert est.excluded and est.excluded.startswith(reason), est.excluded


@pytest.mark.parametrize("size", ["S", "M", "L", "XL", "L / 40 / 12", "M / 38 / 10", "One size", ""])
def test_normal_sizes_kept(size):
    est = model.estimate(make("Kurtka Columbia", 10, "Columbia", size=size), when=NOV)
    assert est.excluded is None, est.excluded


def test_kids_words_and_shoe_sizes():
    assert model.estimate(make("Kurtka Columbia chłopięca", 10, "Columbia"), when=NOV).excluded
    shoe = lambda size: model.estimate(make("Nike Air Max 90", 30, "Nike", size=size), when=NOV).excluded
    for ok in ("36", "38", "41.5", "43", "44.5", "47", "EU 44", "US 10 / EU 44", "44 2/3"):
        assert shoe(ok) is None, ok
    for bad in ("33", "35", "47.5", "48", "EU 34"):
        assert shoe(bad), bad
    # "baby blue" is a colour, not a kids' item
    assert model.estimate(make("Ralph Lauren baby blue sweater", 10, "Ralph Lauren"), when=NOV).excluded is None


def test_filters_can_be_turned_off():
    m = PriceModel(filters={"exclude_kids": False, "excluded_sizes": []})
    assert m.estimate(make("Kurtka Columbia", 10, "Columbia", size="XXL"), when=NOV).excluded is None
    assert m.estimate(make("Kurtka Columbia", 10, "Columbia", size="140 cm"), when=NOV).excluded is None


def test_feeds_limited_to_adult_catalogs(tmp_path):
    calls = []
    class C(FakeClient):
        def __init__(self, unfiltered):
            self.unfiltered = unfiltered
        def search(self, **kw):
            calls.append(kw.get("catalog_ids"))
            return self.unfiltered if kw.get("catalog_ids") is None else []
    # Quiet search: empty both ways -> keep the filter.
    bot = Bot({"feeds": [{"name": "t"}]}, client=C([]), store=Store(tmp_path / "a.db"),
              notifier=FakeNotifier(), model=PriceModel())
    bot.poll_feed({"name": "t"}); bot.poll_feed({"name": "t"})
    assert calls == [[5, 1904], None, [5, 1904], None]
    # Filter is the problem (results only without it) -> drop it for this feed.
    calls.clear()
    bot = Bot({"feeds": [{"name": "t"}]}, client=C([make("Kurtka Columbia", 15, "Columbia")]),
              store=Store(tmp_path / "b.db"), notifier=FakeNotifier(), model=PriceModel())
    bot.poll_feed({"name": "t"}); bot.poll_feed({"name": "t"})
    assert calls == [[5, 1904], None, None]


def test_dolce_gabbana_and_margiela():
    assert model.match_brand(make("x", 10, "Dolce & Gabbana"))[0] == "Dolce & Gabbana"
    assert model.match_brand(make("Marškinėliai D&G", 10, ""))[0] == "Dolce & Gabbana"
    assert model.match_brand(make("x", 10, "Maison Margiela"))[0] == "Maison Margiela"
    # Margiela "Replica" sneakers are a real line, not a fake...
    est = model.estimate(make("Maison Margiela Replica sneakers", 60, "Maison Margiela", size="43"), when=NOV)
    assert est.excluded is None and est.category == "shoes"
    # ...but "replica" still flags fakes for other brands.
    assert model.estimate(make("Nike Air Max replica", 30, "Nike", size="43"), when=NOV).excluded == "fake keywords"


def test_lithuanian_shirt_vs_tshirt():
    assert model.detect_category("Dolce Gabbana marškiniai") == "shirt"
    assert model.detect_category("Nike marškinėliai") == "tshirt"


class PageClient(FakeClient):
    """Feed returns `batches`; item pages return whatever `pages[id]` says."""
    def __init__(self, batches, pages):
        super().__init__(batches)
        self.pages = pages
    def item_page(self, url):
        item_id = int(url.rsplit("/", 1)[1])
        return {"uploaded_at": None, "source": "none", "description": "",
                "likes": None, "views": None, "sold": False, **self.pages.get(item_id, {})}


def _watch_bot(tmp_path, client, notifier, **thr):
    import botvin.pricing as p
    return Bot({"feeds": [{"name": "t"}], "thresholds": {"min_profit": 15, **thr},
                "filters": {"adult_catalogs": []}}, client=client,
               store=Store(tmp_path / "w.db"), notifier=notifier, model=PriceModel())


def _age(store, item_id, minutes):
    """Pretend the item was first seen `minutes` ago and is due for a check."""
    store.db.execute("UPDATE tracking SET first_seen=?, next_check=? WHERE id=?",
                     (time.time() - minutes * 60, time.time() - 1, item_id))
    store.commit()


def test_watchlist_alerts_when_item_heats_up(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None: 1.25)
    item = make("Kurtka Columbia", 28.0, "Columbia", id_=11)   # profit ~13.5: not a deal yet
    item.uploaded_at = None
    pages = {}
    client, notifier = PageClient([[], [item]], pages), FakeNotifier()
    bot = _watch_bot(tmp_path, client, notifier)
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    assert notifier.sent == []
    assert bot.store.tracking(11)["next_check"] is not None      # on the watchlist
    pages[11] = {"likes": 7, "views": 60}                         # 7 likes in 12 min = 35/h
    _age(bot.store, 11, 12)
    assert bot.check_watchlist() == 1
    assert notifier.sent == [(11, True)]


def test_watchlist_confirms_alerted_deal_once(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None: 1.25)
    item = make("Kurtka Columbia", 15.13, "Columbia", id_=12)
    item.uploaded_at = None
    pages = {}
    notifier = FakeNotifier()
    sent_kinds = []
    notifier.send = lambda it, est, v, heating_up=False, confirmed=False: sent_kinds.append(
        (it.id, "confirmed" if confirmed else "heating" if heating_up else "new"))
    bot = _watch_bot(tmp_path, PageClient([[], [item]], pages), notifier)
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    assert sent_kinds == [(12, "new")]
    pages[12] = {"likes": 6, "views": 50}
    _age(bot.store, 12, 10)
    bot.check_watchlist()
    _age(bot.store, 12, 16)
    bot.check_watchlist()
    assert sent_kinds == [(12, "new"), (12, "confirmed")]


def test_watchlist_low_demand_never_alerts_and_sold_is_recorded(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None: 1.25)
    item = make("Kurtka Columbia", 28.0, "Columbia", id_=13)
    item.uploaded_at = None
    pages = {13: {"likes": 2, "views": 40}}                        # 2 likes in 30 min: cold
    notifier = FakeNotifier()
    bot = _watch_bot(tmp_path, PageClient([[], [item]], pages), notifier)
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    _age(bot.store, 13, 30)
    bot.check_watchlist()
    assert notifier.sent == []
    pages[13] = {"likes": 2, "views": 40, "sold": True}
    _age(bot.store, 13, 40)
    bot.check_watchlist()
    row = bot.store.tracking(13)
    assert row["sold_at"] is not None and row["next_check"] is None
    assert bot.store.sell_through("Columbia", "jacket") == (1, 1)


def test_item_page_likes_scoped_to_item():
    from botvin.itempage import parse_item_page
    page = ('<script>{"item":{"id":555,"favourite_count":9,"view_count":120,"is_closed":false}}'
            + "x" * 9000 +
            '{"similar":[{"id":777,"favourite_count":300,"is_closed":true}]}</script>')
    r = parse_item_page(page, now=time.time(), item_id=555)
    assert (r["likes"], r["views"], r["sold"]) == (9, 120, False)


def test_brand_mismatch_in_title():
    emu = make("Šilti, aukšti Emu batai", 6.72, "UGG", size="43")
    assert model.estimate(emu, when=NOV).excluded == "says 'emu', not UGG"
    for title in ("Batai a la UGG", "UGG style boots", "Kurtka typu Moncler", "Striukė kaip Arcteryx",
                  "Kurtka w stylu Stone Island"):
        brand = model.match_brand(make(title, 10, ""))[0]
        assert model.estimate(make(title, 10, brand or "", size="43"), when=NOV).excluded, title
    # genuine items are untouched ("Emu" must be a word, not part of one)
    assert model.estimate(make("UGG Classic Tall batai", 30, "UGG", size="43"), when=NOV).excluded is None
    assert model.estimate(make("Moncler Maya puffer", 90, "Moncler"), when=NOV).excluded is None


def test_adidas_needs_a_really_great_deal():
    t = Thresholds()
    # Adidas shoes resell ~45: 15 EUR would be a deal for most brands, not for Adidas.
    ok = make("Adidas Samba", 15, "Adidas", size="43")
    v = evaluate(ok, model.estimate(ok, when=NOV), t)
    assert not v.is_deal and any("profit" in r for r in v.reasons)
    # Same price for a Nike shoe (resale ~55) passes the normal bar.
    nike = make("Nike Air Max 90", 15, "Nike", size="43")
    assert evaluate(nike, model.estimate(nike, when=NOV), t).is_deal
    # A genuinely great Adidas deal (premium model, near-free) still alerts.
    great = make("Adidas Gore-Tex striukė", 2, "Adidas", size="L", status="Labai gera")
    assert evaluate(great, model.estimate(great, when=NOV), t).is_deal


def test_gloves_are_not_puffers():
    gloves = make("Rękawiczki zimowe the north face", 17.53, "The North Face", size="L")
    est = model.estimate(gloves, when=NOV)
    assert est.category == "accessory" and est.resale < 30
    assert not evaluate(gloves, est, Thresholds()).is_deal


def test_arcteryx_with_4_likes_in_an_hour_is_not_confirmed(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None: 1.25)
    item = make("Arcteryx jacket", 116.20, "Arc'teryx", id_=21, status="Labai gera")
    item.uploaded_at = None
    pages = {}
    notifier = FakeNotifier()
    kinds = []
    notifier.send = lambda it, est, v, heating_up=False, confirmed=False: kinds.append(
        "confirmed" if confirmed else "heating" if heating_up else "new")
    bot = _watch_bot(tmp_path, PageClient([[], [item]], pages), notifier)
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    pages[21] = {"likes": 4}
    _age(bot.store, 21, 60)
    bot.check_watchlist()
    assert "confirmed" not in kinds
