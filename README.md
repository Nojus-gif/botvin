# botvin: Vinted deal finder

Polls Vinted's newest listings every ~20 seconds, estimates what each branded
item would resell for right now (brand × category × season × condition, blended
with live market prices), and pushes the good ones to your phone so you can buy
them before somebody else does.

## How it decides what a deal is

For every new listing:

1. **Brand**: from Vinted's brand field, or from the title if the field is empty
   (`botvin/data/brands.yaml` has about 40 resale-worthy brands with aliases).
   Unknown brands are skipped.
2. **Category**: from title keywords in EN/LT/PL/FI/DE/FR/ES/IT/NL
   (`kurtka zimowa` → puffer, `kevytuntuvatakki` → puffer, `striukė` → jacket…).
   The item word that comes first decides, so `rękawiczki zimowe` (winter
   gloves) are gloves; "winter", "down" or "gore-tex" only upgrade a jacket.
3. **Base resale value**: the brand's table value × the category factor (or a
   per-brand override; Nike shoes ≠ Nike t-shirts).
4. **Multipliers**:
   - **Season**: winter gear is ×1.25 in Nov/Dec and ×0.55 in summer; t-shirts
     and shorts follow the opposite curve.
   - **Condition**: new with tags ×1.25 … satisfactory ×0.7.
   - **Who buys it (gender × size × colour)**: gender comes from the item
     page's Women/Men breadcrumb, words like `moteriška`/`damska`/`męska`/
     `naisten`, or, failing that, the colour: pink/lilac with no other clue is
     treated as women's. Then size demand depends on gender: women's L ×0.75,
     XL ×0.55; men's S ×0.85, XL ×0.95. Women's shoes above 41 and men's below
     41 or above 45 are discounted. A men's item in pink/lilac is ×0.75.
   - **Model names**: `gore-tex`, `nuptse`, `summit series`, `beta ar`, `nano puff`…
   - **Damage words** (`uszkodzony`, `skylė`, `rikki`, `kaputt`…): ×0.75. The
     item still gets alerted, with a ⚠ flag, because a damaged TNF can still flip.
   - **Fake words** (`replica`, `1:1`, `podróbka`…): skipped.
   - **Kids' items and extreme sizes: skipped.** Searches only cover Vinted's
     Men + Women sections. Anything else is dropped if it has a kids' size
     (`140 cm`, `10 m.`, `12 years`), kids' words (`junior`, `chłopięca`,
     `vaikiška`), XXS/XS/XXL/3XL+ sizes, or shoes outside EU 36–47.
     Change this under `filters:` in `config.yaml`.
5. **Market blend**: the bot stores every listing it sees in SQLite. Once it has
   at least 5 comparable listings (same brand and category, last 30 days), it
   blends their median asking price into the estimate.
6. **Demand (likes over time)**: popular items get seen by lots of people
   fast, so few likes after a while means it won't resell quickly. Every
   promising item goes on a watchlist and its page is re-opened 5, 10, 15, 25,
   40 and 60 minutes after upload to read likes, views and whether it sold.
   - **Cold**: fewer than 12 likes/hour after 15 min, or 60+ views with under
     4% liking it. It's no longer a deal (unsold after an hour with ~10 likes = skip).
   - **Hot**: 20+ likes/hour and at least 5 likes. High priority, and it may
     pass with a lower margin. Only real likes count, not "more than usual".
   - **Alerts**: you get the first alert as soon as the price looks right,
     marked "too early to judge demand". If it then turns hot, you get one
     "✅ Demand confirmed" follow-up. An item that wasn't cheap enough at first
     but turns hot gets a "Demand picked up" alert.
   - **Learning**: the bot records how fast watched items sell, and alerts
     show "7/10 similar sold within 1 h" per brand and type.
7. **Learning from what sold**: about 2 hours after listing, every promising
   item and every alert is checked once more. It counts as sold if the page
   says "Parduotas", or if the page no longer opens and search can't find it
   either. Outcomes are grouped by brand + type, brand, type, gender + size,
   estimated margin and demand level. Once there's enough data (10 outcomes
   overall, 3 in a group), estimates are adjusted ×0.7–×1.3 by how often that
   kind of item really sold, and kinds that sell ≥70% of the time get top
   priority. `python -m botvin report` shows what it has learned.
