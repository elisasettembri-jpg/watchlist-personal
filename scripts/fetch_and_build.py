#!/usr/bin/env python3
"""
fetch_and_build.py
-------------------
Lee watchlist.json, busca datos REALES de mercado en al menos dos fuentes
independientes por precio (para no depender de una sola y detectar errores),
calcula niveles tecnicos con matematica simple (nada de opinion ni invencion),
y arma index.html a partir de template.html.

Roles:
  - Este script es Python puro (libreria estandar, sin pip install) para que
    corra en GitHub Actions sin pasos extra de instalacion.
  - No asigna la señal "comprar/esperar/cautela" -- eso queda en manos del
    campo "signal" de watchlist.json, que se edita a mano o se discute en
    chat con Claude. Un script no deberia fabricar una opinion de inversion.

Fuentes usadas:
  - financialmodelingprep.com  (API key gratuita, quote + historico diario)
  - stooq.com                  (gratis, sin key, para cruzar el precio)
  - feeds.finance.yahoo.com    (RSS gratis, titulares de noticias reales)
  - api.coingecko.com          (gratis, sin key, para cripto)
"""

import os
import json
import csv
import io
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

FMP_KEY = os.environ.get("FMP_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHLIST_PATH = os.path.join(ROOT, "watchlist.json")
TEMPLATE_PATH = os.path.join(ROOT, "template.html")
OUTPUT_PATH = os.path.join(ROOT, "index.html")

CRYPTO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fmp_quote(ticker):
    url = f"https://financialmodelingprep.com/api/v3/quote/{ticker}?apikey={FMP_KEY}"
    data = json.loads(http_get(url))
    if not data:
        return None
    return data[0]


def fmp_historical(ticker, days=210):
    url = (f"https://financialmodelingprep.com/api/v3/historical-price-full/"
           f"{ticker}?apikey={FMP_KEY}&timeseries={days}")
    data = json.loads(http_get(url))
    hist = data.get("historical", [])
    return hist


def stooq_price(ticker):
    url = f"https://stooq.com/q/l/?s={ticker.lower()}.us&f=sd2t2ohlcv&h&e=csv"
    raw = http_get(url).decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(raw))
    row = next(reader, None)
    if not row:
        return None
    try:
        close = float(row.get("Close", "N/D"))
        return close
    except (ValueError, TypeError):
        return None


def yahoo_news(ticker, limit=2):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        raw = http_get(url)
        root = ET.fromstring(raw)
        items = root.findall(".//item")[:limit]
        out = []
        for it in items:
            title = it.findtext("title", default="").strip()
            link = it.findtext("link", default="").strip()
            pub = it.findtext("pubDate", default="").strip()
            if title:
                out.append({"title": title, "link": link, "date": pub})
        return out
    except Exception:
        return []


def coingecko_price(ticker):
    cid = CRYPTO_IDS.get(ticker.upper())
    if not cid:
        return None
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=usd"
    try:
        data = json.loads(http_get(url))
        return data.get(cid, {}).get("usd")
    except Exception:
        return None


