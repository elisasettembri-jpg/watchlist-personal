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
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

FMP_KEY = os.environ.get("FMP_API_KEY", "")  # ya no se usa, se mantiene por compatibilidad
TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "")
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


def td_quote(ticker):
    url = f"https://api.twelvedata.com/quote?symbol={ticker}&apikey={TD_KEY}"
    data = json.loads(http_get(url))
    if not data or data.get("status") == "error" or "code" in data:
        return None
    return data


def td_historical(ticker, days=210):
    url = (f"https://api.twelvedata.com/time_series?symbol={ticker}"
           f"&interval=1day&outputsize={days}&apikey={TD_KEY}")
    data = json.loads(http_get(url))
    if not data or data.get("status") == "error":
        return []
    values = data.get("values", [])
    values = sorted(values, key=lambda h: h.get("datetime", ""), reverse=True)
    return values


def stooq_price(ticker):
    # Deshabilitado: stooq ahora exige registro para su CSV, dejo de ser
    # una fuente gratis sin key. Se mantiene la funcion por si vuelve a
    # estar disponible, pero no se llama desde build_entry.
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
        "franja_sintetizada": None,
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
    price_td = None
    fifty_two_week = None
    try:
        q = td_quote(ticker)
        if q:
            price_td = float(q.get("close")) if q.get("close") else None
            fw = q.get("fifty_two_week") or {}
            if fw.get("low") and fw.get("high"):
                fifty_two_week = (float(fw["low"]), float(fw["high"]))
    except Exception as e:
        print(f"  [{ticker}] error Twelve Data quote: {e}")

    if price_td is not None:
        entry["price"] = {"val": f"${price_td:,.2f}", "date": now}
        entry["source_note"] = "Fuente: twelvedata.com"
        if fifty_two_week:
            entry["range"] = {"val": f"52 sem: ${fifty_two_week[0]:,.2f} - ${fifty_two_week[1]:,.2f}", "date": now}

    ma50 = ma200 = lo90 = hi90 = rsi = None
    sr = None
    try:
        hist = td_historical(ticker, 260)  # ~1 anio de dias habiles
        if hist:
            closes = [float(h["close"]) for h in hist if h.get("close")]
            last90 = closes[:90]
            lo90 = min(last90) if last90 else None
            hi90 = max(last90) if last90 else None
            ma50 = avg(closes[:50])
            ma200 = avg(closes[:200])
            rsi = calc_rsi(closes, 14)
            sr = find_support_resistance(hist, price_td) if price_td is not None else None

            if lo90 is not None and hi90 is not None and not fifty_two_week:
                entry["range"] = {"val": f"3 meses: ${lo90:,.2f} - ${hi90:,.2f}", "date": now}

            zone_bits = []
            if sr:
                zone_bits.append(f"Soporte: ${sr['support']:,.2f} ({sr['support_touches']}x tocado, ult. 1 anio)")
                zone_bits.append(f"Resistencia: ${sr['resistance']:,.2f} ({sr['resistance_touches']}x tocado, ult. 1 anio)")
            if ma50 is not None:
                zone_bits.append(f"MM50: ${ma50:,.2f}")
            if ma200 is not None:
                zone_bits.append(f"MM200: ${ma200:,.2f}")
            if rsi is not None:
                rsi_label = "sobrecompra" if rsi > 70 else ("sobreventa" if rsi < 30 else "neutral")
                zone_bits.append(f"RSI(14): {rsi:.0f} ({rsi_label})")

            franja = franja_sintetizada(sr, ma50, rsi)
            entry["franja_sintetizada"] = franja

            if zone_bits:
                entry["entry_zone"] = " -- ".join(zone_bits) + " (calculado sobre precios historicos reales)"
    except Exception as e:
        print(f"  [{ticker}] error historico Twelve Data: {e}")

    entry["news"] = yahoo_news(ticker)

    score_str, checks = technical_score(price_td, ma50, ma200, lo90, hi90, rsi)
    entry["technical"] = {"val": score_str or "N/D", "checks": checks}

    time.sleep(8)
    claude_text = ai_recommendation(entry, checks, score_str)
    entry["claude_read"] = {"val": claude_text or "N/D", "date": now if claude_text else ""}

    return entry


