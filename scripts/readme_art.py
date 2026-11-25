"""Generate Treant README artwork (logo, banner, diagrams) in light and dark variants.

Usage: python3 scripts/readme_art.py docs/assets
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(sys.argv[1])

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

LIGHT = {
    "name": "light",
    "bg": "#FFFFFF",
    "frame": "#E3E9E5",
    "card": "#FFFFFF",
    "border": "#D5DED8",
    "ink": "#0F172A",
    "muted": "#475569",
    "line": "#64748B",
    "forest": "#1F6F4A",
    "moss": "#3FA56B",
    "bark": "#7A5A40",
    "amber": "#B7791F",
    "amberfill": "#E8A33D",
    "red": "#B42318",
    "forest_t": "#EEF6F1",
    "forest_s": "#C3DECE",
    "bark_t": "#F7F2EC",
    "bark_s": "#E3D4C4",
    "amber_t": "#FDF5E6",
    "amber_s": "#F0D39E",
    "red_t": "#FDEEEC",
    "red_s": "#F2BDB6",
    "slate_t": "#F3F6F9",
    "slate_s": "#D6DEE7",
    "run": "#1F7A4D",
    "run_t": "#E3F3E9",
    "ask": "#9A6200",
    "ask_t": "#FCEFD3",
    "deny": "#B42318",
    "deny_t": "#FBE3E0",
    "canopy": "#DCEFE3",
    "logo_edge": "#1F6F4A",
    "logo_node_a": "#E8A33D",
    "logo_node_b": "#3FA56B",
    "logo_node_d": "#1F6F4A",
    "logo_bark": "#7A5A40",
    "shadow": True,
}
DARK = {
    "name": "dark",
    "bg": "#0D1117",
    "frame": "#30363D",
    "card": "#161B22",
    "border": "#30363D",
    "ink": "#E6EDF3",
    "muted": "#A3AEBA",
    "line": "#8B949E",
    "forest": "#56C78F",
    "moss": "#7AD7A5",
    "bark": "#D4A77F",
    "amber": "#F2B85B",
    "amberfill": "#F2B85B",
    "red": "#FF7B72",
    "forest_t": "#0F2219",
    "forest_s": "#24533B",
    "bark_t": "#1F1913",
    "bark_s": "#4D3B2A",
    "amber_t": "#231C10",
    "amber_s": "#5E4720",
    "red_t": "#2A1416",
    "red_s": "#6E2B2D",
    "slate_t": "#141A22",
    "slate_s": "#30363D",
    "run": "#6FDDA0",
    "run_t": "#12301F",
    "ask": "#F5C26B",
    "ask_t": "#33280F",
    "deny": "#FF8A80",
    "deny_t": "#3A1719",
    "canopy": "#173826",
    "logo_edge": "#56C78F",
    "logo_node_a": "#F2B85B",
    "logo_node_b": "#7AD7A5",
    "logo_node_d": "#2FA66C",
    "logo_bark": "#C9976B",
    "shadow": False,
}

ACCENTS = {  # card kind -> (accent, tint, tint stroke)
    "forest": ("forest", "forest_t", "forest_s"),
    "bark": ("bark", "bark_t", "bark_s"),
    "amber": ("amber", "amber_t", "amber_s"),
    "red": ("red", "red_t", "red_s"),
    "slate": ("muted", "slate_t", "slate_s"),
}


def e(s: str) -> str:
    return escape(s, {'"': "&quot;"})


class Doc:
    def __init__(self, t: dict, w: int, h: int, title: str, desc: str, frame: bool = True):
        self.t, self.w, self.h = t, w, h
        self.parts: list[str] = []
        self.title, self.desc, self.frame = title, desc, frame

    def add(self, s: str) -> None:
        self.parts.append(s)

    def style(self) -> str:
        t = self.t
        return f"""<style>
