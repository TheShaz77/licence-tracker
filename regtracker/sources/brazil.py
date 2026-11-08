"""Brazil: payment institutions, FX brokers and Pix participants (Banco Central do Brasil).

Two official BCB publications, merged by CNPJ (the company number):

  1. The Pix participant list, a PDF re-published under a dated file name
     (lista-participantes-instituicoes-em-adesao-pix-YYYYMMDD.pdf). It has two
     tables: active Pix participants, and institutions still joining Pix
     ("em processo de adesão"), which are the applicants. It also lists small
     payment institutions that don't need BCB authorisation but do offer Pix.
  2. The BCB register of authorised institutions ("Relação de instituições em
     funcionamento no país", open data under ODbL) via the BCB's Olinda API,
     which adds address, city, website, email and phone.

Kept: payment institutions (authorised and not subject to authorisation) and
FX brokers (corretoras de câmbio). Banks, credit societies and securities
brokers are left out (configurable with include_types).

Both are checked against robots.txt on every run. If one is refused, the other
still produces the list.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import write_diagnostics
from .usstates import pdf_lines

PIX_PDF = ("https://www.bcb.gov.br/content/estabilidadefinanceira/participantes_pix_pdf/"
           "lista-participantes-instituicoes-em-adesao-pix-{date}.pdf")
PIX_KNOWN = "20260513"
PIX_PAGE = "https://www.bcb.gov.br/estabilidadefinanceira/participantespix"
OLINDA = ("https://olinda.bcb.gov.br/olinda/servico/Instituicoes_em_funcionamento/versao/v1/odata/"
          "SedesSociedades?$top=100000&$format=json")
REGISTER_PAGE = "https://www.bcb.gov.br/estabilidadefinanceira/relacao_instituicoes_funcionamento"
UA = {"User-Agent": "Mozilla/5.0 (compatible; licence-tracker/1.0)"}

CNPJ = re.compile(r"\b(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})\b")
INCLUDE = r"pagamento|c[aâ]mbio"
EXCLUDE = r"t[ií]tulos|valores mobili|\btvm\b|banco|cr[eé]dito|cooperativa|financiamento"

# longest first, so "... não sujeita à autorização" wins over "Instituição de Pagamento"
PIX_TYPES = sorted([
    "Instituição de Pagamento não sujeita à autorização pelo BCB", "Instituição de Pagamento",
    "Sociedade de Crédito Direto", "Sociedade de Crédito, Financiamento e Investimento",
    "Sociedade de Crédito ao Microempreendedor", "Sociedade de Empréstimo entre Pessoas",
    "Banco Múltiplo Cooperativo", "Banco Múltiplo", "Banco Comercial", "Banco de Câmbio",
    "Banco de Investimento", "Banco de Desenvolvimento", "Caixa Econômica Federal",
    "Associação de Poupança e Empréstimo", "Cooperativa de Crédito", "Sociedade Distribuidora de TVM",
    "Sociedade Corretora de TVM", "Sociedade Corretora de Câmbio", "Agência de Fomento",
], key=len, reverse=True)
ROLES = {"provedor de conta transacional": "Pix accounts", "iniciador": "Pix payment initiation (ITP)",
         "liquidante especial": "Pix special settlement"}


def plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip().lower()


def cnpj_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def keep_type(kind: str, name: str = "", include: str = INCLUDE, exclude: str = EXCLUDE) -> bool:
    """Payment institutions and FX brokers, decided on the regulator's own type, not banks, credit
    or securities firms. The register lumps 'Corretoras de TVM e Câmbio' together, so for that
    segment the firm's name has to say câmbio."""
    k = plain(kind)
    if re.search(plain(exclude), k) and "cambio" not in k:
        return False
    if "pagamento" in k:
        return bool(re.search(plain(include), k))
    if "cambio" in k:
        if re.search(r"titulos|tvm|valores", k):    # combined securities + FX segment
            return "cambio" in plain(name)
        return not k.startswith("banco") and bool(re.search(plain(include), k))
    return False


def licence_label(kind: str) -> str:
    k = plain(kind)
    if "nao sujeita" in k:
        return "Payment institution (not subject to BCB authorisation)"
    if "pagamento" in k:
        return "Payment institution (BCB authorised)"
    if "cambio" in k:
        return "FX broker (corretora de câmbio)"
    return kind


# ---------------------------------------------------------------- Pix PDF

def _section_rows(text: str) -> list:
    """Rows of one table, found by their running row numbers 1, 2, 3 ..."""
    rows, pos, n = [], 0, 1
    while True:
        m = re.compile(r"(?:^|\s)%d\s+(?=\S)" % n).search(text, pos)
        if not m:
            break
        c = CNPJ.search(text, m.end())
        if not c:
            break
        nxt = re.compile(r"(?:^|\s)%d\s+(?=\S)" % (n + 1)).search(text, c.end())
        end = nxt.start() if nxt else len(text)
        rows.append((text[m.end():c.start()], c.group(1), text[c.end():end]))
        pos, n = end, n + 1
    return rows


