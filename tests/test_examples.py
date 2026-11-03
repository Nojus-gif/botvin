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
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    old = make("Kurtka Columbia", 15.13, "Columbia", id_=1)
    new = make("Kurtka Columbia", 15.13, "Columbia", id_=2)
    junk = make("Zara jacket", 5, "Zara", id_=3)
    client = FakeClient([[old], [old, new, junk], [old, new, junk]])
    notifier = FakeNotifier()
    bot = Bot({"feeds": [{"name": "t"}], "filters": {"adult_catalogs": []}, "auto_brand_feeds": False}, client=client,
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
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    # Profit ~13.5: not a deal at min_profit 15 with no likes, a deal once hot.
    cold = make("Kurtka Columbia", 28.0, "Columbia", id_=7, fav=0)
    cold.uploaded_at = None
    hot = make("Kurtka Columbia", 28.0, "Columbia", id_=7, fav=6)
    hot.uploaded_at = None
    client = FakeClient([[], [cold], [hot], [hot]])
    notifier = FakeNotifier()
    store = Store(tmp_path / "t.db")
    bot = Bot({"feeds": [{"name": "t"}], "thresholds": {"min_profit": 15},
               "filters": {"adult_catalogs": []}, "auto_brand_feeds": False}, client=client,
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
    found = {}
    def find_item(self, item_id, title, price=None):
        return self.found.get(item_id)
    def item_page(self, url):
        item_id = int(url.rsplit("/", 1)[1])
        return {"uploaded_at": None, "source": "none", "description": "",
                "likes": None, "views": None, "sold": False, **self.pages.get(item_id, {})}


def _watch_bot(tmp_path, client, notifier, **thr):
    import botvin.pricing as p
    return Bot({"feeds": [{"name": "t"}], "thresholds": {"min_profit": 15, **thr},
                "filters": {"adult_catalogs": []}, "auto_brand_feeds": False}, client=client,
               store=Store(tmp_path / "w.db"), notifier=notifier, model=PriceModel())


def _age(store, item_id, minutes):
    """Pretend the item was first seen `minutes` ago and is due for a check."""
    store.db.execute("UPDATE tracking SET first_seen=?, next_check=? WHERE id=?",
                     (time.time() - minutes * 60, time.time() - 1, item_id))
    store.commit()


def test_watchlist_alerts_when_item_heats_up(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
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
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
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
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    item = make("Kurtka Columbia", 28.0, "Columbia", id_=13)
    item.uploaded_at = None
    pages = {13: {"likes": 2, "views": 40}}                        # 2 likes in 30 min: cold
    notifier = FakeNotifier()
    bot = _watch_bot(tmp_path, PageClient([[], [item]], pages), notifier)
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    bot.client.found = {13: make("Kurtka Columbia", 28.0, "Columbia", id_=13, fav=2)}   # still listed
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
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
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


def test_instant_brands_ugg_and_carhartt():
    ugg = make("Balti moteriški UGG batai ant storos platformos", 10.15, "UGG", size="39",
               status="Labai gera")
    est = model.estimate(ugg, when=NOV)
    # UGG was re-priced to the real market (lots of supply): not "instant",
    # but a pair for 10 EUR is still a clear deal.
    assert not est.instant and 35 <= est.resale <= 60, est
    v = evaluate(ugg, est, Thresholds())
    assert v.is_deal and v.priority == "high"
    det0 = make("Carhartt WIP Detroit Jacket", 51.19, "Carhartt", size="M", status="Labai gera")
    est0 = model.estimate(det0, when=NOV)
    from botvin.notify import format_message
    title, body = format_message(det0, est0, evaluate(det0, est0, Thresholds()))
    assert title.startswith("⚡") and "sells within minutes" in body

    det = make("Carhartt WIP Detroit Jacket M", 51.19, "Carhartt", size="M", status="Labai gera")
    est = model.estimate(det, when=NOV)
    assert est.resale >= 120 and evaluate(det, est, Thresholds()).is_deal


def test_auto_feeds_for_instant_brands(tmp_path):
    bot = Bot({"feeds": [{"name": "c", "search_text": "Carhartt"}]}, client=FakeClient([]),
              store=Store(tmp_path / "f.db"), notifier=FakeNotifier(), model=PriceModel())
    texts = [f["search_text"] for f in bot.feeds]
    assert texts.count("Carhartt") == 1 and "carhartt" not in texts   # not duplicated
    assert {"search_text": "ugg", "price_to": 20, "name": "auto-ugg"} in bot.feeds
    off = Bot({"feeds": [], "auto_brand_feeds": False}, client=FakeClient([]),
              store=Store(tmp_path / "g.db"), notifier=FakeNotifier(), model=PriceModel())
    assert all(not f["name"].startswith("auto-") for f in off.feeds)


def test_audience_pink_large_jacket_is_womens_and_uncommon():
    base = model.estimate(make("The North Face kurtka", 20, "The North Face", size="M"), when=NOV)
    pink_l = model.estimate(make("The North Face kurtka różowa", 20, "The North Face", size="L"), when=NOV)
    assert any("women's (by colour)" in f for f in pink_l.flags)
    assert any("size L is uncommon for women" in f for f in pink_l.flags)
    assert pink_l.resale < base.resale * 0.8


def test_audience_gender_from_words_and_page():
    # women's words: S/M fine, XL heavily discounted
    m = model.estimate(make("Moteriška Patagonia striukė", 30, "Patagonia", size="M"), when=NOV)
    xl = model.estimate(make("Moteriška Patagonia striukė", 30, "Patagonia", size="XL"), when=NOV)
    assert xl.resale < m.resale * 0.6
    # men's L is the sweet spot; men's item in pink is a narrow market
    men_l = model.estimate(make("Kurtka męska The North Face", 20, "The North Face", size="L"), when=NOV)
    men_pink = model.estimate(make("Kurtka męska The North Face różowa", 20, "The North Face", size="L"), when=NOV)
    assert men_pink.resale < men_l.resale * 0.8 and "pink men's item x0.75" in men_pink.flags
    # gender/colour from the item page beat guessing
    it = make("The North Face kurtka", 20, "The North Face", size="L")
    it.gender, it.colour = "women", "Rožinė"
    est = model.estimate(it, when=NOV)
    assert any("women's (listed)" in f for f in est.flags) and "colour: pink" in est.flags


def test_audience_shoe_sizes_by_gender():
    w39 = model.estimate(make("Moteriški UGG batai", 20, "UGG", size="39"), when=NOV)
    w43 = model.estimate(make("Moteriški UGG batai", 20, "UGG", size="43"), when=NOV)
    assert w43.resale < w39.resale * 0.6
    m38 = model.estimate(make("Nike Air Max męskie", 20, "Nike", size="38"), when=NOV)
    m43 = model.estimate(make("Nike Air Max męskie", 20, "Nike", size="43"), when=NOV)
    assert m38.resale < m43.resale * 0.6


def test_item_page_gender_and_colour():
    from botvin.itempage import parse_item_page
    page = ('<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":['
            '{"position":1,"name":"Pagrindinis"},{"position":2,"name":"Moterims"},'
            '{"position":3,"name":"Drabužiai"}]}</script>'
            '<script>{"item":{"id":42,"color1":"Rožinė","favourite_count":3}}</script>')
    r = parse_item_page(page, now=time.time(), item_id=42)
    assert r["gender"] == "women" and r["colour"] == "Rožinė"
    assert parse_item_page("<p>x</p>", now=time.time())["gender"] is None


def test_watchlist_gets_likes_from_search_when_page_has_none(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    item = make("Kurtka Columbia", 28.0, "Columbia", id_=31)
    item.uploaded_at = None
    client = PageClient([[], [item]], {})                 # pages never show likes
    client.found = {31: make("Kurtka Columbia", 28.0, "Columbia", id_=31, fav=8)}
    notifier = FakeNotifier()
    bot = _watch_bot(tmp_path, client, notifier)
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    _age(bot.store, 31, 12)
    bot.check_watchlist()
    assert bot.store.tracking(31)["fav_last"] == 8
    assert notifier.sent == [(31, True)]                  # 8 likes in 12 min = 40/h: hot


@pytest.mark.parametrize("text,minutes", [
    ("Įkelta prieš val.", 60), ("Įkelta prieš min.", 1), ("Įkelta dabar", 0),
    ("Įkelta prieš 1 d.", 1440), ("Uploaded a day ago", 1440), ("Uploaded an hour ago", 60),
    ("Seller's other items: 26 minutes ago · Uploaded a day ago", 1440),
    ("Uploaded a day ago · Member items: Įkelta dabar", 1440),
    ("Reviews: 26 minutes ago", None),
])
def test_upload_time_phrases(text, minutes):
    from botvin.itempage import parse_relative
    now = 1_800_000_000.0
    r = parse_relative(text, now)
    assert (None if r is None else round((now - r) / 60)) == minutes


def test_review_marks_sold_unsold_and_gone(tmp_path, monkeypatch):
    import botvin.pricing as p
    from botvin.vinted import VintedError
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    a = make("Kurtka Columbia", 15.13, "Columbia", id_=41)       # still listed
    b = make("Kurtka Columbia zimowa", 15.13, "Columbia", id_=42)  # gone from search -> sold
    c = make("Patagonia jacket", 20, "Patagonia", id_=43)         # page errors ("Įvyko klaida") -> sold
    for it in (a, b, c):
        it.uploaded_at = None

    class C(PageClient):
        last_status = 200
        def item_page(self, url):
            if url.endswith("/43"):
                self.last_status = 404
                raise VintedError("404")
            return super().item_page(url)
    client = C([[], [a, b, c]], {})
    client.found = {41: make("Kurtka Columbia", 15.13, "Columbia", id_=41, fav=4)}
    bot = _watch_bot(tmp_path, client, FakeNotifier())
    bot.reviews_per_sweep = 10
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    assert bot.review_outcomes() == 0                             # not 2 h old yet
    bot.store.db.execute("UPDATE tracking SET first_seen = first_seen - 7300")
    assert bot.review_outcomes() == 3
    st = dict(bot.store.db.execute("SELECT id, status FROM outcomes").fetchall())
    assert st == {41: "unsold", 42: "sold", 43: "sold"}
    how = dict(bot.store.db.execute("SELECT id, sold_how FROM outcomes").fetchall())
    assert how[43] == "page error + not in search" and how[42] == "not in search"
    assert bot.review_outcomes() == 0                             # each item reviewed once


def _fill_outcomes(store, brand, category, sold, unsold, gender=None, size="l", colour=None):
    import itertools
    ids = itertools.count(store.db.execute("SELECT COALESCE(MAX(id),0) FROM outcomes").fetchone()[0] + 1)
    for status, n in (("sold", sold), ("unsold", unsold)):
        for _ in range(n):
            store.record_outcome(next(ids), status=status, sold_how=None, sold_minutes=20,
                                 alerted=1, brand=brand, category=category, gender=gender,
                                 size_letter=size, colour=colour, price=20, resale=60, profit=36,
                                 margin=1.5, margin_band="100-200%", demand_level="normal",
                                 likes=5, title="x", url="u")


def test_learning_boosts_fast_sellers_and_damps_slow_ones(tmp_path):
    from botvin import learning
    store = Store(tmp_path / "l.db")
    assert learning.adjust(store, "Carhartt", "jacket", None, "m", 2).factor == 1.0   # no data yet
    _fill_outcomes(store, "Carhartt", "jacket", sold=9, unsold=1)
    _fill_outcomes(store, "Columbia", "fleece", sold=1, unsold=9)
    fast = learning.adjust(store, "Carhartt", "jacket", None, "m", 2)
    slow = learning.adjust(store, "Columbia", "fleece", None, "m", 2)
    assert fast.factor > 1.1 and fast.fast and "Carhartt jacket 9/10 sold" in fast.note
    assert slow.factor < 0.9 and not slow.fast
    rep = learning.report(store, 2)
    assert "Carhartt jacket" in rep and "90%" in rep


def test_bot_applies_learning_to_alerts(tmp_path, monkeypatch):
    import botvin.pricing as p
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    bot = _watch_bot(tmp_path, FakeClient([]), FakeNotifier())
    _fill_outcomes(bot.store, "Columbia", "jacket", sold=1, unsold=11)
    _fill_outcomes(bot.store, "Patagonia", "jacket", sold=10, unsold=2)
    col = make("Kurtka Columbia", 15.13, "Columbia")
    est = bot._estimate(col, (None, 0))
    assert est.learned_factor < 1 and any("learned: Columbia jacket 1/12" in f for f in est.flags)
    pat = make("Patagonia jacket", 40, "Patagonia")
    est = bot._estimate(pat, (None, 0))
    assert est.fast_seller and evaluate(pat, est, bot.t).priority == "high"


def test_watchlist_detects_sale_when_page_errors_and_search_misses(tmp_path, monkeypatch):
    import botvin.pricing as p
    from botvin.vinted import VintedError
    monkeypatch.setattr(p.PriceModel, "season_mult", lambda self, c, w=None, b=None: 1.25)
    item = make("Stüssy Milano Hoodie", 16.45, "Stüssy", id_=51, size="L")
    item.uploaded_at = None

    class C(PageClient):
        def item_page(self, url):
            raise VintedError("item page -> HTTP 500")          # "Įvyko klaida"
    bot = _watch_bot(tmp_path, C([[], [item]], {}), FakeNotifier())
    bot.run(once=True)
    bot.poll_feed({"name": "t"})
    _age(bot.store, 51, 5)
    bot.check_watchlist()
    row = bot.store.tracking(51)
    assert row["sold_at"] is not None and row["next_check"] is None


def test_learning_summary_pushed_to_phone(tmp_path):
    texts = []
    notifier = FakeNotifier()
    notifier.send_text = lambda title, body, quiet=False: texts.append((title, body, quiet))
    bot = _watch_bot(tmp_path, FakeClient([]), notifier)
    t0 = time.time()
    assert not bot.maybe_send_summary(now=t0)             # first call only starts the clock
    _fill_outcomes(bot.store, "Carhartt", "jacket", sold=5, unsold=0)
    _fill_outcomes(bot.store, "Columbia", "fleece", sold=0, unsold=6)
    assert not bot.maybe_send_summary(now=t0 + 3600)      # not 3 h yet
    assert bot.maybe_send_summary(now=t0 + 3 * 3600 + 1)
    title, body, quiet = texts[-1]
    assert quiet and "5/11 new deals sold" in title
    assert "✅ Carhartt jacket 5/5" in body and "❌ Columbia fleece 0/6" in body
    assert not bot.maybe_send_summary(now=t0 + 7 * 3600)  # nothing new since -> no message


def test_carhartt_anchorage_burgundy_is_not_a_detroit():
    it = make("Kurtka zimowa Carhartt WIP Anchorage Parka M bordowa skate workwear y2k", 58.41,
              "Carhartt", size="M", status="Labai gera",
              desc="Legendinė striukė, panaši į Detroit")
    est = model.estimate(it, when=NOV)
    assert not any("detroit" in f for f in est.flags)          # description mention ignored
    assert not est.instant and est.colour_family == "burgundy"
    assert est.resale < 80 and not evaluate(it, est, Thresholds()).is_deal
    det = make("Carhartt WIP Detroit Jacket", 51.19, "Carhartt", size="M", status="Labai gera")
    est = model.estimate(det, when=NOV)
    assert est.instant and 100 <= est.resale <= 140


@pytest.mark.parametrize("text,fam", [
    ("Bordo", "burgundy"), ("kurtka bordowa", "burgundy"), ("Mėlyna, Juoda", "blue"),
    ("Tamsiai mėlyna striukė", "navy"), ("Carhartt Detroit Hamilton Brown", "brown"),
    ("Bluza Oakley", None), ("Rudeninė striukė", None), ("Must have jacket", None),
    ("Nike tank top", None), ("Kurtka bez kaptura czarna", "black"), ("Patagonia olive", "olive"),
])
def test_colour_families(text, fam):
    from botvin.colours import family
    assert family(text) == fam


def test_colour_changes_value_for_any_popular_brand():
    def r(title, brand, size="L", colour=""):
        it = make(title, 20, brand, size=size)
        it.colour = colour
        return model.estimate(it, when=NOV).resale
    assert r("Nuptse kurtka", "The North Face", colour="Juoda") > r("Nuptse kurtka", "The North Face", colour="Raudona")
    assert r("UGG batai", "UGG", "39", "Ruda") > r("UGG batai", "UGG", "39", "Mėlyna")
    assert r("Dr. Martens 1460 batai", "Dr. Martens", "43", "Juoda") > r("Dr. Martens 1460 batai", "Dr. Martens", "43", "Mėlyna")
    # brands without a colour table use the defaults for the item type
    assert r("Mammut kurtka", "Mammut", colour="Black") > r("Mammut kurtka", "Mammut", colour="Bordo")


def test_bonuses_are_capped():
    it = make("The North Face Nuptse black", 60, "The North Face", size="L", status="Naujas su etiketėmis")
    est = model.estimate(it, when=NOV)
    assert "bonuses capped at x1.6" in est.flags and est.resale <= 115 * 1.6 + 0.01


def test_learning_by_brand_and_colour(tmp_path):
    from botvin import learning
    store = Store(tmp_path / "c.db")
    _fill_outcomes(store, "Carhartt", "jacket", sold=6, unsold=1, colour="black")
    _fill_outcomes(store, "Carhartt", "jacket", sold=0, unsold=4, colour="burgundy")
    got = learning.adjust(store, "Carhartt", "jacket", None, "m", 2, colour="burgundy")
    assert "burgundy Carhartt jacket 0/4" in got.note and got.factor < 0.9
    got = learning.adjust(store, "Carhartt", "jacket", None, "m", 2, colour="black")
    assert "black Carhartt jacket 6/7" in got.note and got.factor > 1.05
    assert "burgundy Carhartt" in learning.report(store, 2)


def test_nike_and_adidas_puffers_skipped():
    for title, brand in (("Biała pikowana kurtka zimowa Nike", "Nike"),
                         ("Nike balta dygsniuota žieminė striukė", "Nike"),
                         ("Adidas puffer jacket", "Adidas"), ("Adidas kurtka puchowa", "Adidas")):
        est = model.estimate(make(title, 15.13, brand, size="M"), when=NOV)
        assert est.excluded and "skipped" in est.excluded, (title, est.category, est.excluded)
    # Nike shoes still count, and sneaker colours don't leak into Nike clothes
    assert model.estimate(make("Nike Air Max 90", 20, "Nike", size="43"), when=NOV).excluded is None
    hoodie = make("Nike hoodie white", 20, "Nike", size="M")
    assert not any("x1.10" in f for f in model.estimate(hoodie, when=NOV).flags)


def test_ugg_tazz_hickory_with_defects():
    it = make("Ugg tazz II hickory", 36.77, "UGG", size="39", status="Gera",
              desc="KOLOR HICKORY Buty posiadają widoczne na zdjęciach wady i były noszone! "
                   "Mają lekko zużytą podeszwę oraz jeden luźny szew.")
    est = model.estimate(it, when=NOV)
    assert "damage mentioned" in est.flags and est.colour_family == "brown"
    assert 30 <= est.resale <= 45                       # worn Tazz: ~40 EUR, so 36.77 is no deal
    assert not evaluate(it, est, Thresholds()).is_deal


def test_ugg_only_under_20_eur():
    t = Thresholds()
    for price, deal in ((15, True), (19.99, True), (20.5, False), (25, False)):
        it = make("UGG Tazz chestnut", price, "UGG", size="39", status="Labai gera")
        v = evaluate(it, model.estimate(it, when=NOV), t)
        assert v.is_deal == deal, (price, v.reasons)


def test_tracksuit_set():
    assert model.detect_category("Ralph Lauren sportinis kostiumas") == "tracksuit"
    assert model.detect_category("Komplet dresowy Nike") == "tracksuit"
    assert model.detect_category("Zara dress black") != "tracksuit"      # "dress" is not "dres"
    it = make("Ralph Lauren sportinis kostiumas", 19.60, "Ralph Lauren", size="L", status="Gera",
              desc="Tamsiai mėlynas sportinis kostiumas.")
    it.colour = "Tamsiai mėlyna"
    est = model.estimate(it, when=NOV)
    assert 40 <= est.resale <= 55 and evaluate(it, est, Thresholds()).is_deal