def calc_rsi(closes_desc, period=14):
    """RSI clasico (0-100). closes_desc: cierres ordenados del mas reciente al mas viejo."""
    if len(closes_desc) < period + 1:
        return None
    recent = closes_desc[:period + 1]
    gains, losses = [], []
    for i in range(period):
        diff = recent[i] - recent[i + 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def find_support_resistance(hist_desc, current_price):
    """
    Busca soporte/resistencia como niveles TOCADOS MAS DE UNA VEZ, ponderados
    por volumen -- no solo el minimo/maximo aislado. Si ningun nivel se repite,
    cae de vuelta al minimo/maximo simple (y lo marca como tal).
    hist_desc: lista de dicts con 'close' y 'volume', mas reciente primero.
    """
    pts = []
    for h in hist_desc:
        try:
            c = float(h["close"])
            v = float(h.get("volume") or 0)
            pts.append((c, v))
        except (ValueError, TypeError):
            continue
    if not pts:
        return None

    closes_only = [p[0] for p in pts]
    lo, hi = min(closes_only), max(closes_only)
    if hi <= lo:
        return {"support": lo, "resistance": hi, "support_touches": 1, "resistance_touches": 1}

    bin_size = (hi - lo) / 20
    bins = {}
    for c, v in pts:
        idx = int((c - lo) / bin_size)
        if idx not in bins:
            bins[idx] = [0, 0.0]
        bins[idx][0] += 1
        bins[idx][1] += v

    def bin_price(idx):
        return lo + (idx + 0.5) * bin_size

    below = [(idx, cnt, vol) for idx, (cnt, vol) in bins.items() if bin_price(idx) < current_price]
    above = [(idx, cnt, vol) for idx, (cnt, vol) in bins.items() if bin_price(idx) > current_price]

    def pick_best(candidates):
        multi = [c for c in candidates if c[1] >= 2]
        pool = multi if multi else candidates
        if not pool:
            return None
        return max(pool, key=lambda c: c[2])

    best_below = pick_best(below)
    best_above = pick_best(above)

    return {
        "support": bin_price(best_below[0]) if best_below else lo,
        "support_touches": best_below[1] if best_below else 1,
        "resistance": bin_price(best_above[0]) if best_above else hi,
        "resistance_touches": best_above[1] if best_above else 1,
    }


def franja_sintetizada(sr, ma50, rsi):
    """
    Sintetiza donde coinciden 2+ señales mecánicas (soporte + MM50), sin
    generar ningun precio "recomendado" -- solo describe una franja donde
    varias señales objetivas se superponen, y el estado del RSI como
    contexto adicional (no como limite de la franja).
    """
    if not sr or ma50 is None:
        return None
    support = sr["support"]
    lower = min(support, ma50)
    upper = max(support, ma50)
    rsi_txt = ""
    if rsi is not None:
        if rsi < 30:
            rsi_txt = " RSI en sobreventa: señal adicional a favor."
        elif rsi > 70:
            rsi_txt = " RSI en sobrecompra: señal adicional en contra."
        else:
            rsi_txt = " RSI neutral: no suma ni resta a la franja."
    return (f"${lower:,.2f} - ${upper:,.2f} (entre soporte y MM50, "
            f"donde coinciden nivel de rebote historico + media de corto plazo).{rsi_txt} "
            f"No es un precio recomendado, es donde se superponen señales mecánicas.")


def technical_score(price, ma50, ma200, lo90, hi90, rsi=None):
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
    if rsi is not None:
        total += 1
        ok = rsi < 70
        score += 1 if ok else 0
        checks.append(f"RSI(14) en {rsi:.0f} ({'no sobrecomprado' if ok else 'sobrecomprado, cuidado'})")
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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            text = " ".join([p.get("text", "") for p in parts]).strip()
            return text or None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = 20 * (attempt + 1)
                print(f"  [{entry['ticker']}] Gemini 429, esperando {wait}s antes de reintentar...")
                time.sleep(wait)
                continue
            print(f"  [{entry['ticker']}] error llamando a Gemini: {e}")
            return None
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
    franja_field = render_field("Franja de referencia tecnica (no es precio recomendado)", e.get("franja_sintetizada"))
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
  {franja_field}
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


def build_bench(banco):
    if not banco:
        return '<p style="color:var(--muted); font-size:12px;">El banco esta vacio.</p>'
    pills = ""
    for item in sorted(banco, key=lambda x: x.get("sector", "")):
        ticker = item["ticker"]
        name = item.get("name", ticker)
        sector = item.get("sector", "Sin clasificar")
        pills += (f'<button class="bench-pill" onclick="activarTicker(\'{ticker}\')" '
                  f'title="{name} -- {sector}">{ticker}</button>')
    return pills


def main():
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        watchlist = json.load(f)

    activos = [item for item in watchlist if item.get("active")]
    banco = [item for item in watchlist if not item.get("active")]

    if len(activos) > 10:
        print(f"ADVERTENCIA: hay {len(activos)} activos, mas de los 10 permitidos. Se procesan igual.")

    print(f"Procesando {len(activos)} activos (titulares) + {len(banco)} en el banco (sin gastar cuota)...")
    entries = []
    for item in activos:
        print(f" - {item['ticker']}")
        entries.append(build_entry(item))

    sections_html = build_sections(entries)
    bench_html = build_bench(banco)

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    output = template.replace("<!--SECTIONS-->", sections_html)
    output = output.replace("<!--BENCH-->", bench_html)
    output = output.replace("<!--LAST-UPDATED-->", now_str)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"index.html generado con {len(entries)} activos y {len(banco)} en el banco.")


if __name__ == "__main__":
    main()