def parse_pix(lines: list) -> list:
    """[{name, cnpj, type, authorised, roles, status, joining}] from the Pix participant PDF."""
    text = " ".join(lines)
    parts = re.split(r"lista de institui\S+ em processo de ades\S+", text, flags=re.I)
    out = []
    for joining, part in ((False, parts[0]), (True, " ".join(parts[1:]))):
        if not part.strip():
            continue
        for head, cnpj, tail in _section_rows(part):
            name = re.sub(r"\s+\d{8}\s*$", "", head).strip()   # drop the ISPB code
            name = re.sub(r"\s+", " ", name)
            pt = plain(tail)
            kind = next((t for t in PIX_TYPES if plain(t) in pt), "")
            after = pt.split(plain(kind), 1)[1] if kind else pt
            auth = re.search(r"\b(sim|nao)\b", after)
            roles = [v for k, v in ROLES.items() if k in pt]
            status = ""
            if joining:
                ms = re.search(r"((?:etapa|aguardando)\b.*)$", tail, re.I)
                status = re.sub(r"\s+", " ", ms.group(1)).strip() if ms else "Joining Pix"
            if name and kind:
                out.append({"name": name, "cnpj": cnpj_digits(cnpj), "type": kind,
                            "authorised": bool(auth and auth.group(1) == "sim"), "roles": roles,
                            "status": status, "joining": joining})
    return out


# ---------------------------------------------------------------- BCB register (Olinda)

def _field(row: dict, *words) -> str:
    """Value of the first column whose (accent-free, lower-case) name contains one of `words`."""
    keys = {plain(k).replace("_", ""): k for k in row}
    for w in words:
        for pk, k in keys.items():
            if w in pk and row.get(k) not in (None, ""):
                return str(row[k]).strip()
    return ""


def parse_register(data: dict) -> list:
    rows = data.get("value") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        name = _field(r, "nomeinstituicao", "nomeentidade", "razaosocial", "nome")
        cnpj = cnpj_digits(_field(r, "cnpj"))
        seg = _field(r, "segmento", "tipoinstituicao", "tipo")
        if not name:
            continue
        site = _field(r, "sitio", "site", "internet", "url")
        if site and not site.lower().startswith("http"):
            site = "https://" + site.lstrip("/")
        ddd, tel = _field(r, "ddd"), _field(r, "telefone", "fone")
        out.append({"name": name, "cnpj": cnpj, "segment": seg,
                    "city": _field(r, "municipio", "cidade"), "uf": _field(r, "uf", "estado"),
                    "address": " ".join(x for x in (_field(r, "endereco", "logradouro"), _field(r, "complemento"),
                                                    _field(r, "bairro"), _field(r, "cep")) if x),
                    "email": _field(r, "email", "e-mail", "correio").lower(), "website": site,
                    "phone": (f"+55 {ddd} {tel}" if ddd and tel else (f"+55 {tel}" if tel else "")),
                    "date": _field(r, "dataautoriz", "datainicio", "data")[:10]})
    return out


# ---------------------------------------------------------------- source

