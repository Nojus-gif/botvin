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


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"Config {path} not found. Copy config.example.yaml to config.yaml and edit it.")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="botvin", description="Vinted deal finder")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every skipped branded item")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("test-notify")
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
        model = PriceModel(brand_overrides=cfg.get("brands"))
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

    if args.cmd == "test-notify":
        Notifier(cfg.get("notify") or {}).send_text("botvin test", "If you see this, alerts work 🎉")
        print("sent")
        return

    bot = Bot(cfg)
    if args.cmd == "once":
        bot.cfg["alert_on_startup"] = True
        bot.run(once=True)
    else:
        bot.run()


if __name__ == "__main__":
    main()