8. **Decision**: `profit = resale − (price incl. buyer fee + shipping)`. You get
   an alert when profit ≥ €20, margin ≥ 60%, confidence ≥ 0.6 and the listing
   is less than 90 minutes old. It's marked **high priority** when profit is
   €50 or more, margin is 200% or more, or the listing is collecting likes fast.

The five sold listings from your screenshots are unit tests
(`tests/test_examples.py`), and all five are flagged as deals in November:

| Listing | Paid | Est. resale | Profit |
|---|---|---|---|
| Columbia kurtka zimowa (good) | 19.94 | ~50 | ~26 |
| Kurtka Columbia (good) | 15.13 | ~45 | ~26 |
| Patagonia kevytuntuvatakki (very good) | 53.20 | ~117 | ~60 |
| TNF kurtka, damaged zipper | 13.93 | ~63 | ~46 |
| Palace gore-tex striukė* | 62.65 | ~188 | ~121 |

\* The real Palace listing had no brand anywhere in its text; the brand was only
visible in the photo. Text rules can't catch that. See "Next steps".

Try any listing offline:

```bash
python -m botvin check "Arcteryx Beta AR jacket" --price 90 --brand "Arc'teryx" --condition very_good
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m botvin test-notify
```

The first command you run creates `config.yaml` with a private, random ntfy
topic and prints its name, like `botvin-3082f4256438`. Then:

1. Install the **ntfy** app (iOS/Android), tap **+** and subscribe to the printed topic.
2. Run `python -m botvin test-notify` again. Your phone should buzz.

`config.yaml` is never uploaded to GitHub, so your topic stays private. Each
push shows the photo, the profit and an "Open on Vinted" button. Telegram also
works (see the comments in `config.yaml`).

```bash
python -m botvin once            # one sweep, alerts on whatever currently qualifies
python -m botvin run             # run forever
python -m botvin -v run          # ...and log why each branded item was skipped
```

Run it on something that's always on, like a cheap VPS or a Raspberry Pi
(`nohup python -m botvin run &`, or a systemd service). A home IP is less likely
to be blocked than a datacenter IP.

## Tuning

- **Feeds** (`config.yaml`): each one is a Vinted search sorted newest first.
  The broad `everything-cheap` feed catches deals on any brand. Targeted feeds
  with `price_to` caps give you more coverage of the brands you care about most.
- **Prices** (`botvin/data/brands.yaml`): these are my starting estimates of
  Vinted resale values. Correct them as you learn what actually sells, or add
  brands under `brands:` in `config.yaml`.
- **Thresholds**: start strict. If you get too many alerts, raise `min_profit`.
  If you get too few, lower `min_margin`.
- **Rate**: keep `min_request_interval` at 2 seconds or more. Hammering Vinted
  gets you blocked faster than it finds deals.

## Caveats

- Vinted has no public API. This uses the same JSON endpoints the website uses,
  with an anonymous session token. Since September 2026 search is at
  `api.vinted.<tld>/svc-catalogue/items`. If Vinted changes it again, run
  `python -m botvin probe` and update `vinted.py`.
- The new API no longer says when an item was listed, so "fresh" means "new
  since the bot started watching". The first sweep after starting only records
  what's already up, and those items get no likes/hour rate.
- Likes are re-measured only while an item is still on the first page of a
  feed. Narrow feeds (one brand, a price cap) keep items visible for longer. Automated access is against Vinted's terms, so keep request rates
  modest and use the bot at your own risk.
- If you get HTTP 403s, make sure `curl_cffi` is installed (it mimics a real
  Chrome browser), slow down, or run from a residential IP.

## Next steps

1. **Read photos with AI** for listings that have no brand in the text, like the
   Palace one, and to spot fakes. Send the photo of a borderline item to a vision
   model and ask "brand / model / authentic?".
2. **Learn from sold items**: re-check alerted items and record how fast they
   disappear, then use real sell-through speed instead of asking prices.
3. **Auto-buy**: once the alerts prove reliable, add a purchase step for
   high-confidence, high-priority deals. This needs a logged-in session, so it
   comes last.