class BrazilSource(Source):
    name = "br_bcb"
    label = "Brazil payment institutions, FX brokers & Pix participants (BCB)"
    fingerprint = "br-bcb-v1"

    def _allowed(self, ctx, url) -> bool:
        if robots_allowed(ctx.session, url):
            return True
        ctx.log.warning("%s: %s is disallowed by robots.txt, skipped", self.name, url.split("?")[0])
        return False

    # -- Pix list: newest dated edition
    def _pix(self, ctx, cfg):
        state_p = ctx.data_dir / "state" / "br_pix_editions.json"
        st = json.loads(state_p.read_text()) if state_p.exists() else {}
        known = st.get("edition") or cfg.get("pix_known_edition") or PIX_KNOWN
        tries = [known]
        if st.get("probed") != ctx.today:
            # once a day, look for a newer edition (newest first); HEAD requests only
            today = dt.date.fromisoformat(ctx.today)
            days = (today - dt.datetime.strptime(known, "%Y%m%d").date()).days
            newer = [(today - dt.timedelta(days=d)).strftime("%Y%m%d") for d in range(0, min(days, 200))]
            tries = newer + tries
        tmpl = cfg.get("pix_pdf") or PIX_PDF
        if not self._allowed(ctx, tmpl.format(date=known)):
            return None, ""
        found = ""
        for date in tries:
            url = tmpl.format(date=date)
            r = ctx.session.head(url, timeout=60, headers=UA, allow_redirects=True)
            if r.status_code == 200 and "html" not in r.headers.get("Content-Type", "").lower():
                found = date
                break
        st["probed"] = ctx.today
        if found:
            st["edition"] = found
        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(st))
        if not found:
            ctx.log.warning("%s: no Pix participant list found (tried %d editions)", self.name, len(tries))
            return None, ""
        url = tmpl.format(date=found)
        r = ctx.session.get(url, timeout=180, headers=UA)
        r.raise_for_status()
        err, rows, lines = None, [], []
        try:
            lines = pdf_lines(r.content)
            rows = parse_pix(lines)
        except Exception as e:
            err = e
        write_diagnostics(ctx, self.name + "_pix", [(url, r.content, {
            "rows": len(lines), "kept": len(rows), "columns": {}, "first_text_lines": lines[:60]}, err)])
        if err:
            raise err
        ctx.log.info("%s: Pix list edition %s, %d participants", self.name, found, len(rows))
        return rows, f"{found[6:]}/{found[4:6]}/{found[:4]}"

    def _register(self, ctx, cfg):
        url = cfg.get("register_api") or OLINDA
        if not self._allowed(ctx, url):
            return None
        r = ctx.session.get(url, timeout=180, headers={**UA, "Accept": "application/json"})
        r.raise_for_status()
        err, rows, data = None, [], {}
        try:
            data = r.json()
            rows = parse_register(data)
        except Exception as e:
            err = e
        first = (data.get("value") or [{}])[0] if isinstance(data, dict) else {}
        write_diagnostics(ctx, self.name + "_register", [(url, r.content, {
            "rows": len(rows), "kept": len(rows), "columns": sorted(first),
            "first_text_lines": [json.dumps(x, ensure_ascii=False) for x in (data.get("value") or [])[:5]]
            if isinstance(data, dict) else []}, err)])
        if err:
            raise err
        return rows

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config(self.name)
        inc, exc = cfg.get("include_types") or INCLUDE, cfg.get("exclude_types") or EXCLUDE
        problems = []
        try:
            pix, edition = self._pix(ctx, cfg)
        except Exception as e:
            pix, edition = None, ""
            problems.append(f"Pix list: {e}")
        try:
            reg = self._register(ctx, cfg)
        except Exception as e:
            reg = None
            problems.append(f"BCB register: {e}")
        for p in problems:
            ctx.log.warning("%s: %s", self.name, p)
        if pix is None and reg is None:
            raise SourceNotReady("neither the Pix list nor the BCB register could be read: " + "; ".join(problems))

        firms = {}   # CNPJ root (first 8 digits) -> merged info
        for r in reg or []:
            if not keep_type(r["segment"], r["name"], inc, exc):
                continue
            k = r["cnpj"][:8] or "n:" + plain(r["name"])
            firms[k] = {"name": r["name"], "kind": r["segment"] or "Instituição de Pagamento", "reg": r,
                        "pix": None}
        for p in pix or []:
            k = p["cnpj"][:8]
            if k in firms:
                firms[k]["pix"] = p
            elif keep_type(p["type"], p["name"], inc, exc):
                firms[k] = {"name": p["name"], "kind": p["type"], "reg": None, "pix": p}

        recs = []
        for k, f in firms.items():
            reg, p = f["reg"] or {}, f["pix"]
            ltype = licence_label(f["kind"])
            if p and not reg and ltype.endswith("(BCB authorised)") and not p["authorised"]:
                ltype = "Payment institution (BCB authorisation pending)"
            if p and p["joining"]:
                pix_txt = f"Joining Pix ({p['status']})"
                ltype += " – joining Pix"
            elif p:
                pix_txt = "Pix participant" + (f": {', '.join(p['roles'])}" if p["roles"] else "")
            else:
                pix_txt = "Not on the Pix participant list" if pix is not None else ""
            detail = "; ".join(x for x in (
                "On the BCB register of authorised institutions" if reg else "", pix_txt) if x)
            recs.append(Record(
                key="br_bcb:" + k, source="br_bcb", jurisdiction="BR", regulator="BCB", name=f["name"],
                licence_type=ltype, active=True, status_detail=detail,
                status_date=reg.get("date", "") or (edition if p else ""),
                reference=reg.get("cnpj") or (p or {}).get("cnpj", ""),
                city=(reg.get("city", "").title() if reg.get("city", "").isupper() else reg.get("city", "")),
                website=reg.get("website", ""), email=reg.get("email", ""),
                services="; ".join((p or {}).get("roles", [])),
                register_url=REGISTER_PAGE if reg else PIX_PAGE,
                extra={"phone": reg.get("phone", ""), "address": reg.get("address", ""), "hq_state": reg.get("uf", ""),
                       "pix_status": pix_txt}))
        if len(recs) < int(cfg.get("min_firms", 50)):
            raise RuntimeError(f"Brazil: only {len(recs)} firms found; see data/debug/br_bcb_*_sample.txt")
        return recs
