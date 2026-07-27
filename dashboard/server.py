"""Dashboard API — captação em tempo real de /work_orders do Nucleus."""

from __future__ import annotations

import io
import os
import re
import threading
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory
from fpdf import FPDF

app = Flask(__name__, static_folder=".", static_url_path="")

BASE_URL = os.environ.get("NUCLEUS_BASE_URL", "https://studiolaser.nucleusapp.com.br").rstrip("/")
EMAIL = os.environ.get("NUCLEUS_EMAIL", "")
PASSWORD = os.environ.get("NUCLEUS_PASSWORD", "")
OPERADOR_ID = os.environ.get("NUCLEUS_OPERADOR_ID", "7012")
OPERADOR_NOME = os.environ.get("NUCLEUS_OPERADOR", "CTA GUILHERME")
DATE_DE = os.environ.get("NUCLEUS_DATE_DE", "01/01/2026")
DATE_ATE = os.environ.get("NUCLEUS_DATE_ATE", "31/12/2026")
CACHE_TTL = int(os.environ.get("NUCLEUS_CACHE_TTL", "60"))
PER_PAGE_DEFAULT = 20

_cache: dict[str, Any] = {"data": None, "error": None, "fetched_at": 0.0, "key": None}
_meta_cache: dict[str, Any] = {"options": None, "fetched_at": 0.0}
_lock = threading.Lock()
_session: requests.Session | None = None
_session_ok = False


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }
    )
    return s


