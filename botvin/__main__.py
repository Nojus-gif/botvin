"""CLI.

    python -m botvin run              # poll forever, notify on deals
    python -m botvin once             # one sweep (alerts on everything that qualifies)
    python -m botvin test-notify      # send a test push to your phone
    python -m botvin check "Columbia kurtka zimowa" --price 19.94 --brand Columbia --condition good
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from .models import Item
from .pricing import PriceModel
from .scorer import Thresholds, evaluate


EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
PLACEHOLDER = "botvin-CHANGE-ME-to-something-random"


def create_config(path: Path) -> None:
    """First run: copy the example config and give it a private random ntfy topic."""
    import secrets
    topic = f"botvin-{secrets.token_hex(6)}"
    text = EXAMPLE.read_text(encoding="utf-8").replace(PLACEHOLDER, topic)
    path.write_text(text, encoding="utf-8")
    print(f"Created {path.resolve()}")
    print()
    print("  In the ntfy app on your phone, tap + and subscribe to this topic:")
    print()
    print(f"      {topic}")
    print()


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        if not EXAMPLE.exists():
            sys.exit(f"Config {path} not found and no config.example.yaml to create it from.")
        create_config(p)
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    topic = ((cfg.get("notify") or {}).get("ntfy") or {}).get("topic")
    if topic == PLACEHOLDER:
        sys.exit(f"Set notify.ntfy.topic in {p.resolve()} (or delete the file to auto-generate one).")
    return cfg


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="botvin", description="Vinted deal finder")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every skipped branded item")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("test-notify")
    sub.add_parser("probe", help="check that the bot can reach Vinted's search")
    sub.add_parser("report", help="what sold and what didn't: the bot's learned demand")
    pg = sub.add_parser("page", help="show the upload time the bot reads from an item page")
    pg.add_argument("url")
    pg.add_argument("--debug", action="store_true", help="show where likes/views/sold appear in the page")
    chk = sub.add_parser("check", help="score a hypothetical listing offline")
    chk.add_argument("title")
    chk.add_argument("--price", type=float, required=True, help="total price incl. Vinted fee")
    chk.add_argument("--brand", default="")
    chk.add_argument("--size", default="L")
    chk.add_argument("--condition", default="good",
                     choices=["new_with_tags", "new_without_tags", "very_good", "good", "satisfactory"])
    chk.add_argument("--description", default="")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # Keep HTTP libraries quiet even in verbose mode.
    for noisy in ("urllib3", "curl_cffi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "check":
        cfg = load_config(args.config) if Path(args.config).exists() else {}
        model = PriceModel(brand_overrides=cfg.get("brands"), filters=cfg.get("filters"))
        item = Item(id=0, title=args.title, price=args.price, total_price=args.price, currency="EUR",
                    brand=args.brand, size=args.size, condition=args.condition, url="-",
                    description=args.description)
        est = model.estimate(item)
        t = Thresholds(**(cfg.get("thresholds") or {}))
        t.max_age_minutes = float("inf")
        v = evaluate(item, est, t)
        print(f"brand={est.brand} category={est.category} resale≈{est.resale:.0f}€ "
              f"confidence={est.confidence:.2f} flags={est.flags}")
        print(f"profit={v.profit:.0f}€ margin={v.margin:.0%} -> "
              f"{'DEAL' if v.is_deal else 'skip'} ({v.priority}) {'; '.join(v.reasons)}")
        return

    cfg = load_config(args.config)
    from .bot import Bot  # imported late so `check` works without network deps
    from .notify import Notifier

    if args.cmd == "report":
        from . import learning
        from .storage import Store
        print(learning.report(Store(cfg.get("database", "botvin.db")),
                              float(cfg.get("review_after_hours", 2))))
        return

    if args.cmd == "probe":
        from .vinted import VintedClient
        client = VintedClient(domain=cfg.get("domain", "vinted.lt"))
        for line in client.probe():
            print(line)
        return

    if args.cmd == "page":
        import time as _time
        from .vinted import VintedClient
        client = VintedClient(domain=cfg.get("domain", "vinted.lt"))
        client.refresh_session()
        info = client.item_page(args.url)
        if info["uploaded_at"]:
            print(f"uploaded {(_time.time() - info['uploaded_at']) / 60:.0f} min ago (via {info['source']})")
        else:
            print("upload time NOT FOUND")
        print(f"likes: {info.get('likes')}  views: {info.get('views')}  sold: {info.get('sold')}")
        import re as _re
        m = _re.search(r"/items/(\d+)", args.url)
        if m and info.get("likes") is None:
            found = client.find_item(int(m.group(1)), args.url.split("/items/")[1].split("?")[0]
                                     .split("-", 1)[-1].replace("-", " "))
            print(f"likes via search: {found.favourites if found else 'not found'}  "
                  f"views via search: {found.views if found else '-'}")
        print(f"gender: {info.get('gender')}  colour: {info.get('colour')}")
        print(f"description: {info['description'][:200]!r}")
        if args.debug:
            from .itempage import debug_matches
            print("--- page snippets mentioning likes/views/sold:")
            for snip in debug_matches(info.get("_html", "")):
                print("  ", snip)
        return

    if args.cmd == "test-notify":
        Notifier(cfg.get("notify") or {}).send_text("botvin test", "If you see this, alerts work 🎉")
        topic = ((cfg.get("notify") or {}).get("ntfy") or {}).get("topic")
        print(f"Test notification sent to ntfy topic: {topic}  (any ERROR above means it did not arrive)")
        return

    bot = Bot(cfg)
    if args.cmd == "once":
        bot.cfg["alert_on_startup"] = True
        bot.run(once=True)
    else:
        bot.run()


if __name__ == "__main__":
    main()
