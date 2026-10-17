# Expense Report CLI

A small command-line tool that turns a list of expense items into a finished, numbered
expense report (HTML + PDF) using your own Jinja2 HTML template. Items are entered through
interactive [Typer](https://typer.tiangolo.com/) prompts, or loaded in bulk from a CSV/JSON file.

## Features

- Interactive item entry (name, description, quantity, rate) with live line amounts
- Review menu before generating: add, edit or delete items
- Automatic report numbers (`ER-2026-0001`, `ER-2026-0002`, ...) and today's date by default
- "Total in words" in Taka/Poisha with lakh/crore grouping
- Bulk import from CSV or JSON (including a previously saved report)
- Outputs an HTML file, an A4 PDF and a JSON copy of the data for every report
- `render` command to rebuild a report from its JSON after a fix or a template change
- Fully non-interactive mode (`--yes`) for scripts

## Requirements

- Python 3.9+ (developed and tested on 3.12)
- The packages in `requirements.txt`: Typer, Jinja2, Rich, Playwright
- Chromium for Playwright (only needed for PDF output)

## Installation

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # one-time download, used to print the PDF
```

If you only want the HTML output you can skip the Chromium step and always pass `--no-pdf`.

## Project layout

```
expense_report.py                  the whole tool
requirements.txt
sample_items.csv                   example input for --items-file
templates/expense_report.html      your Jinja2 template (not included, add yours)
assets/duc_rcyt.jpg                logo referenced by the template (not included, add yours)
output/                            generated reports (created automatically)
```

Relative image paths in the template (such as `../assets/duc_rcyt.jpg`) are resolved from the
template's folder and embedded into the output as data URIs, so the generated HTML and PDF do
not depend on any file paths.

## Usage

```bash
# Interactive: asks for date, report number, then items
python expense_report.py new

# Preload items from a file, then review before generating
python expense_report.py new -i sample_items.csv

# No prompts at all: today's date, next report number, items from the file
python expense_report.py new -i sample_items.csv -y

# Rebuild a saved report (for example after editing its JSON)
python expense_report.py render output/ER-2026-0001.json
```

### Interactive flow

1. **Report date**: press Enter for today, or type `15-10-2026`, `15 October 2026`, `2026-10-15`, etc.
2. **Report number**: press Enter to accept the suggested next number, or type your own.
3. **Items**: for each item you are asked for name, description (optional), quantity (default 1)
   and rate. Leave the name blank to finish. Type `\n` inside a description to start a new line.
4. **Review**: the items are shown in a table with the total. Choose `g` to generate, `a` to add
   more, `e` to edit one (Enter keeps each current value, `-` clears the description),
   `d` to delete, or `q` to discard the report.

### Options for `new`

| Option | Meaning |
| --- | --- |
| `-i`, `--items-file PATH` | Preload items from a `.csv` or `.json` file |
| `-d`, `--date TEXT` | Report date (default: today) |
| `-n`, `--number TEXT` | Report number (default: next in sequence) |
| `-t`, `--template PATH` | Template file (default: `templates/expense_report.html`) |
| `-o`, `--output-dir PATH` | Where reports are saved (default: `output/`) |
| `--pdf` / `--no-pdf` | Also produce a PDF (default: on) |
| `-y`, `--yes` | No prompts; requires `--items-file`; refuses to overwrite an existing report |
| `--open` | Open the PDF (or HTML) when finished |

### Options for `render`

`render REPORT_JSON` accepts `-t/--template`, `-o/--output-dir` (default: next to the JSON file),
`--pdf/--no-pdf` and `--open`. It overwrites existing outputs without asking.

## Item files

**CSV** (header row required; `description` and `quantity` are optional, quantity defaults to 1):

```csv
name,description,quantity,rate
A4 paper (ream),80gsm,5,520
Banner printing,6x3 ft vinyl,2,"1,250.50"
Refreshments,Snacks & tea for volunteers,40,35
```

Column aliases accepted: `item` for `name`, `desc` for `description`, `qty` for `quantity`,
`price` or `unit_price` for `rate`.

**JSON**: a list of items, or an object with an `items` list:

```json
{
  "items": [
    { "name": "A4 paper (ream)", "description": "80gsm", "quantity": 5, "rate": 520 }
  ]
}
```

The `.json` file saved with every report (`number`, `date`, `items`) is also a valid items file,
which makes it easy to start a new report from a previous one.

Rules: quantity and rate must be greater than 0 (and below 1,000,000,000). Rates are rounded to
2 decimal places, and each line amount is `quantity x rate` rounded to 2 decimal places. Errors
in a file are reported with the line or item number.

## Report numbering

The next number is built from `NUMBER_FORMAT` (default `ER-{year}-{seq:04d}`), using the year of
the report date. The sequence is kept in `output/.report_counter.json` and only advances when you
keep the suggested number and the report has been saved. Numbers you type yourself, or pass with
`--number`, never change the counter. To restart numbering, edit or delete that file.

## Template variables

The script is the only thing that fills the template, so these names are the contract between
the two. Use them in your Jinja2 template:

| Variable | Contents |
| --- | --- |
| `er.number` | Report number |
| `er.date` | Report date, formatted with `DATE_DISPLAY_FORMAT` (for example `16 October 2026`) |
| `items` | List of items, each with `name`, `description`, `quantity`, `rate`, `amount` |
| `subtotal`, `total` | Numbers (currently always equal) |
| `total_in_words` | For example `Six Thousand Five Hundred One Taka Only` |

HTML in item names and descriptions is escaped automatically. Referencing a variable that does
not exist makes rendering fail with an error instead of printing an empty value.

## Configuration

Edit the constants at the top of `expense_report.py`:

| Constant | Purpose |
| --- | --- |
| `DEFAULT_TEMPLATE`, `DEFAULT_OUTPUT_DIR` | Default locations, relative to the script |
| `NUMBER_FORMAT` | Report number pattern |
| `DATE_DISPLAY_FORMAT`, `DATE_INPUT_FORMATS` | How dates are shown and which formats are accepted |
| `CURRENCY_MAJOR`, `CURRENCY_MINOR` | Words used in "Total in words" |
| `PDF_MARGIN` | Page margin on all four sides of the A4 PDF |

## Notes and troubleshooting

- **PDF fails or Playwright is missing**: run `pip install playwright` and
  `playwright install chromium`, or use `--no-pdf`. The HTML and JSON are saved before the PDF
  step, so you can fix the problem and run `render` on the JSON.
- **Fonts**: if the template loads a web font (such as Outfit from Google Fonts), the PDF step needs
  an internet connection. Offline, the browser falls back to a default font.
- **Overlapping description lines**: if wrapped description lines overlap in the output, check the
  `line-height` of the description paragraph in your template's CSS. A value smaller than the
  font size (for example `7px` on `11px` text) causes this; use about `16px`.
- **Reports contain personal or financial data**: `output/` is listed in `.gitignore` so
  generated reports are not committed by accident.

## License

Released under the MIT License. See `LICENSE`.
