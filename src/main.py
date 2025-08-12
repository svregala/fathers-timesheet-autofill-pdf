#!/usr/bin/env python3
"""
Local script to convert a photo of a handwritten weekly log into a filled agency PDF.

Workflow
1) OCR the handwritten photo to plain text (pytesseract by default; pluggable to cloud OCR).
2) Parse lines like: "8/4 - 9 to 3pm - 6" into structured rows.
3) Normalize dates (using a provided target month/year; supports cross‑month weeks like 7/28–8/3).
4) Map rows to Monday–Sunday per the agency timesheet.
5) Render values onto the provided PDF template using a configurable layout grid.
6) Save the overlaid, reviewable PDF.

Usage
python timesheet_filler.py \
--image "handwritten.jpg" \
--template "/path/TimeCard_August_4_2025_August_10_2025.pdf" \
--out "filled_timesheet.pdf" \
--month 8 --year 2025 \
--employee "Mario Regala" --client "Albert Tim Cronin"

Notes
- Defaults assume shifts start at 9:00 AM when the start time is omitted, as requested.
- If a handwritten total is missing or disagrees with computed hours, we compute from start/end and flag the discrepancy in console output.
- Coordinates for drawing text are configurable in LAYOUT at the top; run with --debug-grid to help calibrate for your exact PDF.
"""

import argparse
import datetime as dt
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# --- OCR: default to pytesseract (you must have Tesseract installed locally) ---
try:
   import pytesseract  # type: ignore
   from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
   pytesseract = None
   Image = None

# --- PDF overlay libs ---
from reportlab.pdfgen import canvas as rl_canvas  # type: ignore
from reportlab.lib.pagesizes import letter  # type: ignore
from reportlab.pdfbase import pdfmetrics  # type: ignore
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore
from PyPDF2 import PdfReader, PdfWriter  # type: ignore

# ---------------- Configuration ----------------

# Layout grid for the provided time sheet.
# Tune these x/y coordinates once using --debug-grid to match your PDF exactly.
LAYOUT = {
   "page": {"width": 11 * 72, "height": 8.5 * 72},  # landscape letter in points
   # Top-left of the table area (origin for row y positions)
   "table_origin": {"x": 70, "y": 420},
   "row_height": 20,  # Reduced from 30 to decrease spacing between rows
   # Column X positions (tweak to line up with PDF columns)
   "col": {
      "client": 70,
      "day": 190,
      "date": 350,  # Increased from 250 to shift dates to the right
      "start": 420,
      "end": 500,
      "hours": 580,
      # mileage and initials columns exist, but we leave them blank.
   },
   # Header / footer fields
   "header": {
      "employee_name": {"x": 120, "y": 480},
      "period_from": {"x": 550, "y": 498},
      "period_to": {"x": 650, "y": 498},
   },
   "footer": {
      "total_hours": {"x": 580, "y": 285},
      "signature_name": {"x": 150, "y": 115},
      "signature_date": {"x": 440, "y": 180},
   },
   # Font settings
   "font": {"family": "Helvetica", "size": 10},
}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thur", "Fri", "Sat", "Sun"]

DATE_RE = re.compile(r"(?P<m>\d{1,2})[\/-](?P<d>\d{1,2})(?:[\/-](?P<y>\d{2,4}))?")
TIME_RE = re.compile(
   r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?",
   re.IGNORECASE,
)

@dataclass
class DayEntry:
   date: dt.date
   start: dt.time
   end: dt.time
   hours: float
   raw: str

# ---------------- Utilities ----------------

def parse_time(text: str, default_ampm: Optional[str] = None) -> Optional[dt.time]:
   m = TIME_RE.search(text)
   if not m:
      return None
   h = int(m.group("h"))
   minute = int(m.group("min") or 0)
   ampm = (m.group("ampm") or default_ampm or "").lower().replace(".", "")
   # Normalize 12-hour to 24-hour
   if ampm in ("pm", "p") and h != 12:
      h += 12
   if ampm in ("am", "a") and h == 12:
      h = 0
   # If no am/pm specified, leave as-is; caller may infer later
   return dt.time(hour=h % 24, minute=minute)


