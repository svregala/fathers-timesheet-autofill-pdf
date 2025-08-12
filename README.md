# fathers-timesheet-autofill-pdf
- From Notes to PDF: Transform Dad’s weekly timesheet photo into a polished, ready-to-send form — in one command
- Wrote a script to automate filling out required work timesheet form for dad because I got tired of manually filling it out LOL

## The Problem
Every week, my dad hands me a handwritten timesheet — dates in one column, shift hours in another, and total hours at the end.
The agency he works for doesn’t take this as-is. Instead, they require the same data typed neatly into their official PDF time card, complete with start/end times for each day, weekly totals, and a signature line.
What should be a quick task turns into a tedious weekly ritual: squinting at his handwriting, manually typing everything into the PDF, double-checking totals, and making sure it’s all aligned with their Monday–Sunday format.

## The Plan (and What I’m Building)
I’m creating a local script that takes the photo of my dad’s handwritten sheet and automatically:
1. Reads the text using OCR (good enough to handle his handwriting).
2. Parses the date, start time, end time, and total hours for each day.
3. Fills the official agency PDF with this data in the correct rows for Monday through Sunday.
4. Calculates totals and flags mismatches between handwritten and computed hours.
5. Outputs a completed, review-ready PDF that I can tweak if needed before sending.

The goal: turn a 15–20 minute chore into a one-command process, where the hardest part is picking which photo to feed the script.

## Future Plans
- Cross-Month Superpowers: Effortlessly handle weeks that straddle two months (e.g., 7/28–8/3) without breaking a sweat.
- Handwriting Whisperer Mode: Plug in a fancier handwriting OCR so it reads even the “creative” numbers without guessing.
- One-Click Review: Pop open a visual preview so I can approve or tweak before exporting the final PDF.
- Dad-Proof UI: Eventually wrap this in a dead-simple app so even my dad could run it without touching a terminal.
- Full Automation: A future where the PDF just… shows up in my “Ready to Send” folder every Monday morning like magic.