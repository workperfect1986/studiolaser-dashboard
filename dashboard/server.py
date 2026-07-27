"""Dashboard API — captação em tempo real de /fluxo_servicos do Nucleus."""

from __future__ import annotations

import io
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory, session, redirect, url_for
from fpdf import FPDF

app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

BASE_URL = os.environ.get("NUCLEUS_BASE_URL", "https://studiolaser.nucleusapp.com.br").rstrip("/")
EMAIL = os.environ.get("NUCLEUS_EMAIL", "")
PASSWORD = os.environ.get("NUCLEUS_PASSWORD", "")
USER_ID = os.environ.get("NUCLEUS_USER_ID", "7012")
OPERADOR_NOME = os.environ.get("NUCLEUS_OPERADOR", "CTA GUILHERME")
DATE_DE = os.environ.get("NUCLEUS_DATE_DE", "")
DATE_ATE = os.environ.get("NUCLEUS_DATE_ATE", "")
CACHE_TTL = int(os.environ.get("NUCLEUS_CACHE_TTL", "60"))

_cache: dict[str, Any] = {"data": None, "error": None, "fetched_at": 0.0, "key": None}
_lock = threading.Lock()
_session: requests.Session | None = None
_session_ok = False


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    return s


def _login(session: requests.Session) -> None:
    if not EMAIL or not PASSWORD:
        msg = "Credenciais ausentes. Defina NUCLEUS_EMAIL e NUCLEUS_PASSWORD."
        raise RuntimeError(msg)
    r = session.get(f"{BASE_URL}/login", timeout=30)
    r.raise_for_status()
    tm = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', r.text)
    if not tm:
        tm = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', r.text)
    if not tm:
        raise RuntimeError("Token de autenticação não encontrado")
    r2 = session.post(
        f"{BASE_URL}/users/do_login",
        data={"utf8": "✓", "authenticity_token": unescape(tm.group(1)),
              "email": EMAIL, "senha": PASSWORD, "commit": "Entrar"},
        timeout=30, allow_redirects=True,
    )
    r2.raise_for_status()
    c = session.get(f"{BASE_URL}/", timeout=30, allow_redirects=True)
    if "/login" in c.url:
        raise RuntimeError("Login não persistiu")


def get_session() -> requests.Session:
    global _session, _session_ok
    if _session is not None and _session_ok:
        return _session
    _session = _new_session()
    _login(_session)
    _session_ok = True
    return _session