def infer_times(start_txt: str, end_txt: str, assume_start_9am: bool = True) -> Tuple[dt.time, dt.time]:
   """Infer start/end times handling cases like "9 to 5pm" or missing am/pm.
   Rules:
   - If only one time has am/pm and the other lacks it, use the same suffix logic as common daylight shifts.
   - If neither has am/pm: assume start=9:00, end as written (if plausible), else default end=5:00 PM.
   """
   start = parse_time(start_txt)
   end = parse_time(end_txt)

   # If one side carries am/pm and the other not, try to infer a day shift.
   end_has_ampm = bool(re.search(r"[ap]m", end_txt, re.I))
   start_has_ampm = bool(re.search(r"[ap]m", start_txt, re.I))

   if assume_start_9am and start is None:
      start = dt.time(9, 0)

   if start and end:
      # If end specified as, e.g., 7 with pm, start lacked am/pm => assume AM for start if before 12.
      if end_has_ampm and not start_has_ampm and start.hour <= 12:
         # If end is pm and start < 12, keep start as AM.
         pass
      # Handle cases where times roll past midnight (unlikely here). If end <= start, assume same-day pm shift.
      if (end.hour, end.minute) <= (start.hour, start.minute):
         # bump end by +12h if that makes sense (rare for this use case).
         end = (dt.datetime.combine(dt.date.today(), end) + dt.timedelta(hours=12)).time()
      return start, end

   # Fallbacks
   if start is None and assume_start_9am:
      start = dt.time(9, 0)
   if end is None:
      # Default standard end time 5 PM
      end = dt.time(17, 0)
   return start, end


def hours_between(start: dt.time, end: dt.time) -> float:
   s = dt.datetime.combine(dt.date.today(), start)
   e = dt.datetime.combine(dt.date.today(), end)
   if e <= s:
      e += dt.timedelta(days=1)
   delta = e - s
   return round(delta.total_seconds() / 3600.0, 2)


# ---------------- OCR & Parsing ----------------

def ocr_image_to_text(image_path: str) -> str:
   if pytesseract is None or Image is None:
      raise RuntimeError(
         "pytesseract/Pillow not available. Install them or replace ocr_image_to_text with a cloud OCR call."
      )
   img = Image.open(image_path)
   # Optional: simple pre-processing for hand-written notes
   img = img.convert("L")  # grayscale
   return pytesseract.image_to_string(img)


def parse_handwritten_lines(text: str, month: int, year: int) -> List[DayEntry]:
   """Parse free-form lines like '4 - 9 to 3pm - 6' and return DayEntry list.
      If a date includes an explicit month (e.g., 8/4), we honor it; otherwise we use provided month.
   """
   entries: List[DayEntry] = []
   for raw in text.splitlines():
      line = raw.strip()
      if not line:
         continue
      # Extract date
      date_match = DATE_RE.search(line)
      if date_match:
         m = int(date_match.group("m"))
         d = int(date_match.group("d"))
         y = int(date_match.group("y") or year)
      else:
         # Look for a leading day number like '4 -'
         m2 = re.match(r"^(?P<d>\d{1,2})\b", line)
         if not m2:
               continue
         d = int(m2.group("d"))
         m = month
         y = year
      # Split into time segments: '9 to 3pm' or '9-3pm'
      parts = re.split(r"[-–—]|to", line, flags=re.I)
      if len(parts) < 2:
         continue
      start_txt = parts[1] if len(parts) >= 2 else "9"
      end_txt = parts[2] if len(parts) >= 3 else "5pm"

      start_t, end_t = infer_times(start_txt, end_txt, assume_start_9am=True)
      computed_hours = hours_between(start_t, end_t)

      # Try to read a trailing explicit hours number
      hours_match = re.search(r"(\d+(?:\.\d+)?)\s*$", line)
      final_hours = computed_hours
      if hours_match:
         handwritten_hours = float(hours_match.group(1))
         if abs(handwritten_hours - computed_hours) > 0.25:
               print(
                  f"[WARN] {m}/{d}/{y}: handwritten {handwritten_hours}h vs computed {computed_hours}h; using computed."
               )
         else:
               final_hours = handwritten_hours

      try:
         date_obj = dt.date(y, m, d)
      except ValueError:
         # Skip invalid dates
         continue

      entries.append(
         DayEntry(date=date_obj, start=start_t, end=end_t, hours=final_hours, raw=raw)
      )
   return entries


