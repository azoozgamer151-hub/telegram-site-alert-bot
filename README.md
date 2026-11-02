# telegram-site-alert-bot: get a Telegram message when a website changes

Watches any listing page (shop, marketplace, job board, real-estate site...) and sends a
Telegram alert when:

- 🆕 a **new item** appears
- 📉 an item's **price drops** (optionally only below your target price)
- ✅ an item is **back in stock**

Everything is configured in `config.json` with CSS selectors, so supporting a new site means
adding one config block, not rewriting code. State lives in SQLite, so restarts never re-send
old alerts, and the first run only records a baseline (no alert flood).

## Example alerts
```
🆕 New on Books - Travel · 23.21
The Great Railway Bazaar
https://books.toscrape.com/catalogue/...

📉 Price drop 56.88 → 51.88 (-9%)
Full Moon over Noah's Ark
https://books.toscrape.com/catalogue/...
```

## Setup
```bash
pip install -r requirements.txt
cp config.example.json config.json
export TELEGRAM_TOKEN="from @BotFather" TELEGRAM_CHAT_ID="your chat id"

python3 alert_bot.py --dry --once   # first check: saves a baseline, prints nothing
python3 alert_bot.py --demo --dry   # simulate changes to see what alerts look like
python3 alert_bot.py                # run forever (checks every interval_seconds)
```

## Run 24/7 on a Linux server
`alert-bot.service` is a ready systemd unit (auto-restart on crash or reboot):
```bash
sudo cp alert-bot.service /etc/systemd/system/   # edit YOUR_USER first
sudo systemctl enable --now alert-bot
journalctl -u alert-bot -f
```

## Config
| Key | Meaning |
|---|---|
| `urls` | one or more pages to watch |
| `selectors.item` | CSS selector for one listing card |
| `selectors.title` / `title_attr` | title element (and attribute, if the full title is in one) |
| `selectors.link`, `price`, `stock` | optional fields |
| `target_price` | only alert on drops at or below this price (`null` = any drop) |
| `interval_seconds` | how often to check |

## Tests
```bash
python3 -m pytest -q    # parsing, baseline silence, new / price drop / back-in-stock, target price
```

## Notes
Be polite to the sites you watch: a reasonable interval, a delay between pages, and check
their terms and robots.txt.

## License
MIT