def _login(session: requests.Session) -> None:
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "Credenciais ausentes. Defina NUCLEUS_EMAIL e NUCLEUS_PASSWORD no ambiente."
        )
    r = session.get(f"{BASE_URL}/login", timeout=30)
    r.raise_for_status()
    token_m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', r.text)
    if not token_m:
        token_m = re.search(r'value="([^"]+)"[^>]*name="authenticity_token"', r.text)
    if not token_m:
        raise RuntimeError("Token de autenticação não encontrado")
    token = unescape(token_m.group(1))
    resp = session.post(
        f"{BASE_URL}/users/do_login",
        data={
            "utf8": "✓",
            "authenticity_token": token,
            "email": EMAIL,
            "senha": PASSWORD,
            "commit": "Entrar",
        },
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    check = session.get(f"{BASE_URL}/work_orders", timeout=30, allow_redirects=True)
    if "/login" in check.url:
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


def _params(filters: dict[str, Any], page: int = 1) -> dict[str, str]:
    # operador/cliente vazios = sem filtro no Nucleus (filtragem local no dashboard)
    op = filters.get("operador_id")
    if op is None:
        op = ""
    p = {
        "utf8": "✓",
        "company_id": str(filters.get("company_id") or ""),
        "operador_id": str(op),
        "titulo": str(filters.get("titulo") or ""),
        "ordem_servico_id": str(filters.get("ordem_servico_id") or ""),
        "cod_produto": str(filters.get("cod_produto") or ""),
        "id": str(filters.get("id") or filters.get("trabalho_id") or ""),
        "date_de": str(filters.get("date_de") or DATE_DE),
        "date_ate": str(filters.get("date_ate") or DATE_ATE),
        "commit": "Filtrar",
    }
    if filters.get("finalizado"):
        p["finalizado"] = "t"
    if page and page > 1:
        p["page"] = str(page)
    return p


def _parse_counts_from_js(js_text: str) -> dict[str, int]:
    todos_m = re.search(r"aba-id='todos'[\s\S]*?\.html\(\s*(\d+)\s*\)", js_text)
    final_m = re.search(r"aba-id='finalizado_f'[\s\S]*?\.html\(\s*(\d+)\s*\)", js_text)
    return {
        "todos": int(todos_m.group(1)) if todos_m else 0,
        "finalizados": int(final_m.group(1)) if final_m else 0,
    }


def _fetch_counts(filters: dict[str, Any]) -> dict[str, int]:
    """Contagens das abas via endpoint AJAX do Nucleus."""
    session = get_session()
    params = _params(filters, page=1)
    q = urlencode(params) + "&abas%5B%5D=todos&abas%5B%5D=finalizado_f"
    url = f"{BASE_URL}/work_orders/get_tab_number/abas?{q}"
    try:
        resp = session.get(
            url,
            timeout=30,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, application/javascript, */*; q=0.01",
            },
        )
        if resp.status_code != 200:
            return {"todos": 0, "finalizados": 0}
        return _parse_counts_from_js(resp.content.decode("utf-8", errors="replace"))
    except requests.RequestException:
        return {"todos": 0, "finalizados": 0}


def _parse_max_page(html: str) -> int:
    block = re.search(r'<ul class="pagination[\s\S]*?>([\s\S]*?)</ul>', html, re.I)
    text = block.group(1) if block else ""
    pages = [int(n) for n in re.findall(r">\s*(\d+)\s*<", text)]
    # also page=N in links
    pages += [int(n) for n in re.findall(r"[?&]page=(\d+)", html)]
    return max(pages) if pages else 1


def _parse_select_options(html: str, select_id: str) -> list[dict[str, str]]:
    m = re.search(
        rf'<select[^>]*(?:id|name)="{re.escape(select_id)}"[^>]*>([\s\S]*?)</select>',
        html,
        re.I,
    )
    if not m:
        return []
    opts = []
    for om in re.finditer(r'<option[^>]*value="([^"]*)"[^>]*>([\s\S]*?)</option>', m.group(1), re.I):
        val, label = om.group(1), _clean(om.group(2))
        if not val and not label:
            continue
        opts.append({"id": val, "nome": label})
    return opts


def _status_color(status: str, status_class: str = "") -> str:
    status_l = (status or "").lower()
    sc = (status_class or "").lower()
    if "danger" in sc or "delet" in status_l:
        return "danger"
    if "success" in sc or "finaliz" in status_l:
        return "success"
    if "warning" in sc:
        return "warning"
    if "info" in sc or "aprova" in status_l or "atendimento" in status_l or "anál" in status_l or "anal" in status_l:
        return "info"
    return "default"


def _is_deleted_status(status: str) -> bool:
    return "delet" in (status or "").lower()


def _parse_work_orders_raw(html: str) -> list[dict[str, Any]]:
    """Importa TODAS as linhas da tabela (incluindo Deletado) para não quebrar a paginação."""
    tbody_m = re.search(r"<tbody[^>]*>([\s\S]*?)</tbody>", html, re.I)
    if not tbody_m:
        return []
    rows = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", tbody_m.group(1), re.I)
    orders = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>([\s\S]*?)</td>", row, re.I)
        if len(cells) < 8:
            continue
        id_trabalho = _clean(cells[0])
        id_os = _clean(cells[1])
        cliente_m = re.search(r'<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', cells[2], re.I)
        if cliente_m:
            cliente = _clean(cliente_m.group(2))
            cliente_url = cliente_m.group(1)
        else:
            cliente = _clean(cells[2])
            cliente_url = None
        nome = _clean(cells[3])
        previsao = _clean(cells[4])
        status = _clean(cells[5])
        badge_m = re.search(r"class=['\"]([^'\"]*badge[^'\"]*)['\"]", cells[5], re.I)
        status_class = badge_m.group(1) if badge_m else ""
        status_color = _status_color(status, status_class)
        operador = _clean(cells[6])
        data = _clean(cells[7])
        arquivos = _clean(cells[8]) if len(cells) > 8 else ""
        has_download = "download" in cells[8].lower() if len(cells) > 8 else False
        tr_id = re.search(r'id="(\d+)"', row)
        if tr_id and not id_trabalho:
            id_trabalho = tr_id.group(1)
        if not id_trabalho:
            continue
        orders.append(
            {
                "idTrabalho": id_trabalho,
                "idOs": id_os,
                "cliente": cliente,
                "clienteUrl": cliente_url,
                "nome": nome,
                "previsao": previsao,
                "status": status,
                "statusColor": status_color,
                "operador": operador,
                "data": data,
                "arquivos": arquivos or ("Download" if has_download else ""),
                "hasArquivo": has_download,
                "trabalhoUrl": f"/work_orders/{id_trabalho}",
                "printUrl": f"/work_orders/print/{id_trabalho}",
                "deleted": _is_deleted_status(status),
            }
        )
    return orders


def _apply_status_rules(orders: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Tratamento pós-importação: remove Deletado e recalcula contadores."""
    kept: list[dict[str, Any]] = []
    removed_deleted = 0
    for o in orders:
        if o.get("deleted") or _is_deleted_status(o.get("status") or ""):
            removed_deleted += 1
            continue
        clean = dict(o)
        clean.pop("deleted", None)
        kept.append(clean)
    stats = {
        "brutos": len(orders),
        "importados": len(kept),
        "removidosDeletado": removed_deleted,
    }
    return kept, stats


def _build_counts(orders: list[dict[str, Any]], filters: dict[str, Any], stats: dict[str, int], nucleus: dict[str, int]) -> dict[str, int]:
    """Contadores sempre a partir dos dados importados (já tratados)."""
    n = len(orders)
    return {
        "todos": n if not filters.get("finalizado") else 0,
        "finalizados": n if filters.get("finalizado") else 0,
        "importados": n,
        "brutos": stats.get("brutos", n),
        "removidosDeletado": stats.get("removidosDeletado", 0),
        "nucleusTodos": nucleus.get("todos") or 0,
        "nucleusFinalizados": nucleus.get("finalizados") or 0,
    }


def _get_html(filters: dict[str, Any], page: int = 1) -> str:
    session = get_session()
    params = _params(filters, page)
    url = f"{BASE_URL}/work_orders?{urlencode(params)}"
    try:
        resp = session.get(url, timeout=45, allow_redirects=True)
    except requests.RequestException as exc:
        raise RuntimeError(f"Erro de rede: {exc}") from exc
    if "/login" in resp.url or resp.status_code in (401, 403):
        reset_session()
        session = get_session()
        resp = session.get(url, timeout=45, allow_redirects=True)
        if "/login" in resp.url:
            raise RuntimeError("Sessão expirada e re-login falhou")
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def fetch_work_orders(filters: dict[str, Any], page: int = 1, all_pages: bool = False) -> dict[str, Any]:
    """
    Fluxo por importação:
    1) importar páginas brutas do Nucleus
    2) tratar status (excluir Deletado)
    3) recalcular contadores com base no resultado tratado
    """
    page = max(1, int(page or 1))
    html = _get_html(filters, page)
    nucleus_counts = _fetch_counts(filters)
    max_page = _parse_max_page(html)

    raw_orders: list[dict[str, Any]] = []
    if all_pages:
        seen: set[str] = set()
        total_hint = nucleus_counts["finalizados"] if filters.get("finalizado") else nucleus_counts["todos"]
        if total_hint and PER_PAGE_DEFAULT:
            est_pages = max(max_page, (total_hint + PER_PAGE_DEFAULT - 1) // PER_PAGE_DEFAULT)
        else:
            est_pages = max(max_page, 1)
        # percorre todas as páginas; usa tamanho BRUTO da página para decidir fim
        for p in range(1, min(est_pages, 100) + 1):
            h = html if p == 1 else _get_html(filters, p)
            if p == 1:
                max_page = _parse_max_page(h)
                if total_hint and PER_PAGE_DEFAULT:
                    est_pages = max(max_page, (total_hint + PER_PAGE_DEFAULT - 1) // PER_PAGE_DEFAULT)
            batch_raw = _parse_work_orders_raw(h)
            if not batch_raw:
                break
            for o in batch_raw:
                if o["idTrabalho"] not in seen:
                    seen.add(o["idTrabalho"])
                    raw_orders.append(o)
            # paginação pelo total bruto da página (antes de excluir Deletado)
            if len(batch_raw) < PER_PAGE_DEFAULT:
                break
            if total_hint and len(raw_orders) >= total_hint:
                break
        page = 1
        max_page = 1
    else:
        raw_orders = _parse_work_orders_raw(html)

    # 2) tratamento de status
    orders, stats = _apply_status_rules(raw_orders)
    # 3) contadores com base no importado tratado
    counts = _build_counts(orders, filters, stats, nucleus_counts)

    operadores = _parse_select_options(html, "operador_id")
    empresas = _parse_select_options(html, "company_id")
    if operadores or empresas:
        with _lock:
            _meta_cache["options"] = {
                "operadores": operadores,
                "empresas": empresas,
            }
            _meta_cache["fetched_at"] = time.time()

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    imported_total = len(orders)
    per_page = PER_PAGE_DEFAULT
    total_pages = max(1, max_page)
    if not all_pages:
        nucleus_total = nucleus_counts["finalizados"] if filters.get("finalizado") else nucleus_counts["todos"]
        if nucleus_total and per_page:
            total_pages = max(total_pages, (nucleus_total + per_page - 1) // per_page)

    return {
        "generatedAt": now,
        "live": True,
        "source": "work_orders",
        "baseUrl": BASE_URL,
        "filtro": {
            "periodo": f"{filters.get('date_de') or DATE_DE} a {filters.get('date_ate') or DATE_ATE}",
            "operador": OPERADOR_NOME if filters.get("operador_id") else "Todos",
            "operadorId": str(filters.get("operador_id") or ""),
            "companyId": str(filters.get("company_id") or ""),
            "clientes": list(filters.get("clientes") or []),
            "titulo": str(filters.get("titulo") or ""),
            "ordemServicoId": str(filters.get("ordem_servico_id") or ""),
            "codProduto": str(filters.get("cod_produto") or ""),
            "trabalhoId": str(filters.get("id") or filters.get("trabalho_id") or ""),
            "dateDe": str(filters.get("date_de") or DATE_DE),
            "dateAte": str(filters.get("date_ate") or DATE_ATE),
            "finalizado": bool(filters.get("finalizado")),
        },
        "counts": counts,
        "importStats": stats,
        "pagination": {
            "page": page,
            "perPage": per_page,
            "totalPages": total_pages if not all_pages else 1,
            "total": imported_total,
            "allPages": all_pages,
        },
        "orders": orders,
        "total": imported_total,
    }


def _filters_from_request() -> dict[str, Any]:
    args = request.args
    body = request.get_json(silent=True) or {}
    # body first for POST report, query can still override explicit keys if needed
    src = {**args, **body} if request.method == "POST" else {**body, **args}
    clientes = src.get("clientes") or src.get("empresas") or []
    if isinstance(clientes, str):
        clientes = [c.strip() for c in clientes.split(",") if c.strip()]
    if not isinstance(clientes, list):
        clientes = []
    return {
        "company_id": src.get("company_id") or src.get("companyId") or "",
        "operador_id": src.get("operador_id") if "operador_id" in src or "operadorId" in src else src.get("operadorId", ""),
        "titulo": src.get("titulo") or "",
        "ordem_servico_id": src.get("ordem_servico_id") or src.get("ordemServicoId") or "",
        "cod_produto": src.get("cod_produto") or src.get("codProduto") or "",
        "id": src.get("id") or src.get("trabalho_id") or src.get("trabalhoId") or "",
        "date_de": src.get("date_de") or src.get("dateDe") or DATE_DE,
        "date_ate": src.get("date_ate") or src.get("dateAte") or DATE_ATE,
        "finalizado": str(src.get("finalizado", "")).lower() in ("1", "true", "t", "yes"),
        "clientes": [str(c) for c in clientes if c],
    }


def _cache_key(filters: dict[str, Any], page: int, all_pages: bool) -> str:
    return "|".join(
        [
            BASE_URL,
            str(filters.get("company_id")),
            str(filters.get("operador_id")),
            str(filters.get("titulo")),
            str(filters.get("ordem_servico_id")),
            str(filters.get("cod_produto")),
            str(filters.get("id")),
            str(filters.get("date_de")),
            str(filters.get("date_ate")),
            str(bool(filters.get("finalizado"))),
            str(page),
            str(all_pages),
        ]
    )


def get_data(filters: dict[str, Any], page: int = 1, all_pages: bool = False, force: bool = False) -> dict[str, Any]:
    key = _cache_key(filters, page, all_pages)
    now = time.time()
    with _lock:
        if (
            not force
            and _cache["data"] is not None
            and _cache["key"] == key
            and (now - _cache["fetched_at"]) < CACHE_TTL
        ):
            return {
                **_cache["data"],
                "cached": True,
                "cacheAgeSec": int(now - _cache["fetched_at"]),
                "error": _cache["error"],
            }
    try:
        data = fetch_work_orders(filters, page=page, all_pages=all_pages)
        with _lock:
            _cache["data"] = data
            _cache["error"] = None
            _cache["fetched_at"] = time.time()
            _cache["key"] = key
        return {**data, "cached": False, "cacheAgeSec": 0, "error": None}
    except Exception as exc:
        with _lock:
            _cache["error"] = str(exc)
            if _cache["data"] is not None and _cache["key"] == key:
                return {
                    **_cache["data"],
                    "cached": True,
                    "cacheAgeSec": int(time.time() - _cache["fetched_at"]),
                    "error": str(exc),
                    "stale": True,
                }
        raise


def _pdf_safe(s: Any, n: int = 80) -> str:
    text = str(s or "").replace("\n", " ").replace("\r", " ")
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text[:n]


def _parse_br_date(value: str) -> datetime | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(value or ""))
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _pdf_status_rgb(status: str) -> tuple[int, int, int]:
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
        self.cell(160, 6, "Studio Laser  |  Relatorio de Trabalhos", align="L")
        self.set_font("Helvetica", "", 8)
        self.set_text_color(148, 163, 184)
        self.cell(0, 6, _pdf_safe(self.meta.get("periodo"), 60), align="R")
        self.ln(12)

    def footer(self):
        self.set_y(-12)
        self.set_draw_color(226, 232, 240)
        self.set_line_width(0.2)
        self.line(12, self.get_y(), 285, self.get_y())
        self.set_y(-10)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(100, 116, 139)
        self.cell(120, 6, _pdf_safe(self.meta.get("generated"), 50), align="L")
        self.cell(50, 6, "Confidencial - uso interno", align="C")
        self.cell(0, 6, f"Pagina {self.page_no()}/{{nb}}", align="R")


def build_pdf(data: dict[str, Any]) -> bytes:
    orders = data.get("orders") or []
    filtro = data.get("filtro") or {}
    counts = data.get("counts") or {}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    by_status: dict[str, int] = {}
    by_cliente: dict[str, int] = {}
    atrasados = 0
    sem_previsao = 0
    atraso_dias: list[int] = []
    for o in orders:
        st = o.get("status") or "Sem status"
        cl = o.get("cliente") or "Sem cliente"
        by_status[st] = by_status.get(st, 0) + 1
        by_cliente[cl] = by_cliente.get(cl, 0) + 1
        d = _parse_br_date(o.get("previsao") or "")
        if not d:
            sem_previsao += 1
        else:
            delta = (today - d).days
            if delta > 0:
                atrasados += 1
                atraso_dias.append(delta)

    media_atraso = round(sum(atraso_dias) / len(atraso_dias)) if atraso_dias else 0
    clientes_unicos = len(by_cliente)
    total = len(orders)
    gerado = data.get("generatedAt") or datetime.now().isoformat(timespec="seconds")
    try:
        gerado_fmt = datetime.fromisoformat(str(gerado).replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        gerado_fmt = str(gerado)[:19]

    meta = {
        "periodo": filtro.get("periodo") or "-",
        "generated": f"Gerado em {gerado_fmt}",
    }
    pdf = ReportPDF(meta)
    pdf.alias_nb_pages()
    pdf.add_page()

    # ===== CAPA / HEADER =====
    pdf.set_fill_color(15, 23, 42)
    pdf.rect(0, 0, 297, 42, "F")
    pdf.set_fill_color(37, 99, 235)
    pdf.rect(0, 42, 297, 2.2, "F")
    pdf.set_fill_color(34, 211, 238)
    pdf.rect(0, 0, 4, 44, "F")

    pdf.set_xy(14, 10)
    pdf.set_text_color(34, 211, 238)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "STUDIO LASER  ·  NUCLEUS  ·  WORK ORDERS", ln=1)
    pdf.set_x(14)
    pdf.set_text_color(248, 250, 252)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 10, "Relatorio Operacional de Trabalhos", ln=1)
    pdf.set_x(14)
    pdf.set_text_color(148, 163, 184)
    pdf.set_font("Helvetica", "", 10)
    aba = "Finalizados" if filtro.get("finalizado") else "Todos"
    clientes_sel = filtro.get("clientes") or []
    emp_label = f"{len(clientes_sel)} empresas" if clientes_sel else "todas empresas"
    pdf.cell(
        0,
        6,
        f"Empresas: {_pdf_safe(emp_label, 30)}   |   "
        f"Periodo: {_pdf_safe(filtro.get('periodo'), 40)}   |   Aba: {aba}   |   {gerado_fmt}",
        ln=1,
    )

    y = 52
    # ===== KPI CARDS =====
    kpis = [
        ("TOTAL IMPORTADOS", str(total), (37, 99, 235)),
        ("NO RELATORIO", str(total), (14, 165, 233)),
        ("EMPRESAS", str(clientes_unicos), (34, 197, 94)),
        ("ATRASADOS", str(atrasados), (239, 68, 68)),
        ("SEM PREVISAO", str(sem_previsao), (167, 139, 250)),
        ("MEDIA ATRASO", f"{media_atraso}d", (245, 158, 11)),
    ]
    card_w, card_h, gap = 44, 24, 3.5
    x0 = 12
    for i, (label, value, color) in enumerate(kpis):
        x = x0 + i * (card_w + gap)
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(226, 232, 240)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, card_w, card_h, "DF")
        pdf.set_fill_color(*color)
        pdf.rect(x, y, 2.2, card_h, "F")
        pdf.set_xy(x + 5, y + 4)
        pdf.set_text_color(100, 116, 139)
        pdf.set_font("Helvetica", "B", 7)
        pdf.cell(card_w - 8, 4, label, ln=1)
        pdf.set_x(x + 5)
        pdf.set_text_color(15, 23, 42)
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(card_w - 8, 10, value)

    # ===== CHARTS =====
    y = 84
    panel_h = 78
    left_w, right_w = 135, 135

    def panel(x, yy, w, h, title):
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(226, 232, 240)
        pdf.rect(x, yy, w, h, "DF")
        pdf.set_fill_color(15, 23, 42)
        pdf.rect(x, yy, w, 9, "F")
        pdf.set_xy(x + 4, yy + 2)
        pdf.set_text_color(248, 250, 252)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(w - 8, 5, title)

    def hbars(x, yy, w, items, colors):
        if not items:
            pdf.set_xy(x + 6, yy + 20)
            pdf.set_text_color(148, 163, 184)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(w - 12, 6, "Sem dados")
            return
        max_v = max(v for _, v in items) or 1
        row_h = min(8.2, 58 / max(len(items), 1))
        bar_max = w - 62
        for i, (label, val) in enumerate(items):
            cy = yy + 14 + i * row_h
            if cy + row_h > yy + panel_h - 4:
                break
            color = colors[i % len(colors)]
            pdf.set_xy(x + 4, cy)
            pdf.set_text_color(51, 65, 85)
            pdf.set_font("Helvetica", "", 7)
            pdf.cell(38, row_h - 1.5, _pdf_safe(label, 24), align="R")
            bx = x + 44
            pdf.set_fill_color(241, 245, 249)
            pdf.rect(bx, cy + 1.2, bar_max, row_h - 3.2, "F")
            bw = max(1.5, bar_max * (val / max_v))
            pdf.set_fill_color(*color)
            pdf.rect(bx, cy + 1.2, bw, row_h - 3.2, "F")
            pdf.set_xy(bx + bar_max + 2, cy)
            pdf.set_text_color(15, 23, 42)
            pdf.set_font("Helvetica", "B", 7)
            pdf.cell(12, row_h - 1.5, str(val))

    status_items = sorted(by_status.items(), key=lambda x: -x[1])[:8]
    cliente_items = sorted(by_cliente.items(), key=lambda x: -x[1])[:8]
    status_colors = [
        (245, 158, 11), (59, 130, 246), (14, 165, 233), (167, 139, 250),
        (34, 197, 94), (239, 68, 68), (100, 116, 139), (244, 114, 182),
    ]
    client_colors = [
        (37, 99, 235), (14, 165, 233), (167, 139, 250), (245, 158, 11),
        (34, 197, 94), (244, 114, 182), (99, 102, 241), (20, 184, 166),
    ]

    panel(12, y, left_w, panel_h, "Distribuicao por status")
    hbars(12, y, left_w, status_items, status_colors)
    panel(12 + left_w + 3, y, right_w, panel_h, "Top clientes")
    hbars(12 + left_w + 3, y, right_w, cliente_items, client_colors)

    # filtros ativos
    y = 168
    pdf.set_xy(12, y)
    pdf.set_fill_color(241, 245, 249)
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(12, y, 273, 16, "DF")
    pdf.set_xy(16, y + 3)
    pdf.set_text_color(100, 116, 139)
    pdf.set_font("Helvetica", "B", 7)
    pdf.cell(20, 4, "FILTROS", ln=1)
    pdf.set_x(16)
    pdf.set_text_color(30, 41, 59)
    pdf.set_font("Helvetica", "", 8)
    if clientes_sel:
        emp_txt = ", ".join(_pdf_safe(c, 28) for c in clientes_sel[:12])
        if len(clientes_sel) > 12:
            emp_txt += f" +{len(clientes_sel) - 12}"
    else:
        emp_txt = "todas"
    filtros_txt = (
        f"Empresas: {_pdf_safe(emp_txt, 120)}  ·  "
        f"Sem previsao: {sem_previsao}  ·  Fonte: /work_orders"
    )
    pdf.cell(0, 5, filtros_txt)

    # ===== TABELA =====
    pdf.add_page()
    pdf.set_fill_color(15, 23, 42)
    pdf.rect(12, 18, 273, 10, "F")
    pdf.set_xy(16, 20)
    pdf.set_text_color(248, 250, 252)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(180, 6, f"Detalhamento dos trabalhos  ({total})")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(148, 163, 184)
    pdf.cell(0, 6, "Ordenado por atraso (maior primeiro)", align="R")

    cols = [
        ("#", 8),
        ("ID Trab.", 20),
        ("ID OS", 16),
        ("Cliente", 34),
        ("Nome do trabalho", 78),
        ("Despacho", 22),
        ("Atraso", 18),
        ("Status", 42),
        ("Data", 35),
    ]

    def draw_table_header():
        pdf.set_fill_color(30, 41, 59)
        pdf.set_text_color(226, 232, 240)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_x(12)
        for label, w in cols:
            pdf.cell(w, 7, label, border=0, align="C" if label in ("#", "Atraso") else "L", fill=True)
        pdf.ln()

    # sort: atrasados first
    def sort_key(o):
        d = _parse_br_date(o.get("previsao") or "")
        if not d:
            return (2, 0)
        delta = (today - d).days
        if delta > 0:
            return (0, -delta)
        return (1, delta)

    ordered = sorted(orders, key=sort_key)
    draw_table_header()
    pdf.set_font("Helvetica", "", 6.5)
    row_h = 6.2

    for idx, o in enumerate(ordered, start=1):
        if pdf.get_y() > 190:
            pdf.add_page()
            draw_table_header()
            pdf.set_font("Helvetica", "", 6.5)

        d = _parse_br_date(o.get("previsao") or "")
        if not d:
            atraso_txt = "-"
            atraso_rgb = (148, 163, 184)
        else:
            delta = (today - d).days
            if delta > 0:
                atraso_txt = f"{delta}d"
                atraso_rgb = (239, 68, 68)
            elif delta == 0:
                atraso_txt = "hoje"
                atraso_rgb = (245, 158, 11)
            else:
                atraso_txt = f"{abs(delta)}d"
                atraso_rgb = (34, 197, 94)

        if idx % 2 == 0:
            pdf.set_fill_color(248, 250, 252)
        else:
            pdf.set_fill_color(255, 255, 255)

        y_row = pdf.get_y()
        pdf.set_x(12)
        # background row
        pdf.rect(12, y_row, 273, row_h, "F")

        # status color bar
        status = o.get("status") or ""
        srgb = _pdf_status_rgb(status)
        pdf.set_fill_color(*srgb)
        pdf.rect(12, y_row, 1.4, row_h, "F")

        vals = [
            (str(idx), 8, "C", (100, 116, 139)),
            (_pdf_safe(o.get("idTrabalho"), 12), 20, "L", (37, 99, 235)),
            (_pdf_safe(o.get("idOs"), 10), 16, "L", (30, 41, 59)),
            (_pdf_safe(o.get("cliente"), 20), 34, "L", (30, 41, 59)),
            (_pdf_safe(o.get("nome"), 48), 78, "L", (30, 41, 59)),
            (_pdf_safe(o.get("previsao") or "-", 12), 22, "L", (30, 41, 59)),
            (atraso_txt, 18, "C", atraso_rgb),
            (_pdf_safe(status, 26), 42, "L", srgb),
            (_pdf_safe(o.get("data"), 20), 35, "L", (100, 116, 139)),
        ]
        pdf.set_xy(12, y_row + 0.8)
        for text, w, align, rgb in vals:
            pdf.set_text_color(*rgb)
            if text == atraso_txt or text == _pdf_safe(status, 26):
                pdf.set_font("Helvetica", "B", 6.5)
            else:
                pdf.set_font("Helvetica", "", 6.5)
            pdf.cell(w, row_h - 1.2, text, align=align)
        pdf.ln(row_h)

    # legenda final
    if pdf.get_y() < 185:
        pdf.ln(4)
        pdf.set_x(12)
        pdf.set_text_color(100, 116, 139)
        pdf.set_font("Helvetica", "", 7)
        pdf.cell(0, 5, "Legenda de atraso: vermelho = vencido  |  amarelo = hoje  |  verde = no prazo  |  cinza = sem previsao")

    out = pdf.output()
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return out.encode("latin-1", "replace")


@app.get("/")
def index():
    return send_from_directory(".", "index.html")


@app.get("/relatorio")
@app.get("/report")
def report_page():
    return send_from_directory(".", "report.html")


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "source": "work_orders",
            "baseUrl": BASE_URL,
            "cacheTtl": CACHE_TTL,
            "hasCache": _cache["data"] is not None,
            "lastError": _cache["error"],
        }
    )


@app.get("/api/options")
def api_options():
    """Operadores e empresas (do Nucleus)."""
    with _lock:
        opts = _meta_cache.get("options")
        age = time.time() - _meta_cache.get("fetched_at", 0)
    if opts and age < 600:
        return jsonify({**opts, "cached": True})
    try:
        html = _get_html({"operador_id": "", "date_de": DATE_DE, "date_ate": DATE_ATE}, page=1)
        operadores = _parse_select_options(html, "operador_id")
        empresas = _parse_select_options(html, "company_id")
        with _lock:
            _meta_cache["options"] = {"operadores": operadores, "empresas": empresas}
            _meta_cache["fetched_at"] = time.time()
        return jsonify({"operadores": operadores, "empresas": empresas, "cached": False})
    except Exception as exc:
        if opts:
            return jsonify({**opts, "cached": True, "error": str(exc)})
        return jsonify({"error": str(exc), "operadores": [], "empresas": []}), 502


@app.get("/api/work_orders")
@app.get("/api/fluxo")
def api_work_orders():
    filters = _filters_from_request()
    page = int(request.args.get("page") or 1)
    all_pages = str(request.args.get("all", "")).lower() in ("1", "true", "yes")
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    try:
        data = get_data(filters, page=page, all_pages=all_pages, force=force)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc), "baseUrl": BASE_URL, "live": False, "source": "work_orders"}), 502


def _filter_orders_by_clientes(data: dict[str, Any], clientes: list[str]) -> dict[str, Any]:
    if not clientes:
        return data
    wanted = {c.strip().lower() for c in clientes if c and str(c).strip()}
    orders = [
        o for o in (data.get("orders") or [])
        if (o.get("cliente") or "Sem cliente").strip().lower() in wanted
        or (o.get("cliente") or "").strip().lower() in wanted
    ]
    out = dict(data)
    out["orders"] = orders
    out["total"] = len(orders)
    filtro = dict(out.get("filtro") or {})
    filtro["clientes"] = clientes
    out["filtro"] = filtro
    return out


@app.get("/api/report.pdf")
@app.post("/api/report.pdf")
def api_report_pdf():
    filters = _filters_from_request()
    force_raw = request.args.get("force")
    if force_raw is None and request.is_json:
        force_raw = (request.get_json(silent=True) or {}).get("force")
    if isinstance(force_raw, bool):
        force = force_raw
    else:
        force = str(force_raw or "").lower() in ("1", "true", "yes")
    # report module always wants full dataset then local company filter
    filters["operador_id"] = filters.get("operador_id") or ""
    filters["company_id"] = ""
    try:
        data = get_data(filters, page=1, all_pages=True, force=force)
        clientes = filters.get("clientes") or []
        if clientes:
            data = _filter_orders_by_clientes(data, clientes)
            if not data.get("orders"):
                return jsonify({"error": "Nenhum trabalho para as empresas selecionadas"}), 400
        pdf_bytes = build_pdf(data)
        fname = f"relatorio_empresas_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=fname,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/config")
def api_config_get():
    return jsonify(
        {
            "baseUrl": BASE_URL,
            "source": "work_orders",
            "dateDe": DATE_DE,
            "dateAte": DATE_ATE,
            "operadorId": OPERADOR_ID,
            "operador": OPERADOR_NOME,
            "cacheTtl": CACHE_TTL,
            "email": EMAIL,
        }
    )


@app.post("/api/config")
def api_config_post():
    global BASE_URL, DATE_DE, DATE_ATE, OPERADOR_ID, OPERADOR_NOME, CACHE_TTL
    body = request.get_json(silent=True) or {}
    if body.get("baseUrl"):
        BASE_URL = str(body["baseUrl"]).rstrip("/")
        reset_session()
    if body.get("dateDe"):
        DATE_DE = str(body["dateDe"])
    if body.get("dateAte"):
        DATE_ATE = str(body["dateAte"])
    if body.get("operadorId") is not None:
        OPERADOR_ID = str(body["operadorId"])
    if body.get("operador"):
        OPERADOR_NOME = str(body["operador"])
    if body.get("cacheTtl") is not None:
        try:
            CACHE_TTL = max(5, int(body["cacheTtl"]))
        except (TypeError, ValueError):
            pass
    with _lock:
        _cache["data"] = None
        _cache["fetched_at"] = 0.0
        _cache["key"] = None
    return jsonify(
        {
            "ok": True,
            "baseUrl": BASE_URL,
            "dateDe": DATE_DE,
            "dateAte": DATE_ATE,
            "operadorId": OPERADOR_ID,
            "operador": OPERADOR_NOME,
            "cacheTtl": CACHE_TTL,
            "source": "work_orders",
        }
    )


def main():
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Dashboard live: http://{host}:{port}")
    print(f"Nucleus base:  {BASE_URL}/work_orders")
    print(f"Cache TTL:     {CACHE_TTL}s")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