# ---------------- Week mapping ----------------

def week_bounds_from_dates(dates: List[dt.date]) -> Tuple[dt.date, dt.date]:
   if not dates:
      today = dt.date.today()
      start = today - dt.timedelta(days=today.weekday())  # Monday
      return start, start + dt.timedelta(days=6)
   # Choose the Monday of the min date as start of week
   min_d = min(dates)
   start = min_d - dt.timedelta(days=min_d.weekday())
   return start, start + dt.timedelta(days=6)


def map_entries_to_week(entries: List[DayEntry], week_start: dt.date) -> Dict[str, Optional[DayEntry]]:
   mapping: Dict[str, Optional[DayEntry]] = {w: None for w in WEEKDAYS}
   for e in entries:
      idx = (e.date - week_start).days
      if 0 <= idx <= 6:
         mapping[WEEKDAYS[idx]] = e
   return mapping


# ---------------- PDF writing ----------------

def register_fonts():
   try:
      pdfmetrics.registerFont(TTFont("Inter", "Inter-Regular.ttf"))
      LAYOUT["font"]["family"] = "Inter"
   except Exception:
      pass  # fallback to Helvetica


def draw_text(c: rl_canvas.Canvas, x: float, y: float, text: str, size: int = 10):
   c.setFont(LAYOUT["font"]["family"], size)
   c.drawString(x, y, text)


def render_to_pdf(template_pdf: str,
               out_pdf: str,
               employee: str,
               client: str,
               mapping: Dict[str, Optional[DayEntry]],
               period_from: dt.date,
               period_to: dt.date,
               total_hours: float,
               debug_grid: bool = False) -> None:
   reader = PdfReader(template_pdf)
   page = reader.pages[0]
   width = float(page.mediabox.width)
   height = float(page.mediabox.height)

   # Overlay canvas
   overlay_path = out_pdf + ".overlay.pdf"
   c = rl_canvas.Canvas(overlay_path, pagesize=(width, height))
   register_fonts()

   # Debug grid for alignment
   if debug_grid:
      c.setStrokeGray(0.8)
      for x in range(0, int(width), 25):
         c.line(x, 0, x, height)
      for y in range(0, int(height), 25):
         c.line(0, y, width, y)

   # Header fields
   draw_text(c, LAYOUT["header"]["employee_name"]["x"], LAYOUT["header"]["employee_name"]["y"], employee, 11)
   draw_text(c, LAYOUT["header"]["period_from"]["x"], LAYOUT["header"]["period_from"]["y"], period_from.strftime("%m/%d/%Y"))
   draw_text(c, LAYOUT["header"]["period_to"]["x"], LAYOUT["header"]["period_to"]["y"], period_to.strftime("%m/%d/%Y"))

   origin_x = LAYOUT["table_origin"]["x"]
   origin_y = LAYOUT["table_origin"]["y"]
   row_h = LAYOUT["row_height"]

   for i, wd in enumerate(WEEKDAYS):
      y = origin_y - i * row_h
      # Client
      draw_text(c, LAYOUT["col"]["client"], y, client)
      # Skip day label since it's already in the template
      # Date
      the_date = period_from + dt.timedelta(days=i)
      draw_text(c, LAYOUT["col"]["date"], y, the_date.strftime("%m/%d/%Y"))

      e = mapping.get(wd)
      if e:
         draw_text(c, LAYOUT["col"]["start"], y, e.start.strftime("%-I:%M %p") if hasattr(e.start, 'strftime') else "")
         draw_text(c, LAYOUT["col"]["end"], y, e.end.strftime("%-I:%M %p") if hasattr(e.end, 'strftime') else "")
         # Hours typically shown as integer; but keep .2f if needed
         hrs_txt = f"{e.hours:g}" if math.isclose(e.hours, round(e.hours)) else f"{e.hours:.2f}"
         draw_text(c, LAYOUT["col"]["hours"], y, hrs_txt)

   # Footer totals & signature placeholders
   draw_text(c, LAYOUT["footer"]["total_hours"]["x"], LAYOUT["footer"]["total_hours"]["y"], f"{total_hours:g}")
   draw_text(c, LAYOUT["footer"]["signature_name"]["x"], LAYOUT["footer"]["signature_name"]["y"], employee)
   draw_text(c, LAYOUT["footer"]["signature_date"]["x"], LAYOUT["footer"]["signature_date"]["y"], period_to.strftime("%m/%d/%Y"))

   c.save()

   # Merge overlay onto template
   overlay_reader = PdfReader(overlay_path)
   writer = PdfWriter()
   base_page = reader.pages[0]
   base_page.merge_page(overlay_reader.pages[0])
   writer.add_page(base_page)

   with open(out_pdf, "wb") as f:
      writer.write(f)
   try:
      os.remove(overlay_path)
   except OSError:
      pass


