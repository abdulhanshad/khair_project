from __future__ import annotations

import csv
import io
import base64
from html import escape
import re
import textwrap
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from urllib.parse import quote

import altair as alt
import pandas as pd
import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont


SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vRkSXELpOMNXtcw7spAk_1HwJT--CvPi2q-5vWM7QVycb24WWowaOHB1AUi32m4qT162ZH9bozMEQO7/"
    "pub?gid=1055031371&single=true&output=csv"
)
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "khair-logo.png"
BAHRAIN_GOLD_URL = "https://gulfnews.com/gold-forex/bahrain-gold-prices"
HOLDINGS = {
    "22K gold": {"karat": 22, "grams": 7.7},
    "24K biscuit": {"karat": 24, "grams": 10.0},
}
MONTH_RE = re.compile(r"^[A-Za-z]{3}-\d{2}$")
MONEY_RE = re.compile(r"^[+-]?(?:\d[\d,]*\.?\d*|\.\d+)$")

st.set_page_config(
    page_title="Khair · Group Fund",
    page_icon=":material/monitoring:",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def cell_number(value: str | None) -> float | None:
    """Parse sheet currency, including accounting-style negatives."""
    if value is None:
        return None
    value = value.strip().replace("\u00a0", "")
    if not value or value in {"-", "–", "—"}:
        return None
    negative = value.startswith("(") and value.endswith(")")
    raw = value[1:-1] if negative else value
    raw = re.sub(r"\b(?:BHD|BD)\b", "", raw, flags=re.IGNORECASE).replace(",", "").strip()
    if not MONEY_RE.match(raw):
        return None
    number = float(raw)
    return -number if negative else number


def parse_sheet(text: str) -> dict:
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    header_index = next(
        (
            i
            for i, row in enumerate(rows)
            if any(cell.strip().lower() == "name" for cell in row)
            and sum(bool(MONTH_RE.match(cell.strip())) for cell in row) > 1
        ),
        None,
    )
    if header_index is None:
        raise ValueError("The CSV does not contain a Name column and month columns.")

    header = [cell.strip() for cell in rows[header_index]]
    name_index = next(i for i, cell in enumerate(header) if cell.lower() == "name")
    month_indices = [i for i, cell in enumerate(header) if MONTH_RE.match(cell)]
    months = [header[i] for i in month_indices]
    registration_index = next(
        (i for i, cell in enumerate(header) if cell.lower().startswith("reg")), None
    )
    phone_index = next(
        (
            i
            for i, cell in enumerate(header)
            if re.search(r"phone|whats.?app|mobile|contact", cell, re.IGNORECASE)
        ),
        None,
    )
    email_index = next(
        (
            i
            for i, cell in enumerate(header)
            if re.search(r"e.?mail", cell, re.IGNORECASE)
        ),
        None,
    )
    members: list[dict] = []
    next_row = header_index + 1
    for row in rows[next_row:]:
        name = row[name_index].strip() if name_index < len(row) else ""
        if not name:
            break
        payments = [
            cell_number(row[i] if i < len(row) else None) or 0.0
            for i in month_indices
        ]
        registration = (
            cell_number(row[registration_index])
            if registration_index is not None and registration_index < len(row)
            else None
        )
        phone = (
            row[phone_index].strip()
            if phone_index is not None and phone_index < len(row)
            else ""
        )
        email = (
            row[email_index].strip()
            if email_index is not None and email_index < len(row)
            else ""
        )
        member = {
            "Name": name,
            "Registration fee": registration or 0.0,
            "Phone": phone,
            "Email": email,
        }
        member.update({month: amount for month, amount in zip(months, payments)})
        member["Paid months"] = sum(amount > 0 for amount in payments)
        member["Monthly contributions"] = sum(payments)
        members.append(member)
        next_row += 1

    if not members:
        raise ValueError("The sheet has month headers but no member rows.")

    amounts = [
        member[month]
        for member in members
        for month in months
        if member[month] > 0
    ]
    monthly_fee = median(amounts) if amounts else 10.0

    ledger: dict[str, float] = {}
    gold_purchase_values: dict[int, float | None] = {22: None, 24: None}
    iban_no = ""
    known_labels = (
        "reg. fee",
        "meeting exp.",
        "total collection",
        "total outstanding",
        "gold",
        "kuri",
        "rounding",
        "balance",
    )
    for row in rows[next_row:]:
        for index, cell in enumerate(row):
            if re.fullmatch(r"iban\s*(?:no\.?|number)?\s*:?", cell.strip(), re.IGNORECASE):
                iban_no = next((candidate.strip() for candidate in row[index + 1 :] if candidate.strip()), iban_no)
            gold_match = re.search(r"\bgold\s*(22|24)\s*k\b", cell.strip(), re.IGNORECASE)
            if gold_match:
                purchase_value = next(
                    (
                        parsed
                        for candidate in row[index + 1 :]
                        if (parsed := cell_number(candidate)) is not None
                    ),
                    None,
                )
                if purchase_value is not None:
                    gold_purchase_values[int(gold_match.group(1))] = purchase_value

        for index, cell in enumerate(row):
            label = cell.strip().lower()
            if label in known_labels:
                value = next(
                    (
                        parsed
                        for candidate in row[index + 1 :]
                        if (parsed := cell_number(candidate)) is not None
                    ),
                    None,
                )
                if value is not None:
                    ledger[label] = value
                break

    monthly = []
    for month in months:
        collected = sum(member[month] for member in members)
        monthly.append(
            {
                "Month": month,
                "Collected (BHD)": collected,
                "Paid members": sum(member[month] > 0 for member in members),
                "Expected (BHD)": len(members) * monthly_fee,
            }
        )

    return {
        "members": pd.DataFrame(members),
        "months": months,
        "monthly": pd.DataFrame(monthly),
        "ledger": ledger,
        "monthly_fee": monthly_fee,
        "gold_purchase_values": gold_purchase_values,
        "has_phone_column": phone_index is not None,
        "has_email_column": email_index is not None,
        "iban_no": iban_no,
        "has_iban_value": bool(iban_no),
        "loaded_at": datetime.now().astimezone(),
    }


def fetch_sheet() -> dict:
    # This desktop environment exports a placeholder proxy at 127.0.0.1:9.
    # Ignore inherited proxy variables so the server can reach the public CSV.
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        SHEET_CSV_URL,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=25,
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if "csv" not in content_type and "text/plain" not in content_type:
        raise ValueError(f"Expected CSV data, received {content_type or 'unknown content'}.")
    return parse_sheet(response.text)


class _TableReader(HTMLParser):
    """Small standard-library HTML table reader for the published rate page."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] = []
        self.all_text: list[str] = []
        self.in_table = False
        self.in_row = False
        self.in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self.in_table = True
            self.rows = []
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_table and self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.cell = []

    def handle_data(self, data: str) -> None:
        cleaned = data.strip()
        if cleaned:
            self.all_text.append(cleaned)
            if self.in_cell:
                self.cell.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and self.in_cell and tag in {"td", "th"}:
            self.row.append(" ".join(self.cell).strip())
            self.in_cell = False
        elif self.in_table and self.in_row and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            self.tables.append(self.rows)
            self.in_table = False


def fetch_gold_rates() -> dict:
    """Read the latest available Bahrain session rates from Gulf News."""
    session = requests.Session()
    session.trust_env = False
    response = session.get(BAHRAIN_GOLD_URL, timeout=25)
    response.raise_for_status()
    parser = _TableReader()
    parser.feed(response.text)

    rates: dict[int, float] = {}
    for table in parser.tables:
        for row in table:
            if not row:
                continue
            label = row[0].lower()
            karat = 24 if "24 carat" in label else 22 if "22 carat" in label else None
            if karat is None:
                continue
            # The first three values follow Morning, Afternoon, Evening; the last
            # column is yesterday. Use the latest populated session for today.
            session_rates = [cell_number(value) for value in row[1:4]]
            available = [value for value in session_rates if value is not None]
            if available:
                rates[karat] = available[-1]

    if 22 not in rates or 24 not in rates:
        raise ValueError("Could not find today's 22K and 24K Bahrain rates.")

    page_text = " ".join(parser.all_text)
    date_match = re.search(r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3}\s+\d{4})\s+LIVE", page_text)
    update_match = re.search(r"Updated\s+([\w ]+?\s+ago)", page_text, re.IGNORECASE)
    return {
        "22K": rates[22],
        "24K": rates[24],
        "rate_date": date_match.group(1) if date_match else "Latest published session",
        "updated_text": update_match.group(1).strip() if update_match else "Latest published update",
        "loaded_at": datetime.now().astimezone(),
        "source_url": BAHRAIN_GOLD_URL,
    }


def gold_positions(
    rates: dict | None, purchase_values: dict[int, float | None]
) -> list[dict]:
    if not rates or any(purchase_values.get(karat) is None for karat in (22, 24)):
        return []
    positions = []
    for name, holding in HOLDINGS.items():
        rate = float(rates[f"{holding['karat']}K"])
        value = rate * holding["grams"]
        sheet_purchase_value = float(purchase_values[holding["karat"]])
        # The sheet may hold either the total paid amount (e.g. 430 BHD) or
        # a per-gram purchase rate. Convert both forms to a consistent cost basis.
        if sheet_purchase_value > 150:
            cost = sheet_purchase_value
            purchase_rate = cost / holding["grams"]
        else:
            purchase_rate = sheet_purchase_value
            cost = purchase_rate * holding["grams"]
        pnl = value - cost
        positions.append(
            {
                "Holding": name,
                "Weight": holding["grams"],
                "Rate": rate,
                "Purchase rate": purchase_rate,
                "Purchase input": sheet_purchase_value,
                "Cost": cost,
                "Value": value,
                "P/L": pnl,
                "P/L %": pnl / cost * 100 if cost else 0.0,
            }
        )
    return positions


def make_status_image(
    *,
    month: str,
    total_collected: float,
    month_collected: float,
    paid_count: int,
    member_count: int,
    due_amount: float,
    total_outstanding: float | None,
    balance: float | None,
    positions: list[dict],
    pending_details: list[dict],
    iban_no: str = "",
    appearance: str = "dark",
) -> bytes:
    """Create a high-contrast Khair-branded WhatsApp summary card."""
    def font(size: int, bold: bool = False):
        font_path = "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            return ImageFont.load_default(size=size)

    width = 1200
    pending_rows = []
    for detail in pending_details:
        month_lines = textwrap.wrap(" · ".join(detail["months"]), width=94) or ["No month details"]
        row_height = 66 + 25 * (len(month_lines) - 1)
        pending_rows.append((detail, month_lines, row_height))
    gold_count = len(positions) if positions else 1
    pending_content_height = sum(row[2] + 10 for row in pending_rows)
    card_rows = (5 + 1) // 2
    height = max(1240, 1080 + gold_count * 110 + pending_content_height + max(0, card_rows - 2) * 138)
    palette = {
        "dark": {
            "background": "#0D1822", "hero": "#102B31", "logo_back": "#0B1822",
            "hero_outline": "#286257", "hero_muted": "#A8C9C1", "white": "#F2FBF7",
            "muted": "#A8B9B8", "card": "#142630", "edge": "#29454C",
            "progress_card": "#103B37", "progress_outline": "#2D7465",
            "progress_muted": "#B3D9CE", "progress_track": "#29454A",
            "progress_text": "#C8DDD7", "gold_outline": "#705C35",
            "pending_card": "#142C35", "pending_outline": "#37675D",
            "pending_row": "#1A333B", "pending_text": "#BED0CC", "footer": "#94AAA7",
            "up_to_date": "#103B37",
        },
        "light": {
            "background": "#F2F7F5", "hero": "#FFFFFF", "logo_back": "#102B31",
            "hero_outline": "#D7E7DF", "hero_muted": "#55746B", "white": "#17312D",
            "muted": "#657871", "card": "#FFFFFF", "edge": "#D8E7E1",
            "progress_card": "#E8F4EE", "progress_outline": "#B8D8CA",
            "progress_muted": "#416B5F", "progress_track": "#CFE5DA",
            "progress_text": "#416B5F", "gold_outline": "#D8C58D",
            "pending_card": "#EAF3EF", "pending_outline": "#C9DED4",
            "pending_row": "#FFFFFF", "pending_text": "#657871", "footer": "#788C85",
            "up_to_date": "#E8F4EE",
        },
    }[appearance]
    image = Image.new("RGB", (width, height), palette["background"])
    draw = ImageDraw.Draw(image)

    title_font = font(38, True)
    section_font = font(25, True)
    body_font = font(22, True)
    small_font = font(19)
    muted_font = font(17)
    white = palette["white"]
    muted = palette["muted"]
    teal = "#0F766E" if appearance == "light" else "#38D6AD"
    gold = "#A47720" if appearance == "light" else "#E4BD68"
    red = "#B9435A" if appearance == "light" else "#F07C8D"
    card = palette["card"]
    edge = palette["edge"]

    # Branded hero strip
    draw.rounded_rectangle((38, 36, width - 38, 224), radius=34, fill=palette["hero"], outline=palette["hero_outline"], width=2)
    draw.rounded_rectangle((68, 74, 496, 187), radius=20, fill=palette["logo_back"])
    if LOGO_PATH.exists():
        logo = Image.open(LOGO_PATH).convert("RGB")
        logo.thumbnail((400, 98), Image.Resampling.LANCZOS)
        image.paste(logo, (82, 82))
    draw.text((548, 72), "KHAIR GROUP FUND", font=muted_font, fill=palette["hero_muted"])
    draw.text((548, 105), month_label(month), font=title_font, fill=white)
    draw.text((550, 166), "GROUP UPDATE  ·  CONTRIBUTIONS & INVESTMENTS", font=small_font, fill=gold)
    draw.ellipse((width - 112, 70, width - 76, 106), fill=teal)
    draw.ellipse((width - 101, 81, width - 87, 95), fill="#D9FFF4")

    y = 252
    cards = [
        ("TOTAL COLLECTED", money(total_collected), teal),
        ("THIS MONTH", money(month_collected), "#527C96"),
        ("CURRENT MONTH OUTSTANDING", money(due_amount), red),
        ("TOTAL FUND OUTSTANDING", money(total_outstanding) if total_outstanding is not None else "Not listed", gold),
        ("FUND BALANCE", money(balance) if balance is not None else "Not listed", gold),
    ]
    for index, (label, value, accent) in enumerate(cards):
        x = 44 + (index % 2) * 568
        cy = y + (index // 2) * 138
        draw.rounded_rectangle((x, cy, x + 544, cy + 116), radius=22, fill=card, outline=edge, width=2)
        draw.rounded_rectangle((x + 19, cy + 24, x + 26, cy + 91), radius=4, fill=accent)
        draw.ellipse((x + 473, cy + 67, x + 511, cy + 105), outline=accent, width=2)
        draw.ellipse((x + 483, cy + 77, x + 501, cy + 95), outline=accent, width=2)
        draw.text((x + 44, cy + 20), label, font=muted_font, fill=muted)
        draw.text((x + 44, cy + 53), value, font=section_font, fill=white)

    y += 26 + card_rows * 138
    draw.rounded_rectangle((44, y, width - 44, y + 116), radius=22, fill=palette["progress_card"], outline=palette["progress_outline"], width=2)
    draw.text((72, y + 20), "MONTHLY COLLECTION", font=muted_font, fill=palette["progress_muted"])
    draw.text((72, y + 52), f"{paid_count} of {member_count} members paid", font=section_font, fill=white)
    progress = paid_count / member_count if member_count else 0
    bar_left, bar_top, bar_width, bar_height = 650, y + 48, 450, 22
    draw.rounded_rectangle((bar_left, bar_top, bar_left + bar_width, bar_top + bar_height), radius=11, fill=palette["progress_track"])
    if progress:
        draw.rounded_rectangle((bar_left, bar_top, bar_left + max(18, round(bar_width * progress)), bar_top + bar_height), radius=11, fill=teal)
    draw.text((650, y + 78), f"{progress:.0%} complete", font=muted_font, fill=palette["progress_text"])

    y += 154
    draw.text((52, y), "GOLD PORTFOLIO", font=section_font, fill=gold)
    if positions and member_count:
        group_gold_pnl = sum(float(position["P/L"]) for position in positions)
        per_member_gold_pnl = group_gold_pnl / member_count
        per_member_color = teal if per_member_gold_pnl >= 0 else red
        draw.text(
            (width - 54, y + 2),
            f"EQUAL-SHARE P/L / MEMBER  {per_member_gold_pnl:+,.2f} BHD",
            font=small_font,
            fill=per_member_color,
            anchor="ra",
        )
    draw.text((52, y + 34), "Current Bahrain rate estimate  ·  value compared with purchase cost", font=muted_font, fill=muted)
    y += 68
    if positions:
        for position in positions:
            pnl_color = teal if position["P/L"] >= 0 else red
            draw.rounded_rectangle((44, y, width - 44, y + 96), radius=18, fill=card, outline=palette["gold_outline"], width=2)
            draw.text((70, y + 15), f"{position['Holding']}  ·  {position['Weight']:g} g", font=body_font, fill=white)
            draw.text((70, y + 53), f"LIVE {money(position['Rate'])}/g  ·  BUY {money(position['Purchase rate'])}/g  ·  COST {money(position['Cost'])}  ·  VALUE {money(position['Value'])}", font=muted_font, fill=muted)
            pnl_text = f"{position['P/L']:+,.2f} BHD  ({position['P/L %']:+.2f}%)"
            draw.text((width - 72, y + 31), pnl_text, font=body_font, fill=pnl_color, anchor="ra")
            y += 110
    else:
        draw.rounded_rectangle((44, y, width - 44, y + 96), radius=18, fill=card, outline=edge, width=2)
        draw.text((70, y + 34), "Gold purchase value or live rate unavailable", font=body_font, fill=muted)
        y += 110

    y += 18
    if pending_rows:
        section_top = y
        list_height = 58 + sum(row[2] + 10 for row in pending_rows)
        draw.rounded_rectangle((38, section_top, width - 38, section_top + list_height), radius=26, fill=palette["pending_card"], outline=palette["pending_outline"], width=2)
        draw.text((68, y + 20), f"PENDING MEMBERS  ·  THROUGH {month_label(month).upper()}", font=section_font, fill=white)
        draw.text((width - 68, y + 25), f"{len(pending_rows)} MEMBERS", font=small_font, fill=gold, anchor="ra")
        y += 64
        for detail, month_lines, row_height in pending_rows:
            draw.rounded_rectangle((58, y, width - 58, y + row_height), radius=16, fill=palette["pending_row"], outline=edge, width=1)
            draw.text((82, y + 10), detail["name"], font=body_font, fill=white)
            amount_text = f"{detail['month_count']} MONTH(S)  ·  {money(detail['amount'])} DUE"
            draw.text((width - 82, y + 13), amount_text, font=small_font, fill=red, anchor="ra")
            for line_index, line in enumerate(month_lines):
                draw.text((82, y + 40 + line_index * 24), line, font=muted_font, fill=palette["pending_text"])
            y += row_height + 10
    else:
        draw.rounded_rectangle((44, y, width - 44, y + 88), radius=18, fill=palette["up_to_date"], outline=palette["progress_outline"], width=2)
        draw.text((72, y + 29), "ALL CONTRIBUTIONS ARE UP TO DATE", font=body_font, fill=teal)
        y += 100

    draw.rounded_rectangle((44, y + 8, width - 44, y + 104), radius=18, fill=card, outline=palette["progress_outline"], width=2)
    draw.text((72, y + 24), "PAYMENT DETAILS", font=muted_font, fill=palette["hero_muted"])
    draw.text((72, y + 52), f"IBAN NO: {iban_no or 'Not listed in the sheet'}", font=body_font, fill=white)

    footer = f"KHAIR GROUP FUND  ·  Rates: Gulf News Bahrain  ·  Generated {datetime.now().astimezone().strftime('%d %b %Y, %H:%M')}"
    draw.text((54, height - 48), footer, font=muted_font, fill=palette["footer"])
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_image_share_button(image_bytes: bytes, *, filename: str, text: str) -> None:
    """Offer the infographic through the browser's native share sheet (WhatsApp is a target)."""
    image_data = base64.b64encode(image_bytes).decode("ascii")
    safe_filename = escape(filename, quote=True)
    safe_text = escape(text, quote=True)
    st.html(
        f'''<div style="margin: .25rem 0 .5rem">
<button id="khair-share-image" type="button" style="border:0;border-radius:10px;padding:.65rem 1rem;
background:#087f67;color:#fff;font:600 14px sans-serif;cursor:pointer;">
↗ Share image to WhatsApp
</button>
<span id="khair-share-status" role="status" style="margin-left:.6rem;font:13px sans-serif;color:#60766e"></span>
<script>
(() => {{
 const button = document.getElementById('khair-share-image');
 const status = document.getElementById('khair-share-status');
 button.addEventListener('click', async () => {{
   try {{
     const raw = atob('{image_data}');
     const bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
     const file = new File([bytes], '{safe_filename}', {{type:'image/png'}});
     if (!navigator.share || !navigator.canShare || !navigator.canShare({{files:[file]}})) {{
       status.textContent = 'Direct image sharing is not supported here. Download the image and attach it in WhatsApp.';
       return;
     }}
     await navigator.share({{files:[file], title:'Khair group update', text:'{safe_text}'}});
     status.textContent = 'Choose WhatsApp in the share menu.';
   }} catch (error) {{
     status.textContent = error.name === 'AbortError' ? 'Share cancelled.' : 'Sharing failed. Download the image and attach it in WhatsApp.';
   }}
 }});
}})();
</script></div>''',
        unsafe_allow_javascript=True,
    )


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 8:
        return "973" + digits
    if 10 <= len(digits) <= 15:
        return digits
    return None


def money(value: float) -> str:
    return f"BHD {value:,.2f}"


def month_label(value: str) -> str:
    return datetime.strptime(value, "%b-%y").strftime("%b %Y")


def load_if_needed(force: bool = False) -> None:
    if "khair_data" not in st.session_state or force:
        with st.spinner("Refreshing the latest figures…"):
            try:
                st.session_state.khair_data = fetch_sheet()
                st.session_state.khair_error = None
            except Exception as exc:  # Retain the last good snapshot if refresh fails.
                st.session_state.khair_error = str(exc)
            try:
                st.session_state.gold_rates = fetch_gold_rates()
                st.session_state.gold_error = None
            except Exception as exc:
                st.session_state.gold_error = str(exc)


if LOGO_PATH.exists():
    with Image.open(LOGO_PATH) as source_logo:
        logo_mark = source_logo.convert("RGB").crop((20, 12, 92, 82))
        logo_buffer = io.BytesIO()
        logo_mark.save(logo_buffer, format="PNG")
    logo_data = base64.b64encode(logo_buffer.getvalue()).decode("ascii")
else:
    logo_data = ""
dark_mode = st.session_state.get("khair_dark_mode", False)
motion_bg = "#1B303C" if dark_mode else "#DCEBE6"
motion_gradient = "linear-gradient(90deg,#0F766E,#41DAAB,#D8B45C)"
dark_overrides = """
  [data-testid="stAppViewContainer"] { background:#0D1822; color:#F2FBF7; }
  [data-testid="stSidebar"] { display:none !important; }
  [data-testid="stVerticalBlockBorderWrapper"], [data-testid="stMetric"] { background:#142630; border-color:#29454C; }
  [data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"], [data-testid="stWidgetLabel"] { color:#D5E4DF; }
  [data-testid="stBaseButton-primary"] { background:#19A88A; border-color:#19A88A; color:#071A18; }
  [data-testid="stBaseButton-primary"]:hover { background:#38D6AD; border-color:#38D6AD; color:#071A18; }
  [data-testid="stBaseButton-secondary"] { background:#172B35; border-color:#35564F; color:#DDF3E8; }
  [data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea { background:#142630; color:#F2FBF7; border-color:#35564F; }
  [data-testid="stSelectbox"] [data-baseweb="select"] > div { background:#142630; color:#F2FBF7; border-color:#35564F; }
  [data-testid="stRadio"] label { color:#E0ECE8; }
  .khair-hero { background:radial-gradient(ellipse at 8% 0%,#145448 0%,transparent 38%),linear-gradient(115deg,#10222D 0%,#16323B 64%,#0D3D36 100%); color:#F2FBF7; border-color:#27564F; }
  .khair-brand { color:#F2FBF7; }
  .khair-subtitle { color:#B9CBC9; }
  .khair-top-nav a { color:#CBD8D5; }
  .khair-top-nav a:first-child, .khair-top-nav a:focus { background:#0B3D36; color:#51DDB5; border-color:#146956; }
  .khair-top-nav a:hover { background:#1B3A3E; border-color:#38D6AD; color:#F2FBF7; }
  [data-testid="stAppViewContainer"] [data-testid="stElementContainer"]:has(.khair-hero) { background:#0D1822; border-bottom:1px solid #29454C; }
  .khair-hero { background:#101D28; border-bottom:2px solid #315D56; }
  .khair-nav-icon { background:#1D4A42; color:#B8F2DD; }
  .khair-heading-accent { background:linear-gradient(90deg,#26444A,#315D56,#26444A); }
  .khair-infographic-card { background:#142630; border-color:#29454C; color:#F2FBF7; }
  .khair-infographic-hint, .khair-infographic-label { color:#A8B9B8; }
  .khair-infographic-icon { background:#1D4A42; color:#B8F2DD; }
  .khair-icon-blue { background:#203D4C; color:#9BCBE0; }
  .khair-icon-coral { background:#4A3038; color:#F3A3AE; }
  .khair-icon-gold { background:#473F2C; color:#E8D091; }
  .khair-progress-band { background:linear-gradient(115deg,#103B37,#173941); border-color:#2D7465; }
  .khair-progress-track { background:#29454A; }
  .khair-progress-ring { --progress-color:#38D6AD; --progress-track:#29454A; }
  .khair-progress-ring::before { background:#103B37; }
  .khair-progress-ring span, .khair-progress-heading strong { color:#F2FBF7; }
  .khair-progress-heading span { color:#B5D5CC; }
""" if dark_mode else ""

SECTION_LINKS = [
    ("page-top", "Dashboard", "⌂"),
    ("overview", "Overview", "▦"),
    ("members", "Members", "♙"),
    ("fund-ledger", "Fund ledger", "▤"),
    ("share-reminders", "Share & reminders", "↗"),
]

def section_nav_markup() -> str:
    links = "".join(
        f'<a href="#{anchor}"><span class="khair-nav-icon" aria-hidden="true">{icon}</span><span>{label}</span></a>'
        for anchor, label, icon in SECTION_LINKS
    )
    return f'<nav class="khair-top-nav" aria-label="Dashboard sections">{links}</nav>'


def render_section_accent() -> None:
    st.html('<div class="khair-heading-accent" aria-hidden="true"><span></span></div>')


def render_infographic_tiles(tiles: list[tuple[str, str, str, str, str]]) -> None:
    card_html = "".join(
        '<article class="khair-infographic-card">'
        f'<div class="khair-infographic-top"><span class="khair-infographic-label">{escape(label)}</span>'
        f'<span class="khair-infographic-icon khair-icon-{tone}" aria-hidden="true">{icon}</span></div>'
        f'<div class="khair-infographic-value">{escape(value)}</div>'
        f'<div class="khair-infographic-hint">{escape(hint)}</div></article>'
        for label, icon, value, hint, tone in tiles
    )
    st.html(f'<div class="khair-infographic-grid">{card_html}</div>')


def render_infographic_summary(
    *, total: float, monthly_total: float, current_month_outstanding: float,
    total_outstanding: float | None, balance: float | None,
    paid: int, member_count: int, completion: float, month: str,
) -> None:
    completion_pct = min(100, max(0, round(completion * 100)))
    due_count = max(0, member_count - paid)
    cards = [
        ("Total contributions", "✦", money(total), "Collected across the fund", "emerald"),
        (f"{month_label(month)} collected", "◷", money(monthly_total), f"{paid} members have paid", "blue"),
        ("Current month outstanding", "!", money(current_month_outstanding), f"{due_count} members to follow up", "coral"),
        (
            "Total outstanding in fund",
            "↘",
            money(total_outstanding) if total_outstanding is not None else "Not listed",
            "From the Total Outstanding sheet row",
            "gold",
        ),
        ("Fund balance", "◇", money(balance) if balance is not None else "Not listed", "Available fund balance", "gold"),
    ]
    render_infographic_tiles(cards)
    st.html(
        '<section class="khair-progress-band" aria-label="Monthly contribution progress">'
        '<div class="khair-progress-layout">'
        f'<div class="khair-progress-ring" style="--progress-angle:{completion_pct * 3.6:.1f}deg"><span>{completion_pct}%</span></div>'
        '<div class="khair-progress-copy"><div class="khair-progress-heading">'
        f'<strong>Monthly contribution progress</strong><span>{paid} of {member_count} members paid</span></div>'
        '<div class="khair-progress-track"><div class="khair-progress-fill" '
        f'style="width:{completion_pct}%"></div></div></div></div></section>'
    )

st.html('<span id="page-top" aria-hidden="true"></span>')

st.html(
    f"""
    <style>
      [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {{ display:none !important; }}
      section.main > div {{ padding-top: 1.3rem; }}
      [data-testid="stBaseButton-primary"] {{ background:#0F766E; border-color:#0F766E; color:#FFFFFF; }}
      [data-testid="stBaseButton-primary"]:hover {{ background:#0B655D; border-color:#0B655D; color:#FFFFFF; }}
      [data-testid="stBaseButton-secondary"] {{ border-color:#B8CEC5; color:#0F766E; background:#FFFFFF; }}
      [data-testid="stAppViewContainer"] [data-testid="stElementContainer"]:has(.khair-hero) {{ position:sticky; top:3.5rem; z-index:10000; padding-top:8px; margin-bottom:12px; background:#F4F8F6; border-bottom:1px solid #D7E7DF; }}
      .khair-hero {{ position:relative; display:flex; align-items:center; gap:18px; padding:10px 16px; margin:0;
        border:1px solid #D7E7DF; border-top:3px solid #0F766E; border-bottom:2px solid #BFD8CD; border-radius:14px; color:#17312D;
        background:radial-gradient(ellipse at 8% 0%,#E4F5ED 0%,transparent 46%),#FFFFFF; box-shadow:0 8px 24px #123D3115; }}
      .khair-brand-lockup {{ display:flex; align-items:center; gap:10px; flex:0 0 auto; }}
      .khair-logo-frame {{ width:64px; height:64px; flex:0 0 64px; display:grid; place-items:center;
        border-radius:17px; background:transparent; }}
      .khair-logo-frame img {{ width:62px; height:62px; object-fit:contain; border-radius:15px; }}
      .khair-brand {{ color:#17312D; font:700 29px/1.1 sans-serif; letter-spacing:-.6px; }}
      .khair-subtitle {{ margin-top:7px; color:#657871; font:500 14px/1.45 sans-serif; }}
      .khair-top-nav {{ display:flex; align-items:center; justify-content:flex-end; gap:4px; min-width:0; margin-left:auto; overflow-x:auto; scrollbar-width:thin; }}
      .khair-top-nav a {{ display:inline-flex; align-items:center; justify-content:center; gap:8px; padding:9px 12px; border-radius:9px;
        background:transparent; color:#455264; border:1px solid transparent; font-weight:650; white-space:nowrap; text-decoration:none;
        transition:background .18s ease, color .18s ease, border-color .18s ease; }}
      .khair-top-nav a:first-child, .khair-top-nav a:focus {{ background:#E1F6EF; color:#087A61; border-color:#BCEBD7; outline:none; }}
      .khair-top-nav a:hover {{ background:#E6F4EE; color:#0F766E; border-color:#9AC8B3; }}
      .khair-nav-icon {{ display:inline-grid; place-items:center; min-width:20px; height:20px; color:currentColor; font-size:15px; }}
      .khair-heading-accent {{ width:100%; height:3px; margin:-13px 0 22px; overflow:hidden; border-radius:5px;
        background:linear-gradient(90deg,#C8E5D7,#E8D6A7,#C8E5D7); }}
      .khair-heading-accent span {{ display:block; height:100%; width:22%; border-radius:inherit;
        background:linear-gradient(90deg,#0F766E,#41DAAB,#D8B45C); animation:khair-heading-sweep 6s ease-in-out infinite; }}
      .khair-infographic-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin:14px 0; }}
      .khair-infographic-card {{ position:relative; min-height:132px; padding:17px 18px 15px; overflow:hidden;
        border:1px solid #D7E7DF; border-radius:18px; background:#FFFFFF; color:#17312D;
        box-shadow:0 6px 18px #123D310B; }}
      .khair-infographic-card::after {{ content:""; position:absolute; right:-32px; bottom:-44px; width:112px; height:112px;
        border:1px solid #0F766E18; border-radius:50%; box-shadow:0 0 0 14px #0F766E08,0 0 0 29px #0F766E05; pointer-events:none; }}
      .khair-infographic-card > * {{ position:relative; z-index:1; }}
      .khair-infographic-top {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
      .khair-infographic-icon {{ display:grid; place-items:center; width:31px; height:31px; border-radius:10px;
        background:#E1F2E9; color:#0F766E; font-size:16px; font-weight:700; }}
      .khair-icon-blue {{ background:#E5F0F5; color:#3C7D98; }}
      .khair-icon-coral {{ background:#F8E8E9; color:#B9435A; }}
      .khair-icon-gold {{ background:#F5F0E2; color:#A47720; }}
      .khair-infographic-label {{ color:#657871; font-size:12px; font-weight:700; letter-spacing:.09em; text-transform:uppercase; }}
      .khair-infographic-value {{ margin-top:13px; font-size:25px; line-height:1.1; font-weight:750; letter-spacing:-.04em; }}
      .khair-infographic-hint {{ margin-top:5px; color:#71827C; font-size:12px; }}
      .khair-progress-band {{ margin:12px 0 28px; padding:17px 20px; border:1px solid #B9D9CB; border-radius:18px;
        background:linear-gradient(115deg,#E8F5EE,#F8FBF9 68%,#F4EEDF); }}
      .khair-progress-heading {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:10px; }}
      .khair-progress-heading strong {{ color:#17312D; font-size:15px; }}
      .khair-progress-heading span {{ color:#567168; font-size:13px; }}
      .khair-progress-track {{ height:10px; overflow:hidden; border-radius:99px; background:#D0E4DA; }}
      .khair-progress-fill {{ height:100%; border-radius:inherit; background:linear-gradient(90deg,#0F766E,#48C99F,#D8B45C); }}
      .khair-progress-layout {{ display:flex; align-items:center; gap:16px; }}
      .khair-progress-ring {{ position:relative; display:grid; place-items:center; width:62px; height:62px; flex:0 0 62px;
        --progress-color:#0F766E; --progress-track:#D0E4DA; border-radius:50%;
        background:conic-gradient(var(--progress-color) var(--progress-angle),var(--progress-track) 0); }}
      .khair-progress-ring::before {{ content:""; position:absolute; inset:7px; border-radius:50%; background:#F7FBF9; }}
      .khair-progress-ring span {{ position:relative; color:#17312D; font-size:14px; font-weight:750; }}
      .khair-progress-copy {{ flex:1; min-width:0; }}
      @media(max-width:480px) {{ .khair-infographic-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; }} .khair-infographic-card {{ min-height:120px; padding:13px; }} .khair-infographic-value {{ font-size:21px; }} }}
      @keyframes khair-heading-sweep {{ from {{ transform:translateX(-110%); }} to {{ transform:translateX(520%); }} }}
      @media (prefers-reduced-motion: reduce) {{ .khair-heading-accent span {{ animation:none; }} }}
      [data-testid="stAppViewContainer"] #members, [data-testid="stAppViewContainer"] #fund-ledger,
      [data-testid="stAppViewContainer"] #share-reminders, [data-testid="stAppViewContainer"] #overview {{ scroll-margin-top:164px; }}
      @media(max-width:900px) {{ .khair-hero {{ flex-wrap:wrap; gap:8px; }} .khair-top-nav {{ order:2; width:100%; justify-content:flex-start; margin-left:0; }} }}
      @media(max-width:620px) {{ .khair-hero {{ padding:9px 11px; gap:7px; }} .khair-logo-frame {{ width:48px;height:48px;flex-basis:48px; }}
        .khair-logo-frame img {{ width:46px;height:46px; }} .khair-brand {{ font-size:23px; }} .khair-subtitle {{ font-size:12px; }}
        .khair-top-nav a {{ padding:8px 10px; font-size:13px; }}
        [data-testid="stAppViewContainer"] [data-testid="stElementContainer"]:has(.khair-hero) {{ top:3.5rem; }}
        [data-testid="stAppViewContainer"] #members, [data-testid="stAppViewContainer"] #fund-ledger,
        [data-testid="stAppViewContainer"] #share-reminders, [data-testid="stAppViewContainer"] #overview {{ scroll-margin-top:220px; }} }}
      {dark_overrides}
    </style>
    <header class="khair-hero" aria-label="Khair group fund dashboard">
      <div class="khair-brand-lockup">
        <div class="khair-logo-frame">{'<img alt="Khair logo" src="data:image/png;base64,' + logo_data + '">' if logo_data else '<span style="font-size:34px;font-weight:bold">خ</span>'}</div>
        <div><div class="khair-brand">Khair <span style="color:#55dfbd">·</span></div>
          <div class="khair-subtitle">Group Fund &amp; Investment Portal</div>
        </div>
      </div>
      {section_nav_markup()}
    </header>
    """
)

with st.container(
    horizontal=True,
    horizontal_alignment="distribute",
    vertical_alignment="center",
    wrap=True,
):
    st.caption("Live overview · Khair members and fund")
    refresh_clicked = st.button("Refresh data", icon=":material/refresh:", type="primary")
    dark_mode_toggle = st.toggle("Dark mode", value=dark_mode, key="khair_dark_mode")

st.html(
    f"""
    <style>
    @keyframes khair-sweep {{ from {{ transform: translateX(-115%); }} to {{ transform: translateX(430%); }} }}
    .khair-motion {{ height: 4px; width: 100%; overflow: hidden; border-radius: 8px; background: {motion_bg}; }}
    .khair-motion::after {{ content: ""; display: block; height: 100%; width: 24%; border-radius: inherit;
      background: {motion_gradient}; animation: khair-sweep 8s ease-in-out infinite; }}
    @media (prefers-reduced-motion: reduce) {{ .khair-motion::after {{ animation: none; width: 100%; opacity: .55; }} }}
    </style>
    <div class="khair-motion" aria-hidden="true"></div>
    """
)

load_if_needed(force=refresh_clicked)
data = st.session_state.get("khair_data")
error = st.session_state.get("khair_error")
gold_rates = st.session_state.get("gold_rates")
gold_error = st.session_state.get("gold_error")

if data is None:
    st.error("Could not load the published Google Sheet.")
    st.code(error or "No data returned.")
    st.link_button("Open published CSV", SHEET_CSV_URL, icon=":material/open_in_new:")
    st.stop()

if error:
    st.warning(f"Refresh failed; showing the last successful snapshot. {error}")

members: pd.DataFrame = data["members"]
months: list[str] = data["months"]
monthly: pd.DataFrame = data["monthly"]
ledger: dict[str, float] = data["ledger"]
fee: float = data["monthly_fee"]

latest_month = months[-1]
with st.container(border=True):
    filter_col, threshold_col, note_col = st.columns([1, 1.25, 1.4], vertical_alignment="bottom")
    with filter_col:
        selected_month = st.selectbox(
            "Contribution month",
            months,
            index=months.index(latest_month),
            format_func=month_label,
        )
    with threshold_col:
        group_limit = st.number_input(
            "Pending follow-up threshold (months)",
            min_value=1,
            max_value=max(1, len(months)),
            value=min(3, max(1, len(months))),
        )
    with note_col:
        st.caption("Read-only connection · edit contributions in the source sheet")

paid_this_month = int((members[selected_month] > 0).sum())
member_count = len(members)
due_this_month = member_count - paid_this_month
due_amount = due_this_month * fee
paid_total = sum(float(members[month].sum()) for month in months)
balance = ledger.get("balance")
collected_ledger = ledger.get("total collection", paid_total)
completion = paid_this_month / member_count if member_count else 0
month_position = months.index(selected_month)
months_to_date = months[: month_position + 1]
unpaid_counts = members[months_to_date].le(0).sum(axis=1)
follow_up_count = int((unpaid_counts > group_limit).sum())
selected_collection = float(
    monthly.loc[monthly["Month"] == selected_month, "Collected (BHD)"].iloc[0]
)
gold_purchase_values: dict[int, float | None] = data["gold_purchase_values"]
total_outstanding = ledger.get("total outstanding")
positions = gold_positions(gold_rates, gold_purchase_values)
gold_total_cost = sum(item["Cost"] for item in positions)
gold_total_value = sum(item["Value"] for item in positions)
gold_total_pnl = sum(item["P/L"] for item in positions)

st.subheader("Your fund, at a glance")
st.caption(
    f"Connected to Google Sheets · refreshed "
    f"{data['loaded_at'].strftime('%d %b %Y at %H:%M')}"
)
render_infographic_summary(
    total=collected_ledger,
    monthly_total=selected_collection,
    current_month_outstanding=due_amount,
    total_outstanding=total_outstanding,
    balance=balance,
    paid=paid_this_month,
    member_count=member_count,
    completion=completion,
    month=selected_month,
)

with st.container():
    st.header(":material/dashboard: Overview", anchor="overview")
    render_section_accent()
    st.subheader("Gold portfolio")
    st.caption(
        "Purchase basis comes from the values beside Gold 22k and Gold 24k in the sheet. "
        "After editing those cells, select Refresh data to recalculate profit and loss."
    )
    if positions:
        rate_col, pnl_col = st.columns([1.7, 1], gap="medium")
        with rate_col:
            st.caption(
                f"Bahrain reference rate · {gold_rates['rate_date']} · "
                f"updated {gold_rates['updated_text']}"
            )
        with pnl_col:
            st.link_button(
                "Rate source · Gulf News",
                BAHRAIN_GOLD_URL,
                icon=":material/open_in_new:",
            )
        gold_cols = st.columns(2, gap="medium")
        for column, position in zip(gold_cols, positions):
            with column:
                pnl_tone = "emerald" if position["P/L"] >= 0 else "coral"
                pnl_icon = "↗" if position["P/L"] >= 0 else "↘"
                render_infographic_tiles(
                    [
                        (
                            f"{position['Holding']} · {position['Weight']:g} g",
                            pnl_icon,
                            money(position["Value"]),
                            f"{position['P/L']:+,.2f} BHD · {position['P/L %']:+.2f}% vs purchase cost",
                            pnl_tone,
                        )
                    ]
                )
                st.caption(
                    f"Live: {money(position['Rate'])}/g · sheet purchase rate: "
                    f"{money(position['Purchase rate'])}/g · total cost: {money(position['Cost'])}"
                )
        combined_tone = "emerald" if gold_total_pnl >= 0 else "coral"
        render_infographic_tiles(
            [
                ("Combined gold value", "◉", money(gold_total_value), "Current Bahrain rate estimate", "gold"),
                (
                    "Combined gold P/L",
                    "↗" if gold_total_pnl >= 0 else "↘",
                    f"BHD {gold_total_pnl:+,.2f}",
                    f"{gold_total_pnl / gold_total_cost:+.2%} against purchase cost" if gold_total_cost else "Purchase basis unavailable",
                    combined_tone,
                ),
                (
                    "Gold P/L per member",
                    "◇",
                    f"BHD {gold_total_pnl / member_count:+,.2f}" if member_count else "Not available",
                    f"Equal share across {member_count} members" if member_count else "Member count unavailable",
                    "blue",
                ),
            ]
        )
        st.caption(
            f"Equal-share estimate divides the combined P/L by {member_count} members. "
            "Reference-rate estimate; shop buyback prices, spreads, and workmanship can differ."
        )
    elif gold_error:
        st.warning(f"Live Bahrain rates are unavailable right now. {gold_error}")
    elif any(gold_purchase_values.get(karat) is None for karat in (22, 24)):
        missing_karats = [
            f"Gold {karat}k" for karat in (22, 24) if gold_purchase_values.get(karat) is None
        ]
        st.warning(
            "Add a numeric purchase value in the cell next to "
            + " and ".join(missing_karats)
            + " in the published sheet to calculate gold profit and loss."
        )
    else:
        st.info("Live Bahrain gold rates are loading.")

    trend_col, status_col = st.columns([1.6, 1], gap="large")
    with trend_col:
        with st.container(border=True):
            st.subheader("Collection trend")
            chart_data = monthly.copy()
            chart_data["Month label"] = chart_data["Month"].map(month_label)
            wave_colors = (
                ("#DDF2E7", "#77C6A5", "#0F766E", "#D8B45C")
                if not dark_mode
                else ("#1A4942", "#24836B", "#40D0A8", "#D8B45C")
            )
            base = alt.Chart(chart_data).encode(
                x=alt.X("Month label:N", title=None, sort=chart_data["Month label"].tolist(), axis=alt.Axis(labelAngle=0)),
                tooltip=[
                    alt.Tooltip("Month label:N", title="Month"),
                    alt.Tooltip("Collected (BHD):Q", title="Collected", format=",.2f"),
                    alt.Tooltip("Expected (BHD):Q", title="Target", format=",.2f"),
                    alt.Tooltip("Paid members:Q", title="Members paid"),
                ],
            )
            target_rule = alt.Chart(
                pd.DataFrame({"Expected target": [float(chart_data["Expected (BHD)"].max())]})
            ).mark_rule(color=wave_colors[3], strokeDash=[5, 4], strokeWidth=2).encode(
                y=alt.Y("Expected target:Q", title="BHD", scale=alt.Scale(zero=True))
            )
            wave_fill = base.mark_area(
                interpolate="monotone",
                opacity=0.8,
                color=alt.Gradient(
                    gradient="linear",
                    stops=[
                        alt.GradientStop(color=wave_colors[0], offset=0),
                        alt.GradientStop(color=wave_colors[1], offset=0.65),
                        alt.GradientStop(color=wave_colors[2], offset=1),
                    ],
                    x1=0, x2=0, y1=0, y2=1,
                ),
            ).encode(y=alt.Y("Collected (BHD):Q", title="BHD", scale=alt.Scale(zero=True)))
            wave_line = base.mark_line(
                interpolate="monotone", color=wave_colors[2], strokeWidth=3, point=True
            ).encode(y=alt.Y("Collected (BHD):Q", title="BHD", scale=alt.Scale(zero=True)))
            chart_grid = "#E8EBEF" if not dark_mode else "#354253"
            chart_text = "#667281" if not dark_mode else "#AAB4C0"
            collection_chart = (wave_fill + wave_line + target_rule).properties(height=300).configure_view(stroke=None).configure_axis(
                gridColor=chart_grid, labelColor=chart_text, titleColor=chart_text
            )
            st.altair_chart(
                collection_chart,
                width="stretch",
                theme="streamlit",
                key="collection_trend_chart",
            )
            st.caption("Wave: contributions collected · dashed accent line: monthly target")
            st.caption(f"Monthly contribution target: {money(fee)} per member")

    with status_col:
        with st.container(border=True):
            st.subheader(f"{month_label(selected_month)} progress")
            st.progress(completion, text=f"{completion:.0%} collected · {paid_this_month}/{member_count}")
            render_infographic_tiles(
                [("Current month outstanding", "!", money(due_amount), f"{due_this_month} members", "coral")]
            )
            st.caption(f"{follow_up_count} members have more than {group_limit} unpaid months through {month_label(selected_month)}.")

    with st.container(border=True):
        st.subheader("Most pending members")
        most_pending = members[["Name", "Paid months", "Monthly contributions"]].copy()
        most_pending["Unpaid months"] = unpaid_counts.values
        most_pending["Amount outstanding (BHD)"] = most_pending["Unpaid months"] * fee
        most_pending = most_pending[most_pending["Unpaid months"] > 0]
        most_pending = most_pending.sort_values(
            ["Unpaid months", "Name"], ascending=[False, True]
        ).head(8)
        if most_pending.empty:
            st.success("Everyone is paid through the selected month.")
        else:
            st.dataframe(
                most_pending,
                hide_index=True,
                column_config={
                    "Monthly contributions": st.column_config.NumberColumn("Contributed (BHD)", format="%.2f"),
                    "Amount outstanding (BHD)": st.column_config.NumberColumn(format="%.2f"),
                },
                alt="The eight members with the most unpaid months through the selected month",
            )

with st.container():
    st.header(":material/groups: Members", anchor="members")
    render_section_accent()
    st.subheader("Member payments")
    search_col, month_col, status_col = st.columns([1.25, 1, 1], gap="medium")
    with search_col:
        search = st.text_input(
            "Find a member",
            placeholder="Search by name…",
            icon=":material/search:",
        )
    with month_col:
        member_month = st.selectbox(
            "Member payment month",
            months,
            index=months.index(selected_month),
            format_func=month_label,
            key="member_payment_month_filter",
            help="This filter applies to the member list only. The top month filter still controls the overview.",
        )
    with status_col:
        payment_filter = st.segmented_control(
            "Payment status",
            ["Everyone", "Pending", "Paid"],
            default="Everyone",
            selection_mode="single",
        )

    member_months_to_date = months[: months.index(member_month) + 1]
    member_unpaid_counts = members[member_months_to_date].le(0).sum(axis=1)
    view = members[["Name", "Registration fee", "Paid months", "Monthly contributions"]].copy()
    view["Unpaid through selected month"] = member_unpaid_counts.values
    view[month_label(member_month)] = members[member_month].map(
        lambda amount: "Paid" if amount > 0 else "Pending"
    )
    if search.strip():
        view = view[view["Name"].str.contains(search.strip(), case=False, na=False)]
    if payment_filter == "Pending":
        view = view[view[month_label(member_month)] == "Pending"]
    elif payment_filter == "Paid":
        view = view[view[month_label(member_month)] == "Paid"]
    view = view.sort_values("Unpaid through selected month", ascending=False)

    st.dataframe(
        view,
        hide_index=True,
        height=440,
        column_config={
            "Monthly contributions": st.column_config.NumberColumn("Contributed (BHD)", format="%.2f"),
            "Registration fee": st.column_config.NumberColumn("Registration fee (BHD)", format="%.2f"),
        },
        alt="Member payment status and contribution totals",
    )
    st.download_button(
        "Download member list",
        data=view.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"khair-members-{member_month.lower()}.csv",
        mime="text/csv",
        icon=":material/download:",
    )

    with st.expander("View full month-by-month history"):
        matrix = members[["Name", *months]].copy()
        for month in months:
            matrix[month] = matrix[month].map(lambda amount: "✓" if amount > 0 else "—")
        st.dataframe(matrix, hide_index=True, alt="Full member payment history by month")

with st.container():
    st.header(":material/account_balance_wallet: Fund ledger", anchor="fund-ledger")
    render_section_accent()
    st.caption("Values below are read from the ledger rows in the source sheet.")
    ledger_items = [
        ("Balance", "balance"),
        ("Total collection", "total collection"),
        ("Total outstanding", "total outstanding"),
        ("Registration fees", "reg. fee"),
        ("Meeting expenses", "meeting exp."),
        ("Gold", "gold"),
        ("Kuri", "kuri"),
        ("Rounding", "rounding"),
    ]
    ledger_tiles = [
        (
            label,
            "◇",
            money(ledger[key]) if ledger.get(key) is not None else "Not listed",
            "From the shared fund ledger",
            "emerald",
        )
        for label, key in ledger_items
    ]
    render_infographic_tiles(ledger_tiles)

    st.subheader("Monthly totals")
    monthly_view = monthly.assign(Month=monthly["Month"].map(month_label))
    st.dataframe(
        monthly_view,
        hide_index=True,
        column_config={
            "Collected (BHD)": st.column_config.NumberColumn(format="%.2f"),
            "Expected (BHD)": st.column_config.NumberColumn(format="%.2f"),
        },
        alt="Monthly contribution totals and expected target",
    )

with st.container():
    st.header(":material/campaign: Share & reminders", anchor="share-reminders")
    render_section_accent()
    st.subheader("Share a group update")
    if not data["has_iban_value"]:
        st.info("Add a row labeled `IBAN No` to the Khair sheet, put the shared payment IBAN in the next cell, then refresh the sheet.")
    st.caption(
        "Share a clear text update or a branded infographic. Image sharing opens your device’s share menu, "
        "where you can choose WhatsApp; you can also download the image."
    )
    share_format = st.segmented_control(
        "Share format",
        ["Text", "Image"],
        default="Text",
        selection_mode="single",
    )
    image_appearance = st.segmented_control(
        "WhatsApp image theme",
        ["Dark", "Light"],
        default="Dark",
        selection_mode="single",
        key="whatsapp_image_appearance",
    )
    share_col, share_options = st.columns([1.45, 1], gap="large")
    with share_options:
        include_gold = st.checkbox("Include gold profit / loss", value=True)
        include_pending_names = st.checkbox("Include pending names and months", value=True)

    pending_through_month = members.loc[unpaid_counts > 0]
    pending_names = pending_through_month["Name"].tolist()
    pending_details = []
    for _, pending_member in pending_through_month.iterrows():
        unpaid_labels = [
            month_label(month)
            for month in months_to_date
            if float(pending_member[month]) <= 0
        ]
        pending_details.append(
            {
                "name": pending_member["Name"],
                "month_count": len(unpaid_labels),
                "months": unpaid_labels,
                "amount": len(unpaid_labels) * fee,
            }
        )
    pending_details.sort(key=lambda item: (-item["month_count"], item["name"].lower()))
    def pl_marker(value: float) -> str:
        return "🔻" if value < 0 else "🟢"

    common_iban = data.get("iban_no", "")

    summary_lines = [
        f"🌿 *KHAIR GROUP FUND | {month_label(selected_month)}*",
        "Hi, Good Day!",
        "",
        "📊 *FUND SUMMARY*",
        f"💰 Total contributions: {money(collected_ledger)}",
        f"📅 This month collected: {money(selected_collection)} ({paid_this_month}/{member_count} members)",
        f"⏳ Current month outstanding: {due_this_month} members, {money(due_amount)}",
        f"📋 Total outstanding in fund: {money(total_outstanding) if total_outstanding is not None else 'Not listed'}",
        f"🏦 Fund balance: {pl_marker(balance) if balance is not None else 'ℹ️'} {money(balance) if balance is not None else 'Not listed'}",
    ]
    if common_iban:
        summary_lines.append(f"💳 *IBAN No: {common_iban}*")
    else:
        summary_lines.append("💳 *IBAN No: Not listed in the sheet*")
    if include_gold:
        if positions:
            summary_lines.append(
                f"🪙 Gold profit or loss: {pl_marker(gold_total_pnl)} {money(gold_total_pnl)} "
                f"(value {money(gold_total_value)}, cost {money(gold_total_cost)})"
            )
            if member_count:
                member_gold_pnl = gold_total_pnl / member_count
                summary_lines.append(
                    f"👤 Estimated gold share per member: {pl_marker(member_gold_pnl)} "
                    f"BHD {member_gold_pnl:+,.2f}"
                )
            for position in positions:
                summary_lines.append(
                    f"{pl_marker(float(position['P/L']))} {position['Holding']}: "
                    f"{position['Weight']:g} g, {money(position['P/L'])}"
                )
        else:
            summary_lines.append(
                "🪙 Gold P/L is unavailable. Please check the live rate and Gold 22k / Gold 24k values in the sheet."
            )
    if include_pending_names and pending_names:
        listed = [
            f"{detail['name']} ({detail['month_count']} "
            f"{'month' if detail['month_count'] == 1 else 'months'})"
            for detail in pending_details[:20]
        ]
        suffix = f" (+{len(pending_details) - 20} more)" if len(pending_details) > 20 else ""
        summary_lines.append("\n⏳ *PENDING THROUGH THIS MONTH*\n👥 " + ", ".join(listed) + suffix)
    summary_lines.extend(["", "Thanks and Regards,", "Khair Group Fund"])
    summary_text = "\n".join(summary_lines)

    with share_col:
        if share_format == "Text":
            message = st.text_area(
                "Message preview · edit before opening WhatsApp",
                value=summary_text,
                height=240,
                key=f"summary_message_v3_{hash(summary_text)}",
            )
            st.link_button(
                "Open WhatsApp draft",
                f"https://wa.me/?text={quote(message)}",
                icon=":material/chat:",
                type="primary",
            )
        else:
            image_bytes = make_status_image(
                month=selected_month,
                total_collected=collected_ledger,
                month_collected=selected_collection,
                paid_count=paid_this_month,
                member_count=member_count,
                due_amount=due_amount,
                total_outstanding=total_outstanding,
                balance=balance,
                positions=positions if include_gold else [],
            pending_details=pending_details if include_pending_names else [],
            iban_no=common_iban,
            appearance=image_appearance.lower(),
            )
            st.image(image_bytes, alt="Khair group update card for WhatsApp sharing")
            st.download_button(
                "Download status image",
                data=image_bytes,
                file_name=f"khair-update-{selected_month.lower()}.png",
                mime="image/png",
                icon=":material/download:",
                type="primary",
            )
            render_image_share_button(
                image_bytes,
                filename=f"khair-update-{selected_month.lower()}.png",
                text=f"Khair Group Fund update | {month_label(selected_month)}",
            )
            st.caption("Your device’s share menu may list WhatsApp. On unsupported browsers, use Download status image and attach it manually.")

    st.subheader("Personal payment reminder")
    if pending_names:
        reminder_member = st.selectbox(
            "Choose a member with an outstanding contribution",
            pending_names,
            key=f"reminder_member_{selected_month}",
        )
        member_record = members.loc[members["Name"] == reminder_member].iloc[0]
        if not data["has_phone_column"]:
            st.info(
                "To fill this number automatically, add a `Phone` column beside the member names "
                "in the Khair tab, enter each member’s WhatsApp number, then publish the updated tab."
            )
        unpaid_month_labels = [
            month_label(month)
            for month in months_to_date
            if float(member_record[month]) <= 0
        ]
        reminder_due = len(unpaid_month_labels) * fee
        member_iban = common_iban
        default_reminder = (
            f"🌿 *Khair Group Fund | Contribution Reminder*\n\n"
            f"Hi, Good Day! {reminder_member},\n\n"
            "A friendly reminder that our record shows the following contribution(s) pending:\n"
            f"📅 Months pending: {', '.join(unpaid_month_labels)}\n"
            f"🧾 Number of months: {len(unpaid_month_labels)}\n"
            f"💰 Amount due: {money(reminder_due)}\n\n"
            f"💳 *IBAN No: {member_iban or 'Not listed in the sheet'}*\n\n"
            "If you have already paid, please disregard this message and share the payment details so we can update our record.\n\n"
            "Thanks and Regards,\nKhair Group Fund"
        )
        reminder_phone = st.text_input(
            "WhatsApp number",
            placeholder="Bahrain 8-digit number or full country-code number",
            value=str(member_record.get("Phone", "") or ""),
            key=f"reminder_phone_{selected_month}_{reminder_member}",
        )
        reminder_message = st.text_area(
            "Reminder message · edit before opening WhatsApp",
            value=default_reminder,
            height=140,
            key=f"reminder_text_v3_{hash(default_reminder)}",
        )
        normalized_phone = normalize_phone(reminder_phone)
        if normalized_phone:
            st.link_button(
                f"Open WhatsApp reminder for {reminder_member}",
                f"https://wa.me/{normalized_phone}?text={quote(reminder_message)}",
                icon=":material/chat:",
                type="primary",
            )
        else:
            st.caption("Enter a valid number to prepare the WhatsApp draft. Bahrain 8-digit numbers get +973 automatically.")
    else:
        st.success(f"Everyone is paid through {month_label(selected_month)}.")

    st.subheader("One-shot email reminder")
    st.caption(
        "Prepare one BCC email for pending members with addresses in the sheet. "
        "The button opens your email composer with the reminder and payment details ready; review and send it there."
    )
    if not data["has_email_column"]:
        st.info(
            "Add an `Email` column to the Khair tab, fill in member addresses, and republish the tab "
            "as CSV to enable automatic recipient selection."
        )
        st.button(
            "Send email",
            icon=":material/mail:",
            disabled=True,
            help="Recipient addresses are not available in the published sheet yet.",
        )
    else:
        email_pending = pending_through_month.copy()
        email_pending["Email"] = email_pending["Email"].fillna("").astype(str).str.strip()
        email_pending = email_pending[
            email_pending["Email"].str.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", na=False)
        ]
        recipient_options = {
            f"{row['Name']} · {row['Email']}": row["Email"]
            for _, row in email_pending.iterrows()
        }
        if not recipient_options:
            st.caption("No pending members have a valid email address in the published sheet.")
            st.button(
                "Send email",
                icon=":material/mail:",
                disabled=True,
                help="Add valid email addresses for pending members in the published sheet.",
            )
        else:
            selected_recipients = st.multiselect(
                "Pending members to remind",
                options=list(recipient_options),
                default=list(recipient_options),
                key=f"email_recipients_{selected_month}",
            )
            email_iban_text = common_iban or "Not listed in the sheet"
            email_subject = st.text_input(
                "Email subject",
                value=f"Khair contribution reminder · {month_label(selected_month)}",
                key=f"email_subject_{selected_month}",
            )
            email_body = st.text_area(
                "Email message · review before sending",
                value=(
                    f"🌿 KHAIR GROUP FUND | {month_label(selected_month)}\n\n"
                    "Hi, Good Day!\n\n"
                    "This is a friendly reminder that our records show one or more monthly contributions "
                    f"pending through {month_label(selected_month)}.\n\n"
                    "📌 Please review your payment record and arrange any pending contribution when convenient.\n"
                    "✅ If you have already paid, please disregard this reminder and share the payment details "
                    "so we can update our records.\n\n"
                    "📊 FUND SUMMARY\n"
                    f"💰 Total contributions: {money(collected_ledger)}\n"
                    f"📅 This month collected: {money(selected_collection)} ({paid_this_month}/{member_count} members)\n"
                    f"⏳ Current month outstanding: {due_this_month} members, {money(due_amount)}\n"
                    f"📋 Total outstanding in fund: {money(total_outstanding) if total_outstanding is not None else 'Not listed'}\n"
                    f"🏦 Fund balance: {money(balance) if balance is not None else 'Not listed'}\n"
                    f"🪙 Gold profit or loss: {money(gold_total_pnl)}\n\n"
                    f"💳 IBAN NO: {email_iban_text}\n\n"
                    "Thanks and Regards,\nKhair Group Fund"
                ),
                height=170,
                key=f"email_body_v2_{hash((email_iban_text, collected_ledger, selected_collection, due_amount, total_outstanding, balance, gold_total_pnl))}",
            )
            if selected_recipients:
                email_recipients = [recipient_options[label] for label in selected_recipients]
                mailto_url = (
                    f"mailto:?bcc={quote(','.join(email_recipients))}"
                    f"&subject={quote(email_subject)}&body={quote(email_body)}"
                )
                st.link_button(
                    f"Send email · {len(selected_recipients)} member(s)",
                    mailto_url,
                    icon=":material/send:",
                    type="primary",
                )
                st.caption(
                    f"Opens a BCC email for {len(email_recipients)} selected member(s). Review it in your email app and press Send."
                )
            else:
                st.caption("Select at least one member to prepare the email draft.")

st.caption("Khair · Group fund dashboard · Read-only connection")