def reset_session() -> None:
    global _session, _session_ok
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
    _session = None
    _session_ok = False


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrap(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrap


def _child_attr(html_block: str, attr: str, tag: str = "a") -> str | None:
    m = re.search(rf"<{tag}[^>]*?{attr}=['\"]([^'\"]+)['\"]", html_block, re.I)
    return m.group(1) if m else None


def _child_text(html_block: str) -> str:
    return _clean(html_block)


# ---- filtros base ----
def _base_params(filters: dict[str, Any] | None = None) -> dict[str, str]:
    f = filters or {}
    return {
        "utf8": "✓",
        "aba": "todos",
        "chave": "",
        "os_id": "",
        "work_order_id": "",
        "company_id": "",
        "date_de": str(f.get("date_de") or ""),
        "date_ate": str(f.get("date_ate") or ""),
        "date_despacho_de": "",
        "date_despacho_ate": "",
        "user_id": USER_ID,
        "tipo": "",
        "classificacao": "",
        "situacao": "",
        "tecnologia": "",
        "material": "",
        "espessura": "",
        "nivel_dificuldade": "",
        "id_terceiro": "",
        "cod_produto": "",
        "cod_barras": "",
        "local_gravacao_id": "",
        "minhas_ordens_servico": "t",
        "commit": "Filtrar",
    }


# ---- parsing fluxo ----
def _etapa_cards(html: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for m in re.finditer(r"<a[^>]*aba=(\d+|todos)[^>]*>[\s\S]*?<h1[^>]*>\s*(\d+)\s*</h1>[\s\S]*?<h3[^>]*>\s*([^<]+)\s*</h3>", html, re.I):
        cards.append({"aba": m.group(1), "total": int(m.group(2)), "nome": _clean(m.group(3))})
    seen = set()
    return [c for c in cards if c["nome"] not in seen and (seen.add(c["nome"]) or True)]


def _parse_fluxo_page(html: str) -> tuple[list[dict[str, Any]], bool]:
    """Retorna (orders, has_more)."""
    tbody = re.search(r"<tbody[^>]*>([\s\S]*?)</tbody>", html, re.I)
    if not tbody:
        return [], False
    rows = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", tbody.group(1), re.I)
    orders = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", row, re.I)
        if len(cells) < 10:
            continue
        # 0: ID OS
        id_os = _clean(cells[0])
        os_url = _child_attr(cells[0], "href")
        if not id_os and os_url:
            id_os = os_url.rsplit("/", 1)[-1]
        # 1: Versão
        versao = _clean(cells[1])
        # 2: Pedido
        pedido = _clean(cells[2])
        # 3: Nome
        nome = _clean(cells[3])
        # 4: Cliente
        cliente = _clean(cells[4])
        cliente_url = _child_attr(cells[4], "href")
        # 5: Trabalhos
        trabalhos = _clean(cells[5])
        trab_url = _child_attr(cells[5], "href")
        # 6: Operador
        operador = _clean(cells[6])
        op_url = _child_attr(cells[6], "href")
        # 7: Previsão (Despacho: DD/MM/AAAA)
        previsao = _clean(cells[7])
        desp_match = re.search(r"(\d{2}/\d{2}/\d{4})", previsao)
        previsao_data = desp_match.group(1) if desp_match else previsao
        # 8: Tempo (pode ter atraso)
        tempo = _clean(cells[8])
        atraso_class = re.search(r"class=['\"]([^'\"]*tooltip-fluxo[^'\"]*)['\"]", cells[8], re.I)
        atraso_class = (atraso_class.group(1) if atraso_class else "").lower()
        # 9: Etapa
        etapa_html = cells[9]
        etapa = _clean(etapa_html)
        etapa_href = _child_attr(etapa_html, "href", "a")
        # cor da etapa
        desc_match = re.search(r"(Atrasado|No prazo|Urgente)", tempo, re.I)
        if desc_match:
            tempo_desc = desc_match.group(1)
        else:
            tempo_desc = ""
        if "danger" in atraso_class or "atras" in tempo.lower():
            status_color = "danger"
        elif "success" in atraso_class:
            status_color = "success"
        elif "warning" in atraso_class:
            status_color = "warning"
        else:
            status_color = "info"

        orders.append({
            "idOs": id_os,
            "idTrabalho": id_os,  # fluxo_servicos não tem ID trabalho separado; usa ID OS
            "osUrl": os_url,
            "versao": versao,
            "pedido": pedido,
            "nome": nome,
            "cliente": cliente,
            "clienteUrl": cliente_url,
            "trabalhos": trabalhos,
            "trabalhoUrl": trab_url,
            "operador": operador,
            "operadorUrl": op_url,
            "previsao": previsao_data,
            "previsaoRaw": previsao,
            "tempo": tempo,
            "tempoDescricao": tempo_desc,
            "statusColor": status_color,
            "etapa": etapa,
            "etapaUrl": etapa_href,
        })

    has_more = bool(re.search(r"Ver mais ordens", html, re.I))
    return orders, has_more


# ---- fetch all ----
def fetch_fluxo(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    session = get_session()
    all_orders: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    etapas: list[dict[str, Any]] = []

    while page <= 200:
        params = _base_params(filters)
        if page > 1:
            params["page"] = str(page)
        url = f"{BASE_URL}/fluxo_servicos?{urlencode(params)}"
        try:
            resp = session.get(url, timeout=60, allow_redirects=True)
        except requests.RequestException as exc:
            raise RuntimeError(f"Erro de rede página {page}: {exc}") from exc
        if "/login" in resp.url:
            reset_session()
            session = get_session()
            resp = session.get(url, timeout=60, allow_redirects=True)
            if "/login" in resp.url:
                raise RuntimeError("Sessão expirada")
        resp.raise_for_status()
        html = resp.content.decode("utf-8", errors="replace")

        if page == 1:
            etapas = _etapa_cards(html)

        batch, has_more = _parse_fluxo_page(html)
        if not batch:
            break
        for o in batch:
            if o["idOs"] not in seen:
                seen.add(o["idOs"])
                all_orders.append(o)
        if not has_more:
            break
        page += 1

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "generatedAt": now,
        "live": True,
        "source": "fluxo_servicos",
        "baseUrl": BASE_URL,
        "filtro": {
            "periodo": "todas",
            "operador": OPERADOR_NOME,
            "userId": USER_ID,
        },
        "etapas": etapas,
        "orders": all_orders,
        "total": len(all_orders),
        "pagination": {"allPages": True, "total": len(all_orders)},
    }


def get_data(filters: dict[str, Any] | None = None, force: bool = False) -> dict[str, Any]:
    filters = filters or {}
    key = f"fluxo|{filters.get('date_de','')}|{filters.get('date_ate','')}"
    now_t = time.time()
    if not force and _cache["data"] and _cache["key"] == key and (now_t - _cache["fetched_at"]) < CACHE_TTL:
        return {**_cache["data"], "cached": True, "cacheAgeSec": int(now_t - _cache["fetched_at"]), "error": _cache["error"]}
    try:
        data = fetch_fluxo(filters)
        with _lock:
            _cache["data"] = data
            _cache["error"] = None
            _cache["fetched_at"] = time.time()
            _cache["key"] = key
        return {**data, "cached": False, "cacheAgeSec": 0, "error": None}
    except Exception as exc:
        with _lock:
            _cache["error"] = str(exc)
            if _cache["data"]:
                return {**_cache["data"], "cached": True, "cacheAgeSec": int(time.time() - _cache["fetched_at"]), "error": str(exc), "stale": True}
        raise


# ---- PDF ----
def _pdf_safe(s: Any, n: int = 80) -> str:
    t = str(s or "").replace("\n", " ").encode("latin-1", "replace").decode("latin-1")
    return t[:n]


def _parse_br_date(v: str) -> datetime | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(v or ""))
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _status_rgb(status: str) -> tuple[int, int, int]:
    s = (status or "").lower()
    if "delet" in s:
        return (239, 68, 68)
    if "finaliz" in s:
        return (34, 197, 94)
    if "aprova" in s:
        return (245, 158, 11)
    if "an" in s and "lise" in s:
        return (59, 130, 246)
    if "atendimento" in s:
        return (14, 165, 233)
    if "paus" in s:
        return (148, 163, 184)
    return (100, 116, 139)


class ReportPDF(FPDF):
    def __init__(self, meta: dict[str, Any]):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.meta = meta
        self.set_auto_page_break(auto=True, margin=16)

    def header(self):
        if self.page_no() == 1:
            return
        self.set_fill_color(15, 23, 42)
        self.rect(0, 0, 297, 14, "F")
        self.set_xy(12, 4)
        self.set_text_color(226, 232, 240)
        self.set_font("Helvetica", "B", 9)
        self.cell(160, 6, "Studio Laser  |  Fluxo de Servicos", align="L")
        self.set_font("Helvetica", "", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 6, f" {self.meta.get('periodo')}", align="R")
        self.ln(12)

    def footer(self):
        self.set_y(-12)
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.2)
        self.line(12, self.get_y(), 285, self.get_y())
        self.set_y(-10)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(100, 116, 139)
        self.cell(120, 6, f" {self.meta.get('gerado')}", align="L")
        self.cell(50, 6, "Uso interno", align="C")
        self.cell(0, 6, f"Pagina {self.page_no()}/{{nb}}", align="R")


