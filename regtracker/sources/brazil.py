"""Brazil: payment institutions, FX brokers and Pix participants (Banco Central do Brasil).

Two official BCB publications, merged by CNPJ (the company number):

  1. The Pix participant list, a PDF re-published under a dated file name
     (lista-participantes-instituicoes-em-adesao-pix-YYYYMMDD.pdf). It has two
     tables: active Pix participants, and institutions still joining Pix
     ("em processo de adesão"), which are the applicants. It also lists small
     payment institutions that don't need BCB authorisation but do offer Pix.
  2. The BCB register of authorised institutions (non-bank and bank lists) ("Relação de instituições em
     funcionamento no país", open data under ODbL) via the BCB's Olinda API,
     which adds address, city, website, email and phone.

Kept: payment institutions (authorised and not subject to authorisation), FX
brokers (corretoras de câmbio) and banks (commercial, multiple-service,
co-operative, FX banks and Caixa; include_banks: false turns banks off).
Credit societies, credit unions, investment/development banks and securities
brokers are left out.

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
OLINDA_BANKS = ("https://olinda.bcb.gov.br/olinda/servico/Instituicoes_em_funcionamento/versao/v1/odata/"
                "SedesBancoComMultCE?$top=100000&$format=json")
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


BANK = re.compile(r"^(banco (multiplo|comercial|de cambio)|bancos? (comercia|multipl)|caixa economica)")


def keep_type(kind: str, name: str = "", include: str = INCLUDE, exclude: str = EXCLUDE,
              banks: bool = True) -> bool:
    """Payment institutions, FX brokers and (optionally) banks, decided on the regulator's own type.
    Credit societies, credit unions, investment/development banks and securities firms are left
    out. The register lumps 'Corretoras de TVM e Câmbio' together, so for that segment the firm's
    name has to say câmbio."""
    k = plain(kind)
    if BANK.match(k):
        return banks
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
    if BANK.match(k):
        if "cambio" in k:
            return "Bank (FX bank – banco de câmbio)"
        if "caixa" in k:
            return "Bank (Caixa Econômica Federal)"
        if "cooperativo" in k:
            return "Bank (co-operative bank)"
        return "Bank (commercial bank)" if "comercia" in k else "Bank (multiple-service bank)"
    if "nao sujeita" in k:
        return "Payment institution (not subject to BCB authorisation)"
    if "pagamento" in k:
        return "Payment institution (BCB authorised)"
    if "cambio" in k:
        return "FX broker (corretora de câmbio)"
    return kind


# ---------------------------------------------------------------- Pix PDF
#
# pdfplumber reads the table one text line per printed line. A cell that wraps
# (a long name, "Provedor de Conta / Transacional", "Instituição de Pagamento /
# não sujeita à autorização / pelo BCB") is centred on its row, so its other
# pieces sit on the lines just above and below the row's main line, mixed with
# pieces of the other columns. Each row is read from its main line (row number
# + CNPJ) plus those neighbouring pieces.

# pieces of the other columns, removed (case-sensitive) to leave only name pieces
COLUMN_BITS = sorted([
    "Provedor de Conta", "Transacional", "Instituição de Pagamento", "não sujeita à autorização",
    "pelo BCB", "Sociedade de Crédito Direto", "Sociedade de Crédito,", "Sociedade de Crédito", "Financiamento e",
    "Investimento", "Associação de Poupança e", "Empréstimo", "Liquidante Especial", "Cooperativa de Crédito",
    "Banco Múltiplo", "Cooperativo", "Banco Comercial", "Microempreendedor", "Sociedade Distribuidora de",
    "Sociedade Corretora de", "TVM", "Aguardando Início da", "Aguardando Início", "Aguardando", "Etapa de Operação",
    "Etapa Homologatória", "Homologatória", "Operação Restrita", "Restrita", "Etapa", "Início da", "de Operação", "Caixa Econômica", "Federal", "Iniciador",
], key=len, reverse=True)
HEADER = re.compile(r"Nome Reduzido|Tipo de|Autorizada|Participa[cç][aã]o|Inicia[cç][aã]o de|Facilitador|"
                    r"^pelo BCB de|^no SPI|Pagamento Troco|Publicado em|^Lista de|download|clique aqui|"
                    r"Status da|Modalidade|^P[aá]gina \d", re.I)
ROW = re.compile(r"^(\d{1,4})\s+(.*)$")


def _name_bits(line: str) -> str:
    if HEADER.search(line):
        return ""
    for bit in COLUMN_BITS:
        line = line.replace(bit, " ")
    return re.sub(r"\s+", " ", line).strip()


def _gap_parts(gap: list) -> tuple:
    """Lines between two rows -> (half-line piece of the row above, full lines in order,
    half-line piece of the row below). A 2-line cell sits half a line above and below its
    row ("Provedor de Conta" / "Transacional"); a 3-line cell a full line above and below."""
    half_below = next((l for l in gap if "Transacional" in l and "Provedor de Conta" not in l), None)
    half_above = next((l for l in reversed(gap) if "Provedor de Conta" in l), None)
    full = [l for l in gap if l is not half_below and l is not half_above]
    return half_below, full, half_above


def _rows(lines: list) -> list:
    """[(main line, name pieces above, name pieces below, all nearby lines)] for one table."""
    idx, n = [], 1
    for i, l in enumerate(lines):
        m = ROW.match(l)
        if m and int(m.group(1)) == n and CNPJ.search(l):
            idx.append(i)
            n += 1
    gaps = [lines[(idx[k - 1] + 1 if k else 0):i] for k, i in enumerate(idx)]
    gaps.append(lines[idx[-1] + 1:idx[-1] + 4] if idx else [])
    above = [[] for _ in idx]
    below = [[] for _ in idx]
    for k in range(len(idx)):
        hb, full, ha = _gap_parts(gaps[k])
        named = [b for b in (_name_bits(l) for l in full) if b]
        if k:   # the row above takes as many full lines below as it had above (cells are centred)
            take = min(len(above[k - 1]) - (1 if above[k - 1] and above[k - 1][-1] == "\x00half" else 0), len(named))
            below[k - 1] += ([_name_bits(hb)] if hb and _name_bits(hb) else []) + named[:take]
            named = named[take:]
        above[k] = named + ([_name_bits(ha), "\x00half"] if ha and _name_bits(ha) else [])
    if idx:
        hb, full, _ = _gap_parts(gaps[-1])
        n_full = len([x for x in above[-1] if x != "\x00half"]) - (1 if "\x00half" in above[-1] else 0)
        below[-1] += ([_name_bits(hb)] if hb and _name_bits(hb) else []) + \
            [b for b in (_name_bits(l) for l in full) if b][:max(n_full, 0)]
    out = []
    for k, i in enumerate(idx):
        out.append((lines[i], [x for x in above[k] if x != "\x00half"], below[k], lines[max(i - 1, 0):i + 2]))
    return out


def _status(text: str) -> str:
    t = plain(text)
    if "homolog" in t:
        return "Etapa Homologatória"
    if "restrita" in t:
        return ("Aguardando Início da Etapa de Operação Restrita" if "aguardando" in t
                else "Etapa de Operação Restrita")
    m = re.search(r"Aguardando[\wÀ-ú ]*", text)
    return m.group(0).strip() if m else "Joining Pix"


def _applicants_head(lines: list) -> list:
    cut = next((i for i, l in enumerate(lines) if re.search(r"processo de ades", l, re.I)), None)
    return lines[cut:cut + 60] if cut is not None else ["(no applicants table found)"]


def parse_pix(lines: list) -> list:
    """[{name, cnpj, type, authorised, roles, status, joining}] from the Pix participant PDF."""
    cut = next((i for i, l in enumerate(lines) if re.search(r"processo de ades", l, re.I)), len(lines))
    out = []
    for joining, part in ((False, lines[:cut]), (True, lines[cut:])):
        for main, above, below, near in _rows(part):
            m = ROW.match(main)
            c = CNPJ.search(main)
            own_name = re.sub(r"(?:^|\s)\d{8}\s*$", "", main[m.start(2):c.start()]).strip()
            rest = main[c.end():]
            name = " ".join(x for x in [*above, own_name, *below] if x)
            name = re.sub(r"\s+", " ", name).strip(" -")
            pr = plain(rest)
            if "nao sujeita" in pr:
                kind, auth = "Instituição de Pagamento não sujeita à autorização pelo BCB", False
            else:
                kind = next((t for t in PIX_TYPES if plain(t) in pr), "")
                after = pr.split(plain(kind), 1)[1] if kind else pr
                a = re.search(r"\b(sim|nao)\b", after)
                auth = bool(a and a.group(1) == "sim")
            window = " ".join([main, *near])
            if "Iniciador" in rest:
                roles = [ROLES["iniciador"]]
            elif "Liquidante" in rest:
                roles = [ROLES["liquidante especial"]]
            else:
                roles = [ROLES["provedor de conta transacional"]]
            if name and kind:
                out.append({"name": name, "cnpj": cnpj_digits(c.group(1)), "type": kind, "authorised": auth,
                            "roles": roles, "status": _status(window) if joining else "", "joining": joining})
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
    fingerprint = "br-bcb-v3"

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
            "rows": len(lines), "kept": len(rows), "columns": {},
            "first_text_lines": lines[:60] + ["...", "(applicants table)"] + _applicants_head(lines)}, err)])
        if err:
            raise err
        ctx.log.info("%s: Pix list edition %s, %d participants", self.name, found, len(rows))
        return rows, f"{found[6:]}/{found[4:6]}/{found[:4]}"

    def _register(self, ctx, cfg):
        urls = [cfg.get("register_api") or OLINDA]
        if cfg.get("include_banks", True):
            urls.append(cfg.get("register_banks_api") or OLINDA_BANKS)
        rows, notes, got_any = [], [], False
        for url in urls:
            if not self._allowed(ctx, url):
                continue
            r = ctx.session.get(url, timeout=180, headers={**UA, "Accept": "application/json"})
            r.raise_for_status()
            err, got, data = None, [], {}
            try:
                data = r.json()
                got = parse_register(data)
            except Exception as e:
                err = e
            first = (data.get("value") or [{}])[0] if isinstance(data, dict) else {}
            notes.append((url, r.content, {
                "rows": len(got), "kept": len(got), "columns": sorted(first),
                "first_text_lines": [json.dumps(x, ensure_ascii=False) for x in (data.get("value") or [])[:5]]
                if isinstance(data, dict) else []}, err))
            if err:
                write_diagnostics(ctx, self.name + "_register", notes)
                raise err
            got_any = True
            rows += got
        write_diagnostics(ctx, self.name + "_register", notes)
        return rows if got_any else None

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config(self.name)
        inc, exc = cfg.get("include_types") or INCLUDE, cfg.get("exclude_types") or EXCLUDE
        banks = bool(cfg.get("include_banks", True))
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
            if not keep_type(r["segment"] or "Instituição de Pagamento", r["name"], inc, exc, banks):
                continue
            k = r["cnpj"][:8] or "n:" + plain(r["name"])
            firms[k] = {"name": r["name"], "kind": r["segment"] or "Instituição de Pagamento", "reg": r,
                        "pix": None}
        for p in pix or []:
            k = p["cnpj"][:8]
            if k in firms:
                firms[k]["pix"] = p
            elif keep_type(p["type"], p["name"], inc, exc, banks):
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
