#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""צייד החזרי המס: בודק בטפסי 106 אם מס הכנסה חייב לך כסף, ומציג את זה בדף מונפש.

הכול רץ במחשב שלך. אין מפתח, אין חשבון ואין התקנה: פייתון 3.9 ומעלה מספיק,
והספרייה היחידה (pypdf, לקריאת PDF) ארוזה בתיקייה vendor שליד הקובץ.

    python3 tax_refund.py demo [--open]                       הרצה מלאה על טפסי הדוגמה
    python3 tax_refund.py extract FILE... --out RUN           קריאת הטפסים ומחיקת מספרי זהות
    python3 tax_refund.py check RUN/forms.json                בדיקת הנתונים לפני החישוב
    python3 tax_refund.py estimate --forms F --profile P --out RUN/result.json
    python3 tax_refund.py report --result RUN/result.json --out RUN/report.html [--open]

זו הערכה ולא חישוב להגשה ולא ייעוץ מס.
"""
import argparse, datetime, json, math, os, re, sys, webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor"))
RULES = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
C = RULES["constants"]
SRC = RULES["sources"]

DISCLAIMER = ("זו הערכה ולא חישוב להגשה ולא ייעוץ מס. היא נשענת על הנתונים מהטפסים ועל התשובות שלך, "
              "ועלולות להיות בה טעויות. את הסכום הסופי קובעת רשות המסים אחרי שמגישים בקשה.")


# ───────────────────────────── קריאת טפסים ומחיקת מספרי זהות

ID_LABEL = re.compile(r'(מספר\s*זהות|מס[\'׳]?\s*זהות|ת\.?\s*ז\.?|ת"ז|ת״ז|תעודת\s*זהות|מספר\s*ת\.?ז\.?)\s*[:\-]?\s*([\d\s\-]{5,12})')
LONG_DIGITS = re.compile(r'(?<![\d,.])\d(?:[\s\-]?\d){7,9}(?![\d,.])')


def redact(text):
    """מוחק כל מספר שנראה כמו תעודת זהות לפני שמישהו (אדם או מודל) רואה את הטקסט."""
    n = 0

    def lab(m):
        nonlocal n
        n += 1
        return m.group(1) + " [ת.ז. נמחק]"
    text = ID_LABEL.sub(lab, text)

    def dig(m):
        nonlocal n
        n += 1
        return "[מספר נמחק]"
    text = LONG_DIGITS.sub(dig, text)
    return text, n


def pdf_text(path):
    from pypdf import PdfReader
    r = PdfReader(str(path))
    if r.is_encrypted:
        try:
            r.decrypt("")
        except Exception:
            return None, "encrypted"
    return "\n".join((p.extract_text() or "") for p in r.pages), None


AMOUNT = r'(-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|-?\d{1,7}(?:\.\d{1,2})?)'


def amount_after(code, text):
    """מחפש את הסכום שמופיע באותה שורה כמו קוד השדה (למשל 158 או 042)."""
    best = None
    for line in text.splitlines():
        if not re.search(rf'(?<!\d){code}(?!\d)', line):
            continue
        rest = re.sub(rf'(?<!\d){code}(?!\d)', ' ', line, count=1)
        nums = [float(x.replace(',', '')) for x in re.findall(AMOUNT, rest)]
        nums = [x for x in nums if not (1990 <= x <= 2035 and float(x).is_integer() and ',' not in rest)]
        if nums:
            v = max(nums, key=abs)
            best = v if best is None else max(best, v)
    return best


def guess(text, name):
    g = {"file": name, "year": None, "source": "106", "payer": None, "income": None, "tax": None, "pension": None, "months": None}
    if re.search(r'ביטוח\s*לאומי|המוסד לביטוח', text) and not re.search(r'106', text[:400]):
        g["source"] = "btl"
    ym = re.search(r'שנת\s*(?:ה)?מס[^\d]{0,12}(20[1-3]\d)', text) or re.search(r'(20[1-3]\d)\s*[^\d]{0,12}שנת\s*(?:ה)?מס', text)
    if ym:
        g["year"] = int(ym.group(1))
    else:
        ys = re.findall(r'(?<!\d)(20[12]\d)(?!\d)', text)
        if ys:
            g["year"] = int(max(set(ys), key=ys.count))
    inc = amount_after("158", text) or amount_after("172", text)
    g["income"] = inc
    g["tax"] = amount_after("042", text)
    g["pension"] = amount_after("086", text) or amount_after("045", text)
    mm = (re.search(r'(?:חודשי\s*עבודה|מספר\s*חודשים)[^\d\n]{0,15}(\d{1,2})(?!\d)', text)
          or re.search(r'(?<![\d/])(\d{1,2})\s*(?:חודשי\s*עבודה|חודשים)', text))
    if mm and 1 <= int(mm.group(1)) <= 12:
        g["months"] = int(mm.group(1))
    # שם המעסיק: בקובצי PDF בעברית הסדר לפעמים הפוך (הערך לפני התווית), אז בודקים את שני הכיוונים
    opts = [m.group(1) for m in re.finditer(r'(?:שם\s*המעסיק|שם\s*המעביד)\s*[:\-]?\s*([^\n]{3,40})', text)]
    opts += [m.group(1) for m in re.finditer(r'(?:^|\n)([^\n:]{3,40}?)\s*(?:שם\s*המעסיק|שם\s*המעביד)', text)]
    for o in opts:
        o = o.strip(' :')
        if o and '[' not in o and 'מספר' not in o and not re.search(r'\d{3}', o):
            g["payer"] = o
            break
    if g["source"] == "btl":
        g["payer"] = "ביטוח לאומי"
    return g


def cmd_extract(a):
    out = Path(a.out).expanduser()
    (out / "text").mkdir(parents=True, exist_ok=True)
    files = []
    for f in a.files:
        p = Path(f).expanduser()
        files += sorted(p.glob("*.pdf")) + sorted(p.glob("*.PDF")) if p.is_dir() else [p]
    cands, manual = [], []
    for i, p in enumerate(files, 1):
        name = p.name
        if p.suffix.lower() != ".pdf":
            manual.append({"file": name, "why": "תמונה ולא PDF. את המספרים מקלידים ידנית (ראו SKILL.md)."})
            continue
        try:
            text, err = pdf_text(p)
        except Exception as e:
            text, err = None, str(e)[:80]
        if err or not text or len(re.sub(r'\s', '', text)) < 40:
            manual.append({"file": name, "why": "אין בקובץ טקסט שאפשר לקרוא (כנראה סריקה). את המספרים מקלידים ידנית."})
            continue
        clean, n = redact(text)
        (out / "text" / f"{i:02d}.txt").write_text(f"# {name} (נמחקו {n} מספרים מזהים)\n\n{clean}\n", encoding="utf-8")
        g = guess(clean, name)
        g["text"] = f"text/{i:02d}.txt"
        cands.append(g)
    (out / "candidates.json").write_text(json.dumps({"forms": cands, "manual": manual}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"נקראו {len(cands)} קבצים, {len(manual)} צריכים הקלדה ידנית. הטקסט הנקי: {out/'text'}")
    for g in cands:
        miss = [k for k in ("year", "income", "tax") if g[k] is None]
        print(f"  {g['file']}: שנה {g['year']}, הכנסה {g['income']}, מס {g['tax']}, פנסיה {g['pension']}"
              + (f"  ← חסר: {', '.join(miss)}" if miss else ""))
    for m in manual:
        print(f"  {m['file']}: {m['why']}")
    print(f"מועמדים: {out/'candidates.json'}. לפני החישוב מאשרים אותם ושומרים כ-forms.json.")


# ───────────────────────────── בדיקה

def window(today=None):
    y = (today or datetime.date.today()).year
    return list(range(y - 6, y))


def cmd_check(a):
    d = json.loads(Path(a.forms).expanduser().read_text(encoding="utf-8"))
    errs, warns = [], []
    seen = {}
    for f in d.get("forms", []):
        tag = f"{f.get('file') or f.get('payer') or '?'} ({f.get('year')})"
        y = f.get("year")
        if str(y) not in RULES["years"]:
            errs.append(f"{tag}: שנה {y} מחוץ לשנים שהמערכת מכירה ({', '.join(RULES['years'])})")
            continue
        inc, tax = f.get("income"), f.get("tax")
        if not isinstance(inc, (int, float)) or inc <= 0:
            errs.append(f"{tag}: חסרה הכנסה (שדה 158 או 172)")
        if not isinstance(tax, (int, float)) or tax < 0:
            errs.append(f"{tag}: חסר המס שנוכה (שדה 042)")
        if isinstance(inc, (int, float)) and isinstance(tax, (int, float)) and inc > 0:
            r = tax / inc
            if r > 0.5:
                errs.append(f"{tag}: המס ({tax:,.0f}) יותר מחצי מההכנסה ({inc:,.0f}). כנראה שדה לא נכון")
            if inc > 650000:
                warns.append(f"{tag}: הכנסה גבוהה מאוד. ייתכן שחלה חובת דוח שנתי, וזו כבר לא בקשה מקוצרת")
        p = f.get("pension")
        if isinstance(p, (int, float)) and isinstance(inc, (int, float)) and inc and p > 0.12 * inc:
            warns.append(f"{tag}: ההפקדה לפנסיה ({p:,.0f}) גבוהה מ-12% מההכנסה. לוודא שזה שדה 086 או 045 ולא הפקדת המעסיק")
        if int(y) not in window():
            warns.append(f"{tag}: שנת {y} לא בחלון שש השנים שאפשר להגיש עליהן היום")
        seen.setdefault(y, []).append(f.get("source", "106"))
    for m in d.get("manual", []):
        warns.append(f"{m.get('file')}: עוד לא הוקלד")
    for e in errs:
        print("✗", e)
    for w in warns:
        print("!", w)
    if errs:
        print(f"\n{len(errs)} בעיות. מתקנים את forms.json ומריצים שוב.")
        return 1
    print(f"✓ {len(d.get('forms', []))} טפסים תקינים, שנים: {', '.join(str(y) for y in sorted(seen))}")
    return 0


# ───────────────────────────── חישוב

def bracket_tax(year, income):
    R = RULES["years"][str(year)]
    tax, low = 0.0, 0.0
    for up, rate in R["brackets"]:
        if income > low:
            tax += (min(income, up) - low) * rate
        low = up
    if income > low:
        tax += (income - low) * 0.47
    if income > R["surtax_from"]:
        tax += (income - R["surtax_from"]) * C["surtax_rate"]
    return tax


def child_points(year, birth_year, role):
    """לפי הטבלה המרוכזת בכל זכות. role: primary = האם, או ההורה שמקבל את קצבת הילדים. secondary = ההורה השני."""
    age = year - birth_year
    if age < 0 or age > 18:
        return 0.0
    new = year >= 2024
    if age == 0:
        p = 2.5 if new else 1.5
    elif age in (1, 2):
        p = 4.5 if new else 2.5
    elif age == 3:
        p = 3.5 if new else 2.5
    elif age in (4, 5):
        p = 2.5
    elif 6 <= age <= 12:
        p = (2 if year >= 2022 else 1) if role == "primary" else (1 if year >= 2022 else 0)
    elif 13 <= age <= 17:
        p = (2 if year >= 2024 else 1) if role == "primary" else (1 if year >= 2024 else 0)
    else:  # 18
        p = 0.5 if role == "primary" else 0
    return float(p)


def degree_points(year, deg):
    kind, done = deg.get("kind", "first"), int(deg["completed"])
    pts = 0.5 if kind == "second" else 1.0
    if done <= 2022:            # הוראת השעה: שנה אחת, בשנה שאחרי הסיום (ברירת המחדל כאן)
        return pts if year == done + 1 + int(deg.get("delay", 0)) else 0.0
    n = min(int(deg.get("study_years", 1)), 2 if kind == "second" else 3)
    return pts if done + 1 <= year <= done + n else 0.0


def service_points(year, svc, gender):
    try:
        y, m = map(int, str(svc["discharge"]).split("-")[:2])
    except Exception:
        return 0.0
    months = int(svc.get("months", 0))
    full = 24 if svc.get("kind") == "national" else (22 if gender == "female" else 23)
    pts = 2 if months >= full else (1 if months >= 12 else 0)
    start = y * 12 + m          # החודש שאחרי השחרור הוא start+1
    inyear = sum(1 for k in range(start + 1, start + 37) if (k - 1) // 12 == year)
    return pts * inyear / 12


def year_calc(year, forms, profile):
    R = RULES["years"][str(year)]
    point = R["point"]
    inc = sum(f["income"] for f in forms)
    withheld = sum(f["tax"] for f in forms)
    salary = sum(f["income"] for f in forms if f.get("source", "106") == "106")
    dep = sum(f.get("pension") or 0 for f in forms)
    cap = C["pension_share"] * min(salary, R["pension_ceiling_month"] * 12)
    pension_credit = C["pension_credit_rate"] * min(dep, cap)
    gender = profile.get("gender", "male")
    base = C["resident_points"] + (C["woman_points"] if gender == "female" else 0)
    kids = sum(child_points(year, int(c["birth_year"]), c.get("role", "primary" if gender == "female" else "secondary"))
               for c in profile.get("children", []))
    deg = sum(degree_points(year, d) for d in profile.get("degrees", []))
    svc = service_points(year, profile["service"], gender) if profile.get("service") else 0.0
    don = float(profile.get("donations", {}).get(str(year), 0) or 0)
    don_credit = C["donation_credit_rate"] * min(don, C["donation_income_share"] * inc) if don >= R["donation_min"] else 0.0
    gross = bracket_tax(year, inc)

    def tax_with(points, extra=0.0):
        return max(0.0, max(0.0, gross - points * point) - pension_credit - extra)

    t_base = tax_with(base)
    t_kids = tax_with(base + kids)
    t_deg = tax_with(base + kids + deg)
    t_svc = tax_with(base + kids + deg + svc)
    t_all = tax_with(base + kids + deg + svc, don_credit)
    refund = withheld - t_all

    months = [f.get("months") for f in forms if f.get("source", "106") == "106"]
    payers = [f for f in forms if f.get("source", "106") == "106"]
    work_months = sum(m for m in months if m) if months and all(months) else None
    reasons = []
    w_label = "נוכה מהמשכורת יותר מס ממה שנדרש"
    if len(payers) > 1:
        w_label = f"היו לך {len(payers)} מעסיקים באותה שנה, וכל אחד ניכה מס כאילו הוא היחיד"
    elif work_months and work_months < 12:
        w_label = f"עבדת {work_months} חודשים בשנה, והמס נוכה כאילו עבדת שנה שלמה"
    if work_months and work_months < 12 and len(payers) > 1:
        w_label = f"עבדת {work_months} חודשים אצל {len(payers)} מעסיקים, והמס נוכה כאילו עבדת שנה שלמה"
    if any(f.get("source") == "btl" for f in forms):
        w_label += ". גם התשלומים מביטוח לאומי נכנסו לחישוב"
    parts = [
        ("work", w_label, withheld - t_base, SRC["refund"]),
        ("kids", f"{kids:g} נקודות זיכוי על הילדים", t_base - t_kids, SRC["children"]),
        ("degree", "נקודת זיכוי אחת על התואר" if deg == 1 else ("חצי נקודת זיכוי על התואר" if deg == .5 else f"{deg:g} נקודות זיכוי על התואר"), t_kids - t_deg, SRC["degree"]),
        ("service", "נקודות זיכוי על השחרור מהשירות", t_deg - t_svc, SRC["service"]),
        ("donations", f"זיכוי של 35% על תרומות ({don:,.0f} ₪)", t_svc - t_all, SRC["donations"]),
    ]
    clean_parts = all(v > -50 for _, _, v, _ in parts)
    for key, label, v, src in parts:
        if abs(v) < 50:          # סיבה של כמה שקלים רק מבלבלת
            continue
        reasons.append({"key": key, "label": label, "amount": (math.floor(v / 10) * 10) if clean_parts else None, "source": src})

    high = max(0, math.floor(refund / 10) * 10)
    low = max(0, math.floor(refund * (1 - C["safety_margin"]) / 100) * 100)
    status = "promising" if refund >= C["worth_filing"] else ("small" if refund > 0 else ("owe" if refund < -C["worth_filing"] else "none"))
    docs = [f"טופס 106 מ{f.get('payer') or 'המעסיק'}" for f in payers]
    if any(f.get("source") == "btl" for f in forms):
        docs.append("אישור שנתי למס הכנסה מביטוח לאומי")
    if kids:
        docs.append("ספח תעודת זהות עם פרטי הילדים")
    if deg:
        docs.append("אישור זכאות לתואר וטופס 119")
    if svc:
        docs.append("תעודת שחרור")
    if don_credit:
        docs.append("קבלות על התרומות (מוסד עם אישור לפי סעיף 46)")
    docs.append("אישור ניהול חשבון בנק, כדי שההחזר ייכנס אליו")
    return {
        "year": year, "deadline": f"31.12.{year + 6}", "closing": year == window()[0],
        "income": round(inc), "withheld": round(withheld), "should_pay": round(t_all), "refund": round(refund),
        "low": low, "high": high, "status": status,
        "points": {"base": base, "kids": kids, "degree": deg, "service": round(svc, 2), "value": point},
        "pension_credit": round(pension_credit), "donation_credit": round(don_credit),
        "payers": [{"name": f.get("payer") or ("ביטוח לאומי" if f.get("source") == "btl" else "מעסיק"),
                    "source": f.get("source", "106"), "months": f.get("months"),
                    "income": round(f["income"]), "tax": round(f["tax"])} for f in forms],
        "reasons": reasons, "docs": docs,
        "notes": ([] if R["pension_verified"] else ["תקרת ההפקדה לפנסיה בשנה הזו לא אומתה במקור רשמי; נבחר הערך השמרני"]),
    }


FLAG_TEXT = {
    "settlement": ("גרת ביישוב שמזכה בהנחה במס", "האחוז והתקרה משתנים לפי יישוב ושנה, ולכן המערכת לא מחשבת אותם. מצרפים אישור תושבות (טופס 1312א) לבקשה.",
                   SRC["settlement"]),
    "independent_pension": ("הפקדת לפנסיה או לביטוח חיים בעצמך, לא דרך התלוש", "על הפקדה כזו מגיעה הטבה (זיכוי וניכוי), אבל החישוב תלוי בסוג הקופה. מצרפים את האישור השנתי לצורכי מס מהקופה.", SRC["pension"]),
    "single_parent": ("את או אתה הורה יחיד", "יש נקודות נוספות שתלויות במצב המשפחתי המדויק, והמערכת לא מחשבת אותן.", SRC["points"]),
    "disability": ("יש במשפחה ילד או בן משפחה עם מוגבלות", "יש נקודות זיכוי נוספות (טופס 116א).", SRC["points"]),
    "immigrant": ("עלית לארץ בשנים האלה", "עולים חדשים מקבלים נקודות זיכוי לפי חודשים מהעלייה.", SRC["points"]),
    "capital_market": ("היו לך הפסדים בשוק ההון אצל כמה גופים", "לפעמים מגיע החזר על קיזוז הפסדים. מצרפים טופס 867 מכל גוף.", SRC["refund"]),
}


def cmd_estimate(a):
    forms = json.loads(Path(a.forms).expanduser().read_text(encoding="utf-8"))["forms"]
    profile = json.loads(Path(a.profile).expanduser().read_text(encoding="utf-8"))
    by = {}
    for f in forms:
        by.setdefault(int(f["year"]), []).append(f)
    win = window()
    years = [year_calc(y, by[y], profile) for y in sorted(by) if str(y) in RULES["years"]]
    for y in years:
        y["in_window"] = y["year"] in win
    good = [y for y in years if y["status"] == "promising" and y["in_window"]]
    flags = [{"key": k, "title": FLAG_TEXT[k][0], "text": FLAG_TEXT[k][1], "source": FLAG_TEXT[k][2]}
             for k, v in (profile.get("flags") or {}).items() if v and k in FLAG_TEXT]
    missing = [y for y in win if y not in by]
    res = {
        "generated": datetime.date.today().isoformat(),
        "name": profile.get("display_name") or "",
        "years": years, "missing_years": missing,
        "total_low": sum(y["low"] for y in good), "total_high": sum(y["high"] for y in good),
        "promising": [y["year"] for y in good],
        "flags": flags, "links": {"online": SRC["online"], "form135": SRC["form135"], "refund": SRC["refund"]},
        "disclaimer": DISCLAIMER, "rules_checked": RULES["checked"],
    }
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("שנה   הכנסה      נוכה     היה צריך   הפרש     הערכה")
    for y in years:
        print(f"{y['year']}  {y['income']:>9,}  {y['withheld']:>8,}  {y['should_pay']:>8,}  {y['refund']:>7,}  "
              + {"promising": f"{y['low']:,}-{y['high']:,} ₪", "small": "קטן, כנראה לא שווה", "none": "לא נראה החזר",
                 "owe": "זהירות: אולי חוב, לא להגיש בלי בדיקה"}[y["status"]]
              + ("" if y["in_window"] else "  (מחוץ לחלון שש השנים)"))
    print(f"\nסך הכול בשנים המבטיחות: {res['total_low']:,}-{res['total_high']:,} ₪ ({', '.join(map(str, res['promising'])) or 'אין'})")
    if missing:
        print(f"שנים בלי טופס: {', '.join(map(str, missing))}")
    print(f"\n{DISCLAIMER}\nנשמר: {out}")


# ───────────────────────────── הדף המונפש

def font_b64(name):
    import base64
    p = HERE / name
    return base64.b64encode(p.read_bytes()).decode() if p.exists() else ""


def cmd_report(a):
    res = json.loads(Path(a.result).expanduser().read_text(encoding="utf-8"))
    tpl = (HERE / "report_template.html").read_text(encoding="utf-8")
    fonts = (f"@font-face{{font-family:Rubik;src:url(data:font/woff2;base64,{font_b64('rubik-hebrew.woff2')}) format('woff2');font-weight:300 900;unicode-range:U+0590-05FF,U+200C-2010,U+20AA,U+25CC,U+FB1D-FB4F}}"
             f"@font-face{{font-family:Rubik;src:url(data:font/woff2;base64,{font_b64('rubik-latin.woff2')}) format('woff2');font-weight:300 900;unicode-range:U+0000-00FF,U+2000-206F,U+20AC,U+2122}}")
    if a.fonts_css:
        fonts = Path(a.fonts_css).read_text(encoding="utf-8")
    html = (tpl.replace("/*FONTS*/", fonts)
               .replace("/*DATA*/null", json.dumps(res, ensure_ascii=False))
               .replace("/*MODE*/'report'", "'studio'" if a.studio else "'report'"))
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"הדף נשמר: {out}")
    if a.open:
        webbrowser.open(out.resolve().as_uri())


def cmd_demo(a):
    out = Path(a.out).expanduser()
    s = HERE.parent / "samples"
    cmd_extract(argparse.Namespace(files=[str(s)], out=str(out)))
    cand = json.loads((out / "candidates.json").read_text(encoding="utf-8"))
    (out / "forms.json").write_text(json.dumps({"forms": [{k: v for k, v in f.items() if k != "text"} for f in cand["forms"]]},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    if cmd_check(argparse.Namespace(forms=str(out / "forms.json"))):
        sys.exit("הקריאה האוטומטית של טפסי הדוגמה נכשלה")
    cmd_estimate(argparse.Namespace(forms=str(out / "forms.json"), profile=str(s / "profile.json"), out=str(out / "result.json")))
    cmd_report(argparse.Namespace(result=str(out / "result.json"), out=str(out / "report.html"), open=a.open, studio=False, fonts_css=None))


def main():
    ap = argparse.ArgumentParser(description="צייד החזרי המס")
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("extract"); e.add_argument("files", nargs="+"); e.add_argument("--out", required=True)
    c = sp.add_parser("check"); c.add_argument("forms")
    s = sp.add_parser("estimate"); s.add_argument("--forms", required=True); s.add_argument("--profile", required=True); s.add_argument("--out", required=True)
    r = sp.add_parser("report"); r.add_argument("--result", required=True); r.add_argument("--out", required=True)
    r.add_argument("--open", action="store_true"); r.add_argument("--studio", action="store_true"); r.add_argument("--fonts-css")
    d = sp.add_parser("demo"); d.add_argument("--out", default=str(Path.home() / "tax-refund-demo")); d.add_argument("--open", action="store_true")
    a = ap.parse_args()
    rc = {"extract": cmd_extract, "check": cmd_check, "estimate": cmd_estimate, "report": cmd_report, "demo": cmd_demo}[a.cmd](a)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