def build_pdf(data: dict[str, Any]) -> bytes:
    orders = data.get("orders") or []
    etapas = data.get("etapas") or []
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    by_status: dict[str, int] = {}
    by_cliente: dict[str, int] = {}
    atrasados = 0
    sem_prev = 0
    atraso_dias: list[int] = []
    for o in orders:
        st = o.get("etapa") or "Sem etapa"
        cl = o.get("cliente") or "—"
        by_status[st] = by_status.get(st, 0) + 1
        by_cliente[cl] = by_cliente.get(cl, 0) + 1
        d = _parse_br_date(o.get("previsao") or "")
        if not d:
            sem_prev += 1
        else:
            dd = (hoje - d).days
            if dd > 0:
                atrasados += 1
                atraso_dias.append(dd)
    media = round(sum(atraso_dias) / len(atraso_dias)) if atraso_dias else 0
    total = len(orders)
    clientes_unicos = len(by_cliente)
    gerado = data.get("generatedAt") or ""
    try:
        gf = datetime.fromisoformat(str(gerado).replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        gf = str(gerado)[:19]
    meta = {"periodo": "/fluxo_servicos", "gerado": gf}

    pdf = ReportPDF(meta)
    pdf.alias_nb_pages()
    pdf.add_page()

    # capa
    pdf.set_fill_color(15, 23, 42)
    pdf.rect(0, 0, 297, 42, "F")
    pdf.set_fill_color(37, 99, 235)
    pdf.rect(0, 42, 297, 2.2, "F")
    pdf.set_fill_color(34, 211, 238)
    pdf.rect(0, 0, 4, 44, "F")
    pdf.set_xy(14, 10)
    pdf.set_text_color(34, 211, 238)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "STUDIO LASER  ·  NUCLEUS  ·  FLUXO DE SERVICOS", ln=1)
    pdf.set_x(14)
    pdf.set_text_color(248, 250, 252)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 10, "Relatorio Fluxo de Servicos", ln=1)
    pdf.set_x(14)
    pdf.set_text_color(148, 163, 184)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Operador: {_pdf_safe(OPERADOR_NOME)}  |  {gf}", ln=1)

    y = 52
    kpis = [
        ("TOTAL", str(total), (37, 99, 235)),
        ("ATRASADOS", str(atrasados), (239, 68, 68)),
        ("FASE APROVACAO", str(by_status.get("JOB EM APROVAÇÃO", 0)), (245, 158, 11)),
        ("CLIENTES", str(clientes_unicos), (167, 139, 250)),
        ("SEM PREVISAO", str(sem_prev), (148, 163, 184)),
        ("MEDIA ATRASO", f"{media}d", (34, 197, 94)),
    ]
    card_w, card_h, gap = 44, 22, 3.5
    for i, (lab, val, col) in enumerate(kpis):
        x = 12 + i * (card_w + gap)
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(226, 232, 240)
        pdf.rect(x, y, card_w, card_h, "DF")
        pdf.set_fill_color(*col)
        pdf.rect(x, y, 2.2, card_h, "F")
        pdf.set_xy(x + 5, y + 3)
        pdf.set_text_color(100, 116, 139)
        pdf.set_font("Helvetica", "B", 7)
        pdf.cell(card_w - 8, 4, lab, ln=1)
        pdf.set_x(x + 5)
        pdf.set_text_color(15, 23, 42)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(card_w - 8, 10, val)

    y = 80
    panel_h = 76
    lw, rw = 135, 135

    def panel(xx, yy, w, h, title):
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(226, 232, 240)
        pdf.rect(xx, yy, w, h, "DF")
        pdf.set_fill_color(15, 23, 42)
        pdf.rect(xx, yy, w, 9, "F")
        pdf.set_xy(xx + 4, yy + 2)
        pdf.set_text_color(248, 250, 252)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(w - 8, 5, title)

    def hbars(xx, yy, w, items, colors):
        if not items:
            pdf.set_xy(xx + 6, yy + 20)
            pdf.set_text_color(148, 163, 184)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(w - 12, 6, "Sem dados")
            return
        max_v = max(v for _, v in items) or 1
        rh = min(8.2, 56 / max(len(items), 1))
        bm = w - 62
        for i, (lab, val) in enumerate(items):
            cy = yy + 14 + i * rh
            if cy + rh > yy + panel_h - 4:
                break
            col = colors[i % len(colors)]
            pdf.set_xy(xx + 4, cy)
            pdf.set_text_color(51, 65, 85)
            pdf.set_font("Helvetica", "", 7)
            pdf.cell(38, rh - 1.5, _pdf_safe(lab, 24), align="R")
            pdf.set_fill_color(241, 245, 249)
            pdf.rect(xx + 44, cy + 1.2, bm, rh - 3.2, "F")
            bw = max(1.5, bm * (val / max_v))
            pdf.set_fill_color(*col)
            pdf.rect(xx + 44, cy + 1.2, bw, rh - 3.2, "F")
            pdf.set_xy(xx + 44 + bm + 2, cy)
            pdf.set_text_color(15, 23, 42)
            pdf.set_font("Helvetica", "B", 7)
            pdf.cell(12, rh - 1.5, str(val))

    si = sorted(by_status.items(), key=lambda x: -x[1])[:10]
    ci = sorted(by_cliente.items(), key=lambda x: -x[1])[:8]
    sc = [(245, 158, 11), (59, 130, 246), (14, 165, 233), (167, 139, 250),
          (34, 197, 94), (239, 68, 68), (100, 116, 139), (244, 114, 182)]
    cc = [(37, 99, 235), (14, 165, 233), (167, 139, 250), (245, 158, 11),
          (34, 197, 94), (244, 114, 182), (99, 102, 241), (20, 184, 166)]
    panel(12, y, lw, panel_h, "Distribuicao por etapa")
    hbars(12, y, lw, si, sc)
    panel(12 + lw + 3, y, rw, panel_h, "Top clientes")
    hbars(12 + lw + 3, y, rw, ci, cc)

    y = 162
    pdf.set_fill_color(241, 245, 249)
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(12, y, 273, 14, "DF")
    pdf.set_xy(16, y + 3)
    pdf.set_text_color(100, 116, 139)
    pdf.set_font("Helvetica", "B", 7)
    pdf.cell(20, 4, "ETAPAS", ln=1)
    pdf.set_x(16)
    pdf.set_text_color(30, 41, 59)
    pdf.set_font("Helvetica", "", 8)
    et_text = " · ".join(f"{e['nome']}: {e['total']}" for e in etapas if e["total"] > 0 and "todos" not in e["nome"].lower())
    pdf.cell(0, 5, _pdf_safe(et_text, 130))

    # tabela
    pdf.add_page()
    pdf.set_fill_color(15, 23, 42)
    pdf.rect(12, 18, 273, 10, "F")
    pdf.set_xy(16, 20)
    pdf.set_text_color(248, 250, 252)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(180, 6, f"Ordens de servico  ({total})")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(148, 163, 184)
    pdf.cell(0, 6, "Ordenado por atraso", align="R")

    cols = [("ID OS", 22), ("Cliente", 34), ("Nome", 78), ("Despacho", 22), ("Atraso", 18), ("Etapa", 52), ("Operador", 30)]

    def th():
        pdf.set_fill_color(30, 41, 59)
        pdf.set_text_color(226, 232, 240)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_x(12)
        for lab, w in cols:
            pdf.cell(w, 7, lab, border=0, fill=True)
        pdf.ln()

    def sk(o):
        d = _parse_br_date(o.get("previsao") or "")
        if not d:
            return (2, 0)
        dd = (hoje - d).days
        return (0, -dd) if dd > 0 else (1, dd)

    ordered = sorted(orders, key=sk)
    th()
    pdf.set_font("Helvetica", "", 6.5)
    for idx, o in enumerate(ordered, 1):
        if pdf.get_y() > 190:
            pdf.add_page()
            th()
            pdf.set_font("Helvetica", "", 6.5)
        d = _parse_br_date(o.get("previsao") or "")
        if not d:
            atr_txt = "-"
            atr_rgb = (148, 163, 184)
        else:
            dd = (hoje - d).days
            atr_txt = f"{dd}d" if dd > 0 else "0d" if dd == 0 else f"{abs(dd)}d"
            atr_rgb = (239, 68, 68) if dd > 0 else (245, 158, 11) if dd == 0 else (34, 197, 94)
        bg = (248, 250, 252) if idx % 2 == 0 else (255, 255, 255)
        x_start = 12
        pdf.set_fill_color(*bg)
        pdf.rect(x_start, pdf.get_y(), 273, 6.2, "F")
        srgb = _status_rgb(o.get("etapa") or "")
        pdf.set_fill_color(*srgb)
        pdf.rect(x_start, pdf.get_y(), 1.4, 6.2, "F")
        pdf.set_xy(x_start + 2, pdf.get_y() + 0.8)
        vals = [
            (_pdf_safe(o.get("idOs"), 14), 20, (37, 99, 235)),
            (_pdf_safe(o.get("cliente"), 22), 34, (30, 41, 59)),
            (_pdf_safe(o.get("nome"), 50), 78, (30, 41, 59)),
            (_pdf_safe(o.get("previsao") or "-", 12), 22, (30, 41, 59)),
            (atr_txt, 18, atr_rgb),
            (_pdf_safe(o.get("etapa"), 34), 52, srgb),
            (_pdf_safe(o.get("operador"), 18), 30, (100, 116, 139)),
        ]
        for text, w, rgb in vals:
            pdf.set_text_color(*rgb)
            pdf.set_font("Helvetica", "B", 6.5)
            pdf.cell(w, 6.2, text, align="L")
        pdf.ln(6.2)

    out = pdf.output()
    return bytes(out) if isinstance(out, (bytes, bytearray)) else out.encode("latin-1", "replace")