def avg(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def build_entry(item):
    """Devuelve el dict estandarizado para un item, con N/D donde no se pudo verificar."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tipo = item.get("tipo", "accion")
    ticker = item["ticker"]

    entry = {
        "ticker": ticker,
        "name": item.get("name", ticker),
        "tipo": tipo,
        "sector": item.get("sector", "Sin clasificar"),
        "signal": item.get("signal", "nd"),
        "price": {"val": "N/D", "date": ""},
        "range": {"val": "N/D", "date": ""},
        "entry_zone": "N/D",
        "flow": {"val": item.get("flujo_manual", "N/D"),
                 "date": item.get("flujo_manual_fecha", "")},
        "news": [],
        "source_note": "",
        "technical": {"val": "N/D", "checks": []},
        "claude_read": {"val": "N/D", "date": ""},
    }

    if tipo == "privada":
        entry["price"]["val"] = item.get("estado_privada", "Privada -- sin ticker publico")
        entry["range"]["val"] = item.get("ipo_rumor", "Sin fecha de IPO anunciada")
        entry["entry_zone"] = "N/D -- no cotiza"
        return entry

    if tipo == "cripto":
        px = coingecko_price(ticker)
        if px is not None:
            entry["price"] = {"val": f"${px:,.2f}", "date": now}
            entry["source_note"] = "Fuente: CoinGecko (gratis, sin key)"
        return entry

    # acciones y ETFs
    price_fmp = None
    price_stooq = None
    try:
        q = fmp_quote(ticker)
        if q:
            price_fmp = q.get("price")
    except Exception as e:
        print(f"  [{ticker}] error FMP quote: {e}")

    try:
        price_stooq = stooq_price(ticker)
    except Exception as e:
        print(f"  [{ticker}] error Stooq: {e}")

    if price_fmp is not None:
        note = "Fuente: financialmodelingprep.com"
        if price_stooq is not None:
            diff_pct = abs(price_fmp - price_stooq) / price_fmp * 100
            if diff_pct <= 3:
                note += " -- verificado contra stooq.com"
            else:
                note += f" -- OJO: stooq.com difiere {diff_pct:.1f}%, confirmar en broker"
        entry["price"] = {"val": f"${price_fmp:,.2f}", "date": now}
        entry["source_note"] = note
    elif price_stooq is not None:
        entry["price"] = {"val": f"${price_stooq:,.2f}", "date": now}
        entry["source_note"] = "Fuente: stooq.com (FMP no disponible)"

    ma50 = ma200 = lo90 = hi90 = None
    try:
        hist = fmp_historical(ticker, 210)
        if hist:
            closes = [h.get("close") for h in hist]
            last90 = closes[:90]
            lo90 = min([c for c in last90 if c is not None], default=None)
            hi90 = max([c for c in last90 if c is not None], default=None)
            ma50 = avg(closes[:50])
            ma200 = avg(closes[:200])
            if lo90 is not None and hi90 is not None:
                entry["range"] = {"val": f"3 meses: ${lo90:,.2f} - ${hi90:,.2f}", "date": now}
            zone_bits = []
            if ma50 is not None:
                zone_bits.append(f"MM50: ${ma50:,.2f}")
            if ma200 is not None:
                zone_bits.append(f"MM200: ${ma200:,.2f}")
            if zone_bits:
                entry["entry_zone"] = " -- ".join(zone_bits) + " (calculado sobre precios historicos reales)"
    except Exception as e:
        print(f"  [{ticker}] error historico FMP: {e}")

    entry["news"] = yahoo_news(ticker)

    score_str, checks = technical_score(price_fmp or price_stooq, ma50, ma200, lo90, hi90)
    entry["technical"] = {"val": score_str or "N/D", "checks": checks}

    claude_text = ai_recommendation(entry, checks, score_str)
    entry["claude_read"] = {"val": claude_text or "N/D", "date": now if claude_text else ""}

    return entry


def technical_score(price, ma50, ma200, lo90, hi90):
    """
    Puntaje 100% mecanico, sin opinion: cada criterio suma o no segun
    una regla fija y verificable. No es una recomendacion, es conteo.
    """
    if price is None:
        return None, []
    checks = []
    score = 0
    total = 0
    if ma50 is not None:
        total += 1
        ok = price > ma50
        score += 1 if ok else 0
        checks.append(f"Precio {'por encima' if ok else 'por debajo'} de la MM50")
    if ma200 is not None:
        total += 1
        ok = price > ma200
        score += 1 if ok else 0
        checks.append(f"Precio {'por encima' if ok else 'por debajo'} de la MM200")
    if ma50 is not None and ma200 is not None:
        total += 1
        ok = ma50 > ma200
        score += 1 if ok else 0
        checks.append(f"MM50 {'por encima' if ok else 'por debajo'} de la MM200 ({'cruce dorado' if ok else 'cruce bajista'})")
    if lo90 is not None and hi90 is not None and hi90 > lo90:
        total += 1
        pos = (price - lo90) / (hi90 - lo90)
        ok = pos <= 0.5
        score += 1 if ok else 0
        pct = round(pos * 100)
        checks.append(f"Precio en el {pct}% del rango de 3 meses (mas cerca del {'minimo' if ok else 'maximo'})")
    if total == 0:
        return None, []
    return f"{score}/{total}", checks


def ai_recommendation(entry, technical_checks, technical_str):
    """
    Le pide a la API GRATIS de Gemini (Google) una lectura sintetizada,
    usando SOLO los datos objetivos que ya juntamos. Si no hay
    GEMINI_API_KEY configurada, devuelve None y el campo queda en N/D
    en vez de inventar nada.
    """
    if not GEMINI_KEY:
        return None

    news_txt = "; ".join([f'"{n["title"]}" ({n["date"]})' for n in entry.get("news", [])]) or "sin noticias recientes encontradas"
    flow_txt = entry.get("flow", {}).get("val", "N/D")

    prompt = f'''Ticker: {entry["ticker"]} ({entry["name"]})
Precio actual: {entry["price"]["val"]}
Rango 3 meses: {entry["range"]["val"]}
Puntaje tecnico mecanico: {technical_str or "N/D"} -- criterios: {"; ".join(technical_checks) or "N/D"}
Flujo institucional conocido: {flow_txt}
Noticias recientes: {news_txt}

Con SOLO estos datos (no inventes ni asumas nada que no este aca), escribi en español,
en maximo 60 palabras: 1 punto a favor, 1 punto en contra, y una conclusion de
"atractivo" / "esperar" / "cautela". No es asesoramiento financiero personalizado,
es una lectura informativa basada estrictamente en los datos de arriba. Si los datos
son insuficientes para opinar, decilo explicitamente en vez de forzar una conclusion.'''

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}]
    }).encode()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        text = " ".join([p.get("text", "") for p in parts]).strip()
        return text or None
    except Exception as e:
        print(f"  [{entry['ticker']}] error llamando a Gemini: {e}")
        return None


def signal_label(s):
    return {"buy": "Atractivo", "wait": "Esperar", "caution": "Cautela"}.get(s, "Sin clasificar")


def render_field(label, val, date=""):
    if not val or val == "N/D":
        return (f'<div class="field"><div class="k">{label}</div>'
                f'<div class="v nd">N/D -- sin dato verificado</div></div>')
    date_html = f'<span class="fdate">{date}</span>' if date else ""
    return f'<div class="field"><div class="k">{label}{date_html}</div><div class="v">{val}</div></div>'


def render_news(news):
    if not news:
        return '<div class="field"><div class="k">Noticias</div><div class="v nd">Sin noticias encontradas en esta corrida</div></div>'
    rows = ""
    for n in news:
        rows += (f'<div style="margin-bottom:4px;">'
                 f'<a href="{n["link"]}" style="color:var(--blue);">{n["title"]}</a>'
                 f'<span class="fdate"> {n["date"]}</span></div>')
    return f'<div class="field"><div class="k">Noticias</div><div class="v">{rows}</div></div>'


def render_card(e, idx):
    sig = e["signal"]
    price_field = render_field("Precio / valuacion", e["price"]["val"], e["price"]["date"])
    range_field = render_field("Rango 3M / IPO", e["range"]["val"], e["range"]["date"])
    entry_field = render_field("Zona de entrada", e["entry_zone"])
    tech = e.get("technical", {})
    tech_val = tech.get("val", "N/D")
    if tech.get("checks"):
        tech_val += " -- " + "; ".join(tech["checks"])
    tech_field = render_field("Puntaje tecnico (mecanico, sin IA)", tech_val)
    claude_field = render_field("Lectura automatica (IA gratuita, Gemini)", e.get("claude_read", {}).get("val"), e.get("claude_read", {}).get("date", ""))
    flow_field = render_field("Flujo institucional (13F, trimestral)", e["flow"]["val"], e["flow"]["date"])
    news_field = render_news(e["news"])
    source_html = f'<div class="source">{e["source_note"]}</div>' if e["source_note"] else ""
    return f'''<div class="card {sig}" data-idx="{idx}" data-ticker="{e["ticker"]}">
  <div class="card-head">
    <div><div class="ticker">{e["ticker"]}</div><div class="company">{e["name"]}</div></div>
    <div class="head-right">
      <span class="badge {sig}">{signal_label(sig)}</span>
      <button class="btn-del" onclick="removeTicker('{e["ticker"]}')" aria-label="Sacar de la lista">&times;</button>
    </div>
  </div>
  {price_field}
  {source_html}
  {range_field}
  {entry_field}
  {tech_field}
  {claude_field}
  {flow_field}
  {news_field}
</div>'''


def build_sections(entries):
    by_sector = {}
    for idx, e in enumerate(entries):
        by_sector.setdefault(e["sector"], []).append((idx, e))
    html = ""
    for sector in sorted(by_sector.keys()):
        items = by_sector[sector]
        html += f'<section><div class="sec-title"><h2>{sector}</h2><div class="count">{len(items)} activo(s)</div></div><div class="grid">'
        for idx, e in items:
            html += render_card(e, idx)
        html += "</div></section>"
    return html


def main():
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        watchlist = json.load(f)

    print(f"Procesando {len(watchlist)} activos...")
    entries = []
    for item in watchlist:
        print(f" - {item['ticker']}")
        entries.append(build_entry(item))

    sections_html = build_sections(entries)

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    output = template.replace("<!--SECTIONS-->", sections_html)
    output = output.replace("<!--LAST-UPDATED-->", now_str)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"index.html generado con {len(entries)} activos.")


if __name__ == "__main__":
    main()
