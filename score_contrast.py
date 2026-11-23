#!/usr/bin/env python3
"""
score_contrast.py - robust contrast view for see_also search scores.

Reads a JSON file with the COMPLETE hit list, estimates the noise floor and
the spread robustly, and draws the top N as a horizontal bar chart in an
HTML/SVG page (browser tab).

    z_i = (x_i - m) / sigma      m = median(x)
                                 sigma = 1.4826 * MAD    (fallback: IQR/1.349)
    h_i = max(0, z_i)            cut at tau

Important: m and sigma are estimated over ALL scores passed in; only the top N
are drawn. If the list is cut short beforehand, the floor moves into the
transition zone and the hits fall below the threshold.

Usage:
    python3 score_contrast.py scores.json
    python3 score_contrast.py scores.json --top 10 --tau 3.0
    python3 score_contrast.py scores.json --no-open        # only print the path

Input formats (all accepted):
    [0.780, 0.773, ...]
    {"scores": [0.780, 0.773, ...]}
    [{"score": 0.780, "label": "..."}, ...]

stdout: absolute path of the HTML file written.
Exit 0 = ok, 1 = input error, 2 = no separable structure (the page says so).
"""

import argparse
import html
import json
import os
import statistics as st
import sys
import tempfile
import webbrowser

# ── Palette: dunkel, Terminal-Herkunft, gedämpft ──────────────────────────
C = {
    # G1: an static/see_also.css angeglichen (Schiefer, Messing als Akzent)
    "ground":  "#1c1f23",
    "panel":   "#23272c",
    "rule":    "#353b42",
    "grid":    "#2c3137",
    "title":   "#c8a46a",   # Messing
    "text":    "#d8dce1",
    "signal":  "#c8a46a",   # Balken: Treffer ueber der Schwelle
    "noise":   "#5d6670",   # Balken: Rauschen
    "thresh":  "#d99a5b",   # tau-Linie
}
MONO = ("'Avenir Next', 'Avenir', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif")