# ---- Rotas ----
@app.get("/")
@login_required
def index():
    return send_from_directory(".", "index.html")


@app.get("/login")
def login_page():
    return send_from_directory(".", "login.html")


@app.post("/login")
def login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not email or not password:
        return jsonify({"error": "Email e senha são obrigatórios"}), 400
    try:
        s = _new_session()
        r = s.get(f"{BASE_URL}/login", timeout=30)
        r.raise_for_status()
        tm = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', r.text)
        if not tm:
            tm = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', r.text)
        if not tm:
            return jsonify({"error": "Token não encontrado"}), 500
        resp = s.post(
            f"{BASE_URL}/users/do_login",
            data={"utf8": "✓", "authenticity_token": unescape(tm.group(1)), "email": email, "senha": password, "commit": "Entrar"},
            timeout=30, allow_redirects=True,
        )
        if "/login" in resp.url:
            return jsonify({"error": "Credenciais inválidas"}), 401
        session["logged_in"] = True
        session["nucleus_email"] = email
        return jsonify({"ok": True, "redirect": "/"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/logout")
def logout():
    return send_from_directory(".", "login.html")


@app.get("/relatorio")
@app.get("/report")
@login_required
def report_page():
    return send_from_directory(".", "report.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "source": "fluxo_servicos", "baseUrl": BASE_URL})


@app.get("/api/fluxo")
@login_required
def api_fluxo():
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    filters = {
        "date_de": request.args.get("date_de", ""),
        "date_ate": request.args.get("date_ate", ""),
    }
    try:
        data = get_data(filters=filters, force=force)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc), "baseUrl": BASE_URL, "live": False}), 502


@app.get("/api/work_orders")
@login_required
def api_work_orders():
    return api_fluxo()


@app.get("/api/report.pdf")
@app.post("/api/report.pdf")
@login_required
def api_report_pdf():
    try:
        data = get_data(force=True)
        pdf_bytes = build_pdf(data)
        fname = f"fluxo_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True, download_name=fname)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


def main():
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Dashboard live: http://{host}:{port}")
    print(f"Fonte: {BASE_URL}/fluxo_servicos")
    print(f"Cache TTL: {CACHE_TTL}s")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()