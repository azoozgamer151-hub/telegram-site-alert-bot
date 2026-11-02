from pathlib import Path
import json, sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alert_bot as ab  # noqa: E402

SEL = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())["watches"][0]["selectors"]
PAGE = """<article class="product_pod"><h3><a href="a/index.html" title="Book A">Bo...</a></h3>
<p class="price_color">£20.00</p><p class="availability"> In stock</p></article>
<article class="product_pod"><h3><a href="b/index.html" title="Book B">Bo...</a></h3>
<p class="price_color">£30.00</p><p class="availability"> Out of stock</p></article>"""
WATCH = {"name": "w", "urls": [], "selectors": SEL}


def items(price_a=20.0, stock_b="Out of stock", extra=""):
    page = PAGE.replace("£20.00", f"£{price_a:.2f}").replace("Out of stock", stock_b) + extra
    return ab.extract_items(page, "https://x.com/", SEL)


def test_extract():
    its = items()
    assert [i.title for i in its] == ["Book A", "Book B"]
    assert its[0].price == 20.0 and its[0].in_stock and its[1].in_stock is False
    assert its[0].url == "https://x.com/a/index.html"


def test_first_run_is_silent_then_alerts(tmp_path):
    st = ab.Store(str(tmp_path / "t.db"))
    assert ab.diff(st, WATCH, items(), first_run=True) == []
    assert ab.diff(st, WATCH, items(), first_run=False) == []          # nothing changed
    new = '<article class="product_pod"><h3><a href="c/" title="Book C">C</a></h3><p class="price_color">£5</p></article>'
    alerts = ab.diff(st, WATCH, items(price_a=15.0, stock_b="In stock", extra=new), first_run=False)
    kinds = {a.split()[0] for a in alerts}
    assert kinds == {"✅", "📉", "🆕"}                                 # back in stock, price drop, new


def test_target_price(tmp_path):
    st = ab.Store(str(tmp_path / "t.db"))
    w = dict(WATCH, target_price=10)
    ab.diff(st, w, items(), first_run=True)
    assert ab.diff(st, w, items(price_a=15.0), first_run=False) == []   # cheaper but above target
    assert len(ab.diff(st, w, items(price_a=9.0), first_run=False)) == 1