# ── Eingabe ───────────────────────────────────────────────────────────────
def load_scores(path):
    """Liest Scores tolerant aus JSON. Gibt absteigend sortierte Liste zurück."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict):
        data = data.get("scores", data.get("results", data.get("hits")))
        if data is None:
            raise ValueError("JSON object without a key 'scores'/'results'/'hits'")

    if not isinstance(data, list):
        raise ValueError("Expected a list of scores")

    out = []
    for item in data:
        if isinstance(item, (int, float)):
            out.append(float(item))
        elif isinstance(item, dict):
            v = item.get("score", item.get("value", item.get("similarity")))
            if v is None:
                raise ValueError("Object without a key 'score'/'value'/'similarity'")
            out.append(float(v))
        else:
            raise ValueError(f"Unusable entry: {item!r}")

    if not out:
        raise ValueError("Empty score list")

    # Die Vorschrift setzt monotone Eingabe voraus — hier erzwungen.
    return sorted(out, reverse=True)


# ── Schritt 1+2: Boden und robuste Streuung ───────────────────────────────
def robust_scale(x):
    """
    Rauschboden m und robuste Streuung sigma.
    Rückgabe: (m, sigma, methode). sigma = 0.0 heißt: keine trennbare Struktur.
    """
    m = st.median(x)

    mad = st.median([abs(v - m) for v in x])
    if mad > 0:
        return m, 1.4826 * mad, "MAD"

    # Fallback aus Abschnitt 2: mehr als die Hälfte der Werte identisch.
    if len(x) >= 4:
        s = sorted(x)
        q1 = st.median(s[: len(s) // 2])
        q3 = st.median(s[(len(s) + 1) // 2:])
        iqr = q3 - q1
        if iqr > 0:
            return m, iqr / 1.349, "IQR"

    return m, 0.0, "none"


def contrast(scores, tau):
    """Vollständige Berechnungsvorschrift. Gibt dict mit allen Zwischengrößen."""
    m, sigma, method = robust_scale(scores)
    if sigma == 0.0:
        return {"m": m, "sigma": 0.0, "method": method,
                "z": None, "h": None, "signal": None}

    z = [(v - m) / sigma for v in scores]
    h = [max(0.0, v) for v in z]
    signal = [v >= tau for v in z]
    return {"m": m, "sigma": sigma, "method": method,
            "z": z, "h": h, "signal": signal}


# ── SVG ───────────────────────────────────────────────────────────────────
def build_svg(scores, res, tau, top):
    n = min(top, len(scores))
    z, h, sig = res["z"][:n], res["h"][:n], res["signal"][:n]

    # Geometrie — Zeichenfläche bleibt 720 Einheiten breit, damit die
    # vergrößerte Schrift am Schirm auch wirklich größer erscheint.
    W = 720
    pad_l, pad_r = 20, 20
    col_score = 78          # Breite der Score-Spalte links
    gap = 18
    x0 = pad_l + col_score + gap        # Balkenstart
    x1 = W - pad_r - 48                 # Balkenende (Platz für z-Wert)
    row_h, bar_h = 36, 18
    y0 = 48                             # erste Zeile
    axis_h = 46
    H = y0 + n * row_h + axis_h

    # Skala: tau muss immer sichtbar sein, sonst verliert die Linie ihren Sinn
    hmax = max(h) if h else 0.0
    xmax = max(tau * 1.25, hmax * 1.08, 1.0)
    def X(v):
        return x0 + (x1 - x0) * (v / xmax)

    p = []
    p.append(f'<svg viewBox="0 0 {W} {H}" width="100%" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" '
             f'aria-label="Contrast view: {n} hits as bars, '
             f'threshold at {tau} sigma">')

    # Spaltenköpfe
    p.append(f'<text x="{pad_l + col_score}" y="{y0 - 18}" text-anchor="end" '
             f'font-family="{MONO}" font-size="14" fill="{C["text"]}" '
             f'>Score</text>')
    p.append(f'<text x="{x0}" y="{y0 - 18}" font-family="{MONO}" font-size="14" '
             f'fill="{C["text"]}">'
             f'Height above the noise floor</text>')

    # Gitterlinien in ganzen Sigma
    tick = 1
    while tick < xmax:
        gx = X(tick)
        p.append(f'<line x1="{gx:.1f}" y1="{y0 - 4}" x2="{gx:.1f}" '
                 f'y2="{y0 + n * row_h + 4}" stroke="{C["grid"]}" stroke-width="1"/>')
        tick += 1

    # tau-Linie — das Signaturelement
    tx = X(tau)
    p.append(f'<line x1="{tx:.1f}" y1="{y0 - 8}" x2="{tx:.1f}" '
             f'y2="{y0 + n * row_h + 6}" stroke="{C["thresh"]}" '
             f'stroke-width="1.4" stroke-dasharray="4 4" opacity="0.85"/>')

    # Zeilen
    for i in range(n):
        yc = y0 + i * row_h + row_h / 2
        col = C["signal"] if sig[i] else C["noise"]

        # Score links, tabellarisch
        p.append(f'<text x="{pad_l + col_score}" y="{yc + 5.5}" text-anchor="end" '
                 f'font-family="{MONO}" font-size="18" '
                 f'font-variant-numeric="tabular-nums" '
                 f'fill="{C["text"]}">'
                 f'{scores[i]:.3f}</text>')

        # Balken (Mindeststummel, damit die Zeile nicht leer wirkt)
        bw = max(X(h[i]) - x0, 2.0)
        p.append(f'<rect x="{x0}" y="{yc - bar_h/2:.1f}" width="{bw:.1f}" '
                 f'height="{bar_h}" rx="2" fill="{col}"/>')

        # z-Wert am Balkenende
        p.append(f'<text x="{x0 + bw + 9:.1f}" y="{yc + 5:.1f}" '
                 f'font-family="{MONO}" font-size="15" '
                 f'font-variant-numeric="tabular-nums" '
                 f'fill="{C["text"]}">'
                 f'{z[i]:.1f}</text>')

    # Achse
    ay = y0 + n * row_h + 16
    p.append(f'<line x1="{x0}" y1="{ay}" x2="{x1}" y2="{ay}" '
             f'stroke="{C["rule"]}" stroke-width="1"/>')
    tick = 0
    while tick < xmax:
        gx = X(tick)
        p.append(f'<text x="{gx:.1f}" y="{ay + 20}" text-anchor="middle" '
                 f'font-family="{MONO}" font-size="14" fill="{C["text"]}">'
                 f'{tick}σ</text>')
        tick += 1
    p.append(f'<text x="{tx:.1f}" y="{y0 - 18}" text-anchor="middle" '
             f'font-family="{MONO}" font-size="14" fill="{C["text"]}">'
             f'τ={tau:g}</text>')

    p.append("</svg>")
    return "".join(p)


def build_page(scores, res, tau, top, n_total, src):
    ok = res["sigma"] > 0
    shown = min(top, len(scores))

    if ok:
        body = build_svg(scores, res, tau, top)
        meta = (f'Noise floor m = {res["m"]:.4f}, '
                f'spread σ&#770; = {res["sigma"]:.4f} ({res["method"]}); '
                f'top {shown} of {n_total} documents')
        foot = (f'z = (score − m) / σ&#770;, with σ&#770; = 1.4826 × MAD; '
                f'bars show max(0, z). Hits to the right of τ = {tau:g} stand out from the noise.')
    else:
        body = (f'<div class="empty">No hit stands out.<br>'
                f'<span>More than half of the scores are identical, so neither MAD nor IQR '
                f'gives a spread above 0.</span></div>')
        meta = f'Noise floor m = {res["m"]:.4f}; {n_total} documents'
        foot = f'The threshold would be τ = {tau:g}'

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contrast view: {html.escape(os.path.basename(src))}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  /* zoom statt font-size:%, da h1/.sub/.foot und alle SVG-<text>-Elemente
     feste px-Werte tragen, die eine prozentuale Vorgabe auf body nicht
     erben wuerden. zoom skaliert stattdessen visuell alles einheitlich -
     an die see-also-weite 80%-Konvention angeglichen (siehe index.html,
     html/body Regel dort). */
  body {{ margin: 0; background: {C['ground']}; color: {C['text']};
         font-family: {MONO}; display: flex; align-items: flex-start;
         justify-content: center; padding: 22px 18px; min-height: 100vh;
         zoom: 1.0; }}
  .card {{ width: 100%; max-width: 780px; background: {C['panel']};
           border: 1px solid {C['rule']}; border-radius: 3px; padding: 24px 26px; }}
  h1 {{ font-family: 'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif;
        font-size: 22px; font-weight: normal; color: {C['title']}; margin: 0 0 6px; }}
  .sub {{ font-size: 14px; color: #b4bcc5; margin: 0 0 20px; }}
  .foot {{ margin-top: 20px; padding-top: 15px; border-top: 1px solid {C['rule']};
           font-size: 14px; line-height: 1.6; color: #b4bcc5; }}
  .empty {{ padding: 52px 8px; text-align: center; font-size: 19px;
            color: {C['thresh']}; }}
  .empty span {{ display: block; margin-top: 10px; font-size: 16px;
                 color: {C['text']}; white-space: normal; }}
  @media (max-width: 640px) {{
    .card {{ padding: 16px 14px; }}
    .sub, .foot {{ white-space: normal; }}
  }}
</style></head>
<body><div class="card">
  <h1>Do the best hits stand out?</h1>
  <p class="sub">{meta}</p>
  {body}
  <p class="foot">{foot}</p>
</div></body></html>"""


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_path", help="path to the JSON file with the complete list")
    ap.add_argument("--top", type=int, default=10, help="number of bars drawn (default 10)")
    ap.add_argument("--tau", type=float, default=3.0, help="threshold in sigma (default 3.0)")
    ap.add_argument("--out", default=None, help="path of the HTML file to write")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    a = ap.parse_args()

    try:
        scores = load_scores(a.json_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"score_contrast: cannot read input - {e}", file=sys.stderr)
        return 1

    res = contrast(scores, a.tau)
    page = build_page(scores, res, a.tau, a.top, len(scores), a.json_path)

    if a.out:
        out = os.path.abspath(a.out)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(page)
    else:
        fd, out = tempfile.mkstemp(prefix="see-also_kontrast_", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(page)

    if not a.no_open:
        webbrowser.open(f"file://{out}")

    print(out)
    return 0 if res["sigma"] > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