# ---------------- Main ----------------

def main():
   p = argparse.ArgumentParser(description="Fill agency timesheet PDF from handwritten photo")
   p.add_argument("--image", required=True, help="Path to handwritten photo")
   p.add_argument("--template", required=True, help="Path to template PDF (the agency form)")
   p.add_argument("--out", required=True, help="Output PDF path")
   p.add_argument("--month", type=int, required=True, help="Numeric month for most entries (e.g., 8 for August)")
   p.add_argument("--year", type=int, required=True, help="4-digit year for the entries")
   p.add_argument("--employee", default="Mario Regala")
   p.add_argument("--client", default="Albert Tim Cronin")
   p.add_argument("--ocr-json", help="Optional path to a pre-extracted JSON array of entries to skip OCR")
   p.add_argument("--debug-grid", action="store_true", help="Draw a grid to help align coordinates")

   args = p.parse_args()

   if args.ocr_json and os.path.exists(args.ocr_json):
      with open(args.ocr_json) as f:
         data = json.load(f)
      entries = [
         DayEntry(
               date=dt.date.fromisoformat(row["date"]),
               start=dt.time.fromisoformat(row["start"]),
               end=dt.time.fromisoformat(row["end"]),
               hours=float(row["hours"]),
               raw=row.get("raw", ""),
         )
         for row in data
      ]
   else:
      text = ocr_image_to_text(args.image)
      entries = parse_handwritten_lines(text, args.month, args.year)

   if not entries:
      raise SystemExit("No entries parsed. Try --ocr-json or adjust your photo/OCR.")

   # Determine the week bounds (Mon–Sun) and map rows
   dates = [e.date for e in entries]
   week_start, week_end = week_bounds_from_dates(dates)
   mapping = map_entries_to_week(entries, week_start)

   total_hours = round(sum(e.hours for e in entries), 2)

   render_to_pdf(
      template_pdf=args.template,
      out_pdf=args.out,
      employee=args.employee,
      client=args.client,
      mapping=mapping,
      period_from=week_start,
      period_to=week_end,
      total_hours=total_hours,
      debug_grid=args.debug_grid,
   )

   print(f"Wrote {args.out}\nWeek: {week_start} to {week_end}\nTotal hours: {total_hours}")


if __name__ == "__main__":
   main()