.s{{font-family:{SANS};}}
.m{{font-family:{MONO};}}
.title{{font-family:{SANS};font-size:17px;font-weight:700;fill:{t["ink"]};}}
.body{{font-family:{SANS};font-size:14.5px;fill:{t["muted"]};}}
.mono{{font-family:{MONO};font-size:14px;fill:{t["ink"]};}}
.glabel{{font-family:{SANS};font-size:14px;font-weight:700;letter-spacing:1.4px;}}
.alabel{{font-family:{SANS};font-size:14px;font-weight:600;fill:{t["muted"]};}}
.caption{{font-family:{SANS};font-size:14px;font-weight:700;letter-spacing:1.6px;fill:{t["muted"]};}}
</style>"""

    def defs(self) -> str:
        t = self.t
        markers = []
        for key in ("line", "forest", "red", "amber", "muted", "run"):
            c = t[key]
            markers.append(
                f'<marker id="ah-{key}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" '
                f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0.8 L9.2,5 L0,9.2 Z" fill="{c}"/></marker>'
            )
        shadow = ""
        if t["shadow"]:
            shadow = (
                '<filter id="sh" x="-10%" y="-10%" width="120%" height="130%">'
                '<feDropShadow dx="0" dy="1.5" stdDeviation="2" flood-color="#0F172A" flood-opacity="0.07"/></filter>'
            )
        return "<defs>" + "".join(markers) + shadow + "</defs>"

    def render(self) -> str:
        t = self.t
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="ttl dsc">'
            f'<title id="ttl">{e(self.title)}</title><desc id="dsc">{e(self.desc)}</desc>'
        )
        bg = ""
        if self.frame:
            bg = (
                f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="20" '
                f'fill="{t["bg"]}" stroke="{t["frame"]}"/>'
            )
        return head + self.style() + self.defs() + bg + "\n".join(self.parts) + "</svg>\n"

    # ---------- primitives ----------
    def text(
        self, x, y, s, cls="body", anchor="start", fill=None, weight=None, size=None, extra=""
    ):
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        f = f' fill="{fill}"' if fill else ""
        wgt = f' font-weight="{weight}"' if weight else ""
        sz = f' font-size="{size}"' if size else ""
        self.add(f'<text x="{x}" y="{y}" class="{cls}"{a}{f}{wgt}{sz}{extra}>{e(s)}</text>')

    def rect(self, x, y, w, h, fill, stroke=None, rx=12, dash=None, shadow=False, sw=1.2):
        st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        d = f' stroke-dasharray="{dash}"' if dash else ""
        f = ' filter="url(#sh)"' if shadow and self.t["shadow"] else ""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{st}{d}{f}/>'
        )

    def group(self, x, y, w, h, label, kind="forest", dash=None, label_x=None, bottom=False):
        acc, tint, ts = ACCENTS[kind]
        t = self.t
        self.rect(x, y, w, h, t[tint], t[ts], rx=18, dash=dash)
        ly = y + h - 16 if bottom else y + 28
        self.text(
            label_x if label_x is not None else x + 20, ly, label.upper(), cls="glabel", fill=t[acc]
        )

    def card(
        self,
        x,
        y,
        w,
        h,
        title,
        lines=(),
        kind="forest",
        mono_title=False,
        mono_lines=False,
        center=False,
        title_size=None,
        fill=None,
        stroke=None,
        top=False,
    ):
        t = self.t
        acc, _tint, ts = ACCENTS[kind]
        self.rect(x, y, w, h, fill or t["card"], stroke or t[ts], rx=12, shadow=True)
        # accent bar on the left
        self.add(
            f'<rect x="{x + 1}" y="{y + 10}" width="4" height="{h - 20}" rx="2" fill="{t[acc]}"/>'
        )
        n = 1 + len(lines)
        lh = 20
        block = 22 + (len(lines)) * lh
        ty = y + (h - block) / 2 + 16
        if top:
            ty = y + 30
        tx, anchor = (x + w / 2, "middle") if center else (x + 18, "start")
        tcls = "mono" if mono_title else "title"
        extra = ' font-weight="700"' if mono_title else ""
        sz = (
            f' font-size="{title_size}"'
            if title_size
            else (' font-size="15.5px"' if mono_title else "")
        )
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(f'<text x="{tx}" y="{ty:.1f}" class="{tcls}"{a}{extra}{sz}>{e(title)}</text>')
        for i, ln in enumerate(lines):
            cls = "mono" if mono_lines else "body"
            fill = f' fill="{t["muted"]}"' if mono_lines else ""
            self.add(
                f'<text x="{tx}" y="{ty + 22 + i * lh:.1f}" class="{cls}"{a}{fill}>{e(ln)}</text>'
            )
        del n

    def pill(self, x, y, w, h, s, fg, bg, stroke=None, cls="s", size=14, weight=600, mono=False):
        self.rect(x, y, w, h, bg, stroke, rx=h / 2)
        c = "m" if mono else "s"
        self.add(
            f'<text x="{x + w / 2}" y="{y + h / 2 + size * 0.35:.1f}" class="{c}" text-anchor="middle" '
            f'font-size="{size}px" font-weight="{weight}" fill="{fg}">{e(s)}</text>'
        )

    def cylinder(
        self, x, y, w, h, title, lines=(), kind="bark", dash=None, mono_title=True, mark=None
    ):
        t = self.t
        acc, tint, ts = ACCENTS[kind]
        ry = 11
        fill, stroke = t["card"], t[ts]
        d = f' stroke-dasharray="{dash}"' if dash else ""
        f = ' filter="url(#sh)"' if t["shadow"] else ""
        self.add(
            f'<path d="M{x},{y + ry} A{w / 2},{ry} 0 0 1 {x + w},{y + ry} V{y + h - ry} '
            f'A{w / 2},{ry} 0 0 1 {x},{y + h - ry} Z" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{d}{f}/>'
        )
        self.add(
            f'<ellipse cx="{x + w / 2}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" fill="{t[tint]}" '
            f'stroke="{stroke}" stroke-width="1.4"{d}/>'
        )
        cx = x + w / 2
        block = 22 + len(lines) * 20
        ty = y + ry * 2 + (h - ry * 3 - block) / 2 + 16
        cls = "mono" if mono_title else "title"
        self.add(
            f'<text x="{cx}" y="{ty:.1f}" class="{cls}" text-anchor="middle" font-weight="700" '
            f'font-size="15.5px" fill="{t[acc] if kind != "slate" else t["ink"]}">{e(title)}</text>'
        )
        for i, ln in enumerate(lines):
            self.add(
                f'<text x="{cx}" y="{ty + 22 + i * 20:.1f}" class="body" text-anchor="middle">{e(ln)}</text>'
            )
        if mark:
            self.badge(x + w - 20, y + 34, mark)

    def badge(self, cx, cy, mark):
        t = self.t
        if mark == "ok":
            self.add(
                f'<circle cx="{cx}" cy="{cy}" r="11" fill="{t["run_t"]}" stroke="{t["run"]}" stroke-width="1.5"/>'
                f'<path d="M{cx - 5},{cy} l3.5,3.5 l6.5,-7" fill="none" stroke="{t["run"]}" stroke-width="2.4" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
        else:
            self.add(
                f'<circle cx="{cx}" cy="{cy}" r="11" fill="{t["deny_t"]}" stroke="{t["deny"]}" stroke-width="1.5"/>'
                f'<path d="M{cx - 4.5},{cy - 4.5} l9,9 M{cx + 4.5},{cy - 4.5} l-9,9" stroke="{t["deny"]}" '
                f'stroke-width="2.4" stroke-linecap="round"/>'
            )

    def arrow(self, pts, color="line", dash=None, width=1.8, r=12, head=True):
        t = self.t
        d = f"M{pts[0][0]},{pts[0][1]}"
        for i in range(1, len(pts) - 1):
            (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]

            def unit(ax, ay, bx, by):
                dx, dy = bx - ax, by - ay
                ln = (dx * dx + dy * dy) ** 0.5 or 1
                return dx / ln, dy / ln, ln

            ux, uy, l1 = unit(x1, y1, x0, y0)
            vx, vy, l2 = unit(x1, y1, x2, y2)
            rr = min(r, l1 / 2, l2 / 2)
            d += f" L{x1 + ux * rr:.1f},{y1 + uy * rr:.1f} Q{x1},{y1} {x1 + vx * rr:.1f},{y1 + vy * rr:.1f}"
        d += f" L{pts[-1][0]},{pts[-1][1]}"
        ds = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#ah-{color})"' if head else ""
        self.add(
            f'<path d="{d}" fill="none" stroke="{t[color]}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round"{ds}{m}/>'
        )

    def alabel(self, x, y, s, anchor="middle", color=None, bg=True):
        t = self.t
        w = len(s) * 7.3 + 14
        x0 = x - w / 2 if anchor == "middle" else (x if anchor == "start" else x - w)
        if bg:
            self.rect(x0, y - 15, w, 22, t["bg"], None, rx=6)
        tx = x0 + w / 2
        fill = f' fill="{t[color]}"' if color else ""
        self.add(
            f'<text x="{tx}" y="{y + 0.5}" class="alabel" text-anchor="middle"{fill}>{e(s)}</text>'
        )


# ---------------------------------------------------------------- logo
def logo_group(t: dict) -> str:
    """Tree: lobed crown holding a node-link graph, trunk flaring at the base, roots spreading
    sideways onto three stacked strata (the schemas). Variant B of logo_variants.py."""
    dark = t["name"] == "dark"
    crown = "#3FA56B"
    line = "#EAF7EF" if dark else "#FFFFFF"
    amber = t["amberfill"]
    bark = t["logo_bark"]
    circles = [(64, 33, 24), (41, 46, 19), (87, 46, 19), (50, 64, 16), (78, 64, 16), (64, 58, 18)]
    s = "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{crown}"/>' for x, y, r in circles)
    s += f'<path d="M58 72 C58 88 57 97 49 106 H79 C71 97 70 88 70 72 Z" fill="{bark}"/>'
    s += (
        f'<g stroke="{bark}" stroke-width="4.5" stroke-linecap="round" fill="none">'
        '<path d="M53 104 C44 106 36 106 28 104"/><path d="M75 104 C84 106 92 106 100 104"/></g>'
        f'<rect x="20" y="111" width="88" height="5" rx="2.5" fill="{bark}" fill-opacity="0.75"/>'
        f'<rect x="30" y="120" width="68" height="5" rx="2.5" fill="{bark}" fill-opacity="0.5"/>'
    )
    nodes = [(64, 28), (42, 48), (86, 48), (64, 62)]
    edges = [(0, 1), (0, 2), (1, 3), (2, 3), (0, 3)]
    s += f'<g stroke="{line}" stroke-width="3" stroke-linecap="round" stroke-opacity="0.92">'
    for a, b in edges:
        s += f'<line x1="{nodes[a][0]}" y1="{nodes[a][1]}" x2="{nodes[b][0]}" y2="{nodes[b][1]}"/>'
    s += "</g>"
    for i, (x, y) in enumerate(nodes):
        s += f'<circle cx="{x}" cy="{y}" r="{5.8 if i == 0 else 4.6}" fill="{amber if i == 0 else line}"/>'
    return s


def logo(t: dict) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128" role="img" '
        f'aria-labelledby="ttl"><title id="ttl">Treant logo</title>{logo_group(t)}</svg>\n'
    )


# ---------------------------------------------------------------- banner
def banner(t: dict) -> str:
    W, H = 1280, 400
    dark = t["name"] == "dark"
    g0, g1 = ("#FFFFFF", "#E9F4ED") if not dark else ("#0D1117", "#10241A")
    net = t["forest"]
    p = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
            f'aria-labelledby="ttl dsc"><title id="ttl">Treant</title>'
            f'<desc id="dsc">Treant: long-term graph memory for AI coding agents.</desc>'
        ),
        (
            f'<defs><linearGradient id="bgg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{g0}"/>'
            f'<stop offset="1" stop-color="{g1}"/></linearGradient></defs>'
        ),
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="24" fill="url(#bgg)" stroke="{t["frame"]}"/>',
    ]
    # faint decorative graph, top right
    nodes = [
        (1000, 52),
        (1078, 96),
        (1160, 48),
        (1224, 118),
        (1130, 160),
        (1040, 150),
        (1206, 214),
        (1252, 52),
    ]
    edges = [(0, 1), (1, 2), (1, 4), (2, 3), (3, 4), (0, 5), (5, 4), (3, 6), (2, 7), (3, 7)]
    op = "0.16" if not dark else "0.22"
    p.append(f'<g stroke="{net}" stroke-opacity="{op}" stroke-width="2.5">')
    for a, b in edges:
        p.append(
            f'<line x1="{nodes[a][0]}" y1="{nodes[a][1]}" x2="{nodes[b][0]}" y2="{nodes[b][1]}"/>'
        )
    p.append("</g>")
    for i, (x, y) in enumerate(nodes):
        fill = t["amberfill"] if i == 2 else net
        p.append(
            f'<circle cx="{x}" cy="{y}" r="{9 if i in (1, 3) else 6.5}" fill="{fill}" fill-opacity="{op}"/>'
        )
    # logo
    p.append(f'<g transform="translate(96 72) scale(2)">{logo_group(t)}</g>')
    # text
    x = 418
    p.append(
        f'<text x="{x}" y="186" font-family="{SANS}" font-size="104" font-weight="800" letter-spacing="-2" '
        f'fill="{t["ink"]}">Treant</text>'
    )
    p.append(
        f'<text x="{x + 4}" y="246" font-family="{SANS}" font-size="33" font-weight="500" '
        f'fill="{t["muted"]}">Long-term graph memory for AI coding agents</text>'
    )
    chips = ["MCP server", "Postgres + pgvector", "Isolated workspaces", "Command guard"]
    cx = x + 4
    for c in chips:
        w = len(c) * 10.6 + 34
        p.append(
            f'<rect x="{cx}" y="282" width="{w:.0f}" height="40" rx="20" fill="{t["forest_t"]}" '
            f'stroke="{t["forest_s"]}" stroke-width="1.5"/>'
        )
        p.append(
            f'<text x="{cx + w / 2:.0f}" y="308.5" font-family="{SANS}" font-size="19" font-weight="600" '
            f'text-anchor="middle" fill="{t["forest"]}">{e(c)}</text>'
        )
        cx += w + 14
    p.append("</svg>\n")
    return "\n".join(p)


# ---------------------------------------------------------------- architecture
def architecture(t: dict) -> str:
    d = Doc(
        t,
        1000,
        874,
        "Treant system overview",
        "Developer machine with Claude Code, Kiro, treant-client, treant-guard and the Keychain; "
        "a TLS reverse proxy; the Treant host running serve, dashboard, the owner CLI and the worker "
        "on Postgres with control, general and one schema per workspace; external embedding service, "
        "LLM gateway, Jev judge and OIDC provider; nightly backup and isolation test.",
    )
    d.text(24, 38, "SYSTEM OVERVIEW", cls="caption")

    # developer machine
    d.group(24, 56, 620, 214, "Developer machine", "forest")
    d.card(48, 96, 156, 44, "Claude Code", (), "forest")
    d.card(48, 150, 156, 44, "Kiro", (), "forest")
    d.card(48, 212, 156, 44, "Any MCP client", (), "slate")
    d.card(
        236,
        96,
        204,
        120,
        "treant-client",
        ("session hooks", "stdio MCP proxy", "save worker"),
        "forest",
    )
    d.card(472, 96, 152, 56, "treant-guard", ("local, no network",), "amber")
    d.card(472, 160, 152, 56, "Keychain", ("token per workspace",), "slate")
    d.arrow([(204, 118), (236, 118)], "forest")
    d.arrow([(204, 172), (220, 172), (220, 150), (236, 150)], "forest", r=8)
    d.arrow([(440, 124), (472, 124)], "line")
    d.arrow([(440, 188), (472, 188)], "line")

    # external services
    d.group(668, 56, 308, 268, "External services", "slate")
    ext = [
        ("Embedding service", "serve, worker, dashboard"),
        ("LLM gateway", "worker: distill, rewrite, judge"),
        ("Jev judge", "worker: gate score"),
        ("OIDC provider", "dashboard login"),
    ]
    for i, (a, b) in enumerate(ext):
        y = 90 + i * 56
        d.rect(688, y, 268, 50, t["card"], t["slate_s"], rx=10, shadow=True)
        d.add(f'<text x="704" y="{y + 21}" class="title" font-size="15px">{e(a)}</text>')
        d.add(f'<text x="704" y="{y + 40}" class="body" font-size="14px">{e(b)}</text>')

    # proxy
    d.card(48, 314, 572, 46, "Reverse proxy  ·  TLS", (), "slate", center=True)
    d.arrow([(338, 216), (338, 314)], "forest")
    d.alabel(338, 292, "MCP over HTTPS · bearer token", color="forest")
    d.arrow([(126, 256), (126, 314)], "line")

    # host
    d.group(24, 398, 952, 452, "Treant host  ·  Docker Compose", "forest", label_x=444)
    d.card(48, 440, 180, 72, "treant serve", ("MCP at /mcp",), "forest")
    d.card(244, 440, 180, 72, "treant dashboard", ("owner UI",), "forest")
    d.card(440, 440, 180, 72, "treant CLI", ("owner commands",), "forest")
    d.card(688, 440, 268, 72, "treant worker", ("embed · distill · apply · gate",), "forest")
    d.arrow([(138, 360), (138, 440)], "line")
    d.arrow([(334, 360), (334, 440)], "line")
    # worker -> externals
    d.arrow([(822, 440), (822, 324)], "amber", width=2)
    d.alabel(822, 370, "worker: LLM gateway, Jev, embeddings", color="amber")
    # serve and dashboard embed search queries: one subtle dashed connector to the embedding service
    ln = t["line"]
    d.add(
        f'<path d="M204 440 V380 H644" fill="none" stroke="{ln}" stroke-width="1.5" stroke-dasharray="4 4"/>'
    )
    d.add(
        f'<path d="M400 440 V380" fill="none" stroke="{ln}" stroke-width="1.5" stroke-dasharray="4 4"/>'
    )
    d.arrow([(644, 380), (656, 380), (656, 115), (688, 115)], "muted", dash="4 4", width=1.5, r=10)
    d.alabel(530, 380, "query embeddings", color="muted")

    # postgres
    d.group(48, 548, 908, 200, "Postgres 17 + pgvector", "bark")
    d.text(
        936,
        576,
        "app transactions run as one role (SET LOCAL ROLE)",
        cls="body",
        anchor="end",
        size="14px",
    )
    d.cylinder(
        72, 592, 268, 136, "control", ("tokens · jobs · audit", "review queue · canaries"), "bark"
    )
    d.cylinder(
        366,
        592,
        268,
        136,
        "general",
        ("shared concepts and facts", "written only via the gate"),
        "bark",
    )
    d.cylinder(
        660,
        592,
        272,
        136,
        "ws_<name>",
        ("personal · acme · startup", "one schema + role each"),
        "forest",
    )
    for x in (138, 334, 530, 822):
        d.arrow([(x, 512), (x, 548)], "line")

    # timers
    d.card(48, 776, 908, 52, "Nightly timers", (), "amber", title_size="15.5px")
    d.text(
        200,
        807,
        "backup: pg_dump per schema to restic   ·   treant isolation-test: canaries must stay unreachable",
        cls="body",
    )
    d.arrow([(502, 776), (502, 748)], "line")
    return d.render()


# ---------------------------------------------------------------- isolation
def isolation(t: dict) -> str:
    d = Doc(
        t,
        1000,
        838,
        "Treant workspace isolation",
        "A token is bound to one workspace and a set of actions. The bearer middleware looks up sha256 of "
        "the secret, the tool's action is checked, then the transaction runs as that workspace's role. "
        "The role reads and writes its own schema, reads general, inserts into control.jobs and "
        "control.mistakes, and has no grant on other workspaces. Only the gate and the owner write general. "
        "Canaries and the nightly isolation test check every pair of workspaces.",
    )
    d.text(24, 38, "WORKSPACE ISOLATION", cls="caption")
    xs = [48, 284, 520, 756]
    w = 196
    d.card(
        xs[0],
        60,
        w,
        112,
        "Token for acme",
        ("trt_<id>_<secret>", "actions: create, read"),
        "forest",
    )
    d.card(
        xs[1],
        60,
        w,
        112,
        "Bearer middleware",
        ("sha256(secret) lookup", "unknown token: 401"),
        "forest",
    )
    d.card(
        xs[2], 60, w, 112, "Action check", ("the tool's action must", "be in the token"), "forest"
    )
    d.card(
        xs[3],
        60,
        w,
        112,
        "SET LOCAL ROLE",
        ("treant_ws_acme", "login role inherits nothing"),
        "forest",
    )
    for i in range(3):
        d.arrow([(xs[i] + w, 116), (xs[i + 1], 116)], "forest")
    # forbidden
    d.arrow([(618, 172), (618, 204)], "red")
    d.pill(
        xs[2] - 2,
        204,
        200,
        36,
        '{"error": "forbidden"}',
        t["deny"],
        t["deny_t"],
        t["red_s"],
        mono=True,
        size=14,
    )

    # tool -> action table
    d.rect(48, 196, 432, 136, t["card"], t["border"], rx=12, shadow=True)
    d.text(66, 222, "Tool  →  action the token needs", cls="title", size="15.5px")
    rows = [
        ("create", "save_session, report_mistake"),
        ("read", "search, brief, warnings"),
        ("update", "update_fact"),
        ("delete", "retract"),
    ]
    for i, (a, tools) in enumerate(rows):
        y = 236 + i * 23
        d.pill(66, y, 78, 20, a, t["forest"], t["forest_t"], None, size=13.5, weight=700, mono=True)
        d.text(158, y + 15, tools, cls="mono", fill=t["muted"])

    d.group(24, 380, 952, 236, "Postgres  ·  what treant_ws_acme can touch", "bark", bottom=True)
    # bus from role to schemas
    centers = [x + w / 2 for x in xs]
    d.add(f'<path d="M{centers[3]},172 V356" fill="none" stroke="{t["line"]}" stroke-width="1.8"/>')
    d.add(
        f'<path d="M{centers[0] + 12},356 H{centers[3]}" fill="none" stroke="{t["line"]}" stroke-width="1.8"/>'
    )
    d.arrow([(centers[0] + 12, 356), (centers[0], 356), (centers[0], 400)], "red", dash="6 5", r=12)
    for c in centers[1:3]:
        d.arrow([(c, 356), (c, 400)], "line")
    d.arrow([(centers[3], 350), (centers[3], 400)], "forest", width=2)

    d.cylinder(
        xs[0],
        400,
        w,
        166,
        "other ws_*",
        ("personal, startup", "no grant at all"),
        "red",
        dash="6 4",
        mark="no",
    )
    d.cylinder(xs[1], 400, w, 166, "control", ("jobs, mistakes", "insert only"), "bark", mark="ok")
    d.cylinder(
        xs[2], 400, w, 166, "general", ("read only", "for every workspace"), "bark", mark="ok"
    )
    d.cylinder(
        xs[3], 400, w, 166, "ws_acme", ("read and write", "its own schema"), "forest", mark="ok"
    )

    # writers of general
    d.card(424, 636, 228, 62, "Worker gate", ("treant_publisher role",), "amber")
    d.card(676, 636, 276, 62, "Owner: CLI or dashboard", ("treant_admin role",), "amber")
    d.arrow([(574, 636), (600, 568)], "amber", width=2)
    d.arrow([(760, 636), (662, 568)], "amber", width=2)
    d.text(424, 724, "publishes after rules + two judges", cls="body", size="14px")
    d.text(676, 724, "approves, edits, restores (audited)", cls="body", size="14px")

    # canaries
    d.card(
        48,
        636,
        352,
        62,
        "treant canary plant <ws>",
        ("marker items in every workspace",),
        "slate",
        mono_title=True,
    )
    d.arrow([(224, 698), (224, 746)], "line")
    d.card(
        48,
        746,
        904,
        70,
        "treant isolation-test  (nightly)",
        (),
        "slate",
        mono_title=True,
        title_size="15.5px",
    )
    d.text(372, 776, "for every pair: search, brief, guard, graph walk, direct SELECT", cls="body")
    d.text(372, 798, "must find no marker  ·  exit 0 clean, 1 on any fault", cls="body")
    return d.render()


# ---------------------------------------------------------------- pipeline
def pipeline(t: dict) -> str:
    d = Doc(
        t,
        1000,
        906,
        "Treant save and distillation pipeline",
        "save_session is scrubbed, stored and queued; the worker embeds and distills it; apply writes "
        "the workspace half and queues the general half; the gate runs rules, an LLM judge and the Jev "
        "judge with at most two rewrites per fact; items end published, waiting for review or blocked, "
        "and the owner reviews them.",
    )
    d.text(24, 38, "SAVE  →  DISTILL  →  GATE", cls="caption")
    xs = [24, 218, 412, 606, 800]
    w = 176
    cards = [
        ("save_session", ("typed sections from", "a hook or an agent"), "forest", True),
        ("Server scrub", ("secrets, IPs, hosts,", "configured terms"), "forest", False),
        ("Store + queue", ("session, revision,", "journal, two jobs"), "bark", False),
        ("embed", ("journal, facts and", "nodes to vectors"), "forest", True),
        ("distill", ("one LLM call per", "revision"), "forest", True),
    ]
    for x, (ti, ln, k, mono) in zip(xs, cards):
        d.card(x, 62, w, 96, ti, ln, k, mono_title=mono)
    for i in range(4):
        d.arrow([(xs[i] + w, 110), (xs[i + 1], 110)], "forest")
    # quarantine
    d.arrow([(306, 158), (306, 206)], "red", dash="5 4")
    d.alabel(306, 182, "not sure", color="red")
    d.card(218, 206, 176, 76, "Quarantine", ("no jobs until the", "owner releases it"), "red")
    # embed note
    d.text(694, 184, "journal entries are", cls="body", anchor="middle", size="14px")
    d.text(694, 203, "searchable once embedded", cls="body", anchor="middle", size="14px")

    # wrap: distill -> apply
    o = 40  # vertical offset of everything below the first row
    d.arrow([(888, 158), (888, 300 + o)], "forest")
    d.card(
        760,
        300 + o,
        216,
        96,
        "apply",
        ("workspace half and", "general half"),
        "forest",
        mono_title=True,
    )
    d.arrow([(868, 396 + o), (868, 436 + o)], "forest")
    d.cylinder(
        760, 436 + o, 216, 120, "ws_<name>", ("nodes, edges, facts,", "merges, conflicts"), "forest"
    )
    d.arrow([(760, 348 + o), (714, 348 + o)], "amber", width=2)

    # gate group
    d.group(24, 270 + o, 690, 254, "Gate in front of general", "amber")
    d.text(694, 298 + o, "one job per queued item", cls="body", anchor="end", size="14px")
    gx = [48, 272, 496]
    gw = 194
    steps = [
        ("1  Rules", ("ticket IDs, IPs, account", "IDs, domains, terms")),
        ("2  LLM judge", ("workspace judge_alias,", "one question per kind")),
        ("3  Jev judge", ("score ≥ threshold, asked", "only after the LLM's yes")),
    ]
    for x, (ti, ln) in zip(gx, steps):
        d.card(x, 318 + o, gw, 92, ti, ln, "amber")
    for i in range(2):
        d.arrow([(gx[i] + gw, 364 + o), (gx[i + 1], 364 + o)], "amber")
    # rewrite loop
    d.arrow(
        [(593, 410 + o), (593, 462 + o), (145, 462 + o), (145, 410 + o)], "amber", dash="6 5", r=14
    )
    d.alabel(369, 466 + o, "a fact fails: rewrite and gate again, at most 2 times", color="amber")
    d.text(
        48,
        506 + o,
        "A judge that is down makes the job wait and retry; nothing passes on one judge.",
        cls="body",
        size="14px",
    )

    # outcomes
    ox = [48, 272, 496]
    for cx in (145, 369, 593):
        d.arrow([(cx, 524 + o), (cx, 584 + o)], "line")
    outs = [
        ("published", ("pass, publish = auto",), "run", "run_t"),
        ("waiting_review", ("pass, publish = review",), "ask", "ask_t"),
        ("blocked", ("still failing after", "rewrites, or a concept"), "deny", "deny_t"),
    ]
    for x, (a, sub, fg, bg) in zip(ox, outs):
        d.rect(x, 584 + o, gw, 82, t[bg], t[fg], rx=14, sw=1.5)
        ty = 618 + o if len(sub) == 1 else 610 + o
        d.add(
            f'<text x="{x + gw / 2}" y="{ty}" class="mono" text-anchor="middle" font-weight="700" '
            f'font-size="16px" fill="{t[fg]}">{e(a)}</text>'
        )
        for k, ln in enumerate(sub):
            d.text(x + gw / 2, ty + 26 + k * 19, ln, cls="body", anchor="middle", size="14px")
    d.rect(760, 584 + o, 216, 82, t["slate_t"], t["slate_s"], rx=14)
    d.text(868, 612 + o, "published items are", cls="body", anchor="middle", size="14px")
    d.text(868, 632 + o, "readable by every", cls="body", anchor="middle", size="14px")
    d.text(868, 652 + o, "workspace", cls="body", anchor="middle", size="14px")

    # owner review
    d.card(
        272,
        732 + o,
        704,
        98,
        "Owner review",
        (
            "dashboard queue or treant general approve | drop, also for pending items;",
            "an approval runs the rules again on the final text and is audited",
        ),
        "amber",
    )
    d.arrow([(369, 666 + o), (369, 732 + o)], "amber", width=2)
    d.arrow([(593, 666 + o), (593, 732 + o)], "amber", width=2)
    d.arrow([(272, 781 + o), (145, 781 + o), (145, 666 + o)], "run", width=2)
    d.alabel(206, 770 + o, "approve", color="run", bg=False)
    return d.render()


def guard(t: dict) -> str:
    d = Doc(
        t,
        1000,
        890,
        "Treant guard decision",
        "A shell command from a Claude Code or Kiro hook passes a prefilter, is read segment by segment, "
        "each target is resolved, then compared with the deny, protect and allow lists under the "
        "workspace's mode, giving run, ask or deny.",
    )
    d.text(24, 38, "GUARD: BEFORE EACH SHELL COMMAND", cls="caption")
    xs = [24, 268, 512, 756]
    w = 220
    d.card(
        xs[0],
        60,
        w,
        132,
        "Shell command",
        ("Claude Code PreToolUse", "or Kiro preToolUse"),
        "forest",
        top=True,
    )
    d.rect(42, 142, 184, 34, t["slate_t"], t["slate_s"], rx=8)
    d.text(56, 164, "kubectl delete ns x", cls="mono", size="14px")
    d.card(
        xs[1],
        60,
        w,
        132,
        "Prefilter",
        ("guarded program or tool", "switch on the line? folder", "mapped, mode not off?"),
        "forest",
    )
    d.card(
        xs[2],
        60,
        w,
        132,
        "Read the line",
        ("segment by segment;", "follows cd, export, unset;", "the rest: can't tell"),
        "forest",
    )
    d.card(
        xs[3],
        60,
        w,
        132,
        "Resolve each target",
        ("flags, then env, then", "config files, then", "default_from"),
        "forest",
    )
    for i in range(3):
        d.arrow([(xs[i] + w, 126), (xs[i + 1], 126)], "forest")
    d.arrow([(378, 192), (378, 222)], "run")
    d.pill(318, 222, 120, 30, "no: run", t["run"], t["run_t"], None, size=14, weight=700)

    # target kinds
    kinds = [
        "kube_context",
        "aws_profile",
        "git_remote_host",
        "terraform_dir",
        "ansible_inventory",
        "cloudflare_account",
    ]
    d.text(24, 290, "TARGET KINDS IN THE SHIPPED RULE PACK", cls="caption")
    x = 24
    for k in kinds:
        kw = len(k) * 8.45 + 22
        d.pill(
            x, 304, kw, 32, k, t["ink"], t["slate_t"], t["slate_s"], mono=True, size=14, weight=500
        )
        x += kw + 10
    d.arrow([(866, 192), (866, 264), (952, 264), (952, 386)], "forest", r=12)

    # matrix
    d.group(24, 360, 952, 368, "Each target, by the workspace's mode", "amber")
    cols = ["protect (default)", "production", "allowlist"]
    rows = [
        ("in a deny list", ("deny", "deny", "deny")),
        ("in a protect list", ("ask", "ask", "ask")),
        ("in the workspace's own allow list", ("run", "run", "run")),
        ("only in another workspace's allow", ("ask", "run", "ask")),
        ("listed nowhere", ("run", "run", "ask")),
        ("can't tell (computed value)", ("run", "run", "ask")),
    ]
    cx0, cw = 404, 176
    for j, c in enumerate(cols):
        d.text(cx0 + j * (cw + 12) + cw / 2, 420, c, cls="title", anchor="middle", size="15.5px")
    for i, (label, vals) in enumerate(rows):
        y = 438 + i * 44
        d.rect(44, y, 912, 38, t["card"], t["border"], rx=9)
        d.text(62, y + 24.5, label, cls="body", fill=t["ink"], size="15px")
        for j, v in enumerate(vals):
            fg, bg = {"deny": ("deny", "deny_t"), "ask": ("ask", "ask_t"), "run": ("run", "run_t")}[
                v
            ]
            d.pill(
                cx0 + j * (cw + 12) + 34,
                y + 6,
                cw - 68,
                26,
                v,
                t[fg],
                t[bg],
                None,
                size=14,
                weight=700,
            )
    d.text(
        44,
        718,
        "mode off: the guard steps aside for that workspace; memory still applies",
        cls="body",
        size="14px",
    )

    # combine
    d.arrow([(500, 728), (500, 762)], "line")
    cw2 = 296
    bx = [24, 352, 680]
    d.card(
        bx[0],
        762,
        cw2,
        104,
        "Whole command",
        ("any denied target: deny", "otherwise any finding: ask", "nothing found: run"),
        "red",
    )
    d.card(
        bx[1],
        762,
        cw2,
        104,
        "Fails closed",
        ("invalid config, internal error", "or the 3 s budget: ask"),
        "amber",
    )
    d.card(
        bx[2],
        762,
        cw2,
        104,
        "Memory warnings",
        (
            "a rule matched and a token is",
            "set: a memory warning on the",
            "server turns run into ask",
        ),
        "forest",
    )
    return d.render()


# ---------------------------------------------------------------- features
ICONS = {
    # 40x40 line icons, drawn in the accent colour
    "memory": '<circle cx="20" cy="20" r="13"/><path d="M20 12v8l5 4"/><path d="M5 9v6h6"/>',
    "layers": '<path d="M20 6 34 13 20 20 6 13Z"/><path d="M6 20l14 7 14-7"/><path d="M6 27l14 7 14-7"/>',
    "gate": '<path d="M8 34V12a12 12 0 0 1 24 0v22"/><path d="M4 34h32"/><path d="M14 22l4 4 8-8"/>',
    "scrub": '<path d="M20 4 33 9v10c0 8-6 14-13 17C13 33 7 27 7 19V9Z"/><path d="M14 15h12M14 21h8M14 27h10"/>',
    "guard": '<rect x="4" y="7" width="32" height="26" rx="4"/><path d="M10 15l5 4-5 4"/><path d="M19 25h10"/>',
    "dash": (
        '<rect x="4" y="6" width="32" height="28" rx="4"/><path d="M4 13h32"/>'
        '<circle cx="13" cy="24" r="3"/><circle cx="27" cy="20" r="3"/><circle cx="24" cy="29" r="2.5"/>'
        '<path d="M15.8 23 24 20.6M25.6 22.6 24.5 26.6"/>'
    ),
}


def features(t: dict) -> str:
    d = Doc(
        t,
        1000,
        556,
        "Treant features",
        "Six feature cards: memory across sessions, isolated workspaces, gated shared know-how, "
        "scrubbing before storage, a command guard and an owner dashboard.",
    )
    items = [
        (
            "memory",
            "forest",
            "Memory across sessions",
            [
                "A brief at session start, a save",
                "at session end, hybrid search over",
                "vectors and a knowledge graph.",
            ],
        ),
        (
            "layers",
            "bark",
            "Isolated workspaces",
            [
                "One Postgres schema and role per",
                "workspace. A token reaches",
                "exactly one workspace.",
            ],
        ),
        (
            "gate",
            "amber",
            "Gated shared know-how",
            [
                "Rules, an LLM judge and the Jev",
                "judge decide what may enter the",
                "shared general schema.",
            ],
        ),
        (
            "scrub",
            "forest",
            "Scrubbed before storage",
            [
                "Client and server remove secrets,",
                "IPs and host names. Unsure saves",
                "wait in quarantine.",
            ],
        ),
        (
            "guard",
            "red",
            "Command guard",
            [
                "Checks kubectl, aws, terraform, git",
                "push and more before they run:",
                "run, ask or deny per target.",
            ],
        ),
        (
            "dash",
            "slate",
            "Owner dashboard",
            [
                "Review queue, memory browser with",
                "history, graph view, audit log.",
                "OIDC login, no JavaScript.",
            ],
        ),
    ]
    cw, ch, gap = 304, 244, 20
    for i, (icon, kind, title, lines) in enumerate(items):
        col, row = i % 3, i // 3
        x, y = 24 + col * (cw + gap), 24 + row * (ch + gap)
        acc, tint, ts = ACCENTS[kind]
        d.rect(x, y, cw, ch, t["card"], t["border"], rx=16, shadow=True)
        d.rect(x + 24, y + 24, 60, 60, t[tint], t[ts], rx=14)
        d.add(
            f'<g transform="translate({x + 34} {y + 34})" fill="none" stroke="{t[acc]}" stroke-width="2.4" '
            f'stroke-linecap="round" stroke-linejoin="round">{ICONS[icon]}</g>'
        )
        d.add(f'<text x="{x + 24}" y="{y + 120}" class="title" font-size="19px">{e(title)}</text>')
        for k, ln in enumerate(lines):
            d.add(
                f'<text x="{x + 24}" y="{y + 152 + k * 24}" class="body" font-size="15.5px">{e(ln)}</text>'
            )
    return d.render()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for t in (LIGHT, DARK):
        sfx = "" if t["name"] == "light" else "-dark"
        files = {
            f"logo{sfx}.svg": logo(t),
            f"banner{sfx}.svg": banner(t),
            f"architecture{sfx}.svg": architecture(t),
            f"isolation{sfx}.svg": isolation(t),
            f"pipeline{sfx}.svg": pipeline(t),
            f"guard{sfx}.svg": guard(t),
            f"features{sfx}.svg": features(t),
        }
        for name, body in files.items():
            (OUT / name).write_text(body, encoding="utf-8")
            print("wrote", OUT / name)


main()
